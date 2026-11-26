from datetime import datetime
from time import monotonic

import nexapip as tg

from shell import palette
from shell.geometry import place
from shell.icons import load
from shell.context_menu import ContextMenu
from shell.surface import add_rounded, set_rounded_color


class Taskbar:
    height = 56
    content_height = 48
    menu_button_width = 56
    clock_width = 72
    show_desktop_width = 32
    window_width = 56

    def __init__(self, activity, root, apps, open_app, activate_window, screen_width,
                 screen_height, rtl=False, context_action=None,
                 window_context_actions=None):
        self.activity = activity
        self.open_app = open_app
        self.activate_window = activate_window
        self.context_action = context_action
        self.window_context_actions = window_context_actions
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.rtl = rtl
        self.density = activity.getconfiguration()["density"]
        self.buttons = {}
        self.window_views = {}
        self.window_spacer = None
        self.window_content_width = 0
        self.window_viewport_width = 0
        self.show_desktop_line = None
        self.menu_visible = False
        self.active_window = None
        self.clock_text = None
        self.next_clock_check = 0

        self.frame = tg.FrameLayout(activity, root)
        self.frame.setdimensions(tg.View.MATCH_PARENT, self.height)
        place(self.frame, 0, screen_height - self.height, top=True)
        self.surface = add_rounded(activity, self.frame, max(1, screen_width - 16),
                                   self.content_height, palette.surface, 14)
        place(self.surface, 8, 4)

        self.view = tg.LinearLayout(activity, self.frame, vertical=False)
        self.view.setdimensions(max(1, screen_width - 16), self.content_height)
        self.view.sendlongclickevent(True)
        place(self.view, 8, 4)
        if rtl:
            self._add_show_desktop()
            self._add_clock()
            self._add_window_scroll()
            self._add_menu_button()
        else:
            self._add_menu_button()
            self._add_window_scroll()
            self._add_clock()
            self._add_show_desktop()

        menu_height = min(500, max(160, screen_height - self.height - 24))
        self.menu_height = menu_height
        self.app_menu_width = min(380, max(1, screen_width - 16))
        self.menu_frame = tg.FrameLayout(activity, root)
        self.menu_frame.setvisibility(tg.View.GONE)
        self.menu_frame.setdimensions(self.app_menu_width, menu_height)
        place(self.menu_frame, 8, screen_height - self.height - menu_height - 8)
        self.menu_surface = add_rounded(activity, self.menu_frame, self.app_menu_width,
                                        menu_height, palette.surface, 14)
        self.menu = tg.LinearLayout(activity, self.menu_frame)
        self.menu.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        header = tg.LinearLayout(activity, self.menu, vertical=False)
        header.setdimensions(tg.View.MATCH_PARENT, 72)
        header.setlinearlayoutparams(0)
        heading = tg.TextView(activity, "\u2003All apps", header)
        self.heading = heading
        heading.setdimensions(tg.View.MATCH_PARENT, 72)
        heading.setlinearlayoutparams(0)
        heading.settextsize(20)
        heading.settextcolor(palette.text)
        heading.setgravity(0, 1)
        search_row = tg.LinearLayout(activity, self.menu, vertical=False)
        search_row.setdimensions(tg.View.MATCH_PARENT, 44)
        search_row.setlinearlayoutparams(0)
        search_row.setmargin(8, "left")
        search_row.setmargin(8, "right")
        search_row.setbackgroundcolor(palette.window_content)
        search_icon = tg.ImageView(activity, search_row)
        search_icon.setdimensions(20, 20)
        search_icon.setimage(load("search"))
        search_icon.setmargin(10, "left")
        search_icon.setmargin(8, "right")
        search_icon.setmargin(12, "top")
        self.app_search = tg.EditText(activity, "", search_row, singleline=True,
                                      line=False, inputtype="text")
        self.app_search.setdimensions(0, 44)
        self.app_search.setlinearlayoutparams(1)
        self.app_search.setbackgroundcolor(palette.window_content)
        self.app_search.settextsize(14)
        self.app_search.settextcolor(palette.text)
        self.app_search.sendtextevent(True)
        divider = tg.LinearLayout(activity, self.menu)
        divider.setdimensions(tg.View.MATCH_PARENT, 1)
        divider.setlinearlayoutparams(0)
        divider.setbackgroundcolor(palette.accent)
        self.divider = divider
        self.menu_entries = []
        self.app_entries = []
        self.app_headings = {}
        scroll = tg.NestedScrollView(activity, self.menu, fillviewport=True, nobar=True)
        scroll.setheight(0)
        scroll.setlinearlayoutparams(1)
        entries = tg.LinearLayout(activity, scroll)
        entries.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        self.apps_container = entries
        self.app_menu_roots = []
        self.no_apps = tg.TextView(activity, "no apps found", entries)
        self.no_apps.setdimensions(tg.View.MATCH_PARENT, 48)
        self.no_apps.settextsize(14)
        self.no_apps.settextcolor(palette.muted)
        self.no_apps.setgravity(1, 1)
        self.no_apps.setvisibility(tg.View.GONE)
        self._populate_apps(apps)
        self.context_menu = ContextMenu(
            activity, root,
            lambda: (self.screen_width, self.screen_height),
            actions=("show_desktop", "open_task_manager", "open_settings", "open",
                     "restore", "maximize", "minimize", "close_window"),
            labels={"show_desktop": "show desktop",
                    "open_task_manager": "task manager",
                    "open_settings": "settings",
                    "close_window": "close window"})
        self.context_window = None

    def refresh_theme(self):
        set_rounded_color(self.surface, max(1, self.screen_width - 16),
                          self.content_height, palette.surface, 14)
        set_rounded_color(self.menu_surface, self.app_menu_width, self.menu_height,
                          palette.surface, 14)
        self.clock.settextcolor(palette.text)
        self.show_desktop_line.setbackgroundcolor(palette.accent)
        self.context_menu.refresh_theme()
        self.menu.setbackgroundcolor(palette.surface)
        self.heading.settextcolor(palette.text)
        self.divider.setbackgroundcolor(palette.accent)
        self.app_search.setbackgroundcolor(palette.window_content)
        self.app_search.settextcolor(palette.text)
        self.no_apps.settextcolor(palette.muted)
        for view, kind in self.menu_entries:
            if kind in ("heading", "description"):
                view.settextcolor(palette.muted)
            else:
                view.setbackgroundcolor(palette.window_content)
                view.settextcolor(palette.text)
        for window_id, button in self.window_views.items():
            button.setbackgroundcolor(palette.surface_light if window_id == self.active_window
                                      else palette.surface)

    def resize(self, screen_width, screen_height):
        self.screen_width = max(1, screen_width)
        self.screen_height = max(1, screen_height)
        self.frame.setdimensions(tg.View.MATCH_PARENT, self.height)
        place(self.frame, 0, self.screen_height - self.height, top=True)
        set_rounded_color(self.surface, max(1, self.screen_width - 16),
                          self.content_height, palette.surface, 14)
        self.view.setdimensions(max(1, self.screen_width - 16), self.content_height)
        place(self.surface, 8, 4)
        place(self.view, 8, 4)
        self._layout_window_buttons()
        self._reveal_window(self.active_window)

        self.app_menu_width = min(380, max(1, self.screen_width - 16))
        self.menu_height = min(500, max(160, self.screen_height - self.height - 24))
        self.menu_frame.setdimensions(self.app_menu_width, self.menu_height)
        place(self.menu_frame, 8, self.screen_height - self.height - self.menu_height - 8,
              top=self.menu_visible)
        set_rounded_color(self.menu_surface, self.app_menu_width, self.menu_height,
                          palette.surface, 14)
        if self.context_menu.visible:
            self.context_menu.reposition()
    def _add_window_scroll(self):
        self.window_scroll = tg.HorizontalScrollView(self.activity, self.view, nobar=True)
        self.window_scroll.setdimensions(0, tg.View.MATCH_PARENT)
        self.window_scroll.setlinearlayoutparams(1)
        self.window_scroll.sendlongclickevent(True)
        self.window_list = tg.LinearLayout(self.activity, self.window_scroll, vertical=False)
        self.window_list.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        if self.rtl:
            self.window_spacer = tg.LinearLayout(self.activity, self.window_list)
            self.window_spacer.setdimensions(0, self.content_height)
            self.window_spacer.setlinearlayoutparams(0, 0)

    def _layout_window_buttons(self):
        viewport_width = max(0, self.screen_width - 16 - self.clock_width
                             - self.menu_button_width - self.show_desktop_width)
        content_width = max(viewport_width, len(self.window_views) * self.window_width)
        self.window_viewport_width = viewport_width
        self.window_content_width = content_width
        self.window_list.setdimensions(content_width, self.content_height)
        if self.window_spacer is not None:
            self.window_spacer.setdimensions(
                content_width - len(self.window_views) * self.window_width, self.content_height)
            offset = round(max(0, content_width - viewport_width) * self.density)
            self.window_scroll.setscrollposition(offset, 0, smooth=False)

    def _reveal_window(self, window_id):
        window_ids = tuple(self.window_views)
        if window_id not in self.window_views:
            return
        index = window_ids.index(window_id)
        spacer_width = (max(0, self.window_content_width
                            - len(window_ids) * self.window_width)
                        if self.window_spacer is not None else 0)
        start = round((spacer_width + index * self.window_width) * self.density)
        end = start + round(self.window_width * self.density)
        viewport = round(self.window_viewport_width * self.density)
        current = self.window_scroll.getscrollposition()[0]
        target = current
        if start < current:
            target = start
        elif end > current + viewport:
            target = end - viewport
        max_scroll = round(max(0, self.window_content_width
                               - self.window_viewport_width) * self.density)
        target = max(0, min(max_scroll, target))
        if target != current:
            self.window_scroll.setscrollposition(target, 0, smooth=False)

    def _add_clock(self):
        self.clock_text = datetime.now().strftime("%H:%M")
        self.clock = tg.TextView(self.activity, self.clock_text, self.view)
        self.clock.sendclickevent(True)
        self.clock.sendlongclickevent(True)
        self.clock.settextcolor(palette.text)
        self.clock.setgravity(1, 1)
        self.clock.setdimensions(self.clock_width, self.content_height)
        self.clock.setlinearlayoutparams(0)
        self.buttons[self.clock.id] = ("clock", None)

    def _add_show_desktop(self):
        button = tg.FrameLayout(self.activity, self.view)
        button.setdimensions(self.show_desktop_width, self.content_height)
        button.setlinearlayoutparams(0)
        button.sendclickevent(True)
        line = tg.LinearLayout(self.activity, button)
        line.setdimensions(2, 32)
        line.setbackgroundcolor(palette.accent)
        line.setmargin((self.show_desktop_width - 2) // 2, "left")
        line.setmargin(8, "top")
        self.show_desktop_line = line
        self.buttons[button.id] = ("show_desktop", None)

    def tick(self):
        now = monotonic()
        if now < self.next_clock_check:
            return
        self.next_clock_check = now + 1
        clock_text = datetime.now().strftime("%H:%M")
        if clock_text != self.clock_text:
            self.clock_text = clock_text
            self.clock.settext(clock_text)

    def _add_menu_button(self):
        button = tg.LinearLayout(self.activity, self.view, vertical=True)
        button.setdimensions(self.menu_button_width, self.content_height)
        button.setlinearlayoutparams(0)
        button.sendclickevent(True)
        button.sendlongclickevent(True)
        image = tg.ImageView(self.activity, button)
        image.setdimensions(28, 28)
        image.setimage(load("apps"))
        image.setmargin((self.menu_button_width - 28) // 2, "left")
        image.setmargin((self.content_height - 28) // 2, "top")
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
        self.menu_entries.append((heading, "heading"))
        self.app_menu_roots.append(heading)
        self.app_headings[letter] = heading

    def _add_app_entry(self, parent, app):
        button = tg.LinearLayout(self.activity, parent, vertical=False)
        button.setdimensions(tg.View.MATCH_PARENT, 56)
        button.setlinearlayoutparams(0)
        button.setmargin(8, "left")
        button.setmargin(8, "right")
        button.setmargin(3, "top")
        button.setmargin(3, "bottom")
        button.setbackgroundcolor(palette.window_content)
        self.menu_entries.append((button, "entry"))
        button.sendclickevent(True)
        self.buttons[button.id] = ("app", app.app_id)
        self.app_menu_roots.append(button)
        self.app_entries.append((app, button))

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
            labels = tg.LinearLayout(self.activity, button, vertical=True)
            labels.setdimensions(0, 56)
            labels.setlinearlayoutparams(1)
            labels.setmargin(12, "left")
            title = tg.TextView(self.activity, app.name, labels)
            title.setdimensions(tg.View.MATCH_PARENT, 31)
            title.setlinearlayoutparams(0)
            title.settextsize(14)
            title.settextcolor(palette.text)
            title.setgravity(0, 1)
            self.menu_entries.append((title, "label"))
            summary = (getattr(app, "summary", "")
                       or getattr(app, "description", ""))
            summary = str(summary or "").strip()
            if len(summary) > 48:
                summary = summary[:45].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
            description = tg.TextView(self.activity, summary, labels)
            description.setdimensions(tg.View.MATCH_PARENT, 23)
            description.setlinearlayoutparams(0)
            description.settextsize(11)
            description.settextcolor(palette.muted)
            description.setgravity(0, 1)
            self.menu_entries.append((description, "description"))

        if self.rtl:
            add_label()
            add_icon()
        else:
            add_icon()
            add_label()

    def _populate_apps(self, apps):
        self.apps = tuple(sorted(
            (app for app in apps.values()
             if not getattr(app, "hosted_window", False)),
            key=lambda item: item.name.casefold()))
        previous_letter = None
        for app in self.apps:
            letter = app.name[:1].upper()
            if letter != previous_letter:
                self._add_app_heading(self.apps_container, letter)
                previous_letter = letter
            self._add_app_entry(self.apps_container, app)
        self._filter_apps(self.app_search.gettext())

    def _filter_apps(self, query):
        query = query.strip().casefold()
        visible_by_letter = {}
        visible_count = 0
        for app, button in self.app_entries:
            searchable = " ".join((app.name, app.app_id,
                                   str(getattr(app, "summary", "")),
                                   str(getattr(app, "description", "")))).casefold()
            matches = not query or query in searchable
            button.setvisibility(tg.View.VISIBLE if matches else tg.View.GONE)
            if matches:
                visible_by_letter[app.name[:1].upper()] = True
                visible_count += 1
        for letter, heading in self.app_headings.items():
            heading.setvisibility(tg.View.VISIBLE if visible_by_letter.get(letter)
                                  else tg.View.GONE)
        self.no_apps.setvisibility(tg.View.GONE if visible_count else tg.View.VISIBLE)

    def set_apps(self, apps):
        for view in self.app_menu_roots:
            self.buttons.pop(view.id, None)
            view.delete()
        self.app_menu_roots.clear()
        self.menu_entries.clear()
        self.app_entries.clear()
        self.app_headings.clear()
        self.no_apps.setvisibility(tg.View.GONE)
        self._populate_apps(apps)

    def add_window(self, window_id, app, title=None):
        button = tg.LinearLayout(self.activity, self.window_list, vertical=False)
        button.setdimensions(self.window_width, self.content_height)
        button.setlinearlayoutparams(0, 1 if self.rtl else None)
        button.setbackgroundcolor(palette.surface)
        button.sendclickevent(True)
        button.sendlongclickevent(True)
        image = tg.ImageView(self.activity, button)
        image.setdimensions(28, 28)
        image.setbackgroundcolor(0xFFE8EDF2)
        image.setimage(load(app.icon))
        image.setmargin((self.window_width - 28) // 2, "left")
        image.setmargin((self.content_height - 28) // 2, "top")
        image.setlinearlayoutparams(0)
        self.buttons[button.id] = ("window", window_id)
        self.window_views[window_id] = button
        self._layout_window_buttons()
        self._reveal_window(window_id)

    def set_active_window(self, window_id):
        self.active_window = window_id
        for current_id, button in self.window_views.items():
            button.setbackgroundcolor(palette.surface_light if current_id == window_id else palette.surface)
        if window_id is not None:
            self._reveal_window(window_id)

    def remove_window(self, window_id):
        view = self.window_views.pop(window_id, None)
        if view is None:
            return
        if self.context_window == window_id:
            self.dismiss_context_menu()
        self.buttons.pop(view.id, None)
        view.delete()
        self._layout_window_buttons()
        self._reveal_window(self.active_window)

    def dismiss_menu(self):
        if not self.menu_visible:
            return
        self.menu_visible = False
        self.menu_frame.setvisibility(tg.View.GONE)

    def dismiss_context_menu(self):
        self.context_menu.hide()
        self.context_window = None

    def _show_context_menu(self, window_id=None):
        self.context_window = window_id
        if window_id is None:
            actions = ["show_desktop", "open_task_manager", "open_settings"]
            x = (self.screen_width - self.context_menu.width) // 2
        else:
            actions = (self.window_context_actions(window_id)
                       if self.window_context_actions is not None else
                       ("open", "maximize", "minimize", "close_window"))
            window_ids = tuple(self.window_views)
            index = window_ids.index(window_id) if window_id in self.window_views else 0
            scroll_x = self.window_scroll.getscrollposition()[0] / self.density
            if self.rtl:
                origin = 8 + self.show_desktop_width + self.clock_width
                spacer = max(0, self.window_content_width
                             - len(window_ids) * self.window_width)
            else:
                origin = 8 + self.menu_button_width
                spacer = 0
            x = (origin + spacer + index * self.window_width - scroll_x
                 + self.window_width / 2 - self.context_menu.width / 2)
        height = len(actions) * self.context_menu.row_height + 8
        if not actions:
            return
        y = self.screen_height - self.height - height - 4
        self.context_menu.show(actions, x, y, self._run_context_action)

    def _run_context_action(self, action):
        window_id = self.context_window
        self.context_window = None
        if self.context_action is not None:
            self.context_action(window_id, action)

    def handle_event(self, event):
        if self.context_menu.handle_event(event):
            return True
        if event.type == tg.Event.text and event.id == self.app_search.id:
            self._filter_apps(event.value.get("text", ""))
            return True
        if event.type == tg.Event.back and self.context_menu.visible:
            self.dismiss_context_menu()
            return True
        if event.type == tg.Event.longClick:
            if event.id in (self.view.id, self.window_scroll.id, self.clock.id):
                self._show_context_menu()
                return True
            action = self.buttons.get(event.id)
            if action is not None and action[0] == "window":
                self._show_context_menu(action[1])
                return True
            if action is not None and action[0] == "menu":
                self._show_context_menu()
                return True
        if event.type == tg.Event.back and self.menu_visible:
            self.dismiss_menu()
            return True
        if event.type != tg.Event.click or event.id not in self.buttons:
            return False
        action, value = self.buttons[event.id]
        self.dismiss_context_menu()
        if action == "menu":
            self.menu_visible = not self.menu_visible
            self.menu_frame.setvisibility(tg.View.VISIBLE if self.menu_visible else tg.View.GONE)
            if self.menu_visible:
                self.app_search.settext("")
                self._filter_apps("")
                place(self.menu_frame, 8, self.screen_height - self.height - self.menu_height - 8, top=True)
            return True

        self.dismiss_menu()
        if action == "app":
            self.open_app(value)
        elif action == "clock":
            if "clock" in self.apps:
                self.open_app("clock")
        elif action == "show_desktop":
            if self.context_action is not None:
                self.context_action(None, "show_desktop")
        else:
            self.activate_window(value)
        return True
