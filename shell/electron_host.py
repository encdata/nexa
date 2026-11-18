import json
from collections import deque
import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
import secrets
import shutil
import signal
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import nexapip as tg
from nexapip.webview import ELECTRON_PRELOAD


HOST_SCRIPT = Path(__file__).resolve().parents[1] / "runtime" / "electron_host.mjs"
HOST_EVENT_QUEUE_SIZE = 4
logger = logging.getLogger("nexa.electron")


class ElectronHost:
    def __init__(self, app_root, entry="index.html", node=None, main_entry=None,
                 screen_size=None, scale_factor=1.0):
        self.app_root = Path(app_root).resolve()
        self.entry = entry
        self.main_entry = main_entry
        self.node = node or shutil.which("node")
        self.screen_size = screen_size or (800, 600)
        self.scale_factor = scale_factor
        self.process = None
        self.ipc_token = secrets.token_urlsafe(32)
        self.url = None
        self.server_url = None
        self.renderer_origin = None
        self.window_id = None
        self.renderer_options = {}
        self.renderer_title = ""
        self.renderer_visible = True
        self._events = Queue(maxsize=HOST_EVENT_QUEUE_SIZE)
        self._pending = deque()
        self._reader = None
        self._event_reader = None
        self._event_stream = None
        self._control_stream = None
        self._process_group = False
        self._reader_stop = Event()
        self.preload_url = None

    @property
    def origin(self):
        if self.server_url is None:
            return None
        parsed = urlsplit(self.server_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @property
    def bridge_origins(self):
        origins = {self.origin}
        if self.renderer_origin is not None:
            origins.add(self.renderer_origin)
        return sorted(origin for origin in origins if origin)

    @property
    def sync_ipc_url(self):
        if self.server_url is None:
            return None
        parsed = urlsplit(self.server_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/__nexa/ipc", "", ""))

    def start(self):
        if self.process is not None:
            if self.process.poll() is None:
                return self.url
            self.close()
        if not self.node:
            raise RuntimeError("Termux Node.js is required to host this Electron renderer")
        if not self.app_root.exists():
            raise FileNotFoundError(self.app_root)

        environment = os.environ.copy()
        environment["NEXA_ELECTRON_IPC_TOKEN"] = self.ipc_token
        environment["NEXA_ELECTRON_SCREEN_WIDTH"] = str(max(1, int(self.screen_size[0])))
        environment["NEXA_ELECTRON_SCREEN_HEIGHT"] = str(max(1, int(self.screen_size[1])))
        environment["NEXA_ELECTRON_SCALE_FACTOR"] = str(max(0.1, float(self.scale_factor)))
        event_read_fd, event_write_fd = os.pipe()
        control_read_fd, control_write_fd = os.pipe()
        environment["NEXA_ELECTRON_EVENT_FD"] = str(event_write_fd)
        environment["NEXA_ELECTRON_PARENT_FD"] = str(control_read_fd)
        self._reader_stop.clear()
        try:
            self.process = subprocess.Popen(
                [self.node, str(HOST_SCRIPT), str(self.app_root), self.entry, "0",
                 self.main_entry or ""],
                cwd=str(self.app_root.parent if self.app_root.is_file() else self.app_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                text=True,
                bufsize=1,
                pass_fds=(event_write_fd, control_read_fd),
                start_new_session=os.name == "posix"
            )
            self._process_group = os.name == "posix"
            logger.info("started Electron host pid=%s app=%s",
                        self.process.pid, self.app_root)
        except Exception:
            os.close(event_read_fd)
            os.close(event_write_fd)
            os.close(control_read_fd)
            os.close(control_write_fd)
            raise
        os.close(event_write_fd)
        os.close(control_read_fd)
        self._control_stream = os.fdopen(control_write_fd, "wb", buffering=0)
        self._event_stream = os.fdopen(event_read_fd, "r", encoding="utf-8")
        self._reader = Thread(
            target=self._read_output, args=(self.process.stdout,), daemon=True)
        self._reader.start()
        self._event_reader = Thread(
            target=self._read_events, args=(self._event_stream,), daemon=True)
        self._event_reader.start()
        ready = self._next_event(10)
        if ready is None:
            self.close()
            raise TimeoutError("Node renderer host did not start within 10 seconds")
        if ready.get("event") != "ready" or not ready.get("url"):
            self.close()
            raise RuntimeError(ready.get("error", "Node renderer host failed to start"))
        self.server_url = ready["url"]
        self.url = self.server_url
        if self.main_entry:
            while True:
                message = self._next_event(1)
                if message is None:
                    if self.process.poll() is not None:
                        self.close()
                        raise RuntimeError(
                            "Electron main process exited before creating a renderer window")
                    continue
                if message.get("event") == "renderer" and message.get("url"):
                    self.url = message["url"]
                    self.renderer_origin = _http_origin(self.url)
                    self.window_id = message.get("windowId")
                    self.preload_url = message.get("preloadUrl")
                    self.renderer_options = message.get("options") or {}
                    self.renderer_title = message.get("title") or ""
                    self.renderer_visible = message.get("visible", True)
                    logger.info("Electron main process created window %s at %s (visible=%s)",
                                self.window_id, self.url, self.renderer_visible)
                    break
                if message.get("event") == "mainError":
                    self.close()
                    raise RuntimeError(message.get("error", "Electron main process failed"))
                if message.get("event") in ("appQuit", "appExit"):
                    self.close()
                    raise RuntimeError("Electron app exited before creating a window")
                self._pending.append(message)
        return self.url

    def _read_output(self, stream):
        last_log = 0
        suppressed_logs = 0
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            line = line.replace(self.ipc_token, "[redacted]")
            now = monotonic()
            if now - last_log < 1:
                suppressed_logs += 1
                continue
            if suppressed_logs:
                logger.info("suppressed %s rapid Electron host log lines", suppressed_logs)
                suppressed_logs = 0
            logger.info("host: %s", line[:1000])
            last_log = now

        if suppressed_logs:
            logger.info("suppressed %s rapid Electron host log lines", suppressed_logs)

    def _read_events(self, stream):
        for line in stream:
            if self._reader_stop.is_set():
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"nexa electron: invalid host event: {line[:1000]}")
                continue
            while not self._reader_stop.is_set():
                try:
                    self._events.put(message, timeout=0.1)
                    break
                except Full:
                    continue
        while not self._reader_stop.is_set():
            try:
                self._events.put(None, timeout=0.1)
                break
            except Full:
                continue

    def _next_event(self, timeout):
        try:
            return self._events.get(timeout=timeout)
        except Empty:
            return None

    def ipc_request(self, message, window_id=None):
        if self.server_url is None:
            raise RuntimeError("Electron host is not running")
        parsed = urlsplit(self.server_url)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, "/__nexa/ipc", "", ""))
        payload = dict(message)
        payload["windowId"] = self.window_id if window_id is None else window_id
        payload["token"] = self.ipc_token
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            result = json.loads(error.read().decode("utf-8"))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("value")

    def renderer_event(self, event, window_id=None):
        self._post_renderer_event(event.type, event.value, window_id)

    def update_screen_metrics(self, width, height, scale_factor):
        self._post_renderer_event("screenMetricsChanged", {
            "width": width,
            "height": height,
            "scaleFactor": scale_factor
        })

    def _post_renderer_event(self, event_type, value, window_id=None):
        if self.server_url is None or not self.main_entry:
            return
        parsed = urlsplit(self.server_url)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, "/__nexa/renderer-event", "", ""))
        payload = {"windowId": self.window_id if window_id is None else window_id,
                   "type": event_type, "value": value, "token": self.ipc_token}
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=5):
                pass
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace").strip()
            print(f"nexa: electron renderer event failed ({error.code}): {detail[:1000]}")
        except OSError as error:
            print(f"nexa: electron renderer event failed: {error}")

    def window_command(self, window_id, action):
        if self.server_url is None:
            return False
        parsed = urlsplit(self.server_url)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc,
                               "/__nexa/window-command", "", ""))
        payload = {"windowId": window_id, "action": action, "token": self.ipc_token}
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=5) as response:
                return bool(json.loads(response.read().decode("utf-8")).get("value"))
        except OSError as error:
            print(f"nexa: electron window command failed: {error}")
            return False

    def drain_events(self):
        events = list(self._pending)
        self._pending.clear()
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            if event is not None:
                events.append(event)
        return events

    def has_pending_events(self):
        return bool(self._pending) or not self._events.empty()

    def close(self):
        process, self.process = self.process, None
        self._reader_stop.set()
        event_stream, self._event_stream = self._event_stream, None
        control_stream, self._control_stream = self._control_stream, None
        process_group, self._process_group = self._process_group, False
        self.url = None
        self.server_url = None
        self.renderer_origin = None
        self.window_id = None
        self.renderer_options = {}
        self.renderer_title = ""
        self.renderer_visible = True
        self.preload_url = None
        if process is not None and process_group:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process is not None and process.poll() is None:
            process.terminate()
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if process_group:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
        elif process is not None and process_group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if control_stream is not None:
            control_stream.close()
        if event_stream is not None:
            event_stream.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        self._pending.clear()
        while True:
            try:
                self._events.get_nowait()
            except Empty:
                break
        self._reader = None
        self._event_reader = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()
        return False


