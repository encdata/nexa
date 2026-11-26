import html
import json
import os
from urllib.parse import quote_plus, urlsplit

import nexapip as tg

from shell import palette
from shell.text_direction import is_rtl
from shell.ui_layout import LayoutTemplate, button, create_layout, row, text_input


DATA_PATH = os.path.expanduser("~/.config/nexa/browser.json")
SEARCH_URL = "https://duckduckgo.com/?q={}"


class BrowserApp:
    app_id = "browser"
    name = "Nexa Browser"
    summary = "tabs, bookmarks, history, devtools"
    description = "Tabbed browsing, bookmarks, history, session restore, and WebView devtools."
    icon = "nexa"
    preload_ui = True
    build_threaded = True

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
        self.history, self.bookmarks, self.downloads, saved_tabs, saved_active = self._load_data()

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
        self.tab_template = LayoutTemplate(activity, "nexa.browser.tab", row(
            button("new tab", key="button", width=104, height=40,
                   textSize=12, textColor=palette.text),
            button("×", key="close", width=36, height=40,
                   textSize=18, textColor=palette.muted),
            key="row", width=144, height=40,
        ))
        self.new_tab_button = self._button(tab_bar, "+", "new_tab", 42)
        self.new_tab_button.settextsize(22)
        self.download_button = self._button(tab_bar, "↓", "downloads", 42)
        self.download_button.settextsize(20)

        self.progress = tg.ProgressBar(activity, self.root)
        self.progress.setdimensions(tg.View.MATCH_PARENT, 3)
        self.progress.setlinearlayoutparams(0)
        self.progress.setvisibility(tg.View.GONE)

        self.pages = tg.FrameLayout(activity, self.root)
        self.pages.setheight(0)
        self.pages.setlinearlayoutparams(1)
        self.download_panel = tg.LinearLayout(activity, self.pages, vertical=True)
        self.download_panel.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.download_panel.setbackgroundcolor(palette.window_content)
        self.download_panel.setvisibility(tg.View.GONE)
        self.downloads_visible = False
        self._build_download_panel()
        self.status = tg.TextView(activity, "ready", self.root)
        self.status.setdimensions(tg.View.MATCH_PARENT, 24)
        self.status.setlinearlayoutparams(0)
        self.status.setmargin(8, "left")
        self.status.settextsize(11)
        self.status.settextcolor(palette.muted)
        self.status.setgravity(0, 1)

        self.initial_default_navigation = False
        if saved_tabs:
            tab_ids = [self._new_tab(url, defer_load=True) for url in saved_tabs]
            self._select_tab(tab_ids[saved_active], load=False)
        else:
            self.initial_default_navigation = True
            self._new_tab(defer_load=True)

    def on_window_open(self):
        tab = self.tabs[self.active_tab]
        use_default_page = (self.initial_default_navigation
                            and not tab["url"] and not tab["loaded"])
        self.initial_default_navigation = False
        if use_default_page:
            self._load_url(tab, "https://www.google.com")
        else:
            self._ensure_tab_loaded(tab)

    def refresh_theme(self):
        self.root.setbackgroundcolor(palette.window_content)
        self.toolbar.setbackgroundcolor(palette.surface_light)
        self.address.setbackgroundcolor(palette.surface)
        self.address.settextcolor(palette.text)
        self.status.settextcolor(palette.muted)
        for action, button in self.button_views.items():
            button.setbackgroundcolor(palette.surface_light)
            button.settextcolor(palette.text)
        self.download_button.setbackgroundcolor(
            palette.accent if self.downloads_visible else palette.surface_light)
        self.download_panel.setbackgroundcolor(palette.window_content)
        self.download_header.setbackgroundcolor(palette.surface_light)
        self.download_note.settextcolor(palette.muted)
        self._render_downloads()
        for tab_id, tab in self.tabs.items():
            tab["row"].setbackgroundcolor(
                palette.surface_light if tab_id == self.active_tab else palette.surface)
            tab["button"].settextcolor(palette.text)
            tab["close"].settextcolor(palette.muted)
        self._refresh_home_page()

    def _build_toolbar(self):
        controls = [
            ("back", "‹", 38),
            ("forward", "›", 38),
            ("reload", "↻", 38),
            ("home", "⌂", 38),
            ("address", None, 0),
            ("go", "go", 42),
            ("bookmark", "☆", 38),
            ("devtools", "dev", 42),
            ("javascript", "js", 38),
        ]
        if self.rtl:
            controls.reverse()
        specifications = []
        for action, label, width in controls:
            if action == "address":
                specifications.append(text_input(
                    "", key="browser.address", width=0, height=40, weight=1,
                    margins={"left": 3, "right": 3},
                    background=palette.surface, textColor=palette.text,
                    textSize=14, gravity="start_center_vertical"))
                continue

            view = button(label, key=f"browser.{action}", style="browser.control",
                          width=width,
                          textSize=20 if action in ("back", "forward", "reload", "home",
                                                    "bookmark") else 14,
                          enabled=action not in ("back", "forward"))
            if action == "bookmark":
                view["states"] = {
                    "saved": {"text": "★", "textColor": palette.accent},
                    "unsaved": {"text": "☆", "textColor": palette.text},
                }
                view["state"] = "unsaved"
            specifications.append(view)

        self.toolbar_layout = create_layout(self.activity, self.toolbar, specifications, {
            "browser.control": {"height": 40, "margins": {"left": 2, "right": 2},
                                "background": palette.surface_light,
                                "textColor": palette.text},
        })
        for action, _label, _width in controls:
            if action == "address":
                continue
            view = self.toolbar_layout[f"browser.{action}"]
            self.buttons[view.id] = (action, None)
            self.button_views[action] = view
            if action == "javascript":
                self.javascript_button = view
            elif action == "bookmark":
                self.bookmark_button = view

        self.address = self.toolbar_layout["browser.address"]

    def _build_download_panel(self):
        header = tg.LinearLayout(self.activity, self.download_panel, vertical=False)
        self.download_header = header
        header.setdimensions(tg.View.MATCH_PARENT, 48)
        header.setlinearlayoutparams(0)
        header.setbackgroundcolor(palette.surface_light)

        title = tg.TextView(self.activity, "downloads", header)
        title.setdimensions(0, 48)
        title.setlinearlayoutparams(1)
        title.setmargin(12, "left")
        title.settextsize(16)
        title.settextcolor(palette.text)
        title.setgravity(0, 1)

        self._button(header, "clear list", "clear_downloads", 88)
        self._button(header, "done", "close_downloads", 64)

        self.download_scroll = tg.NestedScrollView(
            self.activity, self.download_panel, fillviewport=False, nobar=True)
        self.download_scroll.setheight(0)
        self.download_scroll.setlinearlayoutparams(1)
        self.download_rows = tg.LinearLayout(self.activity, self.download_scroll,
                                             vertical=True)
        self.download_rows.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)

        note = tg.TextView(
            self.activity,
            "queued in Android Downloads; progress appears in its notification",
            self.download_panel)
        self.download_note = note
        note.setdimensions(tg.View.MATCH_PARENT, 40)
        note.setlinearlayoutparams(0)
        note.setmargin(10, "left")
        note.setmargin(10, "right")
        note.settextsize(11)
        note.settextcolor(palette.muted)
        note.setgravity(0, 1)
        self._render_downloads()

    def _render_downloads(self):
        self.download_rows.clearchildren()
        if not self.downloads:
            empty = tg.TextView(self.activity, "no downloads yet", self.download_rows)
            empty.setdimensions(tg.View.MATCH_PARENT, 52)
            empty.setlinearlayoutparams(0)
            empty.setmargin(12, "left")
            empty.settextsize(14)
            empty.settextcolor(palette.muted)
            empty.setgravity(0, 1)
            return

        for item in self.downloads:
            row_view = tg.LinearLayout(self.activity, self.download_rows, vertical=True)
            row_view.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
            row_view.setlinearlayoutparams(0)
            row_view.setmargin(4, "left")
            row_view.setmargin(4, "right")
            row_view.setmargin(3, "top")
            row_view.setmargin(3, "bottom")
            row_view.setbackgroundcolor(palette.surface)
            name = tg.TextView(self.activity, item["name"], row_view)
            name.setdimensions(tg.View.MATCH_PARENT, 28)
            name.setlinearlayoutparams(0)
            name.setmargin(8, "left")
            name.settextsize(14)
            name.settextcolor(palette.text)
            name.setgravity(0, 1)
            source = urlsplit(item["url"]).hostname or item["url"]
            state = (f"failed: {item.get('error', 'download could not start')}"
                     if item.get("state") == "failed" else "queued in Downloads")
            detail = tg.TextView(self.activity, f"{source} · {state}", row_view)
            detail.setdimensions(tg.View.MATCH_PARENT, 24)
            detail.setlinearlayoutparams(0)
            detail.setmargin(8, "left")
            detail.settextsize(11)
            detail.settextcolor(palette.muted)
            detail.setgravity(0, 1)

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
            return [], [], [], [], 0
        except (OSError, json.JSONDecodeError) as error:
            print(f"nexa browser: couldn't read saved data: {error}")
            return [], [], [], [], 0

        if not isinstance(data, dict):
            return [], [], [], [], 0
        saved_history = data.get("history", [])
        saved_bookmarks = data.get("bookmarks", [])
        if not isinstance(saved_history, list):
            saved_history = []
        if not isinstance(saved_bookmarks, list):
            saved_bookmarks = []
        history = [url for url in saved_history if self._is_web_url(url)][:100]
        bookmarks = list(dict.fromkeys(
            url for url in saved_bookmarks if self._is_web_url(url)))
        saved_downloads = data.get("downloads", [])
        if not isinstance(saved_downloads, list):
            saved_downloads = []
        downloads = []
        for item in saved_downloads:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            name = item.get("name")
            if not self._is_web_url(url) or not isinstance(name, str) or not name.strip():
                continue
            download_id = item.get("id")
            if type(download_id) is not int:
                download_id = None
            record = {"id": download_id, "url": url, "name": name.strip()[:240],
                      "state": "failed" if item.get("state") == "failed" else "queued"}
            error = item.get("error")
            if record["state"] == "failed" and isinstance(error, str):
                record["error"] = error[:180]
            downloads.append(record)
        downloads = downloads[-50:]
        session = data.get("session", {})
        session = session if isinstance(session, dict) else {}
        saved_tabs = session.get("tabs", [])
        if not isinstance(saved_tabs, list):
            saved_tabs = []
        active = session.get("active", 0)
        if type(active) is not int:
            active = 0
        tabs = []
        selected = None
        for index, url in enumerate(saved_tabs):
            if not self._is_web_url(url):
                continue
            if index == active:
                selected = len(tabs)
            tabs.append(url)
        active = selected if selected is not None else 0
        return history, bookmarks, downloads, tabs, active

    def _save_data(self):
        directory = os.path.dirname(DATA_PATH)
        try:
            os.makedirs(directory, exist_ok=True)
            temporary = f"{DATA_PATH}.tmp"
            with open(temporary, "w", encoding="utf-8") as data_file:
                tabs = [tab for tab in self.tabs.values()
                        if not tab["is_devtools"] and self._is_web_url(tab["url"])]
                active_tab = self.tabs.get(self.active_tab)
                if active_tab is not None and active_tab["is_devtools"]:
                    active_tab = self.tabs.get(active_tab["devtools_target_tab_id"])
                active = next((index for index, tab in enumerate(tabs)
                               if tab is active_tab), 0)
                json.dump({"history": self.history[:100], "bookmarks": self.bookmarks,
                           "downloads": self.downloads[-50:],
                           "session": {"tabs": [tab["url"] for tab in tabs],
                                       "active": active}},
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

    def _new_tab(self, initial_url=None, javascript=False, defer_load=False):
        self.next_tab_id += 1
        tab_id = self.next_tab_id
        tab_layout = self.tab_template.create(self.tab_list)
        row_view = tab_layout["row"]
        tab_button = tab_layout["button"]
        close_button = tab_layout["close"]

        tab = {"id": tab_id, "layout": tab_layout, "row": row_view,
               "button": tab_button, "close": close_button,
               "url": "", "stack": [], "index": -1, "javascript": javascript,
               "loaded": False,
               "is_devtools": False, "devtools_target_tab_id": None,
               "devtools_tab_id": None}
        if initial_url:
            tab["url"] = initial_url
            self._push_url(tab, initial_url)
        self.tabs[tab_id] = tab
        self.buttons[tab_button.id] = ("select_tab", tab_id)
        self.buttons[close_button.id] = ("close_tab", tab_id)
        tab["webview"] = None
        self._select_tab(tab_id, load=not defer_load)
        if initial_url:
            if tab_id == self.active_tab:
                self.address.settext(initial_url)
                self._update_tab_label(tab)
        elif not defer_load:
            self._load_url(tab, "https://www.google.com")
        return tab_id

    def _ensure_tab_view(self, tab):
        if tab["webview"] is not None:
            return tab["webview"]
        webview = tg.WebView(self.activity, self.pages,
                             enable_javascript=tab["javascript"])
        webview.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self._set_desktop_mode(webview)
        webview.allownavigation(True)
        tab["webview"] = webview
        self.webviews[webview.id] = tab["id"]
        webview.setvisibility(
            tg.View.VISIBLE if tab["id"] == self.active_tab and not self.downloads_visible
            else tg.View.GONE)
        return webview

    def _set_desktop_mode(self, webview):
        self.activity.c.send_msg({
            "method": "setDesktopMode",
            "params": {"aid": self.activity.aid, "id": webview.id, "enabled": True},
        })

    def _select_tab(self, tab_id, load=True):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        self.downloads_visible = False
        self.download_panel.setvisibility(tg.View.GONE)
        self.download_button.setbackgroundcolor(palette.surface_light)
        self.active_tab = tab_id
        for current_id, current in self.tabs.items():
            webview = current["webview"]
            if webview is not None:
                webview.setvisibility(
                    tg.View.VISIBLE if current_id == tab_id else tg.View.GONE)
            current["row"].setbackgroundcolor(
                palette.surface_light if current_id == tab_id else palette.surface)
        if load:
            self._ensure_tab_loaded(tab)
        self.address.settext(tab["url"])
        self.javascript_button.settext("js on" if tab["javascript"] else "js")
        self._set_bookmark_state(tab["url"])
        self._update_navigation_buttons(tab)
        self.status.settext("ready" if tab["url"] else "new tab")

    def _ensure_tab_loaded(self, tab):
        if tab["loaded"]:
            return
        webview = self._ensure_tab_view(tab)
        if tab["url"]:
            webview.loaduri(tab["url"])
        else:
            webview.setdata(self._home_page())
        tab["loaded"] = True

    def _toggle_downloads(self):
        if self.downloads_visible:
            self._select_tab(self.active_tab)
            return
        for tab in self.tabs.values():
            if tab["webview"] is not None:
                tab["webview"].setvisibility(tg.View.GONE)
        self._render_downloads()
        self.downloads_visible = True
        self.download_panel.setvisibility(tg.View.VISIBLE)
        self.download_button.setbackgroundcolor(palette.accent)
        self.status.settext(f"{len(self.downloads)} downloads")

    def _record_download(self, value):
        url = value.get("url", "")
        name = value.get("fileName", "download")
        if not self._is_web_url(url) or not isinstance(name, str) or not name.strip():
            self.status.settext("download started")
            return
        download_id = value.get("downloadId")
        if type(download_id) is not int:
            download_id = None
        if download_id is not None and any(item["id"] == download_id
                                           for item in self.downloads):
            return
        self.downloads.insert(0, {"id": download_id, "url": url,
                                  "name": name.strip()[:240], "state": "queued"})
        self.downloads = self.downloads[:50]
        self._save_data()
        self._refresh_home_page()
        if self.downloads_visible:
            self._render_downloads()
        self.status.settext(f"download started: {name[:64]}")

    def _record_download_error(self, value):
        url = value.get("url", "")
        if not self._is_web_url(url):
            self.status.settext(f"download failed: {value.get('error', 'unknown error')}")
            return
        name = value.get("fileName")
        parsed = urlsplit(url)
        if not isinstance(name, str) or not name.strip():
            name = parsed.path.rsplit("/", 1)[-1] or parsed.hostname or "download"
        error = str(value.get("error", "download could not start"))[:180]
        self.downloads.insert(0, {"id": None, "url": url, "name": name[:240],
                                  "state": "failed", "error": error})
        self.downloads = self.downloads[:50]
        self._save_data()
        self._refresh_home_page()
        if self.downloads_visible:
            self._render_downloads()
        self.status.settext(f"download failed: {error[:64]}")

    def _close_tab(self, tab_id):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        if tab["devtools_tab_id"] in self.tabs:
            self._close_tab(tab["devtools_tab_id"])
        if tab["is_devtools"]:
            target = self.tabs.get(tab["devtools_target_tab_id"])
            if target is not None and target["webview"] is not None:
                target["devtools_tab_id"] = None
                target["webview"].close_devtools()
        was_active = tab_id == self.active_tab
        webview = tab["webview"]
        if webview is not None:
            self.webviews.pop(webview.id, None)
        self.buttons.pop(tab["button"].id, None)
        self.buttons.pop(tab["close"].id, None)
        if webview is not None:
            webview.delete()
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
                self.toolbar_layout.set(
                    f"browser.{action}", enabled=enabled,
                    textColor=palette.text if enabled else palette.muted)

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
        tab["url"] = url
        tab["loaded"] = True
        if record:
            self._save_data()
        self._ensure_tab_view(tab).loaduri(url)
        if tab["id"] == self.active_tab:
            self.address.settext(url)
            self._set_bookmark_state(url)
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
        self._save_data()

    def _forward(self):
        tab = self.tabs[self.active_tab]
        if tab["index"] >= len(tab["stack"]) - 1:
            return
        tab["index"] += 1
        self._load_url(tab, tab["stack"][tab["index"]], record=False)
        self._save_data()

    def _reload(self):
        tab = self.tabs[self.active_tab]
        if tab["url"]:
            self._load_url(tab, tab["url"], record=False)
        else:
            self._ensure_tab_view(tab).setdata(self._home_page())
            tab["loaded"] = True

    def _go_home(self):
        tab = self.tabs[self.active_tab]
        tab["url"] = ""
        tab["stack"] = []
        tab["index"] = -1
        tab["loaded"] = True
        self._ensure_tab_view(tab).setdata(self._home_page())
        self.address.settext("")
        self._set_bookmark_state("")
        self._update_tab_label(tab)
        self._update_navigation_buttons(tab)
        self.status.settext("new tab")
        self._save_data()

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
        self._set_bookmark_state(url)
        self._refresh_home_page()

    def _set_bookmark_state(self, url):
        self.toolbar_layout.set_state(
            "browser.bookmark", "saved" if url in self.bookmarks else "unsaved")

    def _refresh_home_page(self):
        for tab in self.tabs.values():
            if not tab["url"] and tab["webview"] is not None:
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

    def _open_devtools(self):
        tab = self.tabs[self.active_tab]
        if tab["is_devtools"]:
            self.status.settext("devtools is already open in this tab")
            return
        if tab["devtools_tab_id"] in self.tabs:
            self._select_tab(tab["devtools_tab_id"])
            return
        if not tab["url"]:
            self.status.settext("open a page before opening devtools")
            return
        try:
            frontend_url = tab["webview"].get_devtools_url()
        except Exception as error:
            self.status.settext(f"devtools failed: {error}")
            return
        if not frontend_url:
            self.status.settext("webview devtools is unavailable")
            return
        target_tab_id = tab["id"]
        devtools_id = self._new_tab(initial_url=frontend_url, javascript=True)
        devtools_tab = self.tabs[devtools_id]
        devtools_tab["is_devtools"] = True
        devtools_tab["devtools_target_tab_id"] = target_tab_id
        devtools_tab["layout"].set("button", text="devtools")
        tab["devtools_tab_id"] = devtools_id
        self._select_tab(devtools_id)
        self.status.settext("webview devtools")

    def _update_tab_label(self, tab):
        host = urlsplit(tab["url"]).hostname if tab["url"] else None
        label = host.removeprefix("www.") if host else "new tab"
        tab["layout"].set("button", text=label[:14])

    def _home_page(self):
        bookmarks = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(urlsplit(url).hostname or url)}</a></li>'
            for url in self.bookmarks) or "<li>no bookmarks yet</li>"
        history = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></li>'
            for url in self.history) or "<li>your recent pages will show here</li>"
        downloads = "".join(
            f'<li>{html.escape(item["name"])} · {html.escape(urlsplit(item["url"]).hostname or item["url"])}'
            f'{" · failed" if item.get("state") == "failed" else ""}</li>'
            for item in self.downloads[:5]) or "<li>no downloads yet</li>"
        return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ background:#{palette.window_content & 0xFFFFFF:06x}; color:#{palette.text & 0xFFFFFF:06x}; font:16px sans-serif; margin:24px; }}
