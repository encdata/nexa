import os
import logging
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
from shell.deb_launch_progress import DebLaunchProgress
from shell.text_direction import force_ltr
from shell.wallpaper import add as add_wallpaper, load as load_wallpaper
from shell.personalization import save as save_personalization, store_wallpaper
from shell.icons import load as load_icon
from shell import palette
from shell import file_actions
from shell.x86_runtime import ensure_installed, ready as x86_runtime_ready

logger = logging.getLogger("nexa.desktop")


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
        self.desktop.on_apps_changed = self.refresh_apps
        self.windows.on_install_deb = self.desktop._install_deb
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
        # The desktop and start menu already contain these discovered apps.
        # Avoid rebuilding both views once per launcher during startup.
        self.window_host.apps_changed = None
        for app in tuple(self.apps.values()):
            spec = getattr(app, "launch_spec", None)
            if spec is not None and getattr(app, "external_launcher", False):
                argv, cwd, env = spec
                self.register_wayland_launcher(app.app_id, app.name, app.icon, argv, cwd, env)
        self.window_host.apps_changed = self._launcher_apps_changed
        self.wayland_host = None
        self.wayland_start_lock = Lock()
        self.wayland_launch_queue = []
        self.wayland_starting = False
        self.wayland_start_error = None
        runtime_ready = startup.get("x86_runtime_ready")
        self.x86_runtime_ready = (x86_runtime_ready() if runtime_ready is None
                                  else runtime_ready)
        self.closed = False
        self.pointer = Pointer(activity, self.root, self.density)
        self.desktop.pointer = self.pointer
        self.windows.pointer = self.pointer
        self.launch_progress = DebLaunchProgress(
            activity, self.root, lambda: (self.screen_width, self.screen_height))
        self.launch_sequence = 0
        self.pending_linux_launches = {}
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
                for app in tuple(self.apps.values()):
                    spec = getattr(app, "launch_spec", None)
                    if spec is not None and getattr(app, "external_launcher", False):
                        argv, cwd, env = spec
                        self.register_wayland_launcher(
                            app.app_id, app.name, app.icon, argv, cwd, env)
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

    def register_wayland_launcher(self, app_id, name, icon, argv, cwd=None, env=None):
        argv = tuple(os.fspath(value) for value in argv)
        env = {str(key): str(value) for key, value in (env or {}).items()}

        def launch():
            if self.closed:
                return None
            from shell.deb_packages import graphics_packages_installed
            needs_gpu_setup = ("BOX64_LD_LIBRARY_PATH" in env
                               and not graphics_packages_installed())
            self.launch_sequence += 1
            token = self.launch_sequence
            self.pending_linux_launches[token] = {
                "name": name, "app_id": app_id,
                "command": os.path.basename(argv[0]).casefold(),
                "process": None, "started": monotonic(),
                "launched": None,
            }
            host_ready = self.wayland_host is not None
            self.launch_progress.show(
                token, name,
                "starting Linux app" if host_ready else "preparing Wayland host", 18)
            with self.wayland_start_lock:
                if self.closed:
                    self.pending_linux_launches.pop(token, None)
                    self.launch_progress.complete(token, "Nexa is closing")
                    return None
                needs_setup = (WaylandHost.needs_x86_runtime(argv)
                               and not self.x86_runtime_ready)
                if self.wayland_host is not None and not needs_setup and not needs_gpu_setup:
                    host = self.wayland_host
                    try:
                        process = host.launch(argv, cwd, env)
                    except Exception as error:
                        self.pending_linux_launches.pop(token, None)
                        self.launch_progress.complete(token, error)
                        return None
                    self._set_launch_process(token, process)
                    return process
                request = (argv, cwd, env, token)
                if request not in self.wayland_launch_queue:
                    self.wayland_launch_queue.append(request)
                self.launch_progress.update(
                    token, "preparing Linux graphics" if needs_gpu_setup
                    else "preparing x86 runtime", 24)
                if not self.wayland_starting:
                    self.wayland_starting = True
                    Thread(target=self._start_wayland_app,
                           name="nexa-wayland-start", daemon=True).start()
            return None

        return self.window_host.register_launcher(app_id, name, icon, launch)

    def _set_launch_process(self, token, process):
        pending = self.pending_linux_launches.get(token)
        if pending is None:
            return
        pending["process"] = process
        pending["launched"] = monotonic()
        self.launch_progress.update(token, "waiting for the app's first frame", 75)

    def _wayland_surface_ready(self, _surface, app_id, title, created_at):
        ready = [(token, item) for token, item in self.pending_linux_launches.items()
                 if item["launched"] is not None and item["launched"] <= created_at]
        if not ready:
            return
        actual = f"{app_id} {title}".casefold()
        token, pending = next((entry for entry in ready if any(
            candidate and candidate.casefold() in actual
            for candidate in (entry[1]["command"], entry[1]["name"]))), ready[0])
        self.pending_linux_launches.pop(token, None)
        self.launch_progress.complete(token)

    def _check_linux_launches(self):
        now = monotonic()
        for token, pending in tuple(self.pending_linux_launches.items()):
            process = pending["process"]
            launched = pending["launched"]
            if launched is None:
                if now - pending["started"] > 180:
                    self.pending_linux_launches.pop(token, None)
                    self.launch_progress.complete(token, "startup timed out")
            elif process.poll() is not None and now - launched >= 2:
                self.pending_linux_launches.pop(token, None)
                self.launch_progress.complete(
                    token, f"process exited before opening a window ({process.returncode})")
            elif now - launched >= 45:
                self.pending_linux_launches.pop(token, None)
                self.launch_progress.complete(token, "no Wayland window appeared")

    def _start_wayland_app(self):
        host = self.wayland_host
        created_host = False
        failed = False
        try:
            while True:
                with self.wayland_start_lock:
                    closing = self.closed
                    requested = tuple(self.wayland_launch_queue)
                    needs_setup = (not self.x86_runtime_ready and any(
                        WaylandHost.needs_x86_runtime(argv)
                        for argv, _cwd, _env, _token in requested))
                if closing:
                    if created_host:
                        host.close()
                    return
                if needs_setup:
                    self._post_launch_update(requested, "checking x86 runtime", 24)
                    ensure_installed(on_status=lambda status, percent:
                                     self._post_launch_update(requested, status, percent))
                    with self.wayland_start_lock:
                        if self.closed:
                            return
                        self.x86_runtime_ready = True
                if host is None:
                    self._post_launch_update(requested, "starting Wayland display", 48)
                    host = WaylandHost(
                        self.window_host, width=self.screen_width, height=self.screen_height)
                    created_host = True
                with self.wayland_start_lock:
                    closing = self.closed
                    launches = tuple(self.wayland_launch_queue)
                    late_setup = (not self.x86_runtime_ready and any(
                        WaylandHost.needs_x86_runtime(argv)
                        for argv, _cwd, _env, _token in launches))
                    if not closing and not late_setup:
                        self.wayland_host = host
                        host.on_surface_ready = self._wayland_surface_ready
                        self.wayland_start_error = None
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
            for argv, cwd, env, token in launches:
                with self.wayland_start_lock:
                    if self.closed or self.wayland_host is not host:
                        break
                try:
                    self._post_launch_update(((argv, cwd, env, token),),
                                             "starting Linux app", 64)
                    if "BOX64_LD_LIBRARY_PATH" in env:
                        host.require_gpu()
                        from shell.deb_packages import (ensure_graphics_packages,
                                                        graphics_packages_installed,
                                                        refresh_graphics_environment)
                        if not graphics_packages_installed():
                            self._post_launch_update(
                                ((argv, cwd, env, token),),
                                "installing Mesa GPU libraries", 28)
                            try:
                                ensure_graphics_packages(
                                    on_progress=lambda status, percent:
                                    self._post_launch_update(
                                        ((argv, cwd, env, token),), status,
                                        28 + int(percent * 0.45)))
                            except Exception as error:
                                logger.exception("Mesa GPU setup failed")
                                raise RuntimeError(
                                    f"could not prepare Mesa GPU rendering: {error}") from error
                        refresh_graphics_environment(env)
                    process = host.launch(argv, cwd, env)
                    file_actions.post_to_ui(self._set_launch_process, token, process)
                except Exception as error:
                    logger.exception("couldn't launch Wayland app %s", argv[0])
                    file_actions.post_to_ui(self._fail_linux_launch, token, error)
        except Exception as error:
            failed = True
            logger.exception("couldn't start Wayland host")
            self.wayland_start_error = error
            with self.wayland_start_lock:
                queued = tuple(self.wayland_launch_queue)
                self.wayland_launch_queue.clear()
            requests = (*locals().get("requested", ()), *queued)
            for token in {request[3] for request in requests}:
                file_actions.post_to_ui(self._fail_linux_launch, token, error)
            file_actions.post_to_ui(self.activity.c.toast,
                                    f"Wayland host failed: {str(error)[:120]}")
        finally:
            with self.wayland_start_lock:
                self.wayland_starting = False
                retry = bool(self.wayland_launch_queue) and not self.closed and not failed
                if retry:
                    self.wayland_starting = True
            if retry:
                Thread(target=self._start_wayland_app,
                       name="nexa-wayland-start", daemon=True).start()

    def _post_launch_update(self, requests, message, percent):
        for request in requests:
            file_actions.post_to_ui(self.launch_progress.update, request[3], message, percent)

    def _fail_linux_launch(self, token, error):
        self.pending_linux_launches.pop(token, None)
        self.launch_progress.complete(token, error)

    def set_theme(self, theme):
        if theme not in ("dark", "light") or theme == self.personalization["theme"]:
            return
        self.personalization["theme"] = palette.set_theme(theme)
        save_personalization(self.personalization)
        self.root.setbackgroundcolor(palette.background)
        self.windows.refresh_theme()
        self.taskbar.refresh_theme()
        self.desktop.refresh_theme()
        self.launch_progress.refresh_theme()

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
        self._check_linux_launches()
        self.launch_progress.tick()
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
        self.taskbar.tick()
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
        self.launch_progress.resize()
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
