import ctypes
from collections import OrderedDict, deque
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from threading import Condition, Event, Lock, Thread
import time

import nexapip as tg


class _NativeEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("surface", ctypes.c_uint64),
        ("parent", ctypes.c_uint64),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("stride", ctypes.c_int),
        ("format", ctypes.c_int),
        ("app_id", ctypes.c_char * 256),
        ("title", ctypes.c_char * 512),
        ("pixels", ctypes.c_void_p),
        ("next", ctypes.c_void_p),
    ]


def _decode(value):
    return value.split(b"\0", 1)[0].decode("utf-8", "replace")


def _linux_keycode(android_code):
    if 7 <= android_code <= 16:
        return android_code - 5
    return {
        19: 103, 20: 108, 21: 105, 22: 106, 55: 51, 56: 52,
        57: 56, 58: 100, 59: 42, 60: 54, 61: 15, 62: 57,
        66: 28, 67: 14, 68: 41, 69: 12, 70: 13, 71: 26, 72: 27,
        73: 43, 74: 39, 75: 40, 76: 53, 92: 104, 93: 109,
        111: 1, 112: 111, 113: 29, 114: 97, 115: 58, 117: 125,
        118: 126, 122: 102, 123: 107, 124: 110, 143: 69,
        29: 30, 30: 48, 31: 46, 32: 32, 33: 18, 34: 33, 35: 34,
        36: 35, 37: 23, 38: 36, 39: 37, 40: 38, 41: 50, 42: 49,
        43: 24, 44: 25, 45: 16, 46: 19, 47: 31, 48: 20, 49: 22,
        50: 47, 51: 17, 52: 45, 53: 21, 54: 44,
    }.get(android_code)


_TEXT_KEYS = {
    **dict(zip("abcdefghijklmnopqrstuvwxyz", (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
                                             50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45,
                                             21, 44))),
    **dict(zip("1234567890", range(2, 12))),
    " ": 57, "\n": 28, "\r": 28, "\t": 15, "-": 12, "=": 13,
    "[": 26, "]": 27, ";": 39, "'": 40, "`": 41, "\\": 43,
    ",": 51, ".": 52, "/": 53, "\x7f": 14, "\x08": 14, "\x1b": 1,
}
_SHIFTED_TEXT_KEYS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+{}:\"~|<>?",
                              ((code, True) for code in (
                                  30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50,
                                  49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44,
                                  2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 26, 27,
                                  39, 40, 41, 43, 51, 52, 53))))
_TEXT_SEQUENCES = {
    "\x1b[A": 103, "\x1b[B": 108, "\x1b[C": 106, "\x1b[D": 105,
    "\x1b[H": 102, "\x1b[F": 107, "\x1b[3~": 111,
    "\x1b[5~": 104, "\x1b[6~": 109,
}


