import time

import nexapip as tg

from shell import palette
from shell.geometry import place
from shell.surface import add_rounded, set_image_scale_type, set_rounded_color


class DebLaunchProgress:
    width = 336
    height = 78

    def __init__(self, activity, parent, screen_size):
        self.activity = activity
        self.screen_size = screen_size
        self.token = None
        self.hide_at = None
        self.frame = tg.FrameLayout(activity, parent)
        self.frame.setdimensions(self.width, self.height)
        self.frame.setvisibility(tg.View.GONE)
        self.background = add_rounded(activity, self.frame, self.width, self.height,
                                      palette.surface, 14)
        set_image_scale_type(self.background, "fit_xy")
        content = tg.LinearLayout(activity, self.frame, vertical=True)
        content.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        self.title = self._text(content, "starting Linux app", 15, 30)
        self.title.setmargin(14, "left")
        self.title.setmargin(5, "top")
        self.detail = self._text(content, "preparing Wayland host", 12, 24)
        self.detail.setmargin(14, "left")
        self.detail.settextcolor(palette.muted)

        self.progress = tg.ProgressBar(activity, content)
        self.progress.setdimensions(tg.View.MATCH_PARENT, 3)
        self.progress.setlinearlayoutparams(0)
        self.progress.setmargin(14, "left")
        self.progress.setmargin(14, "right")
        self.progress.setmargin(2, "bottom")

    def _text(self, parent, value, size, height):
        view = tg.TextView(self.activity, value, parent)
        view.setdimensions(tg.View.MATCH_PARENT, height)
        view.setlinearlayoutparams(0)
        view.settextsize(size)
        view.settextcolor(palette.text)
        view.setgravity(0, 1)
        return view

    def show(self, token, app_name, message, percent):
        self.token = token
        self.hide_at = None
        self.title.settext(f"starting {app_name}")
        self.update(token, message, percent)
        width = self.screen_size()[0]
        self.width = min(360, max(1, width - 32))
        self.frame.setdimensions(self.width, self.height)
        set_image_scale_type(self.background, "fit_xy")
        place(self.frame, max(0, (width - self.width) // 2), 18, top=True)
        self.frame.setvisibility(tg.View.VISIBLE)

    def update(self, token, message, percent):
        if token != self.token:
            return
        self.detail.settext(message)
        if percent is not None:
            self.progress.setprogress(max(0, min(100, int(percent))))

    def complete(self, token, error=None):
        if token != self.token:
            return
        if error is None:
            self.title.settext("Linux app is ready")
            self.detail.settext("Wayland window opened")
            self.progress.setprogress(100)
            self.hide_at = time.monotonic() + 1.2
        else:
            self.title.settext("Linux app failed to start")
            self.detail.settext(str(error)[:120])
            self.progress.setprogress(0)
            self.hide_at = time.monotonic() + 5

    def tick(self):
        if self.hide_at is not None and time.monotonic() >= self.hide_at:
            self.frame.setvisibility(tg.View.GONE)
            self.token = None
            self.hide_at = None

    def resize(self):
        if self.token is None:
            return
        width = self.screen_size()[0]
        self.width = min(360, max(1, width - 32))
        self.frame.setdimensions(self.width, self.height)
        place(self.frame, max(0, (width - self.width) // 2), 18, top=True)

    def refresh_theme(self):
        set_rounded_color(self.background, self.width, self.height, palette.surface, 14)
        self.title.settextcolor(palette.text)
        self.detail.settextcolor(palette.muted)
