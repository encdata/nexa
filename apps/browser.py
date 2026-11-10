import html
import json
import os
from urllib.parse import quote_plus, urlsplit

import termuxgui as tg

from shell import palette
from shell.text_direction import is_rtl


DATA_PATH = os.path.expanduser("~/.config/nexa/browser.json")
SEARCH_URL = "https://duckduckgo.com/?q={}"


class BrowserApp:
    app_id = "browser"
    name = "Nexa Browser"
    icon = "window"

    def build(self, activity, parent, context=None):
        return BrowserSession(activity, parent)


class BrowserSession:
    def __init__(self, activity, parent):
        self.activity = activity
        self.rtl = is_rtl(activity.getconfiguration())
        self.buttons = {}
        self.button_views = {}
        self.webviews = {}
        self.tabs = {}
        self.next_tab_id = 0
        self.active_tab = None
        self.history, self.bookmarks = self._load_data()

        self.root = tg.LinearLayout(activity, parent)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.root.setbackgroundcolor(palette.window_content)

        self.toolbar = tg.LinearLayout(activity, self.root, vertical=False)
        self.toolbar.setdimensions(tg.View.MATCH_PARENT, 48)
        self.toolbar.setlinearlayoutparams(0)
        self._build_toolbar()

        tab_bar = tg.LinearLayout(activity, self.root, vertical=False)
        tab_bar.setdimensions(tg.View.MATCH_PARENT, 42)
        tab_bar.setlinearlayoutparams(0)
        self.tab_scroll = tg.HorizontalScrollView(activity, tab_bar, nobar=True)
        self.tab_scroll.setdimensions(0, tg.View.MATCH_PARENT)
        self.tab_scroll.setlinearlayoutparams(1)
        self.tab_list = tg.LinearLayout(activity, self.tab_scroll, vertical=False)
        self.tab_list.setdimensions(tg.View.WRAP_CONTENT, tg.View.MATCH_PARENT)
        self.new_tab_button = self._button(tab_bar, "+", "new_tab", 42)
        self.new_tab_button.settextsize(22)

        self.progress = tg.ProgressBar(activity, self.root)
        self.progress.setdimensions(tg.View.MATCH_PARENT, 3)
        self.progress.setlinearlayoutparams(0)
        self.progress.setvisibility(tg.View.GONE)

        self.pages = tg.FrameLayout(activity, self.root)
        self.pages.setheight(0)
        self.pages.setlinearlayoutparams(1)
        self.status = tg.TextView(activity, "ready", self.root)
        self.status.setdimensions(tg.View.MATCH_PARENT, 24)
        self.status.setlinearlayoutparams(0)
        self.status.setmargin(8, "left")
        self.status.settextsize(11)
        self.status.settextcolor(palette.muted)
        self.status.setgravity(0, 1)

        self._new_tab()

    def _build_toolbar(self):
        controls = [
            ("back", "‹", 38),
            ("forward", "›", 38),
            ("reload", "↻", 38),
            ("home", "⌂", 38),
            ("address", None, 0),
            ("go", "go", 42),
            ("bookmark", "☆", 38),
            ("javascript", "js", 38),
        ]
        if self.rtl:
            controls.reverse()
        for action, label, width in controls:
            if action == "address":
                self.address = tg.EditText(self.activity, "", self.toolbar, singleline=True,
                                           line=False, inputtype="text")
                self.address.setdimensions(0, 40)
                self.address.setlinearlayoutparams(1)
                self.address.setmargin(3, "left")
                self.address.setmargin(3, "right")
                self.address.setbackgroundcolor(palette.surface)
                self.address.settextcolor(palette.text)
                self.address.settextsize(14)
                self.address.setgravity(0, 1)
                continue
            button = self._button(self.toolbar, label, action, width)
            if action in ("back", "forward", "reload", "home", "bookmark"):
                button.settextsize(20)
            if action == "javascript":
                self.javascript_button = button
            elif action == "bookmark":
                self.bookmark_button = button

    def _button(self, parent, label, action, width):
        button = tg.TextView(self.activity, label, parent)
        button.setdimensions(width, 40)
        button.setlinearlayoutparams(0)
        button.setmargin(2, "left")
        button.setmargin(2, "right")
        button.setbackgroundcolor(palette.surface_light)
        button.settextcolor(palette.text)
        button.settextsize(14)
        button.setgravity(1, 1)
        button.sendclickevent(True)
        self.buttons[button.id] = (action, None)
        self.button_views[action] = button
        return button

    def _load_data(self):
        try:
            with open(DATA_PATH, encoding="utf-8") as data_file:
                data = json.load(data_file)
        except FileNotFoundError:
            return [], []
        except (OSError, json.JSONDecodeError) as error:
            print(f"nexa browser: couldn't read saved data: {error}")
            return [], []

        if not isinstance(data, dict):
            return [], []
        saved_history = data.get("history", [])
        saved_bookmarks = data.get("bookmarks", [])
        if not isinstance(saved_history, list):
            saved_history = []
        if not isinstance(saved_bookmarks, list):
            saved_bookmarks = []
        history = [url for url in saved_history if self._is_web_url(url)][:100]
        bookmarks = list(dict.fromkeys(
            url for url in saved_bookmarks if self._is_web_url(url)))
        return history, bookmarks

    def _save_data(self):
        directory = os.path.dirname(DATA_PATH)
        try:
            os.makedirs(directory, exist_ok=True)
            temporary = f"{DATA_PATH}.tmp"
            with open(temporary, "w", encoding="utf-8") as data_file:
                json.dump({"history": self.history[:100], "bookmarks": self.bookmarks},
                          data_file, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, DATA_PATH)
        except OSError as error:
            print(f"nexa browser: couldn't save browser data: {error}")

    @staticmethod
    def _is_web_url(value):
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)

    def _normalize_address(self, value):
        value = value.strip()
        if not value:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return SEARCH_URL.format(quote_plus(value))
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            return parsed._replace(scheme=parsed.scheme.lower()).geturl()
        if (not parsed.scheme or parsed.scheme.lower() in ("http", "https")) \
                and "://" not in value and not any(char.isspace() for char in value):
            return f"https://{value}"
        return SEARCH_URL.format(quote_plus(value))

    def _new_tab(self):
        self.next_tab_id += 1
        tab_id = self.next_tab_id
        row = tg.LinearLayout(self.activity, self.tab_list, vertical=False)
        row.setdimensions(144, 40)
        row.setlinearlayoutparams(0)
        tab_button = tg.TextView(self.activity, "new tab", row)
        tab_button.setdimensions(104, 40)
        tab_button.setlinearlayoutparams(0)
        tab_button.settextsize(12)
        tab_button.settextcolor(palette.text)
        tab_button.setgravity(1, 1)
        tab_button.sendclickevent(True)
        close_button = tg.TextView(self.activity, "×", row)
        close_button.setdimensions(36, 40)
        close_button.setlinearlayoutparams(0)
        close_button.settextsize(18)
        close_button.settextcolor(palette.muted)
        close_button.setgravity(1, 1)
        close_button.sendclickevent(True)

        tab = {"id": tab_id, "row": row, "button": tab_button, "close": close_button,
               "url": "", "stack": [], "index": -1, "javascript": False}
        self.tabs[tab_id] = tab
        self.buttons[tab_button.id] = ("select_tab", tab_id)
        self.buttons[close_button.id] = ("close_tab", tab_id)
        webview = tg.WebView(self.activity, self.pages)
        webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        webview.allownavigation(False)
        tab["webview"] = webview
        self.webviews[webview.id] = tab_id
        self._select_tab(tab_id)
        webview.setdata(self._home_page())
        return tab_id

    def _select_tab(self, tab_id):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        self.active_tab = tab_id
        for current_id, current in self.tabs.items():
            current["webview"].setvisibility(
                tg.View.VISIBLE if current_id == tab_id else tg.View.GONE)
            current["row"].setbackgroundcolor(
                palette.surface_light if current_id == tab_id else palette.surface)
        self.address.settext(tab["url"])
        self.javascript_button.settext("js on" if tab["javascript"] else "js")
        self.bookmark_button.settext("★" if tab["url"] in self.bookmarks else "☆")
        self._update_navigation_buttons(tab)
        self.status.settext("ready" if tab["url"] else "new tab")

    def _close_tab(self, tab_id):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        was_active = tab_id == self.active_tab
        self.webviews.pop(tab["webview"].id, None)
        self.buttons.pop(tab["button"].id, None)
        self.buttons.pop(tab["close"].id, None)
        tab["webview"].delete()
        tab["row"].delete()
        del self.tabs[tab_id]
        if not self.tabs:
            self._new_tab()
        elif was_active:
            self._select_tab(next(reversed(self.tabs)))

    def _update_navigation_buttons(self, tab):
        can_go_back = tab["index"] > 0
        can_go_forward = 0 <= tab["index"] < len(tab["stack"]) - 1
        for action, enabled in (("back", can_go_back), ("forward", can_go_forward)):
            button = self.button_views.get(action)
            if button is not None:
                button.settextcolor(palette.text if enabled else palette.muted)

    def _push_url(self, tab, url):
        if tab["index"] >= 0 and tab["stack"][tab["index"]] == url:
            return
        tab["stack"] = tab["stack"][:tab["index"] + 1]
        tab["stack"].append(url)
        tab["index"] = len(tab["stack"]) - 1

    def _navigate(self, value):
        url = self._normalize_address(value)
        if url is None:
            self.status.settext("enter a web address or search")
            return
        self._load_url(self.tabs[self.active_tab], url)

    def _load_url(self, tab, url, record=True):
        if not self._is_web_url(url):
            self.status.settext("only http and https links are allowed")
            return
        if record:
            self._push_url(tab, url)
            self.history.insert(0, url)
            self.history = self.history[:100]
            self._save_data()
        tab["url"] = url
        tab["webview"].loaduri(url)
        if tab["id"] == self.active_tab:
            self.address.settext(url)
            self.bookmark_button.settext("★" if url in self.bookmarks else "☆")
            self._update_tab_label(tab)
            self._update_navigation_buttons(tab)
            self.progress.setprogress(0)
            self.progress.setvisibility(tg.View.VISIBLE)
            self.status.settext("loading")

    def _back(self):
        tab = self.tabs[self.active_tab]
        if tab["index"] <= 0:
            return
        tab["index"] -= 1
        self._load_url(tab, tab["stack"][tab["index"]], record=False)

    def _forward(self):
        tab = self.tabs[self.active_tab]
        if tab["index"] >= len(tab["stack"]) - 1:
            return
        tab["index"] += 1
        self._load_url(tab, tab["stack"][tab["index"]], record=False)

    def _reload(self):
        tab = self.tabs[self.active_tab]
        if tab["url"]:
            self._load_url(tab, tab["url"], record=False)
        else:
            tab["webview"].setdata(self._home_page())

    def _go_home(self):
        tab = self.tabs[self.active_tab]
        tab["url"] = ""
        tab["stack"] = []
        tab["index"] = -1
        tab["webview"].setdata(self._home_page())
        self.address.settext("")
        self.bookmark_button.settext("☆")
        self._update_tab_label(tab)
        self._update_navigation_buttons(tab)
        self.status.settext("new tab")

    def _toggle_bookmark(self):
        url = self.tabs[self.active_tab]["url"]
        if not url:
            self.status.settext("open a page to bookmark it")
            return
        if url in self.bookmarks:
            self.bookmarks.remove(url)
            self.status.settext("bookmark removed")
        else:
            self.bookmarks.insert(0, url)
            self.status.settext("bookmarked")
        self._save_data()
        self.bookmark_button.settext("★" if url in self.bookmarks else "☆")
        self._refresh_home_page()

    def _refresh_home_page(self):
        for tab in self.tabs.values():
            if not tab["url"]:
                tab["webview"].setdata(self._home_page())

    def _toggle_javascript(self):
        tab = self.tabs[self.active_tab]
        enabled = self.activity.c.send_read_msg({
            "method": "allowJavascript",
            "params": {"aid": self.activity.aid, "id": tab["webview"].id,
                       "allow": not tab["javascript"]},
        })
        tab["javascript"] = bool(enabled)
        self.javascript_button.settext("js on" if tab["javascript"] else "js")
        self.status.settext("javascript enabled" if tab["javascript"] else "javascript disabled")

    def _update_tab_label(self, tab):
        host = urlsplit(tab["url"]).hostname if tab["url"] else None
        label = host.removeprefix("www.") if host else "new tab"
        tab["button"].settext(label[:14])

    def _home_page(self):
        bookmarks = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(urlsplit(url).hostname or url)}</a></li>'
            for url in self.bookmarks) or "<li>no bookmarks yet</li>"
        history = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></li>'
            for url in self.history) or "<li>your recent pages will show here</li>"
        return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ background:#171c24; color:#e8edf2; font:16px sans-serif; margin:24px; }}
