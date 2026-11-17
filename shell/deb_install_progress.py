import os

import nexapip as tg

from shell import palette
from shell.geometry import place
from shell.surface import add_rounded, set_image_scale_type, set_rounded_color


class DebInstallProgress:
    width = 344
    height = 204

    def __init__(self, activity, parent, bounds):
        self.activity = activity
        self.bounds = bounds
        self.visible = False
        self.finished = False
        self.overlay = tg.FrameLayout(activity, parent)
        self.overlay.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.overlay.setbackgroundcolor(0x77000000)
        self.overlay.setvisibility(tg.View.GONE)

        self.dialog = tg.FrameLayout(activity, self.overlay)
        self.dialog.setdimensions(self.width, self.height)
        self.background = add_rounded(activity, self.dialog, self.width, self.height,
                                      palette.surface, 14)
        set_image_scale_type(self.background, "fit_xy")
        content = tg.LinearLayout(activity, self.dialog, vertical=True)
        content.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        self.title = self._text(content, "installing Debian package", 20, 38)
        self.title.setmargin(16, "left")
        self.title.setmargin(12, "top")
        self.detail = self._text(content, "checking package", 13, 30)
        self.detail.setmargin(16, "left")
        self.detail.settextcolor(palette.muted)

        self.progress = tg.ProgressBar(activity, content)
        self.progress.setdimensions(tg.View.MATCH_PARENT, 4)
        self.progress.setlinearlayoutparams(0)
        self.progress.setmargin(16, "left")
        self.progress.setmargin(16, "right")
        self.progress.setprogress(0)

        self.explanation = self._text(
            content, "resolves dependencies, downloads packages, extracts files, and adds apps",
            12, 48)
        self.explanation.setmargin(16, "left")
        self.explanation.setmargin(16, "right")
        self.explanation.settextcolor(palette.muted)

        self.close_button = tg.TextView(activity, "close", content)
        self.close_button.setdimensions(76, 36)
        self.close_button.setlinearlayoutparams(0)
        self.close_button.setmargin(8, "right")
        self.close_button.setmargin(6, "bottom")
        self.close_button.setbackgroundcolor(palette.surface_light)
        self.close_button.settextsize(13)
        self.close_button.settextcolor(palette.text)
        self.close_button.setgravity(1, 1)
        self.close_button.sendclickevent(True)

    def _text(self, parent, value, size, height):
        view = tg.TextView(self.activity, value, parent)
        view.setdimensions(tg.View.MATCH_PARENT, height)
        view.setlinearlayoutparams(0)
        view.settextsize(size)
        view.settextcolor(palette.text)
        view.setgravity(0, 1)
        return view

    def show(self, path):
        self.finished = False
        self.title.settext(f"installing {os.path.basename(path)}")
        self.detail.settext("checking Debian package")
        self.progress.setprogress(0)
        self.close_button.setvisibility(tg.View.GONE)
        width, height = self.bounds()
        self.width = min(360, max(1, width - 32))
        self.dialog.setdimensions(self.width, self.height)
        set_rounded_color(self.background, self.width, self.height, palette.surface, 14)
        place(self.overlay, 0, 0, top=True)
        place(self.dialog, max(0, (width - self.width) // 2),
              max(0, (height - self.height) // 2), top=True)
        self.overlay.setvisibility(tg.View.VISIBLE)
        self.visible = True

    def update(self, message, percent):
        if not self.visible or self.finished:
            return
        self.detail.settext(message)
        if percent is not None:
            self.progress.setprogress(max(0, min(100, int(percent))))

    def finish(self, result, error):
        if not self.visible:
            return
        self.finished = True
        if error is not None:
            self.title.settext("package install failed")
            self.detail.settext(str(error))
            self.progress.setprogress(0)
        else:
            package, count = result
            self.title.settext(f"{package} installed")
            self.detail.settext(f"{count} graphical app(s) added to Nexa")
            self.progress.setprogress(100)
        self.close_button.setvisibility(tg.View.VISIBLE)

    def hide(self):
        self.overlay.setvisibility(tg.View.GONE)
        self.visible = False

    def handle_event(self, event):
        if not self.visible:
            return False
        if event.type == tg.Event.back:
            if self.finished:
                self.hide()
            return True
        if event.type == tg.Event.click and event.id == self.close_button.id and self.finished:
            self.hide()
        return True
