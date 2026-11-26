import json
from collections import deque
from concurrent.futures import Future
import logging
import os
from pathlib import Path
import re
from queue import Empty, Full, Queue
import secrets
import shlex
import shutil
import signal
import subprocess
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import nexapip as tg
from nexapip.webview import ELECTRON_PRELOAD


HOST_SCRIPT = Path(__file__).resolve().parents[1] / "runtime" / "electron_host.mjs"
HOST_EVENT_QUEUE_SIZE = 4
logger = logging.getLogger("nexa.electron")


def _background_throttling(options):
    preferences = options.get("webPreferences") or {}
    return preferences.get("backgroundThrottling") is not False


def _load_headers(options):
    headers = options.get("headers", {}) if isinstance(options, dict) else {}
    if not isinstance(headers, dict):
        raise ValueError("Electron loadURL headers must be an object")
    validated = {}
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not isinstance(value, str)
                or any(ord(char) < 32 and char not in "\t" or ord(char) == 127
                       for char in value)):
            raise ValueError("Electron loadURL contains an invalid HTTP header")
        validated[name] = value
    return validated or None


class ElectronHost:
    def __init__(self, app_root, entry="index.html", node=None, main_entry=None,
                 screen_size=None, scale_factor=1.0, environment=None,
                 node_runner=None):
        self.app_root = Path(app_root).resolve()
        self.entry = entry
        self.main_entry = main_entry
        self.node = node or shutil.which("node")
        self.environment = environment or {}
        self.node_runner = node_runner
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
        self.renderer_user_agent = None
        self.renderer_load_options = {"headers": {}}
        self.renderer_visible = True
        self.renderer_profile_name = None
        self.renderer_profile_temporary = False
        self.renderer_web_request_enabled = False
        self._events = Queue(maxsize=HOST_EVENT_QUEUE_SIZE)
        self._pending = deque()
        self._reader = None
        self._event_reader = None
        self._event_stream = None
        self._control_stream = None
        self._control_lock = Lock()
        self._process_group = False
        self._reader_stop = Event()
        self._renderer_event_queue = Queue(maxsize=64)
        self._renderer_event_stop = Event()
        self._renderer_event_worker = None
        self._renderer_event_drop_warned = False
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
        environment.update(self.environment)
        environment["NEXA_ELECTRON_IPC_TOKEN"] = self.ipc_token
        environment["NEXA_ELECTRON_SCREEN_WIDTH"] = str(max(1, int(self.screen_size[0])))
        environment["NEXA_ELECTRON_SCREEN_HEIGHT"] = str(max(1, int(self.screen_size[1])))
        environment["NEXA_ELECTRON_SCALE_FACTOR"] = str(max(0.1, float(self.scale_factor)))
        event_read_fd, event_write_fd = os.pipe()
        control_read_fd, control_write_fd = os.pipe()
        environment["NEXA_ELECTRON_EVENT_FD"] = str(event_write_fd)
        environment["NEXA_ELECTRON_PARENT_FD"] = str(control_read_fd)
        self._reader_stop.clear()
        self._renderer_event_stop.clear()
        self._renderer_event_drop_warned = False
        if self.main_entry:
            self._renderer_event_worker = Thread(
                target=self._renderer_event_loop,
                name="nexa-electron-events", daemon=True)
            self._renderer_event_worker.start()
        try:
            command = [*(self.node if isinstance(self.node, (list, tuple)) else [self.node]),
                       str(HOST_SCRIPT), str(self.app_root), self.entry, "0",
                       self.main_entry or ""]
            if self.node_runner:
                environment.pop("LD_LIBRARY_PATH", None)
                command = [self.node_runner, "--shell",
                           "export LD_PRELOAD=libresolv.so.2; exec " + shlex.join(command)]
            self.process = subprocess.Popen(
                command,
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
            self._renderer_event_stop.set()
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
            exit_code = self.process.poll()
            self.close()
            if exit_code is not None:
                raise RuntimeError(
                    f"Node renderer host exited before ready (status {exit_code})")
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
                    exit_code = self.process.poll()
                    if exit_code is not None:
                        self.close()
                        raise RuntimeError(
                            "Electron main process exited before creating a renderer window "
                            f"(status {exit_code})")
                    continue
                if message.get("event") == "processExit":
                    logger.error("Electron main process called process.exit before its first window "
                                 "(code=%s): %s", message.get("code", 0),
                                 message.get("stack", "no stack available"))
                    self.close()
                    raise RuntimeError(
                        "Electron main process called process.exit before creating a renderer window")
                if message.get("event") == "renderer" and message.get("url"):
                    self.url = message["url"]
                    self.renderer_origin = _http_origin(self.url)
                    self.window_id = message.get("windowId")
                    self.preload_url = message.get("preloadUrl")
                    self.renderer_options = message.get("options") or {}
                    self.renderer_title = message.get("title") or ""
                    self.renderer_user_agent = message.get("userAgent")
                    self.renderer_load_options = message.get("loadOptions") or {"headers": {}}
                    self.renderer_visible = message.get("visible", True)
                    self.renderer_profile_name = message.get("profileName")
                    self.renderer_profile_temporary = message.get("profileTemporary") is True
                    self.renderer_web_request_enabled = message.get("webRequestEnabled") is True
                    logger.info("Electron main process created window %s at %s (visible=%s)",
                                self.window_id, self.url, self.renderer_visible)
                    break
                if message.get("event") == "mainError":
                    self.close()
                    raise RuntimeError(message.get("error", "Electron main process failed"))
                if message.get("event") in ("appQuit", "appExit"):
                    logger.error("Electron app exited before its first window (event=%s, code=%s)",
                                 message["event"], message.get("code", 0))
                    self.close()
                    raise RuntimeError("Electron app exited before creating a window")
                self._pending.append(message)
        return self.url

    def _read_output(self, stream):
        previous_line = None
        repeated_lines = 0
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            line = line.replace(self.ipc_token, "[redacted]")
            if line == previous_line:
                repeated_lines += 1
                continue
            if repeated_lines:
                logger.info("previous Electron host line repeated %s more times", repeated_lines)
                repeated_lines = 0
            logger.info("host: %s", line[:1000])
            previous_line = line

        if repeated_lines:
            logger.info("previous Electron host line repeated %s more times", repeated_lines)

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
        self._queue_renderer_event(event.type, event.value, window_id)

    def _queue_renderer_event(self, event_type, value, window_id=None):
        if self.server_url is None or not self.main_entry or self._renderer_event_stop.is_set():
            return
        try:
            self._renderer_event_queue.put_nowait(
                (event_type, value, window_id))
        except Full:
            if not self._renderer_event_drop_warned:
                logger.warning("Electron renderer event queue is full; dropping events")
                self._renderer_event_drop_warned = True

    def _renderer_event_loop(self):
        while not self._renderer_event_stop.is_set():
            message = self._renderer_event_queue.get()
            try:
                if message is None or self._renderer_event_stop.is_set():
                    return
                event_type, value, window_id = message
                self._post_renderer_event(event_type, value, window_id)
            finally:
                self._renderer_event_queue.task_done()

    def update_screen_metrics(self, width, height, scale_factor):
        self._queue_renderer_event("screenMetricsChanged", {
            "width": width,
            "height": height,
            "scaleFactor": scale_factor
        }, None)

    def update_window_geometry(self, window_id, x, y, width, height, maximized):
        if not self.main_entry:
            return False
        return self._send_control_message({
            "kind": "windowGeometry",
            "windowId": window_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "maximized": maximized
        })

    def update_devtools_state(self, contents_id, opened):
        if not self.main_entry:
            return False
        return self._send_control_message({
            "kind": "devToolsState",
            "contentsId": contents_id,
            "opened": bool(opened)
        })

    def _post_renderer_event(self, event_type, value, window_id=None):
        if not self.main_entry:
            return
        payload = {"kind": "rendererEvent",
                   "windowId": self.window_id if window_id is None else window_id,
                   "type": event_type, "value": value}
        self._send_control_message(payload)

    def native_call_result(self, request_id, value=None, error=None):
        payload = {"kind": "nativeCallResult", "requestId": request_id,
                   "value": value}
        if error is not None:
            payload["error"] = str(error)
        return self._send_control_message(payload)

    def _send_control_message(self, payload):
        control_stream = self._control_stream
        if control_stream is None:
            return False
        try:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            if len(data) > 1024 * 1024:
                logger.warning("Electron control message exceeds 1 MiB; dropping it")
                return False
            with self._control_lock:
                if self._control_stream is not control_stream:
                    return False
                offset = 0
                while offset < len(data):
                    written = control_stream.write(data[offset:])
                    if not written:
                        raise OSError("Electron control pipe accepted no data")
                    offset += written
            return True
        except (OSError, ValueError) as error:
            if not self._renderer_event_stop.is_set():
                logger.warning("Electron control pipe failed: %s", error)
                self._renderer_event_stop.set()
            return False

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
        self._renderer_event_stop.set()
        while True:
            try:
                self._renderer_event_queue.get_nowait()
                self._renderer_event_queue.task_done()
            except Empty:
                break
        try:
            self._renderer_event_queue.put_nowait(None)
        except Full:
            pass
        renderer_event_worker, self._renderer_event_worker = self._renderer_event_worker, None
        event_stream, self._event_stream = self._event_stream, None
        control_stream, self._control_stream = self._control_stream, None
        process_group, self._process_group = self._process_group, False
        self.url = None
        self.server_url = None
        self.renderer_origin = None
        self.window_id = None
        self.renderer_options = {}
        self.renderer_title = ""
        self.renderer_user_agent = None
        self.renderer_load_options = {"headers": {}}
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
        if renderer_event_worker is not None and renderer_event_worker is not current_thread():
            renderer_event_worker.join(timeout=0.1)

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
                 window_visible=True, container_visible=True, background_throttling=True,
                 profile_name=None, web_request_enabled=None, user_agent=None,
                 load_options=None):
        self.controller = controller
        self.host = controller.host
        self.activity = activity
        self.parent = parent
        self.window_id = window_id
        self.url = url
        self.load_options = load_options or {"headers": {}}
        self.preload_url = preload_url
        self.user_agent = (self.host.renderer_user_agent
                           if user_agent is None else user_agent)
        self.profile_name = profile_name or self.host.renderer_profile_name
        self.web_request_enabled = (self.host.renderer_web_request_enabled
                                    if web_request_enabled is None
                                    else web_request_enabled)
        if self.profile_name in controller._web_request_profiles:
            self.web_request_enabled = True
        self.window_visible = window_visible
        self.container_visible = container_visible
        self.background_throttling = bool(background_throttling)
        self._applied_visible = True
        self._closed = False
        self._renderer_dead = False
        self._renderer_killed_hidden = False
        self._renderer_console_errors = {}
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
                "window_id": window_id,
                "profile_name": self.profile_name,
                "web_request_enabled": self.web_request_enabled,
                "background_throttling": self.background_throttling
            })
        if not self.visible:
            self.webview.setvisibility(tg.View.GONE)
            self._applied_visible = False
        self.webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.webview.setdesktopmode(True)
        if isinstance(self.user_agent, str):
            self.webview.setuseragent(self.user_agent)
        self.webview.allownavigation(True)
        self.ipc = tg.ElectronBridge(
            self.webview,
            transport=lambda request: self.host.ipc_request(request, self.window_id),
            on_ready=lambda: controller._bridge_ready(self.window_id))
        self.ipc.handle("__nexa:renderer-error", self._log_renderer_error)
        self.webview.loaduri(url, _load_headers(self.load_options))

    @staticmethod
    def _log_renderer_error(detail):
        if not isinstance(detail, dict):
            logger.error("renderer error: %s", detail)
            return
        logger.error("renderer window=%s %s in %s: %s (%s:%s:%s; frame=%s; main=%s; require=%s; identifier=%s; global=%s)%s",
                     detail.get("windowId"),
                     detail.get("type", "error"), detail.get("context", "window"),
                     detail.get("message", ""),
                     detail.get("source", ""), detail.get("line", 0),
                     detail.get("column", 0), detail.get("frameUrl", ""),
                     detail.get("isMainFrame"), detail.get("hasRequire"),
                     detail.get("hasRequireIdentifier"),
                     detail.get("hasGlobalRequire"),
                     "\n" + detail["stack"] if detail.get("stack") else "")

    def _log_console_error(self, detail):
        message = str(detail.get("msg", ""))[:1000]
        source = str(detail.get("source", ""))[:500]
        line = detail.get("line", "?")
        contents_id = getattr(self, "contents_id", self.window_id)
        webview_id = getattr(getattr(self, "webview", None), "id", None)
        repeat_count = detail.get("repeatCount")
        if isinstance(repeat_count, int) and repeat_count > 0:
            logger.error("renderer console window=%s contents=%s webview=%s: %s (%s:%s; repeated %s times)",
                         self.window_id,
                         contents_id, webview_id,
                         message, source, line, repeat_count)
            return
        key = (message, source, line)
        if key not in self._renderer_console_errors:
            if len(self._renderer_console_errors) >= 128:
                self._renderer_console_errors.pop(next(iter(self._renderer_console_errors)))
            self._renderer_console_errors[key] = 0
        count = self._renderer_console_errors[key] + 1
        self._renderer_console_errors[key] = count
        if count & (count - 1) == 0:
            logger.error("renderer console window=%s contents=%s webview=%s: %s (%s:%s; repeated %s times)",
                         self.window_id,
                         contents_id, webview_id,
                         message, source, line, count)

    def _preload(self):
        script = ELECTRON_PRELOAD
        if self.host.main_entry:
            cwd = (self.host.app_root.parent if self.host.app_root.is_file()
                   else self.host.app_root)
            home = os.environ.get("HOME", str(Path.home()))
            script = ("window.__NEXA_ELECTRON_SYNC_ENDPOINT__ = " +
                      json.dumps(self.host.sync_ipc_url) + ";\n" +
                      "window.__NEXA_ELECTRON_IPC_TOKEN__ = " +
                      json.dumps(self.host.ipc_token) + ";\n" +
                      "window.__NEXA_ELECTRON_WINDOW_ID__ = " +
                      json.dumps(self.window_id) + ";\n" +
                      "window.__NEXA_ELECTRON_CWD__ = " +
                      json.dumps(str(cwd)) + ";\n" +
                      "window.__NEXA_ELECTRON_HOME__ = " +
                      json.dumps(home) + ";\n" + script)
        return script

    def load(self, url, load_options=None):
        self.url = url
        if load_options is not None:
            self.load_options = load_options
        if self._renderer_dead:
            if self.visible:
                self.controller._restore_renderer(self)
            return
        self.webview.loaduri(url, _load_headers(self.load_options))

    def handle_event(self, event):
        current = self.controller._views.get(self.window_id)
        if current is not None and current is not self:
            return current.handle_event(event)
        if self._renderer_dead:
            return True
        if event.type == tg.Event.webviewBridgeError:
            logger.error("webview bridge failed: %s",
                         event.value.get("error", "unknown error"))
            return True
        if event.type == tg.Event.webviewConsoleMessage:
            value = event.value
            self._log_console_error(value)
            return True
        if event.type in (tg.Event.webviewNavigation, tg.Event.webviewInPageNavigation,
                          tg.Event.webviewLoadStarted,
                          tg.Event.webviewCommitVisible, tg.Event.webviewLoadFinished,
                          tg.Event.webviewProgress,
                          tg.Event.webviewHTTPError, tg.Event.webviewError,
                          tg.Event.webviewRequestError,
                          tg.Event.webviewDestroyed, tg.Event.webviewTitleChanged):
            if event.type == tg.Event.webviewNavigation:
                logger.info("window %s navigation started: %s (main_frame=%s)",
                            self.window_id, event.value.get("url"),
                            event.value.get("isMainFrame"))
            elif event.type == tg.Event.webviewLoadStarted:
                logger.info("window %s document load started: %s",
                            self.window_id, event.value.get("url"))
            elif event.type == tg.Event.webviewCommitVisible:
                logger.info("window %s document committed: %s",
                            self.window_id, event.value.get("url"))
            elif event.type == tg.Event.webviewProgress:
                progress = event.value.get("progress")
                if progress == 100:
                    logger.info("window %s finished loading %s",
                                self.window_id, self.url)
                    self.controller._root_renderer_loaded(self)
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
        if self._renderer_dead:
            if self.visible:
                self.controller._restore_renderer(self)
            return
        self._apply_visibility()

    def set_content_bounds(self, bounds):
        if not isinstance(bounds, dict):
            return
        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        width = max(1, int(bounds.get("width", 1)))
        height = max(1, int(bounds.get("height", 1)))
        self.webview.setdimensions(width, height)
        for direction, margin in (("left", x), ("top", y), ("right", 0), ("bottom", 0)):
            self.webview.setmargin(margin, direction)

    def set_background_throttling(self, enabled):
        self.background_throttling = bool(enabled)
        if self._renderer_dead:
            if self.visible:
                self.controller._restore_renderer(self)
            return
        self.webview.setbackgroundthrottling(self.background_throttling)

    def on_window_visibility_changed(self, visible):
        if visible:
            screen_manager = getattr(self.controller.window_host, "windows", None)
            if screen_manager is not None:
                self.controller.on_screen_resize(
                    screen_manager.screen_width, screen_manager.screen_height,
                    screen_manager.density)
        self.container_visible = visible
        if self._renderer_dead:
            if self.visible:
                self.controller._restore_renderer(self)
            return
        self._apply_visibility()

    def on_screen_resize(self, width, height, scale_factor):
        self.controller.on_screen_resize(width, height, scale_factor)

    def on_window_geometry(self, x, y, width, height, maximized):
        self.controller.host.update_window_geometry(
            self.window_id, x, y, width, height, maximized)

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


