import os
import shutil
import subprocess
from pathlib import Path
from threading import Lock, Thread
from apps import discover

import nexapip as tg
from time import monotonic, sleep

from shell.desktop import Desktop
from shell.taskbar import Taskbar
from shell.window_manager import WindowManager
from shell.window_host import WindowHost
from shell.wayland import WaylandHost
from shell.pointer import Pointer
from shell.text_direction import force_ltr
from shell.wallpaper import add as add_wallpaper, load as load_wallpaper
from shell.personalization import save as save_personalization, store_wallpaper
from shell.icons import load as load_icon
from shell import palette
from shell import file_actions


class DesktopShell:
    def __init__(self, activity, startup, parent, progress, show_progress):
        self.activity = activity
        self.apps = startup["apps"]
        config = startup["configuration"]
        self.screen_width = config["screenwidth"]
        self.screen_height = config["screenheight"]
        self.density = config["density"]
        self.keyboard_hidden = config.get("keyboardHidden", True)
        self.personalization = startup["personalization"]
        self.next_wallpaper_resize_check = 0
        self.rtl = False
        self.root = tg.FrameLayout(activity, parent)
        force_ltr(self.root)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.root.setbackgroundcolor(palette.background)
        self.root.sendtouchevent(True)
        show_progress()
        previous_size = None
        root_size = (0, 0)
        for _ in range(5):
            measured_size = tuple(self.root.getdimensions())
            if measured_size[0] > 0 and measured_size[1] > 0:
                root_size = measured_size
                if measured_size == previous_size:
                    break
            previous_size = measured_size
            sleep(0.04)
        root_width, root_height = root_size
        if root_width > 0 and root_height > 0:
            self.screen_width = round(root_width / self.density)
            self.screen_height = round(root_height / self.density)
        self.wallpaper = add_wallpaper(activity, self.root, startup["wallpaper"], self.density)
        self.wallpaper.resize(self.screen_width, self.screen_height)
        self.windows = WindowManager(activity, self.root, self.apps, self.screen_width, self.screen_height,
                                     self.density, rtl=self.rtl)
        self.windows.on_open_app = self.open_app
        self.windows.personalization = self.personalization
        self.windows.on_set_theme = self.set_theme
        self.windows.on_set_wallpaper = self.set_wallpaper
        self.windows.on_refresh_apps = self.refresh_apps
        self.windows.on_app_is_running = self.app_is_running
        self.desktop = Desktop(activity, self.root, self.apps, self.open_app,
                               self.screen_width, self.screen_height, self.density,
                               startup["positions"], startup["home_items"], progress)
        self.windows.on_association_change = self.desktop.refresh_associated_icons
        self.taskbar = Taskbar(activity, self.root, self.apps, self.open_app,
                               self.windows.activate_from_taskbar,
                               self.screen_width, self.screen_height, rtl=self.rtl,
                               context_action=self._taskbar_context_action,
                               window_context_actions=self.windows.taskbar_context_actions)
        self.windows.on_active_change = self.taskbar.set_active_window
        self.windows.on_close = self.taskbar.remove_window
        self.window_host = WindowHost(self.windows, self.apps, self.taskbar,
                                      self._launcher_apps_changed)
        self.windows.window_host = self.window_host
        self.windows.on_register_wayland_launcher = self.register_wayland_launcher
        self.wayland_host = None
        self.wayland_start_lock = Lock()
        self.wayland_launch_queue = []
        self.wayland_starting = False
        self.x86_runtime_ready = self._x86_runtime_installed()
        self.closed = False
        self.pointer = Pointer(activity, self.root, self.density)
        self.desktop.pointer = self.pointer
        self.windows.pointer = self.pointer
        self.preload_queue = [app_id for app_id, app in self.apps.items()
                              if getattr(app, "preload_ui", False) and app_id != "settings"]
        if "settings" in self.apps and getattr(self.apps["settings"], "preload_ui", False):
            self.windows.prewarm_app("settings")

    @property
    def preloads_ready(self):
        return not self.preload_queue and not any(
            window.get("prewarming") for window in self.windows.windows.values())

    def open_app(self, app_id, context=None):
        app = self.apps.get(app_id)
        if getattr(app, "external_launcher", False):
            return app.launch()
        window_id = self.windows.open_app(app_id, context)
        if window_id is not None:
            self.taskbar.add_window(window_id, self.apps[app_id], self.windows.windows[window_id]["title"])
            self.taskbar.set_active_window(self.windows.active_window)

    def app_is_running(self, app_id):
        return any(window["app"].app_id == app_id
                   for window in (*self.windows.windows.values(), *self.windows.preloaded.values()))

    def refresh_apps(self, callback=None):
        def refreshed(apps, error):
            if error is None:
                load_icon.cache_clear()
                self.apps.clear()
                self.apps.update(apps)
                self.window_host.restore_launchers()
                self.windows.apps = self.apps
                self.desktop.apps = self.apps
                self.taskbar.set_apps(self.apps)
                self.desktop.sync_apps()
                self.desktop.refresh_associated_icons()
                for window in (*self.windows.windows.values(), *self.windows.preloaded.values()):
                    handler = window.get("event_handler")
                    refresh_apps = getattr(handler, "refresh_apps", None)
                    if refresh_apps is not None:
                        refresh_apps(self.apps)
            if callback is not None:
                callback(error)

        file_actions.run_background(discover, refreshed)

    def _launcher_apps_changed(self):
        self.desktop.sync_apps()
        self.taskbar.set_apps(self.apps)

    def register_wayland_launcher(self, app_id, name, icon, argv, cwd=None):
        argv = tuple(os.fspath(value) for value in argv)

        def launch():
            with self.wayland_start_lock:
                if self.closed:
                    return None
                needs_setup = (WaylandHost.needs_x86_runtime(argv)
                               and not self.x86_runtime_ready)
                if self.wayland_host is not None and not needs_setup:
                    return self.wayland_host.launch(argv, cwd)
                self.wayland_launch_queue.append((argv, cwd))
                if not self.wayland_starting:
                    self.wayland_starting = True
                    Thread(target=self._start_wayland_app,
                           name="nexa-wayland-start", daemon=True).start()
            return None

        return self.window_host.register_launcher(app_id, name, icon, launch)

    def _start_wayland_app(self):
        host = self.wayland_host
        created_host = False
        try:
            while True:
                with self.wayland_start_lock:
                    closing = self.closed
                    requested = tuple(self.wayland_launch_queue)
                    needs_setup = (not self.x86_runtime_ready and any(
                        WaylandHost.needs_x86_runtime(argv) for argv, _cwd in requested))
                if closing:
                    if created_host:
                        host.close()
                    return
                if needs_setup:
                    self._ensure_x86_runtime()
                    with self.wayland_start_lock:
                        if self.closed:
                            return
                        self.x86_runtime_ready = True
                if host is None:
                    host = WaylandHost(
                        self.window_host, width=self.screen_width, height=self.screen_height)
                    created_host = True
                with self.wayland_start_lock:
                    closing = self.closed
                    launches = tuple(self.wayland_launch_queue)
                    late_setup = (not self.x86_runtime_ready and any(
                        WaylandHost.needs_x86_runtime(argv) for argv, _cwd in launches))
                    if not closing and not late_setup:
                        self.wayland_host = host
                        self.wayland_launch_queue.clear()
                        break
                if closing:
                    if created_host:
                        host.close()
                    return
                if created_host:
                    host.close()
                    host = None
                    created_host = False
            for argv, cwd in launches:
                with self.wayland_start_lock:
                    if self.closed or self.wayland_host is not host:
                        break
                    host.launch(argv, cwd)
        except Exception as error:
            print(f"nexa: couldn't start Wayland app: {error}")
            self.activity.c.toast("couldn't start Linux app")
            with self.wayland_start_lock:
                self.wayland_launch_queue.clear()
        finally:
            with self.wayland_start_lock:
                self.wayland_starting = False
                retry = bool(self.wayland_launch_queue) and not self.closed
                if retry:
                    self.wayland_starting = True
            if retry:
                Thread(target=self._start_wayland_app,
                       name="nexa-wayland-start", daemon=True).start()

    @staticmethod
    def _ensure_x86_runtime():
        if DesktopShell._x86_runtime_installed():
            return
        pkg = shutil.which("pkg")
        if pkg is None:
            raise RuntimeError("Termux pkg command is unavailable")
        subprocess.run([pkg, "install", "-y", "glibc-repo"], check=True)
        subprocess.run([pkg, "update"], check=True)
        subprocess.run([pkg, "install", "-y", "glibc-runner", "box64-glibc",
                        "libwayland-glibc", "libwayland-protocols-glibc",
                        "libxkbcommon-glibc"], check=True)

    @staticmethod
    def _x86_runtime_installed():
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        glibc = Path(prefix) / "glibc"
        return (shutil.which("grun") is not None
                and (glibc / "bin" / "box64").is_file()
                and (glibc / "lib" / "libwayland-client.so.0").is_file())

    def set_theme(self, theme):
        if theme not in ("dark", "light") or theme == self.personalization["theme"]:
            return
        self.personalization["theme"] = palette.set_theme(theme)
        save_personalization(self.personalization)
        self.root.setbackgroundcolor(palette.background)
        self.windows.refresh_theme()
        self.taskbar.refresh_theme()
        self.desktop.refresh_theme()

    def set_wallpaper(self, path):
        if path:
            path = os.path.abspath(os.path.expanduser(path))
        prepared = load_wallpaper(self.screen_width, self.screen_height, self.density, path)
        stored_path = store_wallpaper(path)
        self.wallpaper.set_prepared(prepared)
        self.personalization["wallpaper"] = stored_path
        save_personalization(self.personalization)
        self.activity.c.toast("wallpaper updated")

    def _taskbar_context_action(self, window_id, action):
        if window_id is not None:
            self.windows.taskbar_window_action(window_id, action)
        elif action == "show_desktop":
            self.windows.toggle_show_desktop()
        elif action == "open_task_manager" and "task_manager" in self.apps:
            self.open_app("task_manager")
        elif action == "open_settings" and "settings" in self.apps:
            self.open_app("settings")

    def handle_event(self, event):
        if self.wayland_host is not None and self.wayland_host.handle_event(event):
            return True
        if event.type == tg.Event.config:
            width = event.value.get("screenwidth", self.screen_width)
            height = event.value.get("screenheight", self.screen_height)
            self._resize_screen(width, height)
            return True
        if (event.type == tg.Event.touch
                and event.id == self.root.id
                and event.value.get("action") == tg.Event.touch_down):
            self.taskbar.dismiss_menu()
            self.taskbar.dismiss_context_menu()
        return (self.windows.handle_event(event)
                or self.desktop.handle_event(event)
                or self.taskbar.handle_event(event))

    def tick(self):
        if self.wayland_host is not None:
            self.wayland_host.pump()
        self.wallpaper.tick()
        self.desktop.tick()
        now = monotonic()
        if now >= self.next_wallpaper_resize_check:
            self.next_wallpaper_resize_check = now + 0.3
            config = self.activity.getconfiguration()
            self.keyboard_hidden = config.get("keyboardHidden", self.keyboard_hidden)
            self._resize_screen(config.get("screenwidth", self.screen_width),
                                config.get("screenheight", self.screen_height))
        self.windows.tick()
        if (self.preload_queue and self.windows.active_window is None
                and not any(window.get("prewarming")
                            for window in self.windows.windows.values())):
            self.windows.prewarm_app(self.preload_queue.pop(0))

    def _resize_screen(self, width, height):
        if (width, height) == (self.screen_width, self.screen_height):
            return
        self.screen_width, self.screen_height = width, height
        self.wallpaper.resize(width, height)
        self.windows.resize(width, height)
        self.desktop.resize(width, height)
        self.taskbar.resize(width, height)
        if self.wayland_host is not None:
            self.wayland_host.resize_output(width, height)

    def close(self):
        with self.wayland_start_lock:
            self.closed = True
            host, self.wayland_host = self.wayland_host, None
            self.wayland_launch_queue.clear()
        self.desktop.close()
        self.windows.shutdown()
        if host is not None:
            host.close()
        self.wallpaper.close()


def show(activity, startup, parent, progress, show_progress):
    return DesktopShell(activity, startup, parent, progress, show_progress)
