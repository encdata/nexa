import json
from concurrent.futures import ThreadPoolExecutor
import logging

from nexapip.event import Event

logger = logging.getLogger("nexa.electron")


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
                future = self._transport_executor.submit(self.transport, request)
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