class _ElectronEmbeddedView:
    def __init__(self, controller, contents_id, window_id, message, parent):
        self.controller = controller
        self.host = controller.host
        self.contents_id = contents_id
        self.window_id = window_id
        self.activity = controller.activity
        self.parent = parent
        self.url = message.get("url", "about:blank")
        self.load_options = message.get("loadOptions") or {"headers": {}}
        self.user_agent = message.get("userAgent")
        self.profile_name = message.get("profileName")
        self.bounds = {"x": 0, "y": 0, "width": 1, "height": 1}
        options = message.get("options") or {}
        self.background_throttling = _background_throttling(options)
        self._closed = False
        self._user_agent_ready = False
        self._renderer_console_errors = {}
        self.webview = tg.WebView(
            self.activity, self.parent,
            visibility=tg.View.VISIBLE if message.get("visible", True) else tg.View.GONE,
            electron_bridge={
                "origins": sorted({origin for origin in (
                    self.host.origin, _http_origin(self.url)) if origin}),
                "preload": self._preload(),
                "preload_url": message.get("preloadUrl"),
                "ipc_endpoint": self.host.sync_ipc_url,
                "ipc_token": self.host.ipc_token,
                "window_id": contents_id,
                "profile_name": self.profile_name,
                "web_request_enabled": message.get("webRequestEnabled") is True,
                "background_throttling": self.background_throttling
            })
        self.ipc = tg.ElectronBridge(
            self.webview,
            transport=lambda request: self.host.ipc_request(request, self.contents_id),
            on_ready=lambda: controller._bridge_ready(self.contents_id))
        self.ipc.handle("__nexa:renderer-error", _ElectronWindowView._log_renderer_error)
        self.apply_state(message)
        self.webview.setdesktopmode(True)
        self._user_agent_ready = True
        if isinstance(self.user_agent, str):
            self.webview.setuseragent(self.user_agent)
        self.webview.allownavigation(True)
        self.webview.loaduri(self.url, _load_headers(self.load_options))

    def _preload(self):
        script = ELECTRON_PRELOAD
        if self.host.main_entry:
            cwd = (self.host.app_root.parent if self.host.app_root.is_file()
                   else self.host.app_root)
            home = os.environ.get("HOME", str(Path.home()))
            script = ("window.__NEXA_ELECTRON_SYNC_ENDPOINT__ = " +
                      json.dumps(self.host.sync_ipc_url) + ";\n" +
                      "window.__NEXA_ELECTRON_IPC_TOKEN__ = " +
                      json.dumps(self.host.ipc_token) + ";\n" +
                      "window.__NEXA_ELECTRON_WINDOW_ID__ = " +
                      json.dumps(self.contents_id) + ";\n" +
                      "window.__NEXA_ELECTRON_CWD__ = " +
                      json.dumps(str(cwd)) + ";\n" +
                      "window.__NEXA_ELECTRON_HOME__ = " +
                      json.dumps(home) + ";\n" + script)
        return script

    def apply_state(self, state):
        user_agent = state.get("userAgent")
        if isinstance(user_agent, str) and user_agent != self.user_agent:
            self.user_agent = user_agent
            if getattr(self, "_user_agent_ready", False):
                self.webview.setuseragent(user_agent)
        bounds = state.get("bounds")
        if isinstance(bounds, dict):
            self.bounds.update({key: value for key, value in bounds.items()
                                if key in self.bounds and isinstance(value, (int, float))})
            x = int(self.bounds["x"])
            y = int(self.bounds["y"])
            width = max(1, int(self.bounds["width"]))
            height = max(1, int(self.bounds["height"]))
            self.webview.setdimensions(width, height)
            for direction, margin in (("left", x), ("top", y), ("right", 0), ("bottom", 0)):
                self.webview.setmargin(margin, direction)
        if "visible" in state:
            self.webview.setvisibility(
                tg.View.VISIBLE if state["visible"] else tg.View.GONE)
        if "backgroundColor" in state:
            color = _android_color(state["backgroundColor"])
            if color is not None:
                self.webview.setbackgroundcolor(color)
        throttling = state.get("backgroundThrottling")
        if isinstance(throttling, bool):
            self.background_throttling = throttling
            self.webview.setbackgroundthrottling(throttling)

    def load(self, url, load_options=None):
        self.url = url
        if load_options is not None:
            self.load_options = load_options
        self.webview.loaduri(url, _load_headers(self.load_options))

    def handle_event(self, event):
        if self._closed or event.id != self.webview.id:
            return False
        if event.type == tg.Event.webviewBridgeError:
            logger.error("embedded WebView bridge failed: %s",
                         event.value.get("error", "unknown error"))
            return True
        if event.type == tg.Event.webviewConsoleMessage:
            _ElectronWindowView._log_console_error(self, event.value)
            return True
        if event.type in (tg.Event.webviewNavigation, tg.Event.webviewInPageNavigation,
                          tg.Event.webviewLoadStarted,
                          tg.Event.webviewCommitVisible, tg.Event.webviewLoadFinished,
                          tg.Event.webviewProgress,
                          tg.Event.webviewHTTPError, tg.Event.webviewError,
                          tg.Event.webviewRequestError,
                          tg.Event.webviewDestroyed, tg.Event.webviewTitleChanged):
            if event.type == tg.Event.webviewProgress and event.value.get("progress") == 100:
                logger.info("embedded contents %s finished loading %s",
                            self.contents_id, self.url)
            elif event.type in (tg.Event.webviewHTTPError, tg.Event.webviewError,
                                tg.Event.webviewDestroyed):
                logger.error("embedded contents %s WebView %s: %s",
                             self.contents_id, event.type, event.value)
            self.host.renderer_event(event, self.contents_id)
            return True
        return self.ipc.handle_event(event)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.ipc.close()
        self.webview.delete()


