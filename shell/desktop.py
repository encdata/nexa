import os
from queue import Empty, SimpleQueue
from threading import Lock, Timer

import termuxgui as tg

from shell import file_actions, file_associations, palette
from shell.context_menu import ContextMenu
from shell.desktop_state import load_home_items, save_positions
from shell.geometry import place
from shell.icons import load
from shell.open_with import OpenWithChooser
from shell.text_prompt import TextPrompt
from shell.view_batch import create_views, set_image, set_text


class Desktop:
    icon_width = 76
    icon_height = 88
    gap = 12
    drag_threshold = 10
    long_press_seconds = 0.55
    population_batch_size = 10

    def __init__(self, activity, root, apps, open_app, screen_width, screen_height, density,
                 positions, home_items, progress=None):
        self.activity = activity
        self.root = root
        self.apps = apps
        self.open_app = open_app
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.density = density
        self.icons = {}
        self.icon_order = []
        self.drag = None
        self.gesture_lock = Lock()
        self.long_press_timer = None
        self.home_scan_timer = None
        self.home_scan_signature = None
        self.home_scan_generation = 0
        self.closed = False
        self.pending_file_actions = set()
        self.pending_home_items = SimpleQueue()
        self.gesture_number = 0
        self.context_icon = None
        self.context_visible = False
        self.positions = positions
        self.surface = tg.FrameLayout(activity, root)
        self.surface.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.surface.setbackgroundcolor(0x00000000)

        x_positions, y_positions = self._grid_axes(screen_width, screen_height)
        columns = len(x_positions)
        max_rows = len(y_positions)
        capacity = columns * max_rows
        home_capacity = max(0, capacity - len(apps))
        self.home_capacity = home_capacity
        items = [(path, "folder" if is_folder else file_associations.icon_for(path, apps, "window"),
                  name, "files", path, is_folder)
                 for path, name, is_folder in home_items[:home_capacity]]
        items.extend((app.app_id, app.icon, app.name, app.app_id, None, None)
                     for app in apps.values())
        self.population_items = []
        for index, (key, symbol, label, app_id, context, is_folder) in enumerate(items):
            grid_index = index
            saved_position = self.positions.get(key)
            if isinstance(saved_position, int) and not isinstance(saved_position, bool):
                grid_index = max(0, saved_position)
            elif (isinstance(saved_position, list) and len(saved_position) == 2
                    and all(isinstance(value, (int, float)) for value in saved_position)):
                x = max(0, min(screen_width - self.icon_width, saved_position[0]))
                y = max(0, min(screen_height - 56 - self.icon_height, saved_position[1]))
                grid_index = self._grid_index_for(x, y, x_positions, y_positions)
            self.population_items.append(
                (key, symbol, label, app_id, context, is_folder, grid_index))
        self.population_index = 0
        self.population_total = len(self.population_items)
        self.population_complete = self.population_total == 0
        self.progress = progress
        self.context_menu = ContextMenu(activity, root,
                                        lambda: (self.screen_width, self.screen_height))
        self.name_prompt = TextPrompt(activity, root,
                                      lambda: (self.screen_width, self.screen_height))
        self.open_with = OpenWithChooser(
            activity, root, lambda: (self.screen_width, self.screen_height), apps, open_app,
            self.refresh_associated_icons)
        self.home_item_snapshot = {path: (name, is_folder)
                                   for path, name, is_folder in home_items[:home_capacity]}
        self._schedule_home_scan()

    @property
    def ready(self):
        return self.population_complete

    def tick(self):
        with self.gesture_lock:
            if self.closed:
                return

            if not self.population_complete:
                batch_end = min(self.population_index + self.population_batch_size,
                                self.population_total)
                items = self.population_items[self.population_index:batch_end]
                occupied = {(icon["x"], icon["y"]) for icon in self.icons.values()}
                self._add_icons(items, occupied=occupied)
                self.population_index = batch_end
                if (self.progress is not None
                        and (self.population_index == self.population_total
                             or self.population_index % 3 == 0)):
                    percent = 72 + round(24 * self.population_index / self.population_total)
                    self.progress(f"placing desktop icons {self.population_index}/{self.population_total}", percent)

                if self.population_index == self.population_total:
                    if self.progress is not None:
                        self.progress("positioning desktop", 97)
                    self.population_complete = True
                    positions = {item["key"]: item["grid_index"]
                                 for item in self.icons.values()}
                    if positions != self.positions:
                        self.positions = positions
                        save_positions(self.positions)
                    if self.progress is not None:
                        self.progress("desktop ready", 98)
                    if self.context_visible:
                        self.context_menu.reposition()

            if self.drag is None:
                try:
                    available = self.pending_home_items.get_nowait()
                except Empty:
                    return
                while True:
                    try:
                        available = self.pending_home_items.get_nowait()
                    except Empty:
                        break
                if available != self.home_item_snapshot:
                    if self.context_visible:
                        self._hide_context_menu()
                    self._sync_home_items(available)

    def _initial_position(self, grid_index, occupied=None):
        x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
        total = len(self.icons) + self.population_total - self.population_index
        largest_index = max(
            [grid_index, *(icon["grid_index"] for icon in self.icons.values()),
             *(item[6] for item in self.population_items[self.population_index + 1:])])
        required_rows = max((total + len(x_positions) - 1) // len(x_positions),
                            largest_index // len(x_positions) + 1)
        while len(y_positions) < required_rows:
            y_positions.append(y_positions[-1] + self.icon_height + self.gap)

        desired = self._grid_position(grid_index, x_positions, y_positions)
        if occupied is None:
            occupied = {(icon["x"], icon["y"]) for icon in self.icons.values()}
        if desired not in occupied:
            return desired

        slots = [(x, y) for y in y_positions for x in x_positions]
        available = [point for point in slots if point not in occupied]
        candidates = available or slots
        return min(candidates, key=lambda point:
                   (point[0] - desired[0]) ** 2 + (point[1] - desired[1]) ** 2)

    def resize(self, screen_width, screen_height):
        with self.gesture_lock:
            self._resize(screen_width, screen_height)
            self.name_prompt.reposition()

    def _resize(self, screen_width, screen_height, force=False, preferred_icon=None):
        old_width, old_height = self.screen_width, self.screen_height
        self.screen_width = max(1, screen_width)
        self.screen_height = max(1, screen_height)
        if not force and (self.screen_width, self.screen_height) == (old_width, old_height):
            return
        if self.drag is not None:
            self._cancel_long_press()
            self.drag = None
            self.pointer.hide()

        x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
        largest_index = max((icon["grid_index"] for icon in self.icons.values()), default=0)
        required_rows = max((len(self.icons) + len(x_positions) - 1) // len(x_positions),
                            largest_index // len(x_positions) + 1)
        while len(y_positions) < required_rows:
            y_positions.append(y_positions[-1] + self.icon_height + self.gap)
        slots = [(x, y) for y in y_positions for x in x_positions]
        occupied = set()
        icon_ids = list(self.icon_order)
        if preferred_icon in self.icons:
            icon_ids.remove(preferred_icon)
            icon_ids.insert(0, preferred_icon)
        for icon_id in icon_ids:
            icon = self.icons[icon_id]
            desired = self._grid_position(icon["grid_index"], x_positions, y_positions)
            if desired in occupied:
                available = [point for point in slots if point not in occupied]
                candidates = available or slots
                x, y = min(candidates, key=lambda point:
                           (point[0] - desired[0]) ** 2 + (point[1] - desired[1]) ** 2)
            else:
                x, y = desired
            icon["x"], icon["y"] = x, y
            place(icon["view"], x, y)
            occupied.add((x, y))

        positions = {item["key"]: item["grid_index"]
                     for item in self.icons.values()}
        if self.population_complete and positions != self.positions:
            self.positions = positions
            save_positions(self.positions)
        if self.context_visible:
            self.context_menu.reposition()

    def _grid_axes(self, width, height):
        max_x = max(0, width - self.icon_width)
        max_y = max(0, height - 56 - self.icon_height)
        x_positions = list(range(self.gap, max_x + 1, self.icon_width + self.gap)) or [0]
        y_positions = list(range(self.gap, max_y + 1, self.icon_height + self.gap)) or [0]
        return x_positions, y_positions

    def _grid_position(self, index, x_positions, y_positions):
        row, col = divmod(max(0, index), len(x_positions))
        y = (y_positions[row] if row < len(y_positions) else
             y_positions[-1] + (row - len(y_positions) + 1) * (self.icon_height + self.gap))
        return x_positions[col], y

    def _grid_index_for(self, x, y, x_positions=None, y_positions=None):
        if x_positions is None or y_positions is None:
            x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
        col = min(range(len(x_positions)), key=lambda i: abs(x_positions[i] - x))
        row = min(range(len(y_positions)), key=lambda i: abs(y_positions[i] - y))
        return row * len(x_positions) + col

    def _add_icons(self, items, positions=None, occupied=None):
        if not items:
            return
        if occupied is None:
            occupied = {(icon["x"], icon["y"]) for icon in self.icons.values()}
        if positions is None:
            positions = []
            for item in items:
                position = self._initial_position(item[6], occupied)
                positions.append(position)
                occupied.add(position)

        specifications = []
        for index, item in enumerate(items):
            _key, symbol, label, _app_id, _context, _is_folder, _grid_index = item
            x, y = positions[index]
            cell_index = len(specifications)
            specifications.extend((
                {"type": "linear", "vertical": True, "width": self.icon_width,
                 "height": self.icon_height, "x": x, "y": y},
                {"type": "image", "parent_index": cell_index, "width": 48, "height": 48,
                 "weight": 0, "margins": {"left": (self.icon_width - 48) // 2, "top": 2},
                 "image": load(symbol)},
                {"type": "text", "parent_index": cell_index, "width": self.icon_width,
                 "height": 36, "weight": 0, "text": label, "textSize": 11,
                 "textColor": palette.text, "gravity": "center"},
            ))

        views = create_views(self.activity, self.surface, specifications)
        for index, (key, symbol, label, app_id, context, is_folder, grid_index) in enumerate(items):
            x, y = positions[index]
            cell, icon, caption = views[index * 3:index * 3 + 3]
            self.icons[cell.id] = {"key": key, "app_id": app_id, "context": context,
                                   "is_folder": is_folder, "label": label, "view": cell,
                                   "image": icon, "caption": caption, "symbol": symbol,
                                   "x": x, "y": y,
                                   "grid_index": self._grid_index_for(x, y)
                                   if grid_index is None else grid_index}
            self.icon_order.append(cell.id)

    def refresh_theme(self):
        for icon in self.icons.values():
            icon["caption"].settextcolor(palette.text)
        self.context_menu.refresh_theme()
        self.name_prompt.refresh_theme()
        self.open_with.refresh_theme()

    def refresh_associated_icons(self, _path=None, _app_id=None):
        for icon in self.icons.values():
            if icon["context"] is None or icon["is_folder"]:
                continue
            symbol = file_associations.icon_for(icon["context"], self.apps, "window")
            if symbol != icon["symbol"]:
                icon["symbol"] = symbol
                set_image(self.activity, icon["image"].id, load(symbol))

    def _hide_context_menu(self):
        self.context_menu.hide()
        self.context_icon = None
        self.context_visible = False

    def _icon_at(self, x, y):
        for icon_id in reversed(self.icon_order):
            icon = self.icons[icon_id]
            if icon["x"] <= x < icon["x"] + self.icon_width and icon["y"] <= y < icon["y"] + self.icon_height:
                return icon_id
        return None

    def _snap_icon_to_grid(self, icon_id):
        icon = self.icons[icon_id]
        x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
        occupied = [(other["x"], other["y"]) for other_id, other in self.icons.items()
                    if other_id != icon_id]

        def overlaps(x, y):
            return any(x < ox + self.icon_width and x + self.icon_width > ox
                       and y < oy + self.icon_height and y + self.icon_height > oy
                       for ox, oy in occupied)

        slots = [(x, y) for y in y_positions for x in x_positions if not overlaps(x, y)]
        if not slots:
            y = y_positions[-1] + self.icon_height + self.gap
            slots = [(x, y) for x in x_positions]
        icon["x"], icon["y"] = min(
            slots, key=lambda point: (point[0] - icon["x"]) ** 2 + (point[1] - icon["y"]) ** 2)
        icon["grid_index"] = self._grid_index_for(icon["x"], icon["y"],
                                                  x_positions, y_positions)
        place(icon["view"], icon["x"], icon["y"])

    def _show_context_menu(self, icon_id):
        icon = self.icons.get(icon_id)
        if icon_id is not None and icon is None:
            return False
        if self.drag and self.drag["icon_id"] == icon_id:
            if self.drag["moved"]:
                return False
            self.context_anchor = (self.drag["start_pointer_x"], self.drag["start_pointer_y"])
            self.drag = None
            self.pointer.hide()
        self.context_icon = icon_id
        self.context_visible = True
        if icon is None:
            actions = ["new_file", "new_folder", "open_terminal", "paste", "refresh",
                       "realign"]
        elif icon["context"] is None:
            actions = ["open"]
        else:
            actions = ["open", "open_terminal", "copy", "cut", "rename", "delete",
                       "properties", "paste"]
            if not icon["is_folder"]:
                actions.insert(1, "open_with")
        disabled = () if file_actions.has_clipboard_item() else ("paste",)
        self.context_menu.show(actions, *self.context_anchor, self._context_action,
                               disabled=disabled)
        return True

    def _context_action(self, action):
        icon = self.icons.get(self.context_icon)
        self.context_icon = None
        self.context_visible = False
        try:
            if action == "refresh":
                self._refresh_home_items()
            elif action == "realign":
                self._arrange_icons()
            elif action in ("new_file", "new_folder"):
                title, initial = (("new file", "New file.txt") if action == "new_file"
                                  else ("new folder", "New folder"))
                create = file_actions.create_file if action == "new_file" else file_actions.create_folder
                self.name_prompt.show(title, initial,
                                      lambda name: self._run_home_file_action(
                                          lambda: create(os.path.expanduser("~"), name)))
            elif action == "rename" and icon is not None and icon["context"] is not None:
                path = icon["context"]
                self.name_prompt.show(
                    "rename", os.path.basename(path),
                    lambda name: self._run_home_file_action(
                        lambda: file_actions.rename(path, name)))
            elif action == "open_terminal":
                if icon is None:
                    directory = os.path.expanduser("~")
                elif icon["context"] is not None:
                    directory = (icon["context"] if icon["is_folder"]
                                 else os.path.dirname(icon["context"]))
                else:
                    return
                self.open_app("terminal", directory)
            elif action in ("copy", "cut") and icon is not None:
                getattr(file_actions, action)(icon["context"])
            elif action == "delete" and icon is not None:
                self._delete_icon(icon["view"].id)
            elif action == "open" and icon is not None:
                if icon["context"] is None:
                    self.open_app(icon["app_id"])
                elif icon["is_folder"]:
                    self.open_app("files", icon["context"])
                else:
                    self.open_with.open(icon["context"])
            elif action == "properties" and icon is not None and icon["context"] is not None:
                self.open_app("files", {"properties": icon["context"]})
            elif action == "open_with" and icon is not None and not icon["is_folder"]:
                self.open_with.show(icon["context"])
            elif action == "paste":
                destination = os.path.expanduser("~")
                if icon is not None and icon["context"] is not None:
                    destination = (icon["context"] if icon["is_folder"]
                                   else os.path.dirname(icon["context"]))
                if file_actions.has_clipboard_item():
                    source = file_actions.clipboard["path"]
                    is_cut = file_actions.clipboard["cut"]
                    if destination not in self.pending_file_actions:
                        self.pending_file_actions.add(destination)

                        def pasted(_result, error):
                            self.pending_file_actions.discard(destination)
                            if self.closed:
                                return
                            if error is not None:
                                print(f"nexa: {error}")
                            else:
                                self._refresh_home_items()

                        file_actions.run_async(
                            lambda: file_actions.paste_item(destination, source, is_cut), pasted)
        except (OSError, ValueError) as error:
            print(f"nexa: {error}")

    def _run_home_file_action(self, operation):
        home = os.path.realpath(os.path.expanduser("~"))
        if home in self.pending_file_actions:
            print("nexa: a desktop file operation is already running")
            return
        self.pending_file_actions.add(home)

        def complete(_result, error):
            self.pending_file_actions.discard(home)
            if self.closed:
                return
            if error is not None:
                print(f"nexa: couldn't update desktop files: {error}")
                return
            self._refresh_home_items()

        file_actions.run_async(operation, complete)

    def _arrange_icons(self):
        x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
        for index, icon_id in enumerate(self.icon_order):
            icon = self.icons[icon_id]
            icon["grid_index"] = index
            icon["x"], icon["y"] = self._grid_position(index, x_positions, y_positions)
            place(icon["view"], icon["x"], icon["y"])
        self.positions = {icon["key"]: icon["grid_index"]
                          for icon in self.icons.values()}
        save_positions(self.positions)

    def _delete_icon(self, icon_id):
        icon = self.icons.get(icon_id)
        if icon is None or icon["context"] is None:
            return False
        path = icon["context"]
        home = os.path.realpath(os.path.expanduser("~"))
        target = os.path.realpath(path)
        if os.path.commonpath((home, target)) != home or target == home:
            print(f"nexa: refusing to delete home item outside home: {path}")
            return False
        if target in self.pending_file_actions:
            return False

        self.pending_file_actions.add(target)

        def deleted(_result, error):
            self.pending_file_actions.discard(target)
            if self.closed:
                return
            if error is not None:
                print(f"nexa: couldn't delete home item {path}: {error}")
                return
            current = self.icons.get(icon_id)
            if current is None or current["context"] != path:
                return
            self.positions.pop(current["key"], None)
            save_positions(self.positions)
            self.icon_order.remove(icon_id)
            del self.icons[icon_id]
            current["view"].delete()

        file_actions.run_async(lambda: file_actions.delete(target), deleted)
        return True

    def _refresh_home_items(self):
        self._schedule_home_scan(delay=0, force=True)

    def _next_icon_position(self, occupied=None):
        max_x = max(0, self.screen_width - self.icon_width)
        max_y = max(0, self.screen_height - 56 - self.icon_height)
        columns = max(1, (self.screen_width - self.gap) // (self.icon_width + self.gap))
        rows = max(1, (self.screen_height - 56 - self.gap) // (self.icon_height + self.gap))
        if occupied is None:
            occupied = [(icon["x"], icon["y"]) for icon in self.icons.values()]
        for row in range(rows):
            for col in range(columns):
                x = min(max_x, self.gap + col * (self.icon_width + self.gap))
                y = min(max_y, self.gap + row * (self.icon_height + self.gap))
                if all(x >= ox + self.icon_width or x + self.icon_width <= ox
                       or y >= oy + self.icon_height or y + self.icon_height <= oy
                       for ox, oy in occupied):
                    return x, y
        return 0, 0

    def _sync_home_items(self, available):
        available = dict(list(available.items())[:self.home_capacity])
        if available == self.home_item_snapshot:
            self._resize(self.screen_width, self.screen_height, force=True)
            return
        current = {icon["key"]: icon_id for icon_id, icon in self.icons.items()
                   if icon["context"] is not None}
        for path, icon_id in current.items():
            if path not in available:
                icon = self.icons.pop(icon_id)
                self.icon_order.remove(icon_id)
                self.positions.pop(path, None)
                icon["view"].delete()
        additions = []
        addition_positions = []
        occupied = [(icon["x"], icon["y"]) for icon in self.icons.values()]
        for path, (name, is_folder) in available.items():
            if path in current:
                icon = self.icons[current[path]]
                if icon["label"] != name:
                    icon["label"] = name
                    set_text(self.activity, icon["caption"].id, name)
                symbol = "folder" if is_folder else file_associations.icon_for(path, self.apps, "window")
                if symbol != icon["symbol"]:
                    icon["symbol"] = symbol
                    set_image(self.activity, icon["image"].id, load(symbol))
                continue
            x, y = self._next_icon_position(occupied)
            saved = self.positions.get(path)
            grid_index = None
            if isinstance(saved, int) and not isinstance(saved, bool):
                grid_index = max(0, saved)
                x_positions, y_positions = self._grid_axes(self.screen_width, self.screen_height)
                x, y = self._grid_position(grid_index, x_positions, y_positions)
            elif isinstance(saved, list) and len(saved) == 2:
                x, y = saved
                grid_index = self._grid_index_for(x, y)
            occupied.append((x, y))
            symbol = "folder" if is_folder else file_associations.icon_for(path, self.apps, "window")
            additions.append((path, symbol, name, "files", path, is_folder, grid_index))
            addition_positions.append((x, y))
        self._add_icons(additions, addition_positions)
        self.home_item_snapshot = available
        self._resize(self.screen_width, self.screen_height, force=True)

    def _schedule_home_scan(self, delay=3, force=False):
        if self.closed:
            return
        if self.home_scan_timer is not None:
            self.home_scan_timer.cancel()
        self.home_scan_generation += 1
        generation = self.home_scan_generation
        self.home_scan_timer = Timer(delay, self._scan_home_items, (generation, force))
        self.home_scan_timer.daemon = True
        self.home_scan_timer.start()

    def _scan_home_items(self, generation, force=False):
        with self.gesture_lock:
            if self.closed or generation != self.home_scan_generation:
                return
            capacity = self.home_capacity
            previous_signature = self.home_scan_signature
        home = os.path.expanduser("~")
        try:
            info = os.stat(home)
            signature = (info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        except OSError:
            signature = None
        if not force and signature == previous_signature:
            with self.gesture_lock:
                if not self.closed and generation == self.home_scan_generation:
                    self._schedule_home_scan()
            return
        available = dict(list({path: (name, is_folder)
                               for path, name, is_folder in load_home_items()}.items())[:capacity])
        with self.gesture_lock:
            if self.closed or generation != self.home_scan_generation:
                return
            self.home_scan_signature = signature
            self.pending_home_items.put(available)
            self._schedule_home_scan()

    def handle_event(self, event):
        with self.gesture_lock:
            return self._handle_event(event)

    def _cancel_long_press(self):
        timer = self.long_press_timer
        self.long_press_timer = None
        if timer is not None:
            timer.cancel()

    def _fire_long_press(self, icon_id, gesture_number):
        with self.gesture_lock:
            self.long_press_timer = None
            if (self.drag is None or self.drag["icon_id"] != icon_id
                    or self.drag["gesture"] != gesture_number or self.drag["moved"]):
                return
            self._show_context_menu(icon_id)

    def close(self):
        with self.gesture_lock:
            self.closed = True
            self.name_prompt.hide()
            self._cancel_long_press()
            if self.home_scan_timer is not None:
                self.home_scan_timer.cancel()
                self.home_scan_timer = None
            self.home_scan_generation += 1
            self.drag = None

    def _handle_event(self, event):
        if self.name_prompt.handle_event(event):
            return True
        if self.open_with.handle_event(event):
            return True
        if self.context_menu.handle_event(event):
            return True
        if event.type == tg.Event.back and self.open_with.visible:
            self.open_with.hide()
            return True
        if self.open_with.visible:
            return True
        if event.type == tg.Event.back and self.context_visible:
            self._hide_context_menu()
            return True
        if event.type != tg.Event.touch or event.id != self.root.id:
            return False
        data = event.value
        action = data.get("action")
        pointers = data.get("pointers", [])
        if action == tg.Event.touch_pointer_up and self.drag and pointers:
            index = data.get("index", -1)
            if 0 <= index < len(pointers) and pointers[index]["id"] == self.drag["pointer_id"]:
                action = tg.Event.touch_up
        if action == tg.Event.touch_down and pointers:
            pointer = pointers[0]
            local_x = pointer["x"] / self.density
            local_y = pointer["y"] / self.density
            if self.context_menu.contains(local_x, local_y):
                return True
            self._hide_context_menu()
            icon_id = self._icon_at(local_x, local_y)
            icon = self.icons.get(icon_id)
            self._cancel_long_press()
            self.gesture_number += 1
            self.drag = {"icon_id": icon_id,
                         "start_x": icon["x"] if icon is not None else local_x,
                         "start_y": icon["y"] if icon is not None else local_y,
                         "pointer_id": pointer["id"],
                         "start_pointer_x": local_x,
                         "start_pointer_y": local_y,
                         "moved": False, "gesture": self.gesture_number}
            timer = Timer(self.long_press_seconds, self._fire_long_press,
                          (icon_id, self.gesture_number))
            timer.daemon = True
            self.long_press_timer = timer
            timer.start()
            if icon is not None:
                self.pointer.move(local_x, local_y)
            else:
                self.pointer.hide()
        elif action == tg.Event.touch_move and pointers and self.drag:
            pointer = next((item for item in pointers if item["id"] == self.drag["pointer_id"]), None)
            if pointer is None:
                return True
            local_x = pointer["x"] / self.density
            local_y = pointer["y"] / self.density
            distance_x = local_x - self.drag["start_pointer_x"]
            distance_y = local_y - self.drag["start_pointer_y"]
            if not self.drag["moved"] and distance_x * distance_x + distance_y * distance_y >= self.drag_threshold ** 2:
                self.drag["moved"] = True
                self._cancel_long_press()
            if self.drag["moved"]:
                icon = self.icons.get(self.drag["icon_id"])
                if icon is not None:
                    icon["x"] = round(max(0, min(self.screen_width - self.icon_width,
                                                self.drag["start_x"] + distance_x)))
                    icon["y"] = round(max(0, min(self.screen_height - 56 - self.icon_height,
                                                self.drag["start_y"] + distance_y)))
                    place(icon["view"], icon["x"], icon["y"])
            if self.drag["icon_id"] is not None:
                self.pointer.move(local_x, local_y)
        elif action in (tg.Event.touch_up, tg.Event.touch_cancel):
            if self.drag and action == tg.Event.touch_up:
                self._cancel_long_press()
                icon_id = self.drag["icon_id"]
                icon = self.icons.get(icon_id)
                if self.drag["moved"] and icon is not None:
                    self._snap_icon_to_grid(icon_id)
                    place(icon["view"], icon["x"], icon["y"], top=True)
                    self.icon_order.remove(icon_id)
                    self.icon_order.append(icon_id)
                elif icon is not None:
                    context = icon["context"]
                    if icon["is_folder"] is False and context is not None:
                        self.open_with.open(context)
                    else:
                        self.open_app(icon["app_id"], context)
            elif self.drag and action == tg.Event.touch_cancel:
                self._cancel_long_press()
                icon = self.icons.get(self.drag["icon_id"])
                if icon is not None:
                    icon["x"], icon["y"] = self.drag["start_x"], self.drag["start_y"]
                    place(icon["view"], icon["x"], icon["y"])
            relayout_icon = None
            if self.drag and action == tg.Event.touch_up and self.drag["moved"]:
                relayout_icon = self.drag["icon_id"]
            if self.drag:
                self.pointer.hide()
                self.drag = None
            if relayout_icon is not None:
                self._resize(self.screen_width, self.screen_height, force=True,
                             preferred_icon=relayout_icon)
        return True
