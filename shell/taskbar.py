from datetime import datetime

import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.icons import load
from shell.surface import add as add_surface, set_color as set_surface_color


class Taskbar:
    height = 56
    menu_button_width = 56
    clock_width = 72
    window_width = 56

    def __init__(self, activity, root, apps, open_app, activate_window, screen_width,
                 screen_height, rtl=False):
        self.activity = activity
        self.open_app = open_app
        self.activate_window = activate_window
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.rtl = rtl
        self.density = activity.getconfiguration()["density"]
        self.buttons = {}
        self.window_views = {}
        self.window_spacer = None
        self.menu_visible = False

        self.frame = tg.FrameLayout(activity, root)
        self.frame.setdimensions(tg.View.MATCH_PARENT, self.height)
        place(self.frame, 0, screen_height - self.height, top=True)
        self.surface = add_surface(activity, self.frame, screen_width, self.height, palette.surface)

        self.view = tg.LinearLayout(activity, self.frame, vertical=False)
        self.view.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        if rtl:
            self._add_clock()
            self._add_window_scroll()
            self._add_menu_button()
        else:
            self._add_menu_button()
            self._add_window_scroll()
            self._add_clock()

        menu_height = min(500, max(160, screen_height - self.height - 24))
        self.menu_height = menu_height
        self.app_menu_width = min(380, max(1, screen_width - 16))
        self.menu_frame = tg.FrameLayout(activity, root)
        self.menu_frame.setdimensions(self.app_menu_width, menu_height)
        place(self.menu_frame, 8, screen_height - self.height - menu_height - 8)
        self.menu_surface = add_surface(activity, self.menu_frame, self.app_menu_width, menu_height,
                                        palette.surface)
        self.menu = tg.LinearLayout(activity, self.menu_frame)
        self.menu.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        header = tg.LinearLayout(activity, self.menu, vertical=False)
        header.setdimensions(tg.View.MATCH_PARENT, 72)
        header.setlinearlayoutparams(0)
        header.setbackgroundcolor(palette.surface_light)
        heading = tg.TextView(activity, "\u2003All apps", header)
        heading.setdimensions(tg.View.MATCH_PARENT, 72)
        heading.setlinearlayoutparams(0)
        heading.settextsize(20)
        heading.settextcolor(palette.text)
        heading.setgravity(0, 1)
        divider = tg.LinearLayout(activity, self.menu)
        divider.setdimensions(tg.View.MATCH_PARENT, 1)
        divider.setlinearlayoutparams(0)
        divider.setbackgroundcolor(palette.accent)
        scroll = tg.NestedScrollView(activity, self.menu, fillviewport=True, nobar=True)
        scroll.setheight(0)
        scroll.setlinearlayoutparams(1)
        entries = tg.LinearLayout(activity, scroll)
        entries.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        sorted_apps = sorted(apps.values(), key=lambda app: app.name.casefold())
        previous_letter = None
        for app in sorted_apps:
            letter = app.name[:1].upper()
            if letter != previous_letter:
                self._add_app_heading(entries, letter)
                previous_letter = letter
            self._add_app_entry(entries, app)
        self.menu_frame.setvisibility(tg.View.GONE)

    def resize(self, screen_width, screen_height):
        self.screen_width = max(1, screen_width)
        self.screen_height = max(1, screen_height)
        self.frame.setdimensions(tg.View.MATCH_PARENT, self.height)
        place(self.frame, 0, self.screen_height - self.height, top=True)
        set_surface_color(self.surface, self.screen_width, self.height, palette.surface)
        self._layout_window_buttons()

        self.app_menu_width = min(380, max(1, self.screen_width - 16))
        self.menu_height = min(500, max(160, self.screen_height - self.height - 24))
        self.menu_frame.setdimensions(self.app_menu_width, self.menu_height)
        place(self.menu_frame, 8, self.screen_height - self.height - self.menu_height - 8,
              top=self.menu_visible)
        set_surface_color(self.menu_surface, self.app_menu_width, self.menu_height, palette.surface)
    def _add_window_scroll(self):
        self.window_scroll = tg.HorizontalScrollView(self.activity, self.view, nobar=True)
        self.window_scroll.setdimensions(0, tg.View.MATCH_PARENT)
        self.window_scroll.setlinearlayoutparams(1)
        self.window_list = tg.LinearLayout(self.activity, self.window_scroll, vertical=False)
        self.window_list.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        if self.rtl:
            self.window_spacer = tg.LinearLayout(self.activity, self.window_list)
            self.window_spacer.setdimensions(0, self.height)
            self.window_spacer.setlinearlayoutparams(0, 0)

    def _layout_window_buttons(self):
        viewport_width = max(0, self.screen_width - self.clock_width - self.menu_button_width)
        content_width = max(viewport_width, len(self.window_views) * self.window_width)
        self.window_list.setdimensions(content_width, self.height)
        if self.window_spacer is not None:
            self.window_spacer.setdimensions(
                content_width - len(self.window_views) * self.window_width, self.height)
            offset = round(max(0, content_width - viewport_width) * self.density)
            self.window_scroll.setscrollposition(offset, 0, smooth=False)

    def _add_clock(self):
        self.clock = tg.TextView(self.activity, datetime.now().strftime("%H:%M"), self.view)
        self.clock.settextcolor(palette.text)
        self.clock.setgravity(1, 1)
        self.clock.setdimensions(self.clock_width, self.height)
        self.clock.setlinearlayoutparams(0)

    def _add_menu_button(self):
        button = tg.LinearLayout(self.activity, self.view, vertical=True)
        button.setdimensions(self.menu_button_width, self.height)
        button.setlinearlayoutparams(0)
        button.setbackgroundcolor(palette.surface_light)
        button.sendclickevent(True)
        image = tg.ImageView(self.activity, button)
        image.setdimensions(28, 28)
        image.setimage(load("apps"))
        image.setmargin((self.menu_button_width - 28) // 2, "left")
        image.setmargin((self.height - 28) // 2, "top")
        image.setlinearlayoutparams(0)
        self.buttons[button.id] = ("menu", None)

    def _add_app_heading(self, parent, letter):
        heading = tg.TextView(self.activity, "\u2003" + letter, parent)
        heading.setdimensions(tg.View.MATCH_PARENT, 38)
        heading.setlinearlayoutparams(0)
        heading.setmargin(14, "top")
        heading.settextsize(14)
        heading.settextcolor(palette.muted)
        heading.setgravity(0, 1)

    def _add_app_entry(self, parent, app):
        button = tg.LinearLayout(self.activity, parent, vertical=False)
        button.setdimensions(tg.View.MATCH_PARENT, 56)
        button.setlinearlayoutparams(0)
        button.setmargin(8, "left")
        button.setmargin(8, "right")
        button.setmargin(3, "top")
        button.setmargin(3, "bottom")
        button.setbackgroundcolor(palette.window_content)
        button.sendclickevent(True)
        self.buttons[button.id] = ("app", app.app_id)

        def add_icon():
            image = tg.ImageView(self.activity, button)
            image.setdimensions(28, 28)
            image.setimage(load(app.icon))
            image.setmargin(12, "left")
            image.setmargin(4, "right")
            image.setmargin(14, "top")
            image.setmargin(14, "bottom")
            image.setlinearlayoutparams(0)

        def add_label():
            label = tg.TextView(self.activity, app.name, button)
            label.setheight(56)
            label.setlinearlayoutparams(1)
            label.setmargin(12, "left")
            label.settextsize(15)
            label.settextcolor(palette.text)
            label.setgravity(0, 1)

        if self.rtl:
            add_label()
            add_icon()
        else:
            add_icon()
            add_label()

    def add_window(self, window_id, app, title=None):
        button = tg.LinearLayout(self.activity, self.window_list, vertical=False)
        button.setdimensions(self.window_width, self.height)
        button.setlinearlayoutparams(0, 1 if self.rtl else None)
        button.setbackgroundcolor(palette.surface)
        button.sendclickevent(True)
        image = tg.ImageView(self.activity, button)
        image.setdimensions(28, 28)
        image.setbackgroundcolor(0xFFE8EDF2)
        image.setimage(load(app.icon))
        image.setmargin((self.window_width - 28) // 2, "left")
        image.setmargin((self.height - 28) // 2, "top")
        image.setlinearlayoutparams(0)
        self.buttons[button.id] = ("window", window_id)
        self.window_views[window_id] = button
        self._layout_window_buttons()

    def set_active_window(self, window_id):
        for current_id, button in self.window_views.items():
            button.setbackgroundcolor(palette.surface_light if current_id == window_id else palette.surface)

    def remove_window(self, window_id):
        view = self.window_views.pop(window_id, None)
        if view is None:
            return
        self.buttons.pop(view.id, None)
        view.delete()
        self._layout_window_buttons()

    def dismiss_menu(self):
        if not self.menu_visible:
            return
        self.menu_visible = False
        self.menu_frame.setvisibility(tg.View.GONE)

    def handle_event(self, event):
        if event.type == tg.Event.back and self.menu_visible:
            self.dismiss_menu()
            return True
        if event.type != tg.Event.click or event.id not in self.buttons:
            return False
        self.clock.settext(datetime.now().strftime("%H:%M"))
        action, value = self.buttons[event.id]
        if action == "menu":
            self.menu_visible = not self.menu_visible
            self.menu_frame.setvisibility(tg.View.VISIBLE if self.menu_visible else tg.View.GONE)
            if self.menu_visible:
                place(self.menu_frame, 8, self.screen_height - self.height - self.menu_height - 8, top=True)
            return True

        self.dismiss_menu()
        if action == "app":
            self.open_app(value)
        else:
            self.activate_window(value)
        return True