class ElectronRenderer:
    def __init__(self, activity, parent, app_root, entry="index.html", node=None,
                 main_entry=None, app_id="electron", app_name="electron", icon="window",
                 window_host=None, close_window=None, host_environment=None,
                 node_runner=None):
        self.app_root = app_root
        self.entry = entry
        self.node = node
        self.node_runner = node_runner
        self.main_entry = main_entry
        self.activity = activity
        self.parent = parent
        runtime_environment = dict(host_environment or {})
        user_agent = activity.c.send_read_msg({
            "method": "getDefaultWebViewUserAgent",
            "params": {"desktopMode": True}
        })
        if not isinstance(user_agent, str) or not user_agent:
            raise RuntimeError("Android WebView did not provide a default user agent")
        runtime_environment.setdefault("NEXA_ELECTRON_USER_AGENT", user_agent)
        self.host_environment = runtime_environment
        screen_manager = getattr(window_host, "windows", None)
        self.screen_size = (
            (screen_manager.screen_width, screen_manager.screen_height)
            if screen_manager is not None else (800, 600))
        self.scale_factor = getattr(screen_manager, "density", 1.0)
        self._screen_metrics = (*self.screen_size, self.scale_factor)
        self.host = ElectronHost(
            self.app_root, self.entry, self.node, self.main_entry,
            self.screen_size, self.scale_factor, runtime_environment,
            self.node_runner)
        self.url = self.host.start()
        self.app_id = app_id
        self.app_name = app_name
        self.icon = icon
        self.window_host = window_host
        self.close_window = close_window
        self._views = {}
        self._embedded_views = {}
        self._embedded_view_ids = {}
        self._devtools_windows = {}
        self._pending_view_events = {}
        self._host_windows = {}
        self._bridge_ready_windows = set()
        self._pending_renderer_messages = {}
        self._closed_windows = set()
        self._remote_closing = set()
        self._root_hidden = not self.host.renderer_visible
        self._root_visibility_managed = False
        self._restart_requested = False
        self._closed = False
        self._tick_lock = Lock()
        self._renderer_recoveries = {}
        self._web_request_profiles = set()
        if self.host.renderer_web_request_enabled and self.host.renderer_profile_name:
            self._web_request_profiles.add(self.host.renderer_profile_name)
        self._temporary_profile_names = set()
        if self.host.renderer_profile_temporary and self.host.renderer_profile_name:
            self._temporary_profile_names.add(self.host.renderer_profile_name)
        try:
            self.window_id = self.host.window_id
            self.root_view = _ElectronWindowView(
                self, activity, parent, self.window_id, self.url,
                self.host.preload_url, self.host.renderer_visible,
                background_throttling=_background_throttling(self.host.renderer_options),
                web_request_enabled=self.host.renderer_web_request_enabled,
                user_agent=self.host.renderer_user_agent,
                load_options=self.host.renderer_load_options)
            self._views[self.window_id] = self.root_view
            self._attach_pending_views(self.window_id)
            self._flush_pending_renderer_messages(self.window_id)
            if not self.host.renderer_visible:
                self.root_view.set_visibility(tg.View.GONE)
        except Exception:
            self.host.close()
            raise

    def handle_event(self, event):
        embedded = self._embedded_view_ids.get(event.id)
        if embedded is not None:
            return embedded.handle_event(event)
        return self.root_view.handle_event(event)

    def handle_hidden_webview_destroyed(self, event):
        if event.type != tg.Event.webviewDestroyed:
            return False
        return self.handle_event(event)

    def on_window_visibility_changed(self, visible):
        self.root_view.on_window_visibility_changed(visible)

    def on_window_resize(self, width, height):
        view = self.root_view
        width = max(1, int(width))
        height = max(1, int(height))
        self._show_hidden_root(view, "window received its initial size")
        logger.info("resizing Electron root window %s content to %sx%s",
                    self.window_id, width, height)
        view.webview.setdimensions(width, height)
        if view.visible and not view._renderer_dead:
            view.webview.setvisibility(tg.View.VISIBLE)
            view._applied_visible = True

    def on_screen_resize(self, width, height, scale_factor):
        metrics = (max(1, int(width)), max(1, int(height)), max(0.1, float(scale_factor)))
        if metrics == self._screen_metrics:
            return
        self._screen_metrics = metrics
        self.screen_size = metrics[:2]
        self.scale_factor = metrics[2]
        self.host.update_screen_metrics(*metrics)

    def on_window_geometry(self, x, y, width, height, maximized):
        self.host.update_window_geometry(
            self.window_id, x, y, width, height, maximized)

    def _bridge_ready(self, window_id):
        logger.info("Electron WebView bridge ready for contents %s", window_id)
        self._bridge_ready_windows.add(window_id)
        self._flush_pending_renderer_messages(window_id)

    def _create_embedded_view(self, message):
        contents_id = message.get("contentsId")
        window_id = message.get("windowId")
        if (type(contents_id) is not int or type(window_id) is not int
                or contents_id == window_id or window_id in self._closed_windows):
            return
        current = self._embedded_views.get(contents_id)
        if current is not None:
            load_options = message.get("loadOptions") or {"headers": {}}
            if (message.get("url") != current.url
                    or load_options != current.load_options):
                current.load(message["url"], load_options)
            current.apply_state(message)
            return
        if message.get("profileTemporary") is True and message.get("profileName"):
            self._temporary_profile_names.add(message["profileName"])
        if message.get("webRequestEnabled") is True and message.get("profileName"):
            self._web_request_profiles.add(message["profileName"])
        if window_id not in self._views:
            self._pending_view_events.setdefault(window_id, []).append(("create", message))
            return
        try:
            view = _ElectronEmbeddedView(
                self, contents_id, window_id, message, self._views[window_id].parent)
        except Exception:
            logger.exception("could not create embedded Electron contents %s", contents_id)
            return
        self._embedded_views[contents_id] = view
        self._embedded_view_ids[view.webview.id] = view
        logger.info("created embedded Electron contents %s for window %s at %s",
                    contents_id, window_id, view.url)
        self._flush_pending_renderer_messages(contents_id)

    def _attach_pending_views(self, window_id):
        for event, message in self._pending_view_events.pop(window_id, ()):
            if event == "create":
                self._create_embedded_view(message)
                continue
            contents_id = message.get("contentsId")
            if contents_id == window_id:
                view = self._views.get(window_id)
                if view is not None:
                    if "bounds" in message:
                        view.set_content_bounds(message["bounds"])
                    if "visible" in message:
                        view.set_visibility(
                            tg.View.VISIBLE if message["visible"] else tg.View.GONE)
                    if isinstance(message.get("backgroundThrottling"), bool):
                        view.set_background_throttling(message["backgroundThrottling"])
                    if "backgroundColor" in message:
                        color = _android_color(message["backgroundColor"])
                        if color is not None:
                            view.webview.setbackgroundcolor(color)
            else:
                view = self._embedded_views.get(contents_id)
                if view is not None:
                    view.apply_state(message)

    def _close_embedded_view(self, contents_id):
        self._close_devtools(contents_id)
        view = self._embedded_views.pop(contents_id, None)
        self._bridge_ready_windows.discard(contents_id)
        self._pending_renderer_messages.pop(contents_id, None)
        for window_id, events in tuple(self._pending_view_events.items()):
            remaining = [(kind, message) for kind, message in events
                         if message.get("contentsId") != contents_id]
            if remaining:
                self._pending_view_events[window_id] = remaining
            else:
                self._pending_view_events.pop(window_id, None)
        if view is None:
            return
        self._embedded_view_ids.pop(view.webview.id, None)
        view.close()

    def _close_devtools(self, contents_id):
        window = self._devtools_windows.pop(contents_id, None)
        view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
        if view is not None:
            try:
                view.webview.close_devtools()
            except Exception:
                logger.exception("could not release WebView DevTools for %s", contents_id)
        self.host.update_devtools_state(contents_id, False)
        if window is not None and not window._closed:
            window.close()

    def _close_embedded_views_for_window(self, window_id):
        for contents_id, view in tuple(self._embedded_views.items()):
            if view.window_id == window_id:
                self._close_embedded_view(contents_id)

    def _renderer_destroyed(self, old_view):
        window_id = old_view.window_id
        if (self._closed or self._views.get(window_id) is not old_view
                or window_id in self._closed_windows):
            return
        self._close_devtools(window_id)
        old_view._renderer_dead = True
        old_view._renderer_killed_hidden = not old_view.visible
        old_view.close(renderer_destroyed=True)
        if old_view.visible:
            self._restore_renderer(old_view)
        else:
            logger.info("WebView renderer for hidden window %s was reclaimed; deferring reload",
                        window_id)

    def _restore_renderer(self, old_view):
        window_id = old_view.window_id
        if (self._closed or self._views.get(window_id) is not old_view
                or window_id in self._closed_windows or not old_view._renderer_dead
                or not old_view.visible):
            return
        now = monotonic()
        last_recovery = self._renderer_recoveries.get(window_id)
        if (not old_view._renderer_killed_hidden and last_recovery is not None
                and now - last_recovery < 60):
            print(f"nexa: electron renderer {window_id} crashed again during recovery")
            return
        old_view._renderer_killed_hidden = False
        self._renderer_recoveries[window_id] = now
        self._bridge_ready_windows.discard(window_id)
        replacement = _ElectronWindowView(
            self, old_view.activity, old_view.parent, window_id,
            old_view.url, old_view.preload_url,
            old_view.window_visible, old_view.container_visible,
            old_view.background_throttling,
            profile_name=old_view.profile_name,
            web_request_enabled=old_view.profile_name in self._web_request_profiles,
            user_agent=old_view.user_agent, load_options=old_view.load_options)
        self._views[window_id] = replacement
        if window_id == self.window_id:
            self.root_view = replacement

    def _flush_pending_renderer_messages(self, window_id):
        view = self._embedded_views.get(window_id) or self._views.get(window_id)
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
        if message.get("profileTemporary") is True and message.get("profileName"):
            self._temporary_profile_names.add(message["profileName"])
        if message.get("webRequestEnabled") is True and message.get("profileName"):
            self._web_request_profiles.add(message["profileName"])

        def build(activity, parent, _state):
            options = message.get("options") or {}
            view = _ElectronWindowView(
                self, activity, parent, window_id, url, message.get("preloadUrl"),
                message.get("visible", True),
                background_throttling=_background_throttling(options),
                profile_name=message.get("profileName"),
                web_request_enabled=message.get("webRequestEnabled") is True,
                user_agent=message.get("userAgent"),
                load_options=message.get("loadOptions"))
            self._views[window_id] = view
            self._attach_pending_views(window_id)
            self._flush_pending_renderer_messages(window_id)
            return view

        def on_close():
            self._closed_windows.add(window_id)
            self._close_embedded_views_for_window(window_id)
            if window_id not in self._remote_closing:
                Thread(target=self.host.window_command,
                       args=(window_id, "close"), daemon=True).start()
            self._host_windows.pop(window_id, None)
            self._views.pop(window_id, None)
            self._pending_view_events.pop(window_id, None)
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
        if options.get("resizable") is False:
            host_window.set_resizable(False)
        if not message.get("visible", True):
            self.window_host.windows.minimize(host_window.window_id)

    def _promote_hidden_root(self, message):
        old_window_id = self.window_id
        self._close_devtools(old_window_id)
        old_view = self.root_view
        self._close_embedded_views_for_window(old_window_id)
        self._views.pop(old_window_id, None)
        self._pending_view_events.pop(old_window_id, None)
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
        self.host.renderer_user_agent = message.get("userAgent")
        self.host.renderer_load_options = message.get("loadOptions") or {"headers": {}}
        self.host.renderer_visible = message.get("visible", True)
        self.host.renderer_profile_name = message.get("profileName")
        self.host.renderer_profile_temporary = message.get("profileTemporary") is True
        self.host.renderer_web_request_enabled = message.get("webRequestEnabled") is True
        if self.host.renderer_web_request_enabled and self.host.renderer_profile_name:
            self._web_request_profiles.add(self.host.renderer_profile_name)
        if self.host.renderer_profile_temporary and self.host.renderer_profile_name:
            self._temporary_profile_names.add(self.host.renderer_profile_name)
        logger.info("promoting Electron window %s to the desktop: %s",
                    self.window_id, self.url)
        self.root_view = _ElectronWindowView(
            self, self.activity, self.parent, self.window_id, self.url,
            message.get("preloadUrl"), message.get("visible", True),
            background_throttling=_background_throttling(self.host.renderer_options),
            profile_name=self.host.renderer_profile_name,
            web_request_enabled=self.host.renderer_web_request_enabled,
            user_agent=self.host.renderer_user_agent,
            load_options=self.host.renderer_load_options)
        self._views[self.window_id] = self.root_view
        self._attach_pending_views(self.window_id)
        if not message.get("visible", True):
            self.root_view.set_visibility(tg.View.GONE)
        self._flush_pending_renderer_messages(self.window_id)

    def _close_child_window(self, window_id):
        self._closed_windows.add(window_id)
        self._close_devtools(window_id)
        self._close_embedded_views_for_window(window_id)
        self._pending_view_events.pop(window_id, None)
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
        for contents_id in tuple(self._devtools_windows):
            self._close_devtools(contents_id)
        for window_id in tuple(self._host_windows):
            self._close_child_window(window_id)
        self.root_view.close()
        for contents_id in tuple(self._embedded_views):
            self._close_embedded_view(contents_id)
        self._delete_temporary_profiles()
        self._web_request_profiles.clear()
        self.host.close()
        self._views.clear()
        self._pending_view_events.clear()
        self._bridge_ready_windows.clear()
        self._pending_renderer_messages.clear()
        self._closed_windows.clear()
        self._host_windows.clear()
        self._remote_closing.clear()
        self._renderer_recoveries.clear()
        try:
            self.host = ElectronHost(
                self.app_root, self.entry, self.node, self.main_entry,
                self.screen_size, self.scale_factor, self.host_environment,
                self.node_runner)
            self.url = self.host.start()
            self.window_id = self.host.window_id
            if self.host.renderer_web_request_enabled and self.host.renderer_profile_name:
                self._web_request_profiles.add(self.host.renderer_profile_name)
            if self.host.renderer_profile_temporary and self.host.renderer_profile_name:
                self._temporary_profile_names.add(self.host.renderer_profile_name)
            self._root_hidden = not self.host.renderer_visible
            self.root_view = _ElectronWindowView(
                self, self.activity, self.parent, self.window_id, self.url,
                self.host.preload_url, self.host.renderer_visible,
                background_throttling=_background_throttling(self.host.renderer_options),
                web_request_enabled=self.host.renderer_web_request_enabled,
                user_agent=self.host.renderer_user_agent,
                load_options=self.host.renderer_load_options)
            self._views[self.window_id] = self.root_view
            self._attach_pending_views(self.window_id)
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
                self._root_visibility_managed = True
                self._root_hidden = True
                logger.info("hiding Electron root window %s", self.window_id)
                self.root_view.set_visibility(tg.View.GONE)
            elif message.get("action") in ("show", "restore"):
                self._root_visibility_managed = True
                logger.info("showing Electron root window %s", self.window_id)
                self.root_view.set_visibility(tg.View.VISIBLE)
                self._root_hidden = False
            elif message.get("action") == "backgroundThrottling":
                self.root_view.set_background_throttling(
                    (message.get("value") or {}).get("enabled", True))
            return
        host_window = self._host_windows.get(message.get("windowId"))
        if host_window is None:
            return
        action = message.get("action")
        value = message.get("value") or {}
        if action == "backgroundThrottling":
            view = self._views.get(message.get("windowId"))
            if view is not None:
                view.set_background_throttling(value.get("enabled", True))
        elif action == "title":
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
        elif action == "resizable":
            host_window.set_resizable(value.get("enabled", True))
        elif action in ("maximize", "unmaximize"):
            window = self.window_host.windows.windows.get(host_window.window_id)
            if window is not None and window["maximized"] != (action == "maximize"):
                self.window_host.windows.toggle_maximize(host_window.window_id)

    def _root_renderer_loaded(self, view):
        self._show_hidden_root(view, "renderer finished loading")

    def _show_hidden_root(self, view, reason):
        if (view is not self.root_view or not self._root_hidden
                or self._root_visibility_managed or self._host_windows):
            return
        # Some Linux Electron apps wait for an updater splash that isn't created here.
        logger.info("showing hidden Electron root window %s: %s",
                    self.window_id, reason)
        self._root_hidden = False
        self._root_visibility_managed = True
        self.root_view.set_visibility(tg.View.VISIBLE)

    def _handle_native_call(self, message):
        method = message.get("method")
        args = message.get("args") or []
        if method == "webContents.setUserAgent":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed and args:
                view.webview.setuseragent(args[0])
                view.user_agent = args[0]
            return
        if method == "webContents.focus":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed:
                view.webview.focus()
            return
        if method in ("webContents.openDevTools", "webContents.closeDevTools"):
            contents_id = message.get("contentsId")
            if type(contents_id) is not int:
                raise ValueError("Electron DevTools needs a numeric WebContents id")
            if method == "webContents.closeDevTools":
                self._close_devtools(contents_id)
                return True
            options = args[0] if args and isinstance(args[0], dict) else {}
            mode = options.get("mode")
            if mode not in (None, "", "detach"):
                raise RuntimeError("Nexa currently supports detached Electron DevTools only")
            if options.get("activate") is False:
                raise RuntimeError("Nexa cannot open Electron DevTools without activating it")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is None or view._closed:
                raise RuntimeError("Electron WebContents has no live Android WebView")
            existing = self._devtools_windows.get(contents_id)
            if existing is not None and not existing._closed:
                existing.focus()
                return True
            if self.window_host is None:
                raise RuntimeError("Nexa window host is unavailable for DevTools")
            frontend_url = view.webview.get_devtools_url()

            def build(activity, parent, _state):
                devtools = tg.WebView(activity, parent, enable_javascript=True)
                devtools.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
                devtools.setdesktopmode(True)
                devtools.allownavigation(True)
                devtools.loaduri(frontend_url)
                return devtools

            window = None

            def on_close():
                if self._devtools_windows.pop(contents_id, None) is window:
                    try:
                        view.webview.close_devtools()
                    except Exception:
                        logger.exception(
                            "could not release WebView DevTools for %s", contents_id)
                    self.host.update_devtools_state(contents_id, False)

            try:
                window = self.window_host.open_window(
                    self.app_id, str(options.get("title") or "Developer Tools"),
                    build, icon=self.icon,
                    on_close=on_close, size=(800, 600))
            except Exception:
                view.webview.close_devtools()
                raise
            self._devtools_windows[contents_id] = window
            return True
        if method == "session.setWebRequestEnabled":
            profile_name = message.get("profileName")
            enabled = message.get("enabled")
            if (not isinstance(profile_name, str) or not re.fullmatch(
                    r"nexa_[a-f0-9]{32}(?:_[a-f0-9]{16})?", profile_name)
                    or not isinstance(enabled, bool)):
                raise ValueError("Electron webRequest state is invalid")
            if enabled:
                self._web_request_profiles.add(profile_name)
            else:
                self._web_request_profiles.discard(profile_name)
            self.activity.c.send_msg({
                "method": "setElectronWebRequestEnabled",
                "params": {"aid": self.activity.aid, "profileName": profile_name,
                           "enabled": enabled}})
            return True
        if method == "webContents.clearCache":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is None or view._closed:
                raise RuntimeError("Electron WebContents has no live Android WebView")
            return view.webview.clearcache()
        if method == "session.clearStorageData":
            profile_name = message.get("profileName")
            if not isinstance(profile_name, str) or not re.fullmatch(
                    r"nexa_[a-f0-9]{32}(?:_[a-f0-9]{16})?", profile_name):
                raise ValueError("Electron session has no Nexa WebView profile")
            return self.activity.c.send_read_msg_async({
                "method": "clearElectronStorage",
                "params": {"aid": self.activity.aid, "profileName": profile_name}})
        if method == "session.cookies":
            profile_name = message.get("profileName")
            if not isinstance(profile_name, str) or not re.fullmatch(
                    r"nexa_[a-f0-9]{32}(?:_[a-f0-9]{16})?", profile_name):
                raise ValueError("Electron session has no Nexa WebView profile")
            params = {"aid": self.activity.aid, "profileName": profile_name,
                      "action": message.get("action")}
            for key in ("url", "name", "details"):
                if key in message:
                    params[key] = message[key]
            return self.activity.c.send_read_msg_async({
                "method": "electronCookies", "params": params})
        if method in ("webContents.reload", "webContents.reloadIgnoringCache"):
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed:
                if method == "webContents.reloadIgnoringCache":
                    view.webview.reloadignoringcache()
                else:
                    view.webview.reload()
            return
        if method == "webContents.stop":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed:
                view.webview.stoploading()
            return
        if method in ("webContents.goBack", "webContents.goForward"):
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed:
                if method == "webContents.goBack":
                    view.webview.goback()
                else:
                    view.webview.goforward()
            return
        if method == "webContents.goToOffset":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            args = message.get("args") or []
            if view is not None and not view._closed and args:
                view.webview.gobackorforward(args[0])
            return
        if method == "webContents.clearHistory":
            contents_id = message.get("contentsId")
            view = self._views.get(contents_id) or self._embedded_views.get(contents_id)
            if view is not None and not view._closed:
                view.webview.clearhistory()
            return
        if method == "webFrame.clearCache":
            view = self._views.get(message.get("windowId"))
            if view is not None and not view._closed:
                view.webview.clearcache()
            return
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
                    contents_id = message.get("contentsId", window_id)
                    if window_id in self._closed_windows:
                        continue
                    view = (self._embedded_views.get(contents_id)
                            or self._views.get(contents_id))
                    if view is not None and contents_id in self._bridge_ready_windows:
                        view.ipc.emit(message["channel"], *message.get("args", []))
                    else:
                        self._pending_renderer_messages.setdefault(contents_id, []).append(message)
                elif event == "rendererView":
                    self._create_embedded_view(message)
                elif event == "rendererViewState":
                    contents_id = message.get("contentsId")
                    if window_id not in self._views:
                        self._pending_view_events.setdefault(window_id, []).append(
                            ("state", message))
                    elif contents_id == window_id:
                        view = self._views.get(window_id)
                        if view is not None:
                            if "bounds" in message:
                                view.set_content_bounds(message["bounds"])
                            if "visible" in message:
                                view.set_visibility(
                                    tg.View.VISIBLE if message["visible"] else tg.View.GONE)
                            if isinstance(message.get("backgroundThrottling"), bool):
                                view.set_background_throttling(
                                    message["backgroundThrottling"])
                            if "backgroundColor" in message:
                                color = _android_color(message["backgroundColor"])
                                if color is not None:
                                    view.webview.setbackgroundcolor(color)
                    else:
                        view = self._embedded_views.get(contents_id)
                        if view is not None:
                            view.apply_state(message)
                elif event == "rendererViewClosed":
                    self._close_embedded_view(message.get("contentsId"))
                elif event == "renderer":
                    if window_id in self._closed_windows:
                        continue
                    if window_id == self.window_id:
                        load_options = message.get("loadOptions") or {"headers": {}}
                        if (message.get("url") != self.url
                                or load_options != self.root_view.load_options):
                            self.url = message["url"]
                            self.root_view.load(self.url, load_options)
                    elif window_id in self._host_windows:
                        view = self._views.get(window_id)
                        load_options = message.get("loadOptions") or {"headers": {}}
                        if (view is not None and (message.get("url") != view.url
                                or load_options != view.load_options)):
                            view.load(message["url"], load_options)
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
                    self._close_embedded_views_for_window(window_id)
                    self._close_child_window(window_id)
                elif event == "mainError":
                    print(f"nexa: electron main process: {message.get('error', 'failed')}")
                elif event == "nativeCall":
                    request_id = message.get("requestId")
                    try:
                        result = self._handle_native_call(message)
                    except Exception as error:
                        if type(request_id) is int:
                            self.host.native_call_result(request_id, error=error)
                        else:
                            logger.exception("Electron native call failed: %s",
                                             message.get("method"))
                    else:
                        if type(request_id) is int:
                            if isinstance(result, Future):
                                def reply(future, request_id=request_id):
                                    try:
                                        value = future.result()
                                        if value is False:
                                            raise RuntimeError(
                                                "Android WebView rejected the Electron operation")
                                    except Exception as error:
                                        self.host.native_call_result(request_id, error=error)
                                    else:
                                        self.host.native_call_result(request_id, value=value)
                                result.add_done_callback(reply)
                            else:
                                self.host.native_call_result(request_id, value=result)
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

    def _delete_temporary_profiles(self):
        for profile_name in self._temporary_profile_names:
            try:
                result = self.activity.c.send_read_msg_async({
                    "method": "deleteElectronProfile",
                    "params": {"aid": self.activity.aid, "profileName": profile_name}})
            except Exception:
                logger.exception("could not request cleanup for temporary Electron profile %s",
                                 profile_name)
                continue

            def report_cleanup(future, name=profile_name):
                try:
                    if future.result() is not True:
                        logger.warning("temporary Electron profile cleanup failed: %s", name)
                except Exception:
                    logger.exception("temporary Electron profile cleanup failed: %s", name)

            result.add_done_callback(report_cleanup)
        self._temporary_profile_names.clear()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for contents_id in tuple(self._devtools_windows):
            self._close_devtools(contents_id)
        for window_id in tuple(self._host_windows):
            self._close_child_window(window_id)
        for contents_id in tuple(self._embedded_views):
            self._close_embedded_view(contents_id)
        try:
            self.root_view.close()
        finally:
            self._delete_temporary_profiles()
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


def _android_color(value):
    if not isinstance(value, str):
        return None
    color = value.strip().lower()
    if color == "transparent":
        return 0
    if not color.startswith("#"):
        return None
    digits = color[1:]
    try:
        if len(digits) == 3:
            red, green, blue = (int(digit * 2, 16) for digit in digits)
            return 0xFF000000 | red << 16 | green << 8 | blue
        if len(digits) == 4:
            alpha, red, green, blue = (int(digit * 2, 16) for digit in digits)
            return alpha << 24 | red << 16 | green << 8 | blue
        if len(digits) == 6:
            return 0xFF000000 | int(digits, 16)
        if len(digits) == 8:
            red, green, blue, alpha = (int(digits[index:index + 2], 16)
                                       for index in range(0, 8, 2))
            return alpha << 24 | red << 16 | green << 8 | blue
    except ValueError:
        return None
    return None
