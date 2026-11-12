import termuxgui as tg

from shell.geometry import place


class LoadingScreen:
    panel_width = 320
    panel_height = 128

    def __init__(self, activity, configuration):
        self.activity = activity
        self.screen_width = configuration["screenwidth"]
        self.screen_height = configuration["screenheight"]
        self.root = tg.FrameLayout(activity)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.root.setbackgroundcolor(0xFF10131A)

        width = min(self.panel_width, max(1, self.screen_width - 32))
        self.panel_width = width
        self.panel = tg.LinearLayout(activity, self.root)
        self.panel.setdimensions(width, self.panel_height)
        self.panel.setbackgroundcolor(0xFF10131A)
        self._place_panel()

        title = tg.TextView(activity, "nexa", self.panel)
        title.settextsize(36)
        title.settextcolor(0xFF45D6C5)
        title.setgravity(1, 1)
        title.setheight(64)
        title.setlinearlayoutparams(0)

        self.status = tg.TextView(activity, "starting...", self.panel)
        self.status.settextsize(16)
        self.status.setgravity(1, 1)
        self.status.setheight(48)
        self.status.setlinearlayoutparams(0)

    def _place_panel(self):
        x = max(0, (self.screen_width - self.panel_width) // 2)
        y = max(0, (self.screen_height - self.panel_height) // 2)
        place(self.panel, x, y, top=True)

    def resize(self, configuration):
        self.screen_width = configuration["screenwidth"]
        self.screen_height = configuration["screenheight"]
        panel_width = min(type(self).panel_width, max(1, self.screen_width - 32))
        if panel_width != self.panel_width:
            self.panel_width = panel_width
            self.panel.setwidth(panel_width)
        self._place_panel()

    def show(self):
        self._place_panel()
        self.panel.setvisibility(tg.View.VISIBLE)

    def hide(self):
        self.panel.setvisibility(tg.View.GONE)

    def update(self, status, progress):
        self.status.settext(status)


def show(activity, configuration):
    return LoadingScreen(activity, configuration)