def _build_library():
    root = Path(__file__).resolve().parents[2]
    source = root / "shell" / "wayland" / "native_host.c"
    cache = Path.home() / ".cache" / "nexa" / "wayland"
    cache.mkdir(parents=True, exist_ok=True)
    library = cache / "libnexa-wayland.so"
    protocols = subprocess.run(
        ["pkg-config", "--variable=pkgdatadir", "wayland-protocols"],
        capture_output=True, text=True)
    wayland_server = subprocess.run(
        ["pkg-config", "--exists", "wayland-server"],
        capture_output=True, text=True)
    scanner = shutil.which("wayland-scanner")
    compiler = shutil.which("clang")
    if (scanner is None or compiler is None or protocols.returncode
            or not protocols.stdout.strip() or wayland_server.returncode):
        subprocess.run(["pkg", "install", "-y", "clang", "pkg-config", "libwayland",
                        "libwayland-protocols"], check=True)
        scanner = shutil.which("wayland-scanner")
        compiler = shutil.which("clang")
        protocols = subprocess.run(
            ["pkg-config", "--variable=pkgdatadir", "wayland-protocols"],
            check=True, capture_output=True, text=True)
        wayland_server = subprocess.run(
            ["pkg-config", "--exists", "wayland-server"],
            capture_output=True, text=True)
    if (scanner is None or compiler is None or not protocols.stdout.strip()
            or wayland_server.returncode):
        raise RuntimeError("install clang, libwayland, and libwayland-protocols to host Wayland apps")
    protocols = protocols.stdout.strip()
    xml = Path(protocols) / "stable" / "xdg-shell" / "xdg-shell.xml"
    decoration_xml = (Path(protocols) / "unstable" / "xdg-decoration"
                      / "xdg-decoration-unstable-v1.xml")
    text_input_xml = (Path(protocols) / "unstable" / "text-input"
                      / "text-input-unstable-v3.xml")
    inputs = [source, xml, decoration_xml, text_input_xml]
    if library.exists() and library.stat().st_mtime >= max(path.stat().st_mtime for path in inputs):
        return library

    header = cache / "xdg-shell-server-protocol.h"
    protocol = cache / "xdg-shell-protocol.c"
    decoration_header = cache / "xdg-decoration-server-protocol.h"
    decoration_protocol = cache / "xdg-decoration-protocol.c"
    text_input_header = cache / "text-input-v3-server-protocol.h"
    text_input_protocol = cache / "text-input-v3-protocol.c"
    subprocess.run([scanner, "server-header", str(xml), str(header)], check=True)
    subprocess.run([scanner, "private-code", str(xml), str(protocol)], check=True)
    subprocess.run([scanner, "server-header", str(decoration_xml), str(decoration_header)], check=True)
    subprocess.run([scanner, "private-code", str(decoration_xml), str(decoration_protocol)], check=True)
    subprocess.run([scanner, "server-header", str(text_input_xml), str(text_input_header)], check=True)
    subprocess.run([scanner, "private-code", str(text_input_xml), str(text_input_protocol)], check=True)
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "wayland-server"],
        check=True, capture_output=True, text=True).stdout.split()
    subprocess.run([
        compiler, "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
        str(source), str(protocol), str(decoration_protocol), str(text_input_protocol),
        "-I" + str(cache),
        *flags, "-lpthread",
        "-o", str(library)], check=True)
    return library


class _SurfaceHandler:
    def __init__(self, host, surface_id, state):
        self.host = host
        self.surface_id = surface_id
        self.state = state

    def handle_event(self, event):
        if self.state["view"] is None or getattr(event, "id", None) != self.state["view"].id:
            return False
        if event.type == tg.Event.touch:
            callback = self.host.on_touch
            if callback is not None:
                callback(self.surface_id, event.value)
            return True
        if event.type == tg.Event.key:
            callback = self.host.on_key
            if callback is not None:
                callback(self.surface_id, event.value)
            return True
        return False

    def on_window_resize(self, width, height):
        callback = self.host.on_resize
        if callback is not None:
            callback(self.surface_id, width, height)

    def close(self):
        self.host._close_surface(self.surface_id)


