from threading import Lock

import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.icons import load
from shell.surface import add as add_surface, set_color as set_surface_color


class WindowManager:
    titlebar_height = 52
    taskbar_height = 56
    control_width = 48
    border_width = 2
    drag_threshold = 10

    def __init__(self, activity, root, apps, screen_width, screen_height, density, rtl=False):
        self.activity = activity
        self.root = root
        self.apps = apps
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.density = density
        self.rtl = rtl
        self.windows = {}
        self.window_order = []
        self.preloaded = {}
        self.button_events = {}
        self.next_id = 0
        self.id_lock = Lock()
        self.drag = None
        self.active_window = None
        self.on_active_change = None
        self.on_close = None
        self.on_open_app = None
        self.on_association_change = None

    def open_app(self, app_id, context=None):
        app = self.apps[app_id]
        cache_key = (app_id, context)
        if self.restore_app(app_id, context):
            return None
        if cache_key in self.preloaded:
            window = self.preloaded.pop(cache_key)
            self.windows[window["id"]] = window
            self.window_order.append(window["id"])
            for control_id, (_owner, action) in window["button_actions"].items():
                self.button_events[control_id] = (window["id"], action)
            window["minimized"] = False
            self._apply_window_geometry(window, top=True)
            window["frame"].setvisibility(tg.View.VISIBLE)
            self._set_active(window["id"])
            return window["id"]

        with self.id_lock:
            self.next_id += 1
            window_id = self.next_id
        title = getattr(app, "get_title", None)
        window_title = title(context) if title is not None else app.name
        available_height = max(160, self.screen_height - self.taskbar_height - 24)
        width = min(self.screen_width - 24, max(360, round(self.screen_width * 0.78)), 820)
        height = min(620, max(300, round(available_height * 0.82)), available_height)
        x = max(0, (self.screen_width - width) // 2)
        y = max(0, (self.screen_height - self.taskbar_height - height) // 2)

        frame = tg.LinearLayout(self.activity, self.root, vertical=True)
        frame.setdimensions(width, height)
        frame.setbackgroundcolor(palette.window_border)
        place(frame, x, y, top=True)

        panel_width = width - self.border_width * 2
        panel_height = height - self.border_width * 2

        top_border = tg.LinearLayout(self.activity, frame)
        top_border.setdimensions(tg.View.MATCH_PARENT, self.border_width)
        top_border.setlinearlayoutparams(0)
        top_border.setbackgroundcolor(palette.window_border)

        middle = tg.LinearLayout(self.activity, frame, vertical=False)
        middle.setdimensions(tg.View.MATCH_PARENT, 0)
        middle.setlinearlayoutparams(1)

        left_border = tg.LinearLayout(self.activity, middle)
        left_border.setdimensions(self.border_width, tg.View.MATCH_PARENT)
        left_border.setlinearlayoutparams(0)
        left_border.setbackgroundcolor(palette.window_border)

        panel = tg.FrameLayout(self.activity, middle)
        panel.setdimensions(0, tg.View.MATCH_PARENT)
        panel.setlinearlayoutparams(1)

        right_border = tg.LinearLayout(self.activity, middle)
        right_border.setdimensions(self.border_width, tg.View.MATCH_PARENT)
        right_border.setlinearlayoutparams(0)
        right_border.setbackgroundcolor(palette.window_border)

        bottom_border = tg.LinearLayout(self.activity, frame)
        bottom_border.setdimensions(tg.View.MATCH_PARENT, self.border_width)
        bottom_border.setlinearlayoutparams(0)
        bottom_border.setbackgroundcolor(palette.window_border)

        surface = add_surface(self.activity, panel, panel_width, panel_height, palette.window_content)

        body = tg.LinearLayout(self.activity, panel)
        body.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        title_frame = tg.FrameLayout(self.activity, body)
        title_frame.setdimensions(tg.View.MATCH_PARENT, self.titlebar_height)
        title_frame.setlinearlayoutparams(0)
        title_surface = add_surface(self.activity, title_frame, panel_width, self.titlebar_height,
                    palette.surface_light)

        titlebar = tg.LinearLayout(self.activity, title_frame, vertical=False)
        titlebar.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        if self.rtl:
            controls = [
                self._add_control(titlebar, window_id, "close", "close", 0xFF8E3340),
                self._add_control(titlebar, window_id, "maximize", "maximize"),
                self._add_control(titlebar, window_id, "minimize", "minimize"),
            ]

        if not self.rtl:
            app_icon = self._add_title_icon(titlebar, app)

        title_view = tg.TextView(self.activity, window_title, titlebar)
        title_view.settextcolor(palette.text)
        title_view.settextsize(15)
        title_view.setheight(self.titlebar_height)
        title_view.setlinearlayoutparams(1)
        title_view.setgravity(0, 1)
        if self.rtl:
            app_icon = self._add_title_icon(titlebar, app)
        else:
            controls = [
                self._add_control(titlebar, window_id, "minimize", "minimize"),
                self._add_control(titlebar, window_id, "maximize", "maximize"),
                self._add_control(titlebar, window_id, "close", "close", 0xFF8E3340),
            ]

        content = tg.FrameLayout(self.activity, body)
        content.setwidth(tg.View.MATCH_PARENT)
        content.setheight(0)
        content.setlinearlayoutparams(1)
        content.setbackgroundcolor(palette.window_content)
        build_with_services = getattr(app, "build_with_services", None)
        if build_with_services is None:
            event_handler = app.build(self.activity, content, context)
        else:
            event_handler = build_with_services(
                self.activity, content, context,
                {"apps": self.apps, "open_app": self.on_open_app or self.open_app,
                 "association_changed": self.on_association_change})
        self._notify_window_resize(event_handler, panel_width, panel_height - self.titlebar_height)

        window = {
            "id": window_id,
            "app": app,
            "frame": frame,
            "panel": panel,
            "surface": surface,
            "title_surface": title_surface,
            "titlebar": titlebar,
            "title_view": title_view,
            "controls": controls,
            "title": window_title,
            "context": context,
            "event_handler": event_handler,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "restore_geometry": None,
            "maximized": False,
            "minimized": False,
            "button_actions": {control.id: self.button_events[control.id] for control in controls},
        }
        self.windows[window_id] = window
        self.window_order.append(window_id)
        # termux:gui sometimes needs a location update after child views are added to lay them out.
        place(frame, x, y, top=True)
        self._set_active(window_id)
        return window_id

    def _discard_window(self, window):
        close = getattr(window["event_handler"], "close", None)
        if close is not None:
            close()
        for control_id in window["button_actions"]:
            self.button_events.pop(control_id, None)
        window["frame"].delete()

    def _add_title_icon(self, parent, app):
        slot = tg.LinearLayout(self.activity, parent, vertical=False)
        slot.setdimensions(48, self.titlebar_height)
        slot.setlinearlayoutparams(0)
        icon = tg.ImageView(self.activity, slot)
        icon.setdimensions(20, 20)
        icon.setmargin(14, "left")
        icon.setmargin(14, "right")
        icon.setmargin(16, "top")
        icon.setmargin(16, "bottom")
        icon.setimage(load(app.icon))
        icon.setlinearlayoutparams(0)
        return slot

    def _add_control(self, parent, window_id, action, icon, color=palette.surface_light):
        button = tg.FrameLayout(self.activity, parent)
        button.setdimensions(self.control_width, self.titlebar_height)
        button.setlinearlayoutparams(0)
        button.setbackgroundcolor(color)
        button.sendclickevent(True)
        content = tg.LinearLayout(self.activity, button, vertical=False)
        content.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        image = tg.ImageView(self.activity, content)
        image.setdimensions(20, 20)
        image.setmargin(14, "left")
        image.setmargin(14, "right")
        image.setmargin(16, "top")
        image.setmargin(16, "bottom")
        image.setimage(load(icon))
        image.setlinearlayoutparams(0)
        self.button_events[button.id] = (window_id, action)
        return button

    def handle_event(self, event):
        event_id = getattr(event, "id", None)
        if event.type == tg.Event.click and event_id in self.button_events:
            window_id, action = self.button_events[event_id]
            self._set_active(window_id)
            if action == "close":
                self.close(window_id)
            elif action == "maximize":
                self.toggle_maximize(window_id)
            else:
                self.minimize(window_id)
            return True
        if event.type == tg.Event.touch and event_id == self.root.id:
            return self._move_window(event.value)
        for window_id in reversed(self.window_order):
            window = self.windows.get(window_id)
            if window is None or window["minimized"]:
                continue
            handler = window["event_handler"]
            if handler is not None and self._handle_app_event(handler, event):
                return True
        return False

    def tick(self):
        for window in tuple(self.windows.values()):
            handler = window["event_handler"]
            on_tick = getattr(handler, "on_tick", None)
            if on_tick is not None:
                on_tick()

    @staticmethod
    def _handle_app_event(handler, event):
        if callable(handler):
            return handler(event)
        handle_event = getattr(handler, "handle_event", None)
        return handle_event(event) if handle_event is not None else False

    def restore_app(self, app_id, context=None):
        for window_id in reversed(self.window_order):
            window = self.windows.get(window_id)
            if window is None:
                continue
            if window["app"].app_id == app_id and window["context"] == context:
                self.restore(window["id"])
                return True
        return False

    def minimize(self, window_id):
        window = self.windows[window_id]
        self._cancel_drag(window_id)
        window["minimized"] = True
        window["frame"].setvisibility(tg.View.GONE)
        if self.active_window == window_id:
            self._activate_top_window()

    def restore(self, window_id):
        window = self.windows[window_id]
        window["minimized"] = False
        window["frame"].setvisibility(tg.View.VISIBLE)
        self._raise_window(window)
        self._set_active(window_id)

    def activate_from_taskbar(self, window_id):
        window = self.windows.get(window_id)
        if window is None:
            return
        if not window["minimized"] and self.active_window == window_id:
            self.minimize(window_id)
        else:
            self.restore(window_id)

    def _set_active(self, window_id):
        if self.active_window == window_id:
            return
        self.active_window = window_id
        if self.on_active_change is not None:
            self.on_active_change(window_id)

    def _activate_top_window(self):
        window_id = next((item for item in reversed(self.window_order)
                          if item in self.windows and not self.windows[item]["minimized"]), None)
        self._set_active(window_id)

    def _raise_window(self, window):
        window_id = window["id"]
        if window_id in self.window_order:
            self.window_order.remove(window_id)
        self.window_order.append(window_id)
        self._place_window(window, top=True)

    def _notify_window_resize(self, handler, width, height):
        callback = getattr(handler, "on_window_resize", None)
        if callback is not None:
            callback(round(width * self.density), round(height * self.density))

    def _cancel_drag(self, window_id):
        if self.drag is None or self.drag["window_id"] != window_id:
            return
        self.drag = None
        self.pointer.hide()

    def resize(self, screen_width, screen_height):
        self.screen_width = max(1, screen_width)
        self.screen_height = max(1, screen_height)
        if self.drag is not None:
            self._cancel_drag(self.drag["window_id"])

        for window in self.windows.values():
            if window["maximized"]:
                window["restore_geometry"] = self._fit_geometry(*window["restore_geometry"])
                window["x"], window["y"] = 0, 0
                window["width"] = self.screen_width
                window["height"] = max(1, self.screen_height - self.taskbar_height)
            else:
                window["x"], window["y"], window["width"], window["height"] = self._fit_geometry(
                    window["x"], window["y"], window["width"], window["height"])
            self._apply_window_geometry(window)

        for window in self.preloaded.values():
            window["x"], window["y"], window["width"], window["height"] = self._fit_geometry(
                window["x"], window["y"], window["width"], window["height"])
            self._apply_window_geometry(window)

    def _fit_geometry(self, x, y, width, height):
        available_width = self.screen_width
        available_height = max(1, self.screen_height - self.taskbar_height)
        width = max(1, min(available_width, round(width)))
        height = max(1, min(available_height, round(height)))
        x = max(0, min(available_width - width, round(x)))
        y = max(0, min(available_height - height, round(y)))
        return x, y, width, height

    def toggle_maximize(self, window_id):
        window = self.windows[window_id]
        if window["maximized"]:
            window["x"], window["y"], window["width"], window["height"] = self._fit_geometry(
                *window["restore_geometry"])
            window["maximized"] = False
        else:
            window["restore_geometry"] = (window["x"], window["y"], window["width"], window["height"])
            window["x"], window["y"] = 0, 0
            window["width"] = self.screen_width
            window["height"] = max(1, self.screen_height - self.taskbar_height)
            window["maximized"] = True
        self._apply_window_geometry(window, top=True)
        if window_id in self.window_order:
            self.window_order.remove(window_id)
        self.window_order.append(window_id)

    def _apply_window_geometry(self, window, top=False):
        width, height = window["width"], window["height"]
        window["frame"].setdimensions(width, height)
        panel_width = width - self.border_width * 2
        panel_height = height - self.border_width * 2
        window["panel"].setdimensions(0, tg.View.MATCH_PARENT)
        window["frame"].setbackgroundcolor(palette.window_border)
        set_surface_color(window["surface"], panel_width, panel_height, palette.window_content)
        set_surface_color(window["title_surface"], panel_width, self.titlebar_height,
                          palette.surface_light)
        self._notify_window_resize(window["event_handler"], panel_width,
                                   max(1, panel_height - self.titlebar_height))
        self._place_window(window, top=top)

    @staticmethod
    def _place_window(window, top=True):
        place(window["frame"], window["x"], window["y"], top=top)

    def _move_window(self, data):
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
            window = next((candidate for candidate in (self.windows[item] for item in reversed(self.window_order))
                           if not candidate["minimized"]
                           and candidate["x"] <= local_x <= candidate["x"] + candidate["width"]
                           and candidate["y"] <= local_y <= candidate["y"] + candidate["height"]), None)
            if window is None:
                return False
            self._raise_window(window)
            self._set_active(window["id"])
            if (window["x"] + 2 <= local_x <= window["x"] + window["width"] - 2
                    and window["y"] + 2 <= local_y <= window["y"] + 2 + self.titlebar_height):
                self.drag = {"window_id": window["id"], "pointer_id": pointer["id"],
                             "start_x": window["x"], "start_y": window["y"],
                             "start_pointer_x": local_x, "start_pointer_y": local_y,
                             "moved": False}
                self.pointer.move(local_x, local_y)
            return True
        if self.drag is None:
            return False
        window = self.windows.get(self.drag["window_id"])
        if window is None:
            self.drag = None
            self.pointer.hide()
            return False
        if action == tg.Event.touch_move and pointers:
            pointer = next((p for p in pointers if p["id"] == self.drag["pointer_id"]), None)
            if pointer is None:
                return True

            local_x = pointer["x"] / self.density
            local_y = pointer["y"] / self.density
            distance_x = local_x - self.drag["start_pointer_x"]
            distance_y = local_y - self.drag["start_pointer_y"]
            if not self.drag["moved"] and distance_x * distance_x + distance_y * distance_y >= self.drag_threshold ** 2:
                self.drag["moved"] = True
            if window["maximized"] or not self.drag["moved"]:
                self.pointer.move(local_x, local_y)
                return True

            window["x"] = round(max(0, min(self.screen_width - window["width"],
                                           self.drag["start_x"] + distance_x)))
            window["y"] = round(max(0, min(self.screen_height - self.taskbar_height - self.titlebar_height,
                                           self.drag["start_y"] + distance_y)))
            self._place_window(window, top=False)
            self.pointer.move(local_x, local_y)
            return True
        if action in (tg.Event.touch_up, tg.Event.touch_cancel):
            self.pointer.hide()
            self.drag = None
            return True
        return True

    def close(self, window_id):
        window = self.windows.pop(window_id, None)
        if window is None:
            return
        if window_id in self.window_order:
            self.window_order.remove(window_id)
        self._cancel_drag(window_id)
        was_active = self.active_window == window_id
        cache_key = (window["app"].app_id, window["context"])
        if getattr(window["app"], "preload_ui", False) and window["context"] is None:
            window["frame"].setvisibility(tg.View.GONE)
            for event_id, (owner, _) in list(self.button_events.items()):
                if owner == window_id:
                    self.button_events.pop(event_id, None)
            previous = self.preloaded.get(cache_key)
            if previous is not None:
                self._discard_window(previous)
            self.preloaded[cache_key] = window
            if self.on_close is not None:
                self.on_close(window_id)
            if was_active:
                self._activate_top_window()
            return

        handler = window["event_handler"]
        close = getattr(handler, "close", None)
        if close is not None:
            close()
        window["frame"].delete()
        for event_id, (owner, _) in list(self.button_events.items()):
            if owner == window_id:
                self.button_events.pop(event_id, None)
        if self.on_close is not None:
            self.on_close(window_id)
        if was_active:
            self._activate_top_window()

    def shutdown(self):
        self.drag = None
        for window in self.windows.values():
            close = getattr(window["event_handler"], "close", None)
            if close is not None:
                close()
        for window in tuple(self.preloaded.values()):
            self._discard_window(window)
        self.preloaded.clear()
        self.windows.clear()
        self.window_order.clear()
        self.button_events.clear()
        self.active_window = None
