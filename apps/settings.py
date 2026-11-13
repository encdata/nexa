import termuxgui as tg

from shell import palette
from shell.icons import load
from shell.settings_registry import get_pages, register_page
from shell.ui_layout import column, create_layout, icon, row, scroll, text_input
from shell.wallpaper import load as load_wallpaper


register_page("system", "System", "window")
register_page("personalization", "Personalization", None)
register_page("apps", "Apps", "apps")
register_page("time", "Time & language", "clock")
register_page("updates", "Nexa updates", None)


class SettingsApp:
    app_id = "settings"
    name = "Settings"
    icon = "settings"
    preload_ui = True
    build_threaded = True

    def build_with_services(self, activity, parent, context, services):
        self.services = services
        return self.build(activity, parent, context)

    def build(self, activity, parent, context=None):
        page_to_restore = getattr(self, "current_page", None)
        self.activity = activity
        self.pages = get_pages()
        self.page_by_id = {page["id"]: page for page in self.pages}
        self.current_page = None
        self.category_layout = None
        self.category_keys = {}
        self.page_handlers = []
        if not hasattr(self, "services"):
            self.services = {}
        self.personalization_actions = {}

        self.root = tg.LinearLayout(activity, parent)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.root.setbackgroundcolor(palette.window_content)

        self.header = tg.LinearLayout(activity, self.root, vertical=False)
        self.header.setdimensions(tg.View.MATCH_PARENT, 56)
        self.header.setlinearlayoutparams(0)
        self.header.setbackgroundcolor(palette.window_content)
        self.header_button = tg.TextView(activity, "", self.header)
        self.header_button.setdimensions(52, 56)
        self.header_button.settextsize(20)
        self.header_button.settextcolor(palette.text)
        self.header_button.setgravity(1, 1)
        self.header_button.sendclickevent(True)

        self.heading = tg.TextView(activity, "Settings", self.header)
        self.heading.setdimensions(0, 56)
        self.heading.setlinearlayoutparams(1)
        self.heading.settextsize(22)
        self.heading.settextcolor(palette.text)
        self.heading.setgravity(0, 1)

        self.search_layout = create_layout(activity, self.root, row(
            icon(load("search"), key="settings.search.icon", width=20, height=20,
                 margins={"left": 10, "right": 8}, scaleType="fit_center"),
            text_input("", key="settings.search", width=0, height=40, weight=1,
                       background=palette.surface, padding={"left": 8, "right": 8},
                       textColor=palette.text, textSize=14,
                       gravity="start_center_vertical",
                       on_text=lambda event: self._filter_categories(
                           event.value.get("text", ""))),
            width=tg.View.MATCH_PARENT, height=44,
            margins={"left": 8, "right": 8, "bottom": 4},
        ))
        self.search = self.search_layout["settings.search"]

        self.body_layout = create_layout(activity, self.root, scroll(
            column(key="settings.content", width=tg.View.MATCH_PARENT,
                   height=tg.View.WRAP_CONTENT,
                   margins={"left": 8, "right": 8}),
            key="settings.scroll", width=tg.View.MATCH_PARENT, height=0,
            weight=1, fillViewport=True, nobar=True,
        ))
        self.scroll = self.body_layout["settings.scroll"]
        self.content = self.body_layout["settings.content"]
        self.category_keys = {}
        self._show_home()
        if page_to_restore in self.page_by_id:
            self._show_page(page_to_restore)
        return self

    def _show_home(self):
        self.current_page = None
        self.header_button.settext("")
        self.header_button.setbackgroundcolor(0x00000000)
        self.heading.settext("Settings")
        self.search.settext("")
        self.content.clearchildren()
        self.page_handlers = []

        categories = []
        for page in self.pages:
            key = f"category.{page['id']}"
            children = []
            if page["icon"] is not None:
                children.append({
                    "type": "image", "key": f"{key}.icon", "width": 40,
                    "height": 40, "margins": {"left": 8, "right": 8,
                                                  "top": 8, "bottom": 8},
                    "image": load(page["icon"]),
                })
            children.append({
                "type": "text", "width": 0, "height": 56, "weight": 1,
                "margins": {"left": 8 if page["icon"] is not None else 64},
                "text": page["title"], "textSize": 14,
                "textColor": palette.text, "gravity": "start_center_vertical",
            })
            categories.append({
                "type": "row", "key": f"{key}.button", "style": "category",
                "on_click": lambda _event, page_id=page["id"]: self._show_page(page_id),
                "children": children,
            })
            self.category_keys[page["id"]] = key + ".button"

        self.category_layout = create_layout(self.activity, self.content, categories, {
            "category": {"width": "MATCH_PARENT", "height": 56,
                         "margins": {"top": 3, "bottom": 3},
                         "background": palette.surface, "clickable": True},
        })

    def _filter_categories(self, query):
        if self.category_layout is None:
            return
        query = query.casefold().strip()
        for page in self.pages:
            key = self.category_keys[page["id"]]
            visible = not query or query in page["title"].casefold()
            self.category_layout.set(
                key, visibility=tg.View.VISIBLE if visible else tg.View.GONE)

    def _show_page(self, page_id):
        page = self.page_by_id.get(page_id)
        if page is None:
            return
        self.activity.hidesoftkeyboard()
        self.current_page = page_id
        self.category_layout = None
        self.search.settext("")
        self.header_button.settext("←")
        self.header_button.setbackgroundcolor(palette.surface)
        self.heading.settext(page["title"])
        self.content.clearchildren()
        self.page_handlers = []
        if page_id == "personalization":
            self._build_personalization()
            return
        for builder in page["content"]:
            handler = builder(self.activity, self.content)
            if handler is not None:
                self.page_handlers.append(handler)


    def _build_personalization(self):
        settings = self.services.get("personalization", {"theme": palette.current_theme,
                                                          "wallpaper": None})
        actions = {}
        self.personalization_actions = actions
        title = tg.TextView(self.activity, "theme", self.content)
        title.setdimensions(tg.View.MATCH_PARENT, 40)
        title.settextsize(13)
        title.settextcolor(palette.muted)
        title.setgravity(0, 1)

        theme_row = tg.LinearLayout(self.activity, self.content, vertical=False)
        theme_row.setdimensions(tg.View.MATCH_PARENT, 48)
        for theme, label in (("dark", "dark"), ("light", "light")):
            button = tg.TextView(self.activity, label, theme_row)
            button.setdimensions(0, 44)
            button.setlinearlayoutparams(1)
            button.setmargin(3, "left")
            button.setmargin(3, "right")
            button.setbackgroundcolor(palette.accent if settings["theme"] == theme
                                     else palette.surface)
            button.settextcolor(palette.text)
            button.settextsize(14)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            actions[button.id] = ("theme", theme)

        wallpaper_title = tg.TextView(self.activity, "wallpaper image path", self.content)
        wallpaper_title.setdimensions(tg.View.MATCH_PARENT, 40)
        wallpaper_title.setmargin(0, "top")
        wallpaper_title.settextsize(13)
        wallpaper_title.settextcolor(palette.muted)
        wallpaper_title.setgravity(0, 1)

        path = settings.get("wallpaper") or ""
        wallpaper_path = tg.EditText(self.activity, path, self.content,
                                     singleline=True, line=False, inputtype="text")
        wallpaper_path.setdimensions(tg.View.MATCH_PARENT, 44)
        wallpaper_path.setbackgroundcolor(palette.surface)
        wallpaper_path.settextcolor(palette.text)
        wallpaper_path.settextsize(13)
        wallpaper_path.setmargin(4, "top")

        preview = tg.ImageView(self.activity, self.content)
        preview.setdimensions(144, 86)
        preview.setvisibility(tg.View.GONE)
        preview.setmargin(4, "top")

        wallpaper_row = tg.LinearLayout(self.activity, self.content, vertical=False)
        wallpaper_row.setdimensions(tg.View.MATCH_PARENT, 48)
        apply_button = tg.TextView(self.activity, "apply wallpaper", wallpaper_row)
        apply_button.setdimensions(0, 44)
        apply_button.setlinearlayoutparams(1)
        apply_button.setmargin(3, "right")
        apply_button.setbackgroundcolor(palette.surface)
        apply_button.settextcolor(palette.text)
        apply_button.settextsize(13)
        apply_button.setgravity(1, 1)
        apply_button.sendclickevent(True)
        actions[apply_button.id] = ("wallpaper", None)
        preview_button = tg.TextView(self.activity, "preview", wallpaper_row)
        preview_button.setdimensions(78, 44)
        preview_button.setmargin(3, "left")
        preview_button.setbackgroundcolor(palette.surface)
        preview_button.settextcolor(palette.text)
        preview_button.settextsize(13)
        preview_button.setgravity(1, 1)
        preview_button.sendclickevent(True)
        actions[preview_button.id] = ("preview", None)
        default_button = tg.TextView(self.activity, "default", wallpaper_row)
        default_button.setdimensions(72, 44)
        default_button.setmargin(3, "left")
        default_button.setbackgroundcolor(palette.surface)
        default_button.settextcolor(palette.text)
        default_button.settextsize(13)
        default_button.setgravity(1, 1)
        default_button.sendclickevent(True)
        actions[default_button.id] = ("wallpaper", "default")

        self.wallpaper_path = wallpaper_path
        self.wallpaper_preview = preview

    def handle_event(self, event):
        if event.type == tg.Event.click and event.id in self.personalization_actions:
            action, value = self.personalization_actions[event.id]
            if action == "theme":
                callback = self.services.get("set_theme")
                if callback is not None:
                    callback(value)
                return True
            path = self.wallpaper_path.gettext().strip()
            if action == "preview":
                if not path:
                    self.activity.c.toast("enter an image path first")
                    return True
                configuration = self.activity.getconfiguration()
                try:
                    image = load_wallpaper(144, 86, configuration["density"], path)
                    self.wallpaper_preview.setimage(image["image"])
                    self.wallpaper_preview.setvisibility(tg.View.VISIBLE)
                except (OSError, ValueError) as error:
                    self.activity.c.toast(f"couldn't load wallpaper: {error}")
                return True
            callback = self.services.get("set_wallpaper")
            if value is None and not path:
                self.activity.c.toast("enter an image path first")
                return True
            try:
                callback(None if value == "default" else path)
                if value == "default":
                    self.wallpaper_path.settext("")
            except (OSError, ValueError) as error:
                self.activity.c.toast(f"couldn't load wallpaper: {error}")
            return True
        if self.search_layout.handle_event(event):
            return True
        if event.type == tg.Event.click:
            if event.id == self.header_button.id and self.current_page is not None:
                self._show_home()
                return True
            if self.category_layout is not None and self.category_layout.handle_event(event):
                return True
        for handler in self.page_handlers:
            handle_event = getattr(handler, "handle_event", None)
            if handle_event is not None and handle_event(event):
                return True
        return False


APP = SettingsApp()