class WaylandHost:
    TOPLEVEL = 1
    TITLE = 2
    FRAME = 3
    UNMAP = 4
    DESTROY = 5
    POPUP = 6
    TEXT_INPUT = 7

    @staticmethod
    def needs_x86_runtime(argv):
        if not argv:
            return False
        executable = shutil.which(os.fspath(argv[0]))
        if executable is None and os.path.isfile(argv[0]):
            executable = os.fspath(argv[0])
        if executable is None:
            return Path(os.fspath(argv[0])).name == "box64"
        try:
            with open(executable, "rb") as binary:
                header = binary.read(20)
            return (len(header) >= 20 and header[:4] == b"\x7fELF"
                    and header[5] == 1
                    and int.from_bytes(header[18:20], "little") == 62)
        except OSError:
            return False

    def __init__(self, window_host, runtime_dir=None, socket_name="nexa-0",
                 width=1280, height=800):
        self.window_host = window_host
        self.density = max(0.1, float(window_host.windows.density))
        self.runtime_dir = Path(runtime_dir or Path.home() / ".cache" / "nexa" / "runtime")
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_dir, 0o700)
        self.socket_name = socket_name
        (self.runtime_dir / socket_name).unlink(missing_ok=True)
        self.environment = os.environ.copy()
        self.environment.update({
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
            "WAYLAND_DISPLAY": socket_name,
        })
        xkb_root = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
        xkb_root = xkb_root / "glibc" / "share" / "X11" / "xkb"
        if xkb_root.is_dir():
            self.environment["XKB_CONFIG_ROOT"] = str(xkb_root)
        self._library = ctypes.CDLL(str(_build_library()))
        self._library.nexa_wl_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                                 ctypes.c_int, ctypes.c_int]
        self._library.nexa_wl_create.restype = ctypes.c_void_p
        self._library.nexa_wl_dispatch.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._library.nexa_wl_dispatch.restype = ctypes.c_int
        self._library.nexa_wl_next_event.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_NativeEvent))]
        self._library.nexa_wl_next_event.restype = ctypes.c_int
        self._library.nexa_wl_free_event.argtypes = [ctypes.POINTER(_NativeEvent)]
        self._library.nexa_wl_close_toplevel.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._library.nexa_wl_focus.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._library.nexa_wl_configure_toplevel.argtypes = [ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_int, ctypes.c_int]
        self._library.nexa_wl_update_output.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self._library.nexa_wl_pointer.argtypes = [ctypes.c_void_p, ctypes.c_uint64,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
        self._library.nexa_wl_key.argtypes = [ctypes.c_void_p, ctypes.c_uint64,
                                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
        self._library.nexa_wl_text_input_commit.argtypes = [ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_char_p]
        self._library.nexa_wl_text_input_delete.argtypes = [ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32]
        self._library.nexa_wl_destroy.argtypes = [ctypes.c_void_p]
        self._server = self._library.nexa_wl_create(
            str(self.runtime_dir).encode(), socket_name.encode(), width, height)
        if not self._server:
            raise RuntimeError(f"could not create Wayland socket {socket_name}")
        self._active_window_callback = self._active_window_changed
        self.window_host.on_active_window_changed = self._active_window_callback
        self._events = deque()
        self._event_lock = Lock()
        self._surfaces = {}
        self._views = {}
        self._pending_popups = []
        self._processes = set()
        self._frame_condition = Condition()
        self._pending_frames = OrderedDict()
        self._frame_stopping = False
        self.on_touch = None
        self.on_key = None
        self.on_resize = None
        self._stopping = Event()
        self._frame_thread = Thread(target=self._present_frames,
                                    name="nexa-wayland-frames", daemon=True)
        self._frame_thread.start()
        self._thread = Thread(target=self._dispatch, name="nexa-wayland", daemon=True)
        self._thread.start()

    def _dispatch(self):
        while not self._stopping.is_set():
            if self._library.nexa_wl_dispatch(self._server, 40) < 0:
                break
            event_ptr = ctypes.POINTER(_NativeEvent)()
            while self._library.nexa_wl_next_event(self._server, ctypes.byref(event_ptr)) > 0:
                event = event_ptr.contents
                frame = None
                if event.type == self.FRAME and event.pixels:
                    frame = ctypes.string_at(event.pixels, event.stride * event.height)
                item = (event.type, event.surface, event.parent, event.x, event.y,
                        _decode(event.app_id),
                        _decode(event.title), event.width, event.height,
                        event.stride, event.format, frame)
                with self._event_lock:
                    if event.type == self.FRAME:
                        for index in range(len(self._events) - 1, -1, -1):
                            previous = self._events[index]
                            if previous[0] == self.FRAME and previous[1] == event.surface:
                                self._events[index] = item
                                break
                        else:
                            self._events.append(item)
                    else:
                        self._events.append(item)
                self._library.nexa_wl_free_event(event_ptr)

    def pump(self):
        while True:
            with self._event_lock:
                if not self._events:
                    break
                event = self._events.popleft()
            (kind, surface_id, parent_id, x, y, app_id, title, width, height,
             stride, fmt, frame) = event
            if kind == self.TOPLEVEL:
                self._open_surface(surface_id, app_id, title)
            elif kind == self.POPUP:
                if not self._open_popup(surface_id, parent_id, x, y, width, height):
                    self._pending_popups.append((surface_id, parent_id, x, y, width, height))
            elif kind == self.TITLE:
                state = self._surfaces.get(surface_id)
                if state and state["window"] is not None:
                    state["window"].set_title(title)
            elif kind == self.FRAME:
                if frame is not None and surface_id in self._surfaces:
                    self._schedule_frame(surface_id, (frame, width, height, stride, fmt))
            elif kind == self.UNMAP:
                state = self._surfaces.get(surface_id)
                if state and state["view"] is not None:
                    state["view"].setvisibility(tg.View.HIDDEN)
            elif kind == self.DESTROY:
                self._close_surface(surface_id, send_close=False)
            elif kind == self.TEXT_INPUT:
                state = self._surfaces.get(surface_id)
                if state is not None:
                    enabled = bool(width)
                    state["text_input_enabled"] = enabled
                    view = state.get("view")
                    if view is not None:
                        if enabled:
                            view.focus(forcesoft=True)
                        else:
                            view.hidekeyboard()
        if self._pending_popups:
            pending, self._pending_popups = self._pending_popups, []
            for popup in pending:
                if not self._open_popup(*popup):
                    self._pending_popups.append(popup)

    def _open_surface(self, surface_id, app_id, title):
        if surface_id in self._surfaces:
            return
        state = {"window": None, "view": None, "frame": None,
                 "parent": None, "popup": False, "container": None,
                 "frame_lock": Lock(), "closed": False,
                 "text_input_enabled": False}
        self._surfaces[surface_id] = state
        app_id = app_id or "wayland-app"
        title = title or app_id

        def build(activity, parent, context):
            state["activity"] = activity
            state["container"] = parent
            view = tg.SurfaceView(activity, parent, keyboard=True, touch=True,
                                  z_order_on_top=False)
            view.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            with state["frame_lock"]:
                state["view"] = view
                pending_frame = state["frame"]
                if pending_frame is not None:
                    with self._frame_condition:
                        if surface_id not in self._pending_frames:
                            self._pending_frames[surface_id] = pending_frame
                            self._frame_condition.notify()
            self._views[view.id] = surface_id
            view.on_touch = lambda value: self._forward_input(surface_id, "touch", value)
            view.on_key = lambda value: self._forward_input(surface_id, "key", value)
            if state["text_input_enabled"]:
                view.focus(forcesoft=True)
            return _SurfaceHandler(self, surface_id, state)

        def closed():
            if not state["closed"]:
                state["closed"] = True
                view = state.get("view")
                if view is not None:
                    self._views.pop(view.id, None)
                    with state["frame_lock"]:
                        state["frame"] = None
                        release_buffer = getattr(view, "release_buffer", None)
                        if release_buffer is not None:
                            release_buffer()
                    state["view"] = None
                self._library.nexa_wl_close_toplevel(self._server, surface_id)

        state["window"] = self.window_host.open_toplevel(
            surface_id, app_id, title, build, on_close=closed,
            on_resize=lambda width, height: self._resize_surface(surface_id, width, height))

    def _active_window_changed(self, window_id):
        surface_id = next((surface for surface, window in self.window_host._toplevels.items()
                           if window.window_id == window_id), 0)
        self._library.nexa_wl_focus(self._server, surface_id)

    def _open_popup(self, surface_id, parent_id, x, y, width, height):
        existing = self._surfaces.get(surface_id)
        if existing is not None:
            view = existing.get("view")
            if view is not None:
                view.setdimensions(max(1, width), max(1, height), px=True)
                from shell.geometry import place
                place(view, x / self.density, y / self.density, top=True)
            return view is not None
        parent = self._surfaces.get(parent_id)
        if parent is None or parent.get("container") is None:
            return False
        state = {"window": None, "view": None, "frame": None,
                 "parent": parent_id, "popup": True,
                 "container": parent["container"], "activity": parent["activity"],
                 "frame_lock": Lock(), "closed": False,
                 "text_input_enabled": False}
        self._surfaces[surface_id] = state
        view = tg.SurfaceView(state["activity"], state["container"], keyboard=True,
                              touch=True, z_order_on_top=False)
        view.setdimensions(max(1, width), max(1, height), px=True)
        from shell.geometry import place
        place(view, x / self.density, y / self.density, top=True)
        state["view"] = view
        self._views[view.id] = surface_id
        return True

    def _present(self, surface_id, state):
        frame = state["frame"]
        view = state["view"]
        if frame is None or view is None or state.get("closed"):
            return
        pixels, width, height, stride, fmt = frame
        if state["popup"]:
            view.setdimensions(width, height, px=True)
        view.present_shm(pixels, width, height, stride,
                         "ARGB8888" if fmt == 0 else "XRGB8888")
        view.setvisibility(tg.View.VISIBLE)
        state["frame"] = None

    def _schedule_frame(self, surface_id, frame):
        with self._frame_condition:
            self._pending_frames[surface_id] = frame
            self._pending_frames.move_to_end(surface_id)
            self._frame_condition.notify()

    def _present_frames(self):
        while True:
            with self._frame_condition:
                while not self._pending_frames and not self._frame_stopping:
                    self._frame_condition.wait()
                if self._frame_stopping:
                    return
                surface_id, frame = self._pending_frames.popitem(last=False)
            state = self._surfaces.get(surface_id)
            if state is None:
                continue
            with state["frame_lock"]:
                if state.get("closed"):
                    continue
                state["frame"] = frame
                try:
                    self._present(surface_id, state)
                except Exception as error:
                    state["frame"] = None
                    print(f"nexa: couldn't present Wayland frame: {error}")

    def _forward_input(self, surface_id, kind, value):
        if kind == "touch":
            state = self._surfaces.get(surface_id)
            pointers = value.get("pointers", [])
            action = value.get("action")
            timestamp = value.get("time", 0) & 0xffffffff
            if state is None:
                return
            index = value.get("index", 0)
            index = index if 0 <= index < len(pointers) else 0
            pointer = pointers[index] if pointers else None
            if (pointer is not None and pointer.get("toolType") == 3) or action in (
                    "hover_move", "hover_exit", "scroll", "button_press", "button_release"):
                self._forward_mouse(surface_id, state, action, value, pointer, timestamp)
                if self.on_touch is not None:
                    self.on_touch(surface_id, value)
                return
            if not pointers:
                if action == "cancel" and state.get("touch_pointer") is not None:
                    self._library.nexa_wl_pointer(self._server, surface_id, 2, 0, 0,
                                                   timestamp, 0x110, 0)
                    self._library.nexa_wl_pointer(self._server, surface_id, 3, 0, 0,
                                                   timestamp, 0x110, 0)
                    state["touch_pointer"] = None
                return
            x = round(pointer["x"] / self.density)
            y = round(pointer["y"] / self.density)
            pointer_id = pointer.get("id")
            if action in ("down", "pointer_down") and state.get("touch_pointer") is None:
                state["touch_pointer"] = pointer_id
                self._library.nexa_wl_pointer(self._server, surface_id, 0, x, y,
                                               timestamp, 0x110, 0)
                self._library.nexa_wl_pointer(self._server, surface_id, 2, x, y,
                                               timestamp, 0x110, 1)
            elif action == "move" and pointer_id == state.get("touch_pointer"):
                self._library.nexa_wl_pointer(self._server, surface_id, 1, x, y,
                                               timestamp, 0x110, 0)
            elif (action in ("up", "pointer_up", "cancel")
                  and pointer_id == state.get("touch_pointer")):
                self._library.nexa_wl_pointer(self._server, surface_id, 1, x, y,
                                               timestamp, 0x110, 0)
                self._library.nexa_wl_pointer(self._server, surface_id, 2, x, y,
                                               timestamp, 0x110, 0)
                self._library.nexa_wl_pointer(self._server, surface_id, 3, x, y,
                                               timestamp, 0x110, 0)
                state["touch_pointer"] = None
            if self.on_touch is not None:
                self.on_touch(surface_id, value)
            return

        if value.get("text"):
            state = self._surfaces.get(surface_id)
            if state is not None and state.get("text_input_enabled"):
                self._send_text_input(surface_id, value["text"])
            else:
                self._send_text(surface_id, value["text"])
            if self.on_key is not None:
                self.on_key(surface_id, value)
            return

        code = _linux_keycode(value.get("code", -1))
        if code is not None:
            self._library.nexa_wl_key(self._server, surface_id,
                value.get("time", 0) & 0xffffffff, code,
                value.get("action") == 0)
        if self.on_key is not None:
            self.on_key(surface_id, value)

    def _forward_mouse(self, surface_id, state, action, value, pointer, timestamp):
        if pointer is None:
            return
        x = round(pointer["x"] / self.density)
        y = round(pointer["y"] / self.density)
        inside = state.get("mouse_inside", False)

        if action == "hover_exit":
            self._library.nexa_wl_pointer(self._server, surface_id, 3, x, y,
                                           timestamp, 0x110, 0)
            state["mouse_inside"] = False
            return

        if action in ("hover_move", "down", "up", "button_press", "button_release", "scroll"):
            self._library.nexa_wl_pointer(self._server, surface_id, 0, x, y,
                                           timestamp, 0x110, 0)
            state["mouse_inside"] = True
        elif action == "move":
            self._library.nexa_wl_pointer(self._server, surface_id,
                1 if inside else 0, x, y, timestamp, 0x110, 0)
            state["mouse_inside"] = True

        if action == "scroll":
            horizontal = round(value.get("horizontalScroll", 0) * 48)
            vertical = round(-value.get("verticalScroll", 0) * 48)
            self._library.nexa_wl_pointer(self._server, surface_id, 4,
                horizontal, vertical, timestamp, 0x110, 0)
        elif action in ("down", "up", "button_press", "button_release"):
            button = value.get("button", 1)
            if button not in (1, 2, 4, 8, 16):
                button = next((bit for bit in (1, 2, 4, 8, 16)
                               if value.get("buttonState", 0) & bit), 1)
            button_code = {1: 0x110, 2: 0x111, 4: 0x112, 8: 0x113, 16: 0x114}[button]
            pressed = action in ("down", "button_press")
            self._library.nexa_wl_pointer(self._server, surface_id, 2, x, y,
                                           timestamp, button_code, int(pressed))

    def _send_text(self, surface_id, text):
        timestamp = (time.monotonic_ns() // 1_000_000) & 0xffffffff
        index = 0
        while index < len(text):
            sequence = next((seq for seq in _TEXT_SEQUENCES if text.startswith(seq, index)), None)
            if sequence:
                code = _TEXT_SEQUENCES[sequence]
                index += len(sequence)
                self._library.nexa_wl_key(self._server, surface_id, timestamp, code, 1)
                self._library.nexa_wl_key(self._server, surface_id, timestamp, code, 0)
                continue
            character = text[index]
            index += 1
            shifted = _SHIFTED_TEXT_KEYS.get(character)
            code = shifted[0] if shifted else _TEXT_KEYS.get(character.lower())
            if code is None:
                continue
            if shifted:
                self._library.nexa_wl_key(self._server, surface_id, timestamp, 42, 1)
            self._library.nexa_wl_key(self._server, surface_id, timestamp, code, 1)
            self._library.nexa_wl_key(self._server, surface_id, timestamp, code, 0)
            if shifted:
                self._library.nexa_wl_key(self._server, surface_id, timestamp, 42, 0)

    def _send_text_input(self, surface_id, text):
        commit = []

        def flush():
            if commit:
                self._library.nexa_wl_text_input_commit(
                    self._server, surface_id, "".join(commit).encode("utf-8"))
                commit.clear()

        index = 0
        while index < len(text):
            if text.startswith("\x1b[3~", index):
                flush()
                self._library.nexa_wl_text_input_delete(self._server, surface_id, 0, 1)
                index += 4
            elif text[index] in ("\x7f", "\x08"):
                flush()
                self._library.nexa_wl_text_input_delete(self._server, surface_id, 1, 0)
                index += 1
            else:
                commit.append(text[index])
                index += 1
        flush()

    def _resize_surface(self, surface_id, width, height):
        width = round(width / self.density)
        height = round(height / self.density)
        self._library.nexa_wl_configure_toplevel(self._server, surface_id, width, height)
        if self.on_resize is not None:
            self.on_resize(surface_id, width, height)

    def resize_output(self, width, height):
        self._library.nexa_wl_update_output(self._server, width, height)

    def _close_surface(self, surface_id, send_close=True):
        state = self._surfaces.pop(surface_id, None)
        if state is None:
            return
        state["closed"] = True
        with self._frame_condition:
            self._pending_frames.pop(surface_id, None)
        for child_id, child in tuple(self._surfaces.items()):
            if child.get("parent") == surface_id:
                self._close_surface(child_id, send_close=False)
        window = state.get("window")
        view = state.get("view")
        if view is not None:
            self._views.pop(view.id, None)
            with state["frame_lock"]:
                release_buffer = getattr(view, "release_buffer", None)
                if release_buffer is not None:
                    release_buffer()
        if window is not None and not window._closed:
            window.close()
        elif view is not None and state["popup"]:
            view.delete()
        if send_close:
            self._library.nexa_wl_close_toplevel(self._server, surface_id)

    def handle_event(self, event):
        surface_id = self._views.get(getattr(event, "id", None))
        if surface_id is None:
            return False
        state = self._surfaces.get(surface_id)
        if state is None:
            return False
        if event.type == tg.Event.touch:
            self._forward_input(surface_id, "touch", event.value)
            return True
        if event.type == tg.Event.key:
            self._forward_input(surface_id, "key", event.value)
            return True
        return False

    def launch(self, argv, cwd=None, env=None):
        argv = [os.fspath(value) for value in argv]
        if not argv:
            raise ValueError("an executable is required")
        environment = self.environment.copy()
        if env:
            environment.update({str(key): str(value) for key, value in env.items()})
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None and os.path.isfile(argv[0]):
            executable = argv[0]
        machine = None
        if executable is not None:
            try:
                with open(executable, "rb") as binary:
                    header = binary.read(20)
                if (len(header) >= 20 and header[:4] == b"\x7fELF"
                        and header[5] == 1):
                    machine = int.from_bytes(header[18:20], "little")
            except OSError:
                pass
        use_box64 = machine == 62 or Path(argv[0]).name == "box64"
        if use_box64:
            command = "exec " + shlex.join(["box64", *argv[1:]]) if Path(argv[0]).name == "box64" else (
                "exec " + shlex.join(["box64", executable, *argv[1:]]))
            argv = ["glibc-runner", "--shell", command]
        process = subprocess.Popen(argv, cwd=cwd, env=environment,
                                   close_fds=True, start_new_session=True)
        self._processes.add(process)
        return process

    def close(self):
        self._stopping.set()
        if self.window_host.on_active_window_changed == self._active_window_callback:
            self.window_host.on_active_window_changed = None
        with self._frame_condition:
            self._frame_stopping = True
            self._pending_frames.clear()
            self._frame_condition.notify_all()
        self._frame_thread.join(timeout=1)
        self._thread.join(timeout=1)
        for process in self._processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, 15)
                except ProcessLookupError:
                    pass
        for process in self._processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, 9)
                    except ProcessLookupError:
                        pass
        for surface_id in tuple(self._surfaces):
            self._close_surface(surface_id, send_close=False)
        self._library.nexa_wl_destroy(self._server)
        self._server = None
        self._surfaces.clear()
        self._views.clear()
        self._pending_popups.clear()
