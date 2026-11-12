import termuxgui as tg

from shell import palette
from shell.icons import load
from shell.surface import set_image_scale_type
from shell.settings_registry import get_pages, register_page


register_page("system", "System", "window")
register_page("personalization", "Personalization", None)
register_page("apps", "Apps", "apps")
register_page("time", "Time & language", "clock")
register_page("updates", "Nexa updates", None)


class SettingsApp:
    app_id = "settings"
    name = "Settings"
    icon = "settings"
    preload_ui = False
    build_threaded = True

    def build(self, activity, parent, context=None):
        self.activity = activity
        self.pages = get_pages()
        self.page_by_id = {page["id"]: page for page in self.pages}
        self.current_page = None
        self.category_buttons = {}
        self.page_handlers = []

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

        self.scroll = tg.NestedScrollView(activity, self.root, fillviewport=True, nobar=True)
        self.scroll.setheight(0)
        self.scroll.setlinearlayoutparams(1)
        self.content = tg.LinearLayout(activity, self.scroll)
        self.content.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        self.content.setmargin(8, "left")
        self.content.setmargin(8, "right")
        self._show_home()
        return self

    def _show_home(self):
        self.current_page = None
        self.category_buttons = {}
        self.header_button.settext("")
        self.header_button.setbackgroundcolor(0x00000000)
        self.heading.settext("Settings")
        self.content.clearchildren()
        self.page_handlers = []

        for page in self.pages:
            self._add_category_button(self.content, page)

    def _add_category_button(self, parent, page):
        button = tg.LinearLayout(self.activity, parent, vertical=False)
        button.setdimensions(tg.View.MATCH_PARENT, 56)
        button.setlinearlayoutparams(0)
        button.setmargin(3, "top")
        button.setmargin(3, "bottom")
        button.setbackgroundcolor(palette.surface)
        button.sendclickevent(True)
        icon = page["icon"]
        if icon is not None:
            image = tg.ImageView(self.activity, button)
            image.setdimensions(40, 40)
            image.setlinearlayoutparams(0)
            image.setmargin(8, "left")
            image.setmargin(8, "right")
            image.setimage(load(icon))
            set_image_scale_type(image, "fit_center")
        label = tg.TextView(self.activity, page["title"], button)
        label.setdimensions(0, 56)
        label.setlinearlayoutparams(1)
        label.setmargin(64 if icon is None else 8, "left")
        label.settextsize(14)
        label.settextcolor(palette.text)
        label.setgravity(0, 1)
        self.category_buttons[button.id] = page["id"]

    def _show_page(self, page_id):
        page = self.page_by_id.get(page_id)
        if page is None:
            return
        self.current_page = page_id
        self.header_button.settext("←")
        self.header_button.setbackgroundcolor(palette.surface)
        self.heading.settext(page["title"])
        self.content.clearchildren()
        self.page_handlers = []
        for builder in page["content"]:
            handler = builder(self.activity, self.content)
            if handler is not None:
                self.page_handlers.append(handler)

    def handle_event(self, event):
        if event.type == tg.Event.click:
            if event.id == self.header_button.id and self.current_page is not None:
                self._show_home()
                return True
            page_id = self.category_buttons.get(event.id)
            if page_id is not None:
                self._show_page(page_id)
                return True
        for handler in self.page_handlers:
            handle_event = getattr(handler, "handle_event", None)
            if handle_event is not None and handle_event(event):
                return True
        return False


APP = SettingsApp()
