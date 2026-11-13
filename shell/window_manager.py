from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic

import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.icons import load
from shell.reusable_object import ReusableObject
from shell.surface import add as add_surface, add_rounded, set_color as set_surface_color
from shell.surface import set_image_scale_type, set_rounded_color


class WindowManager:
    titlebar_height = 52
    taskbar_height = 56
    control_width = 48
    border_width = 4
    drag_threshold = 10
    resize_corner_size = 44
    resize_update_interval = 0.016
    minimum_width = 280
    minimum_height = 220

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
        self.pending_builds = []
        self.build_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-ui-build")
        self.button_events = {}
        self.next_id = 0
        self.id_lock = Lock()
        self.drag = None
        self.active_window = None
        self.show_desktop_windows = None
        self.on_active_change = None
        self.on_close = None
        self.on_open_app = None
        self.on_association_change = None
        self.on_set_theme = None
        self.on_set_wallpaper = None
        self.personalization = None
        self.theme_revision = 0
        self.theme_refresh_pending = False
        self.titlebar_template = ReusableObject(
            activity, f"nexa.titlebar.{int(self.rtl)}", self._titlebar_specifications())

    def _titlebar_specifications(self):
        views = [{"key": "titlebar", "type": "linear", "vertical": False,
                  "width": "MATCH_PARENT", "height": "MATCH_PARENT"}]

        def add(specification, parent_index=0):
            if parent_index is not None:
                specification["parentIndex"] = parent_index
            views.append(specification)
            return len(views) - 1

        def add_app_icon():
            slot = add({"key": "app_icon_slot", "type": "linear", "vertical": False,
                        "width": 48, "height": "MATCH_PARENT"})
            add({"key": "app_icon", "type": "image", "width": 20, "height": 20,
                 "margins": {"left": 14, "right": 14, "top": 16, "bottom": 16},
                 "image": load("nexa")}, slot)

        def add_control(action):
            button = add({"key": f"control_{action}", "type": "frame",
                          "width": self.control_width, "height": self.titlebar_height,
                          "background": palette.surface_light, "clickable": True})
            content = add({"type": "linear", "vertical": False,
                           "width": "MATCH_PARENT", "height": "MATCH_PARENT"}, button)
            add({"type": "image", "width": 20, "height": 20,
                 "margins": {"left": 14, "right": 14, "top": 16, "bottom": 16},
                 "image": load(action)}, content)

        order = ("close", "maximize", "minimize") if self.rtl else (
            "minimize", "maximize", "close")
        if not self.rtl:
            add_app_icon()

        add({"key": "title", "type": "text", "width": 0,
             "height": self.titlebar_height, "weight": 1, "text": "",
             "textSize": 15, "textColor": palette.text,
             "gravity": "start_center_vertical"})

        for action in order:
            add_control(action)
        if self.rtl:
            add_app_icon()
        return views

    def open_app(self, app_id, context=None, prewarm=False):
        app = self.apps[app_id]
        cache_key = (app_id, None) if context is None else None
        for window in self.windows.values():
            if (window.get("prewarming") and window["app"].app_id == app_id
                    and window["context"] == context):
                window["prewarming"] = False
                self.window_order.append(window["id"])
                window["frame"].setvisibility(tg.View.VISIBLE)
                if window.pop("prewarm_position", False):
                    self._center_window(window)
                    self._apply_window_geometry(window)
                place(window["frame"], window["x"], window["y"], top=True)
                self._set_active(window["id"])
                return window["id"]
        if self.restore_app(app_id, context):
            return None
        if (context is not None and not isinstance(context, dict)
                and cache_key is None and (app_id, None) in self.preloaded):
            window = self.preloaded.pop((app_id, None))
            set_context = getattr(window["event_handler"], "set_context", None)
            if set_context is not None:
                set_context(context)
                window["context"] = context
                title = getattr(app, "get_title", None)
                window["title"] = title(context) if title is not None else app.name
                window["title_view"].settext(window["title"])
                self.windows[window["id"]] = window
                self.window_order.append(window["id"])
                for control_id, (_owner, action) in window["button_actions"].items():
                    self.button_events[control_id] = (window["id"], action)
                window["minimized"] = False
                window["frame"].setvisibility(tg.View.VISIBLE)
                self._apply_window_geometry(window, top=True)
                self._set_active(window["id"])
                return window["id"]
            self.preloaded[(app_id, None)] = window
        if cache_key is not None and cache_key in self.preloaded:
            window = self.preloaded.pop(cache_key)
            if window.pop("prewarm_position", False):
                self._center_window(window)
            self.windows[window["id"]] = window
            self.window_order.append(window["id"])
            for control_id, (_owner, action) in window["button_actions"].items():
                self.button_events[control_id] = (window["id"], action)
            window["minimized"] = False
            window["frame"].setvisibility(tg.View.VISIBLE)
            self._apply_window_geometry(window, top=True)
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

        frame = tg.FrameLayout(self.activity, self.root)
        if prewarm:
            frame.setvisibility(tg.View.HIDDEN)
            x, y = self.screen_width + width, self.screen_height + height
        frame.setdimensions(width, height)
        place(frame, x, y, top=True)
        border_surface = add_rounded(self.activity, frame, width, height,
                                     palette.window_border, 14)
        set_image_scale_type(border_surface, "fit_xy")
        place(border_surface, 0, 0)
        panel_width = width - self.border_width * 2
        panel_height = height - self.border_width * 2
        content_background = getattr(app, "content_background", palette.window_content)
        content_surface = add_rounded(self.activity, frame, panel_width, panel_height,
                                      content_background, 10)
        set_image_scale_type(content_surface, "fit_xy")
        place(content_surface, self.border_width, self.border_width)
        panel = tg.LinearLayout(self.activity, frame, vertical=True)
        panel.setdimensions(panel_width, panel_height)
        panel.setbackgroundcolor(content_background)
        place(panel, self.border_width, self.border_width)

        title_frame = tg.FrameLayout(self.activity, panel)
        title_frame.setdimensions(tg.View.MATCH_PARENT, self.titlebar_height)
        title_frame.setlinearlayoutparams(0)
        title_surface = add_surface(self.activity, title_frame, panel_width, self.titlebar_height,
                    palette.surface_light)

        title_parts = self.titlebar_template.create(title_frame, {
            "title": {"text": window_title},
            "app_icon": {"image": load(app.icon)},
        })
        titlebar = title_parts["titlebar"]
        title_view = title_parts["title"]
        control_order = ("close", "maximize", "minimize") if self.rtl else (
            "minimize", "maximize", "close")
        controls = [title_parts[f"control_{action}"] for action in control_order]
        for action, control in zip(control_order, controls):
            self.button_events[control.id] = (window_id, action)

        content = tg.FrameLayout(self.activity, panel)
        content.setwidth(tg.View.MATCH_PARENT)
        content.setheight(0)
        content.setlinearlayoutparams(1)
        content.setbackgroundcolor(getattr(app, "content_background", palette.window_content))
        build_with_services = getattr(app, "build_with_services", None)
        window = {
            "id": window_id,
            "app": app,
            "frame": frame,
            "panel": panel,
            "content": content,
            "border_surface": border_surface,
            "content_surface": content_surface,
            "title_surface": title_surface,
            "titlebar": titlebar,
            "title_view": title_view,
            "controls": controls,
            "title": window_title,
            "context": context,
            "event_handler": None,
            "build_future": None,
            "build_pending": False,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "restore_geometry": None,
            "maximized": False,
            "minimized": False,
            "prewarming": prewarm,
            "prewarm_position": prewarm,
            "handler_activated": False,
            "button_actions": {control.id: self.button_events[control.id] for control in controls},
        }
        if getattr(app, "build_threaded", False):
            window["build_pending"] = True
        else:
            event_handler = self._build_app(app, content, context)
            window["event_handler"] = event_handler
            self._notify_window_resize(event_handler, panel_width,
                                       panel_height - self.titlebar_height)
        self.windows[window_id] = window
        self.window_order.append(window_id)
        # termux:gui sometimes needs a location update after child views are added to lay them out.
        place(border_surface, 0, 0)
        place(panel, self.border_width, self.border_width)
        place(frame, x, y, top=True)
        self._set_active(window_id)
        return window_id

    def prewarm_app(self, app_id):
        if app_id not in self.apps:
            return False
        if (app_id, None) in self.preloaded:
            return False
        if any(window["app"].app_id == app_id and window["context"] is None
               for window in self.windows.values()):
            return False

        active_window = self.active_window
        window_id = self.open_app(app_id, prewarm=True)
        window = self.windows.get(window_id)
        if window is None:
            return False
        window["prewarming"] = True
        window["prewarm_position"] = True
        if window["build_pending"]:
            window["build_pending"] = False
            window["build_future"] = self.build_executor.submit(
                self._build_app, window["app"], window["content"], window["context"])
        if window_id in self.window_order:
            self.window_order.remove(window_id)
        window["frame"].setvisibility(tg.View.HIDDEN)
        if self.active_window == window_id:
            self._set_active(active_window if active_window in self.windows else None)
        return True

    def _center_window(self, window):
        window["x"] = max(0, (self.screen_width - window["width"]) // 2)
        window["y"] = max(0, (self.screen_height - self.taskbar_height - window["height"]) // 2)

    def _build_app(self, app, content, context):
        build_with_services = getattr(app, "build_with_services", None)
        if build_with_services is None:
            return app.build(self.activity, content, context)
        return build_with_services(
            self.activity, content, context,
            {"apps": self.apps, "open_app": self.on_open_app or self.open_app,
             "association_changed": self.on_association_change,
             "personalization": self.personalization,
             "set_theme": self.on_set_theme,
             "set_wallpaper": self.on_set_wallpaper})

    def _finish_build(self, window):
        future = window["build_future"]
        if future is None or not future.done():
            return
        window["build_future"] = None
        try:
            handler = future.result()
        except Exception as error:
            print(f"nexa: couldn't open {window['title']}: {error}")
            message = tg.TextView(self.activity, f"couldn't open {window['title']}: {error}",
                                  window["content"])
            message.setdimensions(tg.View.MATCH_PARENT, 64)
            message.settextcolor(palette.text)
            message.setgravity(1, 1)
            window["build_error"] = True
            return
        window["event_handler"] = handler
        refresh_state = window.pop("theme_refresh_state", None)
        restore_theme_state = getattr(handler, "restore_theme_state", None)
        if refresh_state is not None and restore_theme_state is not None:
            restore_theme_state(refresh_state)
        self._notify_window_resize(
            handler, max(1, window["width"] - self.border_width * 2),
            max(1, window["height"] - self.border_width * 2 - self.titlebar_height))
        if self.active_window == window["id"]:
            self._activate_window_handler(window)

    @staticmethod
    def _activate_window_handler(window):
        if window.get("prewarming") or window.get("handler_activated"):
            return
        handler = window.get("event_handler")
        on_window_open = getattr(handler, "on_window_open", None)
        if on_window_open is not None:
            on_window_open()
            window["handler_activated"] = True

    def _discard_window(self, window):
        close = getattr(window["event_handler"], "close", None)
        if close is not None:
            close()
        for control_id in window["button_actions"]:
            self.button_events.pop(control_id, None)
        window["frame"].delete()

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
            self._finish_build(window)
            if (window.get("prewarming") and window["build_future"] is None
                    and not window["build_pending"]):
                self.close(window["id"])
        for window in tuple(self.pending_builds):
            if not window["build_future"].done():
                continue
            try:
                handler = window["build_future"].result()
                close = getattr(handler, "close", None)
                if close is not None:
                    close()
            except Exception as error:
                print(f"nexa: app closed while building: {error}")
            window["frame"].delete()
            self.pending_builds.remove(window)

        for window in tuple(self.windows.values()):
            if window["minimized"]:
                continue
            handler = window["event_handler"]
            on_tick = getattr(handler, "on_tick", None)
            if on_tick is not None:
                on_tick()

        if self.theme_refresh_pending and not any(
                window["build_pending"] or window["build_future"] is not None
                for window in self.windows.values()):
            self._refresh_theme_windows()

        for window in tuple(self.windows.values()):
            if not window["build_pending"]:
                continue
            window["build_pending"] = False
            window["build_future"] = self.build_executor.submit(
                self._build_app, window["app"], window["content"], window["context"])

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
        if self.show_desktop_windows is not None:
            self.show_desktop_windows = None
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

    def taskbar_context_actions(self, window_id):
        window = self.windows.get(window_id)
        if window is None:
            return ()
        if window["minimized"]:
            return ("restore", "close_window")
        if window["maximized"]:
            return ("restore", "minimize", "close_window")
        return ("open", "maximize", "minimize", "close_window")

    def taskbar_window_action(self, window_id, action):
        window = self.windows.get(window_id)
        if window is None:
            return
        if action == "open":
            self.restore(window_id)
        elif action == "restore":
            if window["maximized"]:
                self.toggle_maximize(window_id)
            self.restore(window_id)
        elif action == "maximize":
            if not window["maximized"]:
                self.toggle_maximize(window_id)
            self.restore(window_id)
        elif action == "minimize":
            self.minimize(window_id)
        elif action == "close_window":
            self.close(window_id)

    def toggle_show_desktop(self):
        if self.show_desktop_windows is not None:
            window_ids = self.show_desktop_windows
            self.show_desktop_windows = None
            for window_id in window_ids:
                if window_id in self.windows:
                    self.restore(window_id)
            return
        window_ids = [window_id for window_id in self.window_order
                      if window_id in self.windows and not self.windows[window_id]["minimized"]]
        if not window_ids:
            return
        self.show_desktop_windows = window_ids
        for window_id in window_ids:
            self.minimize(window_id)

    def _set_active(self, window_id):
        changed = self.active_window != window_id
        self.active_window = window_id
        if changed and self.on_active_change is not None:
            self.on_active_change(window_id)
        if window_id is not None:
            self._activate_window_handler(self.windows.get(window_id, {}))

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
        window["panel"].setdimensions(panel_width, panel_height)
        radius = 0 if window["maximized"] else 14
        set_rounded_color(window["border_surface"], width, height, palette.surface_light, radius)
        content_background = getattr(window["app"], "content_background", palette.window_content)
        set_rounded_color(window["content_surface"], panel_width, panel_height,
                          content_background, max(0, radius - self.border_width))
        window["panel"].setbackgroundcolor(content_background)
        set_surface_color(window["title_surface"], panel_width, self.titlebar_height,
                          palette.surface_light)
        self._notify_window_resize(window["event_handler"], panel_width,
                                   max(1, panel_height - self.titlebar_height))
        place(window["panel"], self.border_width, self.border_width)
        place(window["content_surface"], self.border_width, self.border_width)
        place(window["border_surface"], 0, 0)
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
            mode = None
            bottom = window["y"] + window["height"]
            if not window["maximized"] and local_y >= bottom - self.resize_corner_size:
                if local_x <= window["x"] + self.resize_corner_size:
                    mode = "resize-left"
                elif local_x >= window["x"] + window["width"] - self.resize_corner_size:
                    mode = "resize-right"
            if mode is None and (window["x"] + 2 <= local_x <= window["x"] + window["width"] - 2
                                 and window["y"] + 2 <= local_y <= window["y"] + 2 + self.titlebar_height):
                mode = "move"
            if mode is None:
                return True
            self.drag = {"window_id": window["id"], "pointer_id": pointer["id"],
                         "mode": mode, "start_x": window["x"], "start_y": window["y"],
                         "start_width": window["width"], "start_height": window["height"],
                         "start_pointer_x": local_x, "start_pointer_y": local_y,
                         "last_resize_update": 0,
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
            if not self.drag["moved"]:
                self.pointer.move(local_x, local_y)
                return True

            mode = self.drag["mode"]
            if mode == "move":
                if window["maximized"]:
                    self.pointer.move(local_x, local_y)
                    return True
                window["x"] = round(max(0, min(self.screen_width - window["width"],
                                               self.drag["start_x"] + distance_x)))
                window["y"] = round(max(0, min(self.screen_height - self.taskbar_height - self.titlebar_height,
                                               self.drag["start_y"] + distance_y)))
                self._place_window(window, top=False)
            else:
                max_bottom = max(1, self.screen_height - self.taskbar_height)
                max_height = max(1, max_bottom - self.drag["start_y"])
                min_height = min(self.minimum_height, max_height)
                window["height"] = max(min_height, min(max_height,
                                                       round(self.drag["start_height"] + distance_y)))
                if mode == "resize-left":
                    fixed_right = self.drag["start_x"] + self.drag["start_width"]
                    max_width = max(1, fixed_right)
                    min_width = min(self.minimum_width, max_width)
                    window["width"] = max(min_width, min(max_width,
                                                         round(self.drag["start_width"] - distance_x)))
                    window["x"] = fixed_right - window["width"]
                else:
                    max_width = max(1, self.screen_width - self.drag["start_x"])
                    min_width = min(self.minimum_width, max_width)
                    window["width"] = max(min_width, min(max_width,
                                                         round(self.drag["start_width"] + distance_x)))
                    window["x"] = self.drag["start_x"]
                now = monotonic()
                if now - self.drag["last_resize_update"] >= self.resize_update_interval:
                    self._apply_live_resize(window, move=mode == "resize-left")
                    self.drag["last_resize_update"] = now
            self.pointer.move(local_x, local_y)
            return True
        if action in (tg.Event.touch_up, tg.Event.touch_cancel):
            if self.drag["mode"].startswith("resize-"):
                self._apply_window_geometry(window)
            self.pointer.hide()
            self.drag = None
            return True
        return True

    def _apply_live_resize(self, window, move=False):
        width, height = window["width"], window["height"]
        panel_width = width - self.border_width * 2
        panel_height = height - self.border_width * 2
        window["frame"].setdimensions(width, height)
        window["border_surface"].setdimensions(width, height)
        window["content_surface"].setdimensions(panel_width, panel_height)
        window["panel"].setdimensions(panel_width, panel_height)
        window["title_surface"].setdimensions(panel_width, self.titlebar_height)
        if move:
            self._place_window(window, top=False)


    def refresh_theme(self):
        self.theme_revision += 1
        self.titlebar_template = ReusableObject(
            self.activity, f"nexa.titlebar.{int(self.rtl)}.{self.theme_revision}",
            self._titlebar_specifications())
        for window in self.windows.values():
            self._apply_window_theme(window)
        self.theme_refresh_pending = True

    def _refresh_theme_windows(self):
        self.theme_refresh_pending = False
        for window in self.windows.values():
            self._apply_window_theme(window)
            handler = window["event_handler"]
            refresh = getattr(handler, "refresh_theme", None)
            if refresh is not None:
                refresh()
                continue
            snapshot = getattr(handler, "theme_refresh_state", None)
            window["theme_refresh_state"] = snapshot() if snapshot is not None else None
            close = getattr(handler, "close", None)
            if close is not None:
                close()
            window["content"].clearchildren()
            window["event_handler"] = None
            window["handler_activated"] = False
            window["build_error"] = False
            window["build_pending"] = True
            window["build_future"] = self.build_executor.submit(
                self._build_app, window["app"], window["content"], window["context"])
            window["build_pending"] = False
        for window in tuple(self.preloaded.values()):
            self._discard_window(window)
        self.preloaded.clear()

    def _apply_window_theme(self, window):
        panel_width = max(1, window["width"] - self.border_width * 2)
        panel_height = max(1, window["height"] - self.border_width * 2)
        content_background = getattr(window["app"], "content_background", palette.window_content)
        set_rounded_color(window["border_surface"], window["width"], window["height"],
                          palette.window_border, 14)
        set_rounded_color(window["content_surface"], panel_width, panel_height,
                          content_background, 10)
        window["panel"].setbackgroundcolor(content_background)
        window["content"].setbackgroundcolor(content_background)
        set_surface_color(window["title_surface"], panel_width, self.titlebar_height,
                          palette.surface_light)
        window["title_view"].settextcolor(palette.text)
        for control in window["controls"]:
            control.setbackgroundcolor(palette.surface_light)

    def close(self, window_id):
        window = self.windows.pop(window_id, None)
        if window is None:
            return
        if self.show_desktop_windows is not None:
            self.show_desktop_windows = [item for item in self.show_desktop_windows
                                         if item != window_id]
            if not self.show_desktop_windows:
                self.show_desktop_windows = None
        if window["build_future"] is not None:
            self._finish_build(window)
        if window_id in self.window_order:
            self.window_order.remove(window_id)
        self._cancel_drag(window_id)
        was_active = self.active_window == window_id
        cache_key = (window["app"].app_id, None)
        if (window["build_future"] is None and not window["build_pending"]
                and not window.get("build_error")
                and getattr(window["app"], "preload_ui", False)
                and window["context"] is None):
            window["frame"].setvisibility(
                tg.View.HIDDEN if window.get("prewarm_position") else tg.View.GONE)
            window["prewarming"] = False
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

        if window["build_future"] is not None:
            window["frame"].setvisibility(tg.View.GONE)
            self.pending_builds.append(window)
            for event_id, (owner, _) in list(self.button_events.items()):
                if owner == window_id:
                    self.button_events.pop(event_id, None)
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
        building = [*self.windows.values(), *self.pending_builds]
        for window in building:
            future = window["build_future"]
            if future is None or future.cancel():
                continue
            if future.done():
                self._close_build_result(future)
            else:
                future.add_done_callback(self._close_build_result)
        self.build_executor.shutdown(wait=False, cancel_futures=True)
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

    @staticmethod
    def _close_build_result(future):
        try:
            handler = future.result()
        except Exception:
            return
        close = getattr(handler, "close", None)
        if close is not None:
            try:
                close()
            except Exception as error:
                print(f"nexa: couldn't clean up a background app: {error}")