class ElectronWebsiteRenderer:
    def __init__(self, activity, parent, url):
        self.activity = activity
        self.parent = parent
        self.url = url
        self._closed = False
        self._visible = True
        self._renderer_dead = False
        self._last_recovery = None
        self.webview = self._create_view()

    def _create_view(self):
        webview = tg.WebView(self.activity, self.parent, enable_javascript=True)
        webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        webview.setdesktopmode(True)
        webview.allownavigation(True)
        webview.setvisibility(tg.View.VISIBLE if self._visible else tg.View.GONE)
        webview.loaduri(self.url)
        return webview

    def on_window_visibility_changed(self, visible):
        self._visible = bool(visible)
        if self._closed:
            return
        if self._renderer_dead and self._visible:
            self._restore_renderer()
        elif not self._renderer_dead:
            self.webview.setvisibility(
                tg.View.VISIBLE if self._visible else tg.View.GONE)

    def handle_event(self, event):
        if (self._closed or event.type != tg.Event.webviewDestroyed
                or event.id != self.webview.id):
            return False
        self._renderer_dead = True
        if self._visible:
            self._restore_renderer()
        return True

    def _restore_renderer(self):
        now = monotonic()
        if self._last_recovery is not None and now - self._last_recovery < 60:
            print("nexa: website renderer crashed again during recovery")
            return False
        replacement = self._create_view()
        self.webview = replacement
        self._renderer_dead = False
        self._last_recovery = now
        return True

    def on_screen_resize(self, _width, _height, _scale_factor):
        pass

    def should_tick(self):
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self._renderer_dead:
            self.webview.delete()