h1 {{ color:#45d6c5; font-size:26px; }} h2 {{ font-size:17px; margin-top:28px; }}
form {{ display:flex; gap:8px; }} input {{ box-sizing:border-box; padding:12px; font-size:16px; }}
input[name=q] {{ flex:1; min-width:0; }} button {{ padding:10px 16px; }}
a {{ color:#45d6c5; overflow-wrap:anywhere; }} li {{ margin:10px 0; }}
</style></head><body><h1>Nexa Browser</h1>
<form action="https://duckduckgo.com/"><input name="q" placeholder="search the web" autocomplete="off"><button>search</button></form>
<h2>bookmarks</h2><ul>{bookmarks}</ul>
<h2>recent pages</h2><ul>{history}</ul></body></html>"""

    def _handle_webview_event(self, event):
        event_id = getattr(event, "id", None)
        if event_id is None:
            return False
        tab_id = self.webviews.get(event_id)
        tab = self.tabs.get(tab_id)
        if tab is None:
            return False
        value = event.value
        if event.type == tg.Event.webviewNavigation:
            url = value.get("url", "")
            if url == tab["url"]:
                return True
            # The home page is data-backed; its search form is handled by the
            # toolbar, while external links still pass through URL validation.
            if self._is_web_url(url):
                self._load_url(tab, url)
            else:
                self.status.settext("blocked unsupported link")
            return True
        if tab_id != self.active_tab:
            return True
        if event.type == tg.Event.webviewProgress:
            progress = max(0, min(100, int(value.get("progress", 0))))
            self.progress.setprogress(progress)
            if progress >= 100:
                self.progress.setvisibility(tg.View.GONE)
                self.status.settext("ready")
            return True
        if event.type == tg.Event.webviewHTTPError:
            self.progress.setvisibility(tg.View.GONE)
            self.status.settext(f"http error {value.get('code', '')}")
            return True
        if event.type == tg.Event.webviewError:
            self.progress.setvisibility(tg.View.GONE)
            self.status.settext("page failed to load")
            return True
        if event.type == tg.Event.webviewDestroyed:
            self._restore_webview(tab)
            return True
        return False

    def _restore_webview(self, tab):
        old = tab["webview"]
        self.webviews.pop(old.id, None)
        webview = tg.WebView(self.activity, self.pages)
        webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        webview.allownavigation(False)
        tab["webview"] = webview
        tab["javascript"] = False
        self.webviews[webview.id] = tab["id"]
        webview.setvisibility(tg.View.VISIBLE if tab["id"] == self.active_tab else tg.View.GONE)
        if tab["url"]:
            self._load_url(tab, tab["url"], record=False)
        else:
            webview.setdata(self._home_page())

    def handle_event(self, event):
        if event.type == tg.Event.back:
            self._back()
            return True
        if event.type in (tg.Event.webviewNavigation, tg.Event.webviewProgress,
                          tg.Event.webviewHTTPError, tg.Event.webviewError,
                          tg.Event.webviewDestroyed):
            return self._handle_webview_event(event)
        if event.type != tg.Event.click:
            return False
        if event.id == self.address.id:
            self._navigate(self.address.gettext())
            return True
        action, value = self.buttons.get(event.id, (None, None))
        if action is None:
            return False
        if action == "go":
            self._navigate(self.address.gettext())
        elif action == "back":
            self._back()
        elif action == "forward":
            self._forward()
        elif action == "reload":
            self._reload()
        elif action == "home":
            self._go_home()
        elif action == "bookmark":
            self._toggle_bookmark()
        elif action == "javascript":
            self._toggle_javascript()
        elif action == "new_tab":
            self._new_tab()
        elif action == "select_tab":
            self._select_tab(value)
        elif action == "close_tab":
            self._close_tab(value)
        return True


APP = BrowserApp()
