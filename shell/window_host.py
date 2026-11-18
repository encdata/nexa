from secrets import token_hex

from shell import palette


class _HostedHandler:
    def __init__(self, handler, on_resize):
        self.handler = handler
        self.on_resize = on_resize

    def __call__(self, event):
        if callable(self.handler):
            return self.handler(event)
        callback = getattr(self.handler, "handle_event", None)
        return callback(event) if callback is not None else False

    def __getattr__(self, name):
        return getattr(self.handler, name)

    def on_window_resize(self, width, height):
        callback = getattr(self.handler, "on_window_resize", None)
        if callback is not None:
            callback(width, height)
        if self.on_resize is not None:
            self.on_resize(width, height)


class _HostedApp:
    preload_ui = False
    hosted_window = True

    def __init__(self, app_id, name, icon, content_background):
        self.app_id = app_id
        self.name = name
        self.icon = icon
        self.content_background = content_background

    def get_title(self, context=None):
        return context["title"] if context else self.name

    def build(self, activity, parent, context=None):
        handler = context["build"](activity, parent, context["state"])
        return _HostedHandler(handler, context.get("on_resize"))


class _LauncherApp:
    external_launcher = True
    preload_ui = False

    def __init__(self, app_id, name, icon, launch, supports_file=None):
        self.app_id = app_id
        self.name = name
        self.icon = icon
        self.launch = launch
        self.supports_file = supports_file

    def build(self, activity, parent, context=None):
        raise RuntimeError("external apps create windows through the window host")


class HostWindow:
    def __init__(self, host, window_id, app_id, context):
        self._host = host
        self.window_id = window_id
        self.app_id = app_id
        self._context = context
        self._closed = False

    def set_title(self, title):
        if self._closed:
            return False
        self._context["title"] = str(title)
        return self._host.windows.set_title(self.window_id, title)

    def resize(self, width, height):
        if self._closed:
            return False
        width += self._host.windows.border_width * 2
        height += (self._host.windows.titlebar_height
                   + self._host.windows.border_width * 2)
        return self._host.windows.set_window_size(
            self.window_id, width, height)

    def move(self, x, y):
        if self._closed:
            return False
        return self._host.windows.set_window_position(self.window_id, x, y)

    def focus(self):
        if self._closed:
            return False
        return self._host.windows.focus_window(self.window_id)

    def close(self):
        if self._closed:
            return False
        self._host.windows.close(self.window_id)
        return True

    def _on_closed(self):
        self._closed = True
        callback = self._context.get("on_close")
        if callback is not None:
            callback()

class WindowHost:
    """Creates Nexa-managed top-level windows for external GUI clients."""

    def __init__(self, windows, apps, taskbar, apps_changed=None):
        self.windows = windows
        self.apps = apps
        self.taskbar = taskbar
        self.apps_changed = apps_changed
        self.on_active_window_changed = None
        self._apps = {}
        self._launchers = {}
        self._toplevels = {}

    def register_launcher(self, app_id, name, icon, launch):
        if not isinstance(app_id, str) or not app_id or not callable(launch):
            raise ValueError("app_id and a launch callback are required")
        existing = self.apps.get(app_id)
        if existing is not None and not getattr(existing, "external_launcher", False):
            raise ValueError(f"app id {app_id!r} is already registered")
        supports_file = getattr(existing, "supports_file", None)
        app = _LauncherApp(str(app_id), str(name), icon, launch, supports_file)
        self._launchers[app.app_id] = app
        self.apps[app.app_id] = app
        if self.apps_changed is not None:
            self.apps_changed()
        return app

    def restore_launchers(self):
        for app_id, app in self._launchers.items():
            if app_id not in self.apps:
                self.apps[app_id] = app

    def open_toplevel(self, surface, app_id, title, build, icon=None, state=None,
                      content_background=None, on_close=None, on_resize=None,
                      size=None):
        if surface in self._toplevels:
            raise ValueError("Wayland surface already has a Nexa window")
        opened = {}

        def closed():
            window = opened.get("window")
            if self._toplevels.get(surface) is window:
                self._toplevels.pop(surface, None)
            if on_close is not None:
                on_close()

        window = self.open_window(
            app_id, title, build, icon=icon, state=state,
            content_background=content_background, on_close=closed,
            on_resize=on_resize, size=size)
        opened["window"] = window
        self._toplevels[surface] = window
        self.active_window_changed(self.windows.active_window)
        return window

    def active_window_changed(self, window_id):
        callback = self.on_active_window_changed
        if callback is not None:
            callback(window_id)

    def window_for_toplevel(self, surface):
        return self._toplevels.get(surface)

    def close_toplevel(self, surface):
        window = self._toplevels.get(surface)
        if window is None:
            return False
        return window.close()

    def open_window(self, app_id, title, build, icon=None, state=None,
                    content_background=None, on_close=None, on_resize=None,
                    size=None):
        if not app_id or not callable(build):
            raise ValueError("app_id and a build callback are required")
        launcher = self._launchers.get(str(app_id))
        if launcher is not None:
            if icon is None:
                icon = launcher.icon
            app_name = launcher.name
        else:
            registered = self.apps.get(str(app_id))
            if registered is not None:
                if icon is None:
                    icon = registered.icon
                app_name = registered.name
            else:
                if icon is None:
                    icon = "window"
                app_name = str(app_id)
        internal_id = "host:" + str(app_id)
        app = self._apps.get(internal_id)
        if app is None:
            background = (palette.window_content if content_background is None
                          else content_background)
            app = _HostedApp(internal_id, app_name, icon,
                             background)
            self._apps[internal_id] = app
            self.apps[internal_id] = app
        elif content_background is not None:
            app.content_background = content_background
        app.name = app_name
        app.icon = icon
        self.apps[internal_id] = app

        context = {"token": token_hex(12), "title": str(title), "build": build,
                   "state": state, "on_close": on_close, "on_resize": on_resize,
                   "size": None if size is None else (size[0], size[1])}
        window_id = self.windows.open_app(internal_id, context)
        window = HostWindow(self, window_id, str(app_id), context)
        self.windows.windows[window_id]["host_window"] = window
        self.taskbar.add_window(window_id, app, str(title))
        self.taskbar.set_active_window(window_id)
        return window