class _ElectronWindowView:
    def __init__(self, controller, activity, parent, window_id, url, preload_url=None,
                 window_visible=True, container_visible=True):
        self.controller = controller
        self.host = controller.host
        self.activity = activity
        self.parent = parent
        self.window_id = window_id
        self.url = url
        self.preload_url = preload_url
        self.window_visible = window_visible
        self.container_visible = container_visible
        self._applied_visible = True
        self._closed = False
        logger.info("creating WebView for window %s at %s (visible=%s)",
                    window_id, url, self.visible)
        self.webview = tg.WebView(
            activity, parent,
            visibility=tg.View.VISIBLE if self.visible else tg.View.GONE,
            electron_bridge={
                "origins": sorted({origin for origin in (
                    self.host.origin, _http_origin(url)) if origin}),
                "preload": self._preload(),
                "preload_url": preload_url,
                "ipc_endpoint": self.host.sync_ipc_url,
                "ipc_token": self.host.ipc_token,
                "window_id": window_id
            })
        if not self.visible:
            self.webview.setvisibility(tg.View.GONE)
            self._applied_visible = False
        self.webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.webview.setdesktopmode(True)
        self.webview.allownavigation(True)
        self.ipc = tg.ElectronBridge(
            self.webview,
            on_ready=lambda: controller._bridge_ready(self.window_id))
        self.ipc.handle("__nexa:renderer-error", self._log_renderer_error)
        self.webview.loaduri(url)

    @staticmethod
    def _log_renderer_error(detail):
        if not isinstance(detail, dict):
            logger.error("renderer error: %s", detail)
            return
        logger.error("renderer %s: %s (%s:%s:%s)",
                     detail.get("type", "error"), detail.get("message", ""),
                     detail.get("source", ""), detail.get("line", 0),
                     detail.get("column", 0))

    def _preload(self):
        script = ELECTRON_PRELOAD
        if self.host.main_entry:
            script = ("window.__NEXA_ELECTRON_SYNC_ENDPOINT__ = " +
                      json.dumps(self.host.sync_ipc_url) + ";\n" +
                      "window.__NEXA_ELECTRON_IPC_TOKEN__ = " +
                      json.dumps(self.host.ipc_token) + ";\n" +
                      "window.__NEXA_ELECTRON_WINDOW_ID__ = " +
                      json.dumps(self.window_id) + ";\n" + script)
        return script

    def load(self, url):
        self.url = url
        self.webview.loaduri(url)

    def handle_event(self, event):
        current = self.controller._views.get(self.window_id)
        if current is not None and current is not self:
            return current.handle_event(event)
        if event.type == tg.Event.webviewBridgeError:
            logger.error("webview bridge failed: %s",
                         event.value.get("error", "unknown error"))
            return True
        if event.type == tg.Event.webviewConsoleMessage:
            value = event.value
            logger.error("renderer console: %s (%s:%s)",
                         value.get("msg", ""), value.get("source", ""),
                         value.get("line", "?"))
            return True
        if event.type in (tg.Event.webviewNavigation, tg.Event.webviewProgress,
                          tg.Event.webviewHTTPError, tg.Event.webviewError,
                          tg.Event.webviewDestroyed):
            if event.type == tg.Event.webviewProgress and event.value.get("progress") == 100:
                logger.info("window %s finished loading %s", self.window_id, self.url)
            elif event.type in (tg.Event.webviewHTTPError, tg.Event.webviewError,
                                tg.Event.webviewDestroyed):
                logger.error("window %s WebView %s: %s",
                             self.window_id, event.type, event.value)
            self.host.renderer_event(event, self.window_id)
            if event.type == tg.Event.webviewDestroyed:
                self.controller._renderer_destroyed(self)
            return True
        return self.ipc.handle_event(event)

    def set_visibility(self, visibility):
        self.window_visible = visibility == tg.View.VISIBLE
        self._apply_visibility()

    def on_window_visibility_changed(self, visible):
        if visible:
            screen_manager = getattr(self.controller.window_host, "windows", None)
            if screen_manager is not None:
                self.controller.on_screen_resize(
                    screen_manager.screen_width, screen_manager.screen_height,
                    screen_manager.density)
        self.container_visible = visible
        self._apply_visibility()

    def on_screen_resize(self, width, height, scale_factor):
        self.controller.on_screen_resize(width, height, scale_factor)

    @property
    def visible(self):
        return self.window_visible and self.container_visible

    def _apply_visibility(self):
        visible = self.visible
        if self._applied_visible == visible:
            return
        self._applied_visible = visible
        self.webview.setvisibility(tg.View.VISIBLE if visible else tg.View.GONE)

    def close(self, renderer_destroyed=False):
        if self._closed:
            return
        self._closed = True
        self.ipc.close()
        if not renderer_destroyed:
            self.webview.delete()

    def on_tick(self):
        if self.window_id != self.controller.window_id:
            self.controller.on_tick()

    def should_tick(self):
        return self.controller.should_tick()


