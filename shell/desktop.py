import os
from threading import Lock, Timer

import termuxgui as tg

from shell import file_actions, file_associations, palette
from shell.context_menu import ContextMenu
from shell.desktop_state import load_home_items, save_positions
from shell.geometry import place
from shell.icons import load
from shell.open_with import OpenWithChooser


class Desktop:
    icon_width = 76
    icon_height = 88
    gap = 12
    drag_threshold = 10
    long_press_seconds = 0.55

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
        self.closed = False
        self.pending_file_actions = set()
        self.gesture_number = 0
        self.context_icon = None
        self.context_visible = False
        self.positions = positions
        self.surface = tg.FrameLayout(activity, root)
        self.surface.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.surface.setbackgroundcolor(0x00000000)

        columns = max(1, (screen_width - self.gap) // (self.icon_width + self.gap))
        max_rows = max(1, (screen_height - 100) // (self.icon_height + self.gap))
        capacity = columns * max_rows
        home_capacity = max(0, capacity - len(apps))
        self.home_capacity = home_capacity
        items = [(path, "folder" if is_folder else file_associations.icon_for(path, apps, "window"),
                  name, "files", path, is_folder)
                 for path, name, is_folder in home_items[:home_capacity]]
        items.extend((app.app_id, app.icon, app.name, app.app_id, None, None)
                     for app in apps.values())
        for index, (key, symbol, label, app_id, context, is_folder) in enumerate(items):
            col, row = index % columns, index // columns
            x = self.gap + col * (self.icon_width + self.gap)
            y = self.gap + row * (self.icon_height + self.gap)
            saved_position = self.positions.get(key)
            if (isinstance(saved_position, list) and len(saved_position) == 2
                    and all(isinstance(value, (int, float)) for value in saved_position)):
                x = max(0, min(screen_width - self.icon_width, saved_position[0]))
                y = max(0, min(screen_height - 56 - self.icon_height, saved_position[1]))
            self._add_icon(key, symbol, label, app_id, context, x, y, is_folder)
            if progress is not None:
                percent = 72 + round(24 * (index + 1) / max(1, len(items)))
                progress(f"placing desktop icons {index + 1}/{len(items)}", percent)
        self.context_menu = ContextMenu(activity, root,
                                        lambda: (self.screen_width, self.screen_height))
        self.open_with = OpenWithChooser(
            activity, root, lambda: (self.screen_width, self.screen_height), apps, open_app,
            self.refresh_associated_icons)
        if progress is not None:
            progress("positioning desktop", 97)
        self._resize(screen_width, screen_height, force=True)
        self.home_item_snapshot = {path: (name, is_folder)
                                   for path, name, is_folder in home_items[:home_capacity]}
        self._schedule_home_scan()
        if progress is not None:
            progress("desktop ready", 98)

    def resize(self, screen_width, screen_height):
        with self.gesture_lock:
            self._resize(screen_width, screen_height)

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

        max_x = max(0, self.screen_width - self.icon_width)
        max_y = max(0, self.screen_height - 56 - self.icon_height)
        placed = []
        icon_ids = list(self.icon_order)
        if preferred_icon in self.icons:
            icon_ids.remove(preferred_icon)
            icon_ids.insert(0, preferred_icon)
        for icon_id in icon_ids:
            icon = self.icons[icon_id]
            x = icon["x"] * self.screen_width / old_width
            y = icon["y"] * self.screen_height / old_height
            x = max(0, min(max_x, round(x)))
            y = max(0, min(max_y, round(y)))
            def overlaps(point):
                return any(point[0] < px + self.icon_width and point[0] + self.icon_width > px
                           and point[1] < py + self.icon_height and point[1] + self.icon_height > py
                           for px, py in placed)

            if overlaps((x, y)):
                preferred = (x, y)
                slots = [
                    (min(max_x, self.gap + col * (self.icon_width + self.gap)),
                     min(max_y, self.gap + row * (self.icon_height + self.gap)))
                    for row in range(max(1, self.screen_height // (self.icon_height + self.gap)))
                    for col in range(max(1, self.screen_width // (self.icon_width + self.gap)))
                ]
                available = [point for point in slots if not overlaps(point)]
                if available:
                    x, y = min(available, key=lambda point:
                               (point[0] - preferred[0]) ** 2 + (point[1] - preferred[1]) ** 2)
            icon["x"], icon["y"] = x, y
            place(icon["view"], x, y)
            placed.append((x, y))

        self.positions = {item["key"]: [round(item["x"]), round(item["y"])]
                          for item in self.icons.values()}
        save_positions(self.positions)
        if self.context_visible:
            self.context_menu.reposition()

    def _add_icon(self, key, symbol, label, app_id, context, x, y, is_folder=None):
        cell = tg.LinearLayout(self.activity, self.surface)
        cell.setdimensions(self.icon_width, self.icon_height)
        place(cell, x, y)
        icon = tg.ImageView(self.activity, cell)
        icon.setdimensions(48, 48)
        icon.setimage(load(symbol))
        icon.setmargin((self.icon_width - 48) // 2, "left")
        icon.setmargin(2, "top")
        icon.setlinearlayoutparams(0)
        caption = tg.TextView(self.activity, label, cell)
        caption.setdimensions(self.icon_width, 36)
        caption.settextsize(11)
        caption.settextcolor(palette.text)
        caption.setgravity(1, 1)
        caption.setlinearlayoutparams(0)
        self.icons[cell.id] = {"key": key, "app_id": app_id, "context": context,
                               "is_folder": is_folder,
                               "label": label, "view": cell, "image": icon, "symbol": symbol,
                               "x": x, "y": y}
        self.icon_order.append(cell.id)

    def refresh_associated_icons(self, _path=None, _app_id=None):
        for icon in self.icons.values():
            if icon["context"] is None or icon["is_folder"]:
                continue
            symbol = file_associations.icon_for(icon["context"], self.apps, "window")
            if symbol != icon["symbol"]:
                icon["symbol"] = symbol
                icon["image"].setimage(load(symbol))

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
        max_x = max(0, self.screen_width - self.icon_width)
        max_y = max(0, self.screen_height - 56 - self.icon_height)
        step_x = self.icon_width + self.gap
        step_y = self.icon_height + self.gap
        x_positions = list(range(self.gap, max_x + 1, step_x)) or [0]
        y_positions = list(range(self.gap, max_y + 1, step_y)) or [0]
        if x_positions[-1] != max_x:
            x_positions.append(max_x)
        if y_positions[-1] != max_y:
            y_positions.append(max_y)
        occupied = [(other["x"], other["y"]) for other_id, other in self.icons.items()
                    if other_id != icon_id]

        def overlaps(x, y):
            return any(x < ox + self.icon_width and x + self.icon_width > ox
                       and y < oy + self.icon_height and y + self.icon_height > oy
                       for ox, oy in occupied)

        slots = [(x, y) for y in y_positions for x in x_positions if not overlaps(x, y)]
        if not slots:
            slots = [(x, y) for y in y_positions for x in x_positions]
        icon["x"], icon["y"] = min(
            slots, key=lambda point: (point[0] - icon["x"]) ** 2 + (point[1] - icon["y"]) ** 2)
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
            actions = ["paste", "refresh", "realign"]
        elif icon["context"] is None:
            actions = ["open", "paste"]
        else:
            actions = ["open", "properties", "copy", "cut", "paste", "delete"]
            if not icon["is_folder"]:
                actions.insert(1, "open_with")
        self.context_menu.show(actions, *self.context_anchor, self._context_action)
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

    def _arrange_icons(self):
        columns = max(1, (self.screen_width - self.gap) // (self.icon_width + self.gap))
        for index, icon_id in enumerate(self.icon_order):
            icon = self.icons[icon_id]
            col, row = index % columns, index // columns
            icon["x"] = min(self.screen_width - self.icon_width,
                            self.gap + col * (self.icon_width + self.gap))
            icon["y"] = min(self.screen_height - 56 - self.icon_height,
                            self.gap + row * (self.icon_height + self.gap))
            place(icon["view"], icon["x"], icon["y"])
        self.positions = {icon["key"]: [icon["x"], icon["y"]]
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
        available = {path: (name, is_folder) for path, name, is_folder in load_home_items()}
        self._sync_home_items(available)

    def _next_icon_position(self):
        max_x = max(0, self.screen_width - self.icon_width)
        max_y = max(0, self.screen_height - 56 - self.icon_height)
        columns = max(1, (self.screen_width - self.gap) // (self.icon_width + self.gap))
        rows = max(1, (self.screen_height - 56 - self.gap) // (self.icon_height + self.gap))
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
        for path, (name, is_folder) in available.items():
            if path in current:
                icon = self.icons[current[path]]
                icon["label"] = name
                symbol = "folder" if is_folder else file_associations.icon_for(path, self.apps, "window")
                if symbol != icon["symbol"]:
                    icon["symbol"] = symbol
                    icon["image"].setimage(load(symbol))
                continue
            x, y = self._next_icon_position()
            saved = self.positions.get(path)
            if isinstance(saved, list) and len(saved) == 2:
                x, y = saved
            symbol = "folder" if is_folder else file_associations.icon_for(path, self.apps, "window")
            self._add_icon(path, symbol, name,
                           "files", path, x, y, is_folder)
        self.home_item_snapshot = available
        self._resize(self.screen_width, self.screen_height, force=True)

    def _schedule_home_scan(self):
        if self.closed:
            return
        self.home_scan_timer = Timer(3, self._scan_home_items)
        self.home_scan_timer.daemon = True
        self.home_scan_timer.start()

    def _scan_home_items(self):
        with self.gesture_lock:
            if self.closed:
                return
            available = dict(list({path: (name, is_folder)
                                   for path, name, is_folder in load_home_items()}.items())
                             [:self.home_capacity])
            if self.drag is None and available != self.home_item_snapshot:
                if self.context_visible:
                    self._hide_context_menu()
                self._sync_home_items(available)
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
            self._cancel_long_press()
            if self.home_scan_timer is not None:
                self.home_scan_timer.cancel()
                self.home_scan_timer = None
            self.drag = None

    def _handle_event(self, event):
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
