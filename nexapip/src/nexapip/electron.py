import json
from concurrent.futures import ThreadPoolExecutor
import logging
from threading import BoundedSemaphore

from nexapip.event import Event

logger = logging.getLogger("nexa.electron")
MAX_QUEUED_IPC = 8


class ElectronBridge:
    """Routes Electron renderer IPC calls between a WebView and its Nexa app."""

    def __init__(self, webview, transport=None, on_ready=None):
        self.webview = webview
        self.handlers = {}
        self.transport = transport
        self.on_ready = on_ready
        self._closed = False
        self._transport_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-electron-ipc")
            if transport is not None else None
        )
        self._transport_slots = (
            BoundedSemaphore(MAX_QUEUED_IPC + 1) if transport is not None else None
        )
        self._transport_queue_full_reported = False

    def handle(self, channel, callback):
        self.handlers[channel] = callback

    def emit(self, channel, *args):
        self.webview.reply_electron(json.dumps({
            "kind": "event", "channel": channel, "args": args
        }, separators=(",", ":"), allow_nan=False))

    def handle_event(self, event):
        if self._closed:
            return False
        if event.type != Event.webviewBridgeMessage or event.id != self.webview.id:
            return False

        request = json.loads(event.value["message"])
        if not isinstance(request, dict):
            return False
        kind = request.get("kind")
        if kind == "preload-start":
            logger.info("Electron renderer preload started for WebView %s", self.webview.id)
            return True
        if kind == "preload-ready":
            logger.info("Electron renderer preload finished for WebView %s "
                        "(require=%s, identifier=%s, global=%s, main=%s, frame=%s)",
                        self.webview.id, request.get("hasRequire") is True,
                        request.get("hasRequireIdentifier") is True,
                        request.get("hasGlobalRequire") is True,
                        request.get("isMainFrame"),
                        request.get("frameUrl", ""))
            return True
        if kind == "frame-probe":
            logger.info("Electron renderer frame probe for WebView %s "
                        "(same_origin=%s, require=%s, global=%s, frame=%s)",
                        self.webview.id, request.get("sameOrigin"),
                        request.get("hasRequire"), request.get("hasGlobalRequire"),
                        request.get("frameUrl", ""))
            return True
        if kind == "ready":
            if self.on_ready is not None:
                self.on_ready()
            return True
        if kind not in ("send", "invoke"):
            return False

        channel = request.get("channel")
        args = request.get("args", [])
        if not isinstance(channel, str) or not isinstance(args, list):
            return False
        if kind == "invoke" and type(request.get("id")) is not int:
            return False
        callback = self.handlers.get(channel)
        if callback is None:
            if self.transport is not None:
                if not self._transport_slots.acquire(blocking=False):
                    if kind == "invoke":
                        self._reply(request, error="Electron IPC queue is full")
                    elif not self._transport_queue_full_reported:
                        logger.warning("Electron IPC queue is full; dropping send messages")
                        self._transport_queue_full_reported = True
                    return True
                self._transport_queue_full_reported = False
                try:
                    future = self._transport_executor.submit(self.transport, request)
                except RuntimeError as error:
                    self._transport_slots.release()
                    if kind == "invoke":
                        self._reply(request, error=str(error))
                    else:
                        logger.warning("Electron IPC send failed: %s", error)
                    return True
                future.add_done_callback(lambda _result: self._transport_slots.release())
                if kind == "invoke":
                    future.add_done_callback(
                        lambda result, request=request: self._transport_reply(request, result))
                else:
                    future.add_done_callback(self._transport_send_done)
                return True
            if kind == "invoke":
                self._reply(request, error=f"no Electron IPC handler for {channel}")
            return True

        if kind == "send":
            callback(*args)
            return True

        try:
            self._reply(request, value=callback(*args))
        except Exception as error:
            self._reply(request, error=str(error))
        return True

    def _transport_reply(self, request, future):
        if self._closed:
            return
        try:
            self._reply(request, value=future.result())
        except Exception as error:
            self._reply(request, error=str(error))

    @staticmethod
    def _transport_send_done(future):
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            logger.warning("electron ipc send failed: %s", error)

    def close(self):
        self._closed = True
        if self._transport_executor is not None:
            self._transport_executor.shutdown(wait=False, cancel_futures=True)

    def _reply(self, request, value=None, error=None):
        message = {"kind": "reply", "id": request.get("id")}
        if error is None:
            message["value"] = value
        else:
            message["error"] = error
        self.webview.reply_electron(json.dumps(
            message, separators=(",", ":"), allow_nan=False
        ))