class ElectronRenderer:
    def __init__(self, activity, parent, app_root, entry="index.html", node=None,
                 main_entry=None, app_id="electron", app_name="electron", icon="window",
                 window_host=None, close_window=None):
        self.app_root = app_root
        self.entry = entry
        self.node = node
        self.main_entry = main_entry
        self.activity = activity
        self.parent = parent
        screen_manager = getattr(window_host, "windows", None)
        self.screen_size = (
            (screen_manager.screen_width, screen_manager.screen_height)
            if screen_manager is not None else (800, 600))
        self.scale_factor = getattr(screen_manager, "density", 1.0)
        self._screen_metrics = (*self.screen_size, self.scale_factor)
        self.host = ElectronHost(
            self.app_root, self.entry, self.node, self.main_entry,
            self.screen_size, self.scale_factor)
        self.url = self.host.start()
        self.app_id = app_id
        self.app_name = app_name
        self.icon = icon
        self.window_host = window_host
        self.close_window = close_window
        self._views = {}
        self._host_windows = {}
        self._bridge_ready_windows = set()
        self._pending_renderer_messages = {}
        self._closed_windows = set()
        self._remote_closing = set()
        self._root_hidden = not self.host.renderer_visible
        self._restart_requested = False
        self._closed = False
        self._tick_lock = Lock()
        self._renderer_recoveries = {}
        try:
            self.window_id = self.host.window_id
            self.root_view = _ElectronWindowView(
                self, activity, parent, self.window_id, self.url,
                self.host.preload_url, self.host.renderer_visible)
            self._views[self.window_id] = self.root_view
            self._flush_pending_renderer_messages(self.window_id)
            if not self.host.renderer_visible:
                self.root_view.set_visibility(tg.View.GONE)
        except Exception:
            self.host.close()
            raise

    def handle_event(self, event):
        return self.root_view.handle_event(event)

    def on_window_visibility_changed(self, visible):
        self.root_view.on_window_visibility_changed(visible)

    def on_screen_resize(self, width, height, scale_factor):
        metrics = (max(1, int(width)), max(1, int(height)), max(0.1, float(scale_factor)))
        if metrics == self._screen_metrics:
            return
        self._screen_metrics = metrics
        self.screen_size = metrics[:2]
        self.scale_factor = metrics[2]
        self.host.update_screen_metrics(*metrics)

    def _bridge_ready(self, window_id):
        self._bridge_ready_windows.add(window_id)
        self._flush_pending_renderer_messages(window_id)

    def _renderer_destroyed(self, old_view):
        window_id = old_view.window_id
        if (self._closed or self._views.get(window_id) is not old_view
                or window_id in self._closed_windows):
            return
        now = monotonic()
        last_recovery = self._renderer_recoveries.get(window_id)
        if last_recovery is not None and now - last_recovery < 60:
            print(f"nexa: electron renderer {window_id} crashed again during recovery")
            old_view.close(renderer_destroyed=True)
            return
        self._renderer_recoveries[window_id] = now
        self._bridge_ready_windows.discard(window_id)
        old_view.close(renderer_destroyed=True)
        replacement = _ElectronWindowView(
            self, old_view.activity, old_view.parent, window_id,
            old_view.url, old_view.preload_url,
            old_view.window_visible, old_view.container_visible)
        self._views[window_id] = replacement
        if window_id == self.window_id:
            self.root_view = replacement

    def _flush_pending_renderer_messages(self, window_id):
        view = self._views.get(window_id)
        if view is None or window_id not in self._bridge_ready_windows:
            return
        for message in self._pending_renderer_messages.pop(window_id, ()):
            view.ipc.emit(message["channel"], *message.get("args", []))

    def _open_child_window(self, message):
        window_id = message.get("windowId")
        if window_id is None or window_id == self.window_id or window_id in self._host_windows:
            return
        if self._root_hidden:
            self._promote_hidden_root(message)
            return
        if self.window_host is None:
            print("nexa: electron created another window but no window host is available")
            return
        url = message.get("url")
        if not url:
            return
        logger.info("creating Electron child window %s at %s", window_id, url)

        def build(activity, parent, _state):
            view = _ElectronWindowView(
                self, activity, parent, window_id, url, message.get("preloadUrl"),
                message.get("visible", True))
            self._views[window_id] = view
            self._flush_pending_renderer_messages(window_id)
            return view

        def on_close():
            self._closed_windows.add(window_id)
            if window_id not in self._remote_closing:
                Thread(target=self.host.window_command,
                       args=(window_id, "close"), daemon=True).start()
            self._host_windows.pop(window_id, None)
            self._views.pop(window_id, None)
            self._bridge_ready_windows.discard(window_id)
            self._pending_renderer_messages.pop(window_id, None)
            self._renderer_recoveries.pop(window_id, None)

        options = message.get("options") or {}
        title = message.get("title") or options.get("title") or self.app_name
        size = None
        if isinstance(options.get("width"), (int, float)) and isinstance(
                options.get("height"), (int, float)):
            size = (options["width"], options["height"])
        host_window = self.window_host.open_window(
            self.app_id, title, build, icon=self.icon, state=message,
            on_close=on_close, size=size)
        self._host_windows[window_id] = host_window
        if not message.get("visible", True):
            self.window_host.windows.minimize(host_window.window_id)

    def _promote_hidden_root(self, message):
        old_window_id = self.window_id
        old_view = self.root_view
        self._views.pop(old_window_id, None)
        self._bridge_ready_windows.discard(old_window_id)
        self._pending_renderer_messages.pop(old_window_id, None)
        self._renderer_recoveries.pop(old_window_id, None)
        old_view.close()

        self.window_id = message["windowId"]
        self.url = message["url"]
        self._root_hidden = False
        self.host.window_id = self.window_id
        self.host.preload_url = message.get("preloadUrl")
        self.host.renderer_options = message.get("options") or {}
        self.host.renderer_title = message.get("title") or ""
        self.host.renderer_visible = message.get("visible", True)
        logger.info("promoting Electron window %s to the desktop: %s",
                    self.window_id, self.url)
        self.root_view = _ElectronWindowView(
            self, self.activity, self.parent, self.window_id, self.url,
            message.get("preloadUrl"), message.get("visible", True))
        self._views[self.window_id] = self.root_view
        if not message.get("visible", True):
            self.root_view.set_visibility(tg.View.GONE)
        self._flush_pending_renderer_messages(self.window_id)

    def _close_child_window(self, window_id):
        self._closed_windows.add(window_id)
        host_window = self._host_windows.pop(window_id, None)
        self._views.pop(window_id, None)
        self._renderer_recoveries.pop(window_id, None)
        if host_window is None:
            return
        self._remote_closing.add(window_id)
        try:
            host_window.close()
        finally:
            self._remote_closing.discard(window_id)
            self._bridge_ready_windows.discard(window_id)
            self._pending_renderer_messages.pop(window_id, None)

    def _restart_host(self):
        self._restart_requested = False
        for window_id in tuple(self._host_windows):
            self._close_child_window(window_id)
        self.root_view.close()
        self.host.close()
        self._views.clear()
        self._bridge_ready_windows.clear()
        self._pending_renderer_messages.clear()
        self._closed_windows.clear()
        self._host_windows.clear()
        self._remote_closing.clear()
        self._renderer_recoveries.clear()
        try:
            self.host = ElectronHost(
                self.app_root, self.entry, self.node, self.main_entry,
                self.screen_size, self.scale_factor)
            self.url = self.host.start()
            self.window_id = self.host.window_id
            self._root_hidden = not self.host.renderer_visible
            self.root_view = _ElectronWindowView(
                self, self.activity, self.parent, self.window_id, self.url,
                self.host.preload_url, self.host.renderer_visible)
            self._views[self.window_id] = self.root_view
            if not self.host.renderer_visible:
                self.root_view.set_visibility(tg.View.GONE)
        except Exception as error:
            print(f"nexa: electron relaunch failed: {error}")
            if self.close_window is not None:
                self.close_window()
            else:
                self._closed = True
                self.host.close()

    def _handle_window_state(self, message):
        if message.get("windowId") == self.window_id:
            if message.get("action") in ("hide", "minimize"):
                logger.info("hiding Electron root window %s", self.window_id)
                self.root_view.set_visibility(tg.View.GONE)
            elif message.get("action") in ("show", "restore"):
                logger.info("showing Electron root window %s", self.window_id)
                self.root_view.set_visibility(tg.View.VISIBLE)
            return
        host_window = self._host_windows.get(message.get("windowId"))
        if host_window is None:
            return
        action = message.get("action")
        value = message.get("value") or {}
        if action == "title":
            host_window.set_title(value.get("title", ""))
        elif action == "focus":
            host_window.focus()
        elif action in ("hide", "minimize"):
            logger.info("hiding Electron window %s", message.get("windowId"))
            self.window_host.windows.minimize(host_window.window_id)
        elif action in ("show", "restore"):
            logger.info("showing Electron window %s", message.get("windowId"))
            self.window_host.windows.restore(host_window.window_id)
        elif action == "resize":
            if isinstance(value.get("width"), (int, float)) and isinstance(
                    value.get("height"), (int, float)):
                host_window.resize(value["width"], value["height"])
        elif action == "move":
            if isinstance(value.get("x"), (int, float)) and isinstance(
                    value.get("y"), (int, float)):
                host_window.move(value["x"], value["y"])
        elif action in ("maximize", "unmaximize"):
            window = self.window_host.windows.windows.get(host_window.window_id)
            if window is not None and window["maximized"] != (action == "maximize"):
                self.window_host.windows.toggle_maximize(host_window.window_id)

    def _handle_native_call(self, message):
        method = message.get("method")
        args = message.get("args") or []
        if method == "dialog.showErrorBox":
            title = str(args[0]) if args else self.app_name
            detail = str(args[1]) if len(args) > 1 else ""
            text = f"{title}\n{detail}".strip()
        elif method == "dialog.showMessageBox":
            options = args[0] if args and isinstance(args[0], dict) else {}
            title = str(options.get("title") or options.get("message") or self.app_name)
            detail = str(options.get("detail") or "")
            text = f"{title}\n{detail}".strip()
        else:
            return
        self.activity.c.toast(text[:3500], long=True)

    def on_tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            for message in self.host.drain_events():
                event = message.get("event")
                window_id = message.get("windowId")
                if event == "rendererMessage":
                    if window_id in self._closed_windows:
                        continue
                    view = self._views.get(window_id)
                    if view is not None and window_id in self._bridge_ready_windows:
                        view.ipc.emit(message["channel"], *message.get("args", []))
                    else:
                        self._pending_renderer_messages.setdefault(window_id, []).append(message)
                elif event == "renderer":
                    if window_id in self._closed_windows:
                        continue
                    if window_id == self.window_id:
                        if message.get("url") != self.url:
                            self.url = message["url"]
                            self.root_view.load(self.url)
                    elif window_id in self._host_windows:
                        view = self._views.get(window_id)
                        if view is not None and message.get("url") != view.url:
                            view.load(message["url"])
                    else:
                        self._open_child_window(message)
                elif event == "windowState":
                    self._handle_window_state(message)
                elif event == "windowClosed":
                    if window_id == self.window_id:
                        if self._root_hidden:
                            self.root_view.set_visibility(tg.View.GONE)
                            continue
                        if self.close_window is not None:
                            self.close_window()
                        else:
                            self.close()
                        return
                    self._close_child_window(window_id)
                elif event == "mainError":
                    print(f"nexa: electron main process: {message.get('error', 'failed')}")
                elif event == "nativeCall":
                    self._handle_native_call(message)
                elif event == "appRelaunch":
                    self._restart_requested = True
                elif event in ("appQuit", "appExit"):
                    if self._restart_requested:
                        self._restart_host()
                    elif self.close_window is not None:
                        self.close_window()
                    else:
                        print("nexa: electron app requested quit")
                    return
        finally:
            self._tick_lock.release()

    def should_tick(self):
        return self.host.has_pending_events()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for window_id in tuple(self._host_windows):
            self._close_child_window(window_id)
        try:
            self.root_view.close()
        finally:
            self.host.close()


def _http_origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    port = parsed.port
    if port is None or (parsed.scheme == "https" and port == 443) or (
            parsed.scheme == "http" and port == 80):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"