h1 {{ color:#{palette.accent & 0xFFFFFF:06x}; font-size:26px; }} h2 {{ font-size:17px; margin-top:28px; }}
form {{ display:flex; gap:8px; }} input {{ box-sizing:border-box; padding:12px; font-size:16px; }}
input[name=q] {{ flex:1; min-width:0; }} button {{ padding:10px 16px; }}
a {{ color:#{palette.accent & 0xFFFFFF:06x}; overflow-wrap:anywhere; }} li {{ margin:10px 0; }}
</style></head><body><h1>Nexa Browser</h1>
<form action="https://duckduckgo.com/"><input name="q" placeholder="search the web" autocomplete="off"><button>search</button></form>
<h2>recent downloads</h2><ul>{downloads}</ul>
<h2>bookmarks</h2><ul>{bookmarks}</ul>
<h2>recent pages</h2><ul>{history}</ul></body></html>"""

    def _handle_webview_event(self, event):
        event_id = getattr(event, "id", None)
        tab_id = self.webviews.get(event_id) if event_id is not None else None
        if tab_id is None and event.type != tg.Event.webviewDestroyed:
            # Some Nexa WebView events omit the view id; only the visible
            # tab can safely own an unscoped navigation or load result.
            tab_id = self.active_tab
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
        tab["webview"] = None
        tab["javascript"] = False
        tab["loaded"] = False
        if tab["id"] == self.active_tab:
            self._ensure_tab_loaded(tab)

    def handle_event(self, event):
        if event.type == tg.Event.back:
            if self.downloads_visible:
                self._select_tab(self.active_tab)
                return True
            self._back()
            return True
        if event.type in (tg.Event.webviewNavigation, tg.Event.webviewProgress,
                          tg.Event.webviewHTTPError, tg.Event.webviewError,
                          tg.Event.webviewDestroyed):
            return self._handle_webview_event(event)
        if event.type == tg.Event.webviewDownload:
            self._record_download(event.value)
            return True
        if event.type == tg.Event.webviewDownloadError:
            self._record_download_error(event.value)
            return True
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
        elif action == "devtools":
            self._open_devtools()
        elif action == "downloads":
            self._toggle_downloads()
        elif action == "clear_downloads":
            self.downloads = []
            self._save_data()
            self._render_downloads()
            self._refresh_home_page()
            self.status.settext("download list cleared")
        elif action == "close_downloads":
            self._select_tab(self.active_tab)
        elif action == "new_tab":
            self._new_tab()
            self._save_data()
        elif action == "select_tab":
            self._select_tab(value)
            self._save_data()
        elif action == "close_tab":
            self._close_tab(value)
            self._save_data()
        return True


APP = BrowserApp()
