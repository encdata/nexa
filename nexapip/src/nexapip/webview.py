from pathlib import Path
import re
from typing import Optional, Literal
from urllib.parse import urlsplit

from nexapip.activity import Activity
from nexapip.view import View

ELECTRON_PRELOAD = Path(__file__).with_name("electron_preload.js").read_text(encoding="utf-8")


class WebView(View):
    """This represents a WebView."""

    def __init__(self, activity: Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None,
                 electron_bridge: Optional[dict] = None,
                 enable_javascript: bool = False,
                 shortcut_events: bool = False):
        args = {"aid": activity.aid, "visibility": visibility}
        if shortcut_events:
            args["shortcutEvents"] = True
        profile_name = None
        if enable_javascript:
            args["enableJavascript"] = True
        if parent is not None:
            args["parent"] = parent.id
        if electron_bridge is not None:
            origins = electron_bridge.get("origins")
            preload = electron_bridge.get("preload", ELECTRON_PRELOAD)
            if not isinstance(origins, (list, tuple, set)) or not origins:
                raise ValueError("electron_bridge needs a non-empty origins list")
            if not all(isinstance(origin, str) for origin in origins):
                raise ValueError("electron bridge origins must be strings")
            if not isinstance(preload, str) or not preload.strip():
                raise ValueError("electron_bridge needs a preload script")
            args["electronBridge"] = {"origins": list(origins), "preload": preload}
            profile_name = electron_bridge.get("profile_name")
            if profile_name is not None:
                if not isinstance(profile_name, str) or not re.fullmatch(
                        r"nexa_[a-f0-9]{32}(?:_[a-f0-9]{16})?", profile_name):
                    raise ValueError("electron profile name must be a Nexa profile")
                args["electronBridge"]["profileName"] = profile_name
            preload_url = electron_bridge.get("preload_url")
            if preload_url is not None:
                parsed_preload = urlsplit(preload_url) if isinstance(preload_url, str) else None
                try:
                    preload_port = parsed_preload.port if parsed_preload is not None else None
                except ValueError:
                    preload_port = None
                if (parsed_preload is None or parsed_preload.scheme != "http"
                        or parsed_preload.hostname != "127.0.0.1"
                        or preload_port is None or not 1 <= preload_port <= 65535
                        or parsed_preload.username is not None or parsed_preload.password is not None
                        or not parsed_preload.path.startswith("/__nexa/preload/")
                        or parsed_preload.query or parsed_preload.fragment):
                    raise ValueError("electron preload must use the local Nexa host")
                args["electronBridge"]["preloadUrl"] = preload_url
            transport = ("ipc_endpoint", "ipc_token", "window_id")
            if any(key in electron_bridge for key in transport):
                endpoint = electron_bridge.get("ipc_endpoint")
                token = electron_bridge.get("ipc_token")
                window_id = electron_bridge.get("window_id")
                parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
                try:
                    port = parsed.port if parsed is not None else None
                except ValueError:
                    port = None
                if (parsed is None or parsed.scheme != "http"
                        or parsed.hostname != "127.0.0.1"
                        or port is None or not 1 <= port <= 65535
                        or parsed.username is not None or parsed.password is not None
                        or parsed.path != "/__nexa/ipc" or parsed.query or parsed.fragment):
                    raise ValueError("electron ipc endpoint must use the local Nexa host")
                if not isinstance(token, str) or not token:
                    raise ValueError("electron bridge needs an ipc token")
                if type(window_id) is not int:
                    raise ValueError("electron bridge needs a numeric window id")
                args["electronBridge"].update({
                    "ipcEndpoint": endpoint,
                    "ipcToken": token,
                    "windowId": window_id
                })
            background_throttling = electron_bridge.get("background_throttling")
            if background_throttling is not None:
                if not isinstance(background_throttling, bool):
                    raise TypeError("electron background_throttling must be a bool")
                args["electronBridge"]["backgroundThrottling"] = background_throttling
        view_id = activity.c.send_read_msg({"method": "createWebView", "params": args})
        if profile_name is not None and view_id == -1:
            raise RuntimeError("Electron sessions need Android WebView MULTI_PROFILE support")
        View.__init__(self, activity, view_id)

    def allowjavascript(self, allow: bool):
        """Sets whether Javascript execution is allowed in the WebView.
        If requesting to allow, the user is prompted and can deny the request.
        Blocks until the user responded.
        Returns wheter Javascript is enabled after the call."""
        return self.a.c.send_read_msg({"method": "allowJavascript", "params": {
            "aid": self.a.aid, "id": self.id, "allow": allow}})

    def allowcontenturi(self, allow: bool):
        """Sets whether it is allowed to load content from a content:// URI."""
        self.a.c.send_msg({"method": "allowContentURI", "params": {"aid": self.a.aid, "id": self.id, "allow": allow}})

    def allownavigation(self, allow: bool):
        """Sets whether the user and Javascript are allowed to navigate to different sites."""
        self.a.c.send_msg({"method": "allowNavigation", "params": {"aid": self.a.aid, "id": self.id, "allow": allow}})

    def setdesktopmode(self, enabled: bool):
        """Uses a desktop viewport and user agent for the loaded page."""
        self.a.c.send_msg({"method": "setDesktopMode", "params": {
            "aid": self.a.aid, "id": self.id, "enabled": enabled}})

    def setuseragent(self, user_agent: str):
        self.a.c.send_msg({"method": "setWebViewUserAgent", "params": {
            "aid": self.a.aid, "id": self.id, "userAgent": user_agent}})

    def setbackgroundthrottling(self, enabled: bool):
        self.a.c.send_msg({"method": "setElectronBackgroundThrottling", "params": {
            "aid": self.a.aid, "id": self.id, "enabled": enabled}})

    def clearcache(self):
        """Clears the Android app's shared WebView cache."""
        return self.a.c.send_read_msg_async({"method": "clearWebViewCache", "params": {
            "aid": self.a.aid, "id": self.id}})

    def clear_site_data(self, url: str):
        """Clears WebView storage for the site containing this URL."""
        return self.a.c.send_read_msg_async({"method": "clearWebViewSiteData", "params": {
            "aid": self.a.aid, "id": self.id, "url": url}})

    def reload(self):
        self.a.c.send_msg({"method": "reloadWebView", "params": {
            "aid": self.a.aid, "id": self.id}})

    def reloadignoringcache(self):
        self.a.c.send_msg({"method": "reloadWebViewIgnoringCache", "params": {
            "aid": self.a.aid, "id": self.id}})

    def get_devtools_url(self):
        """Returns the Android WebView DevTools frontend URL for this page."""
        return self.a.c.send_read_msg({"method": "getWebViewDevToolsUrl", "params": {
            "aid": self.a.aid, "id": self.id}})

    def close_devtools(self):
        self.a.c.send_msg({"method": "closeWebViewDevTools", "params": {
            "aid": self.a.aid, "id": self.id}})

    def stoploading(self):
        self.a.c.send_msg({"method": "stopWebViewLoading", "params": {
            "aid": self.a.aid, "id": self.id}})

    def setdata(self, data: str):
        """Sets the document data."""
        self.a.c.send_msg({"method": "setData", "params": {"aid": self.a.aid, "id": self.id, "doc": data}})

    def loaduri(self, uri: str, headers=None):
        """Loads a URI."""
        params = {"aid": self.a.aid, "id": self.id, "uri": uri}
        if headers:
            params["headers"] = headers
        self.a.c.send_msg({"method": "loadURI", "params": params})

    def goback(self):
        """Goes back in the history."""
        self.a.c.send_msg({"method": "goBack", "params": {"aid": self.a.aid, "id": self.id}})

    def goforward(self):
        """Goes forward in the history."""
        self.a.c.send_msg({"method": "goForward", "params": {"aid": self.a.aid, "id": self.id}})

    def gobackorforward(self, steps: int):
        self.a.c.send_msg({"method": "goBackOrForward", "params": {
            "aid": self.a.aid, "id": self.id, "steps": int(steps)}})

    def clearhistory(self):
        self.a.c.send_msg({"method": "clearHistory", "params": {
            "aid": self.a.aid, "id": self.id}})

    def evaluatejs(self, code: str):
        """Runs Javascript in the WebView, if Javascript is enabled."""
        self.a.c.send_msg({"method": "evaluateJS", "params": {"aid": self.a.aid, "id": self.id, "code": code}})

    def reply_electron(self, message: str):
        """Sends a message to the Electron preload bridge in the calling frame."""
        self.a.c.send_msg({"method": "webviewBridgeReply", "params": {
            "aid": self.a.aid, "id": self.id, "message": message}})
