import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.surface import add_rounded, set_image_scale_type, set_rounded_color


class TextPrompt:
    height = 152

    def __init__(self, activity, parent, bounds):
        self.activity = activity
        self.bounds = bounds
        self.visible = False
        self.on_submit = None
        self.width = 320
        self.overlay = tg.FrameLayout(activity, parent)
        self.overlay.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.overlay.setbackgroundcolor(0x77000000)
        self.overlay.sendclickevent(True)

        self.dialog = tg.FrameLayout(activity, self.overlay)
        self.dialog.setdimensions(self.width, self.height)
        self.background = add_rounded(activity, self.dialog, self.width, self.height,
                                      palette.surface, 14)
        set_image_scale_type(self.background, "fit_xy")
        content = tg.LinearLayout(activity, self.dialog, vertical=True)
        content.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        self.title = tg.TextView(activity, "", content)
        self.title.setdimensions(tg.View.MATCH_PARENT, 36)
        self.title.setlinearlayoutparams(0)
        self.title.setmargin(4, "top")
        self.title.setmargin(16, "left")
        self.title.settextsize(16)
        self.title.settextcolor(palette.text)
        self.title.setgravity(0, 1)

        self.input = tg.EditText(activity, "", content, singleline=True, line=False,
                                 inputtype="text")
        self.input.setdimensions(tg.View.MATCH_PARENT, 40)
        self.input.setlinearlayoutparams(0)
        self.input.setmargin(4, "top")
        self.input.setmargin(12, "left")
        self.input.setmargin(12, "right")
        self.input.setbackgroundcolor(palette.window_content)
        self.input.settextcolor(palette.text)
        self.input.settextsize(14)

        buttons = tg.LinearLayout(activity, content, vertical=False)
        buttons.setdimensions(tg.View.MATCH_PARENT, 40)
        buttons.setlinearlayoutparams(0)
        buttons.setmargin(8, "top")
        buttons.setmargin(8, "bottom")
        self.cancel = self._button(activity, buttons, "cancel")
        self.confirm = self._button(activity, buttons, "ok")
        self.events = {self.overlay.id: "cancel", self.cancel.id: "cancel",
                       self.confirm.id: "submit"}
        self.overlay.setvisibility(tg.View.GONE)

    @staticmethod
    def _button(activity, parent, label):
        button = tg.TextView(activity, label, parent)
        button.setdimensions(0, 36)
        button.setlinearlayoutparams(1)
        button.setmargin(8, "left")
        button.setmargin(8, "right")
        button.setbackgroundcolor(palette.surface_light)
        button.settextsize(13)
        button.settextcolor(palette.text)
        button.setgravity(1, 1)
        button.sendclickevent(True)
        return button

    def show(self, title, value, on_submit):
        width, height = self.bounds()
        self.width = min(320, max(1, width - 32))
        self.dialog.setdimensions(self.width, self.height)
        self.title.settext(title)
        self.input.settext(value)
        self.on_submit = on_submit
        place(self.overlay, 0, 0, top=True)
        self.reposition(width, height)
        self.overlay.setvisibility(tg.View.VISIBLE)
        self.visible = True
        self.input.focus(forcesoft=True)

    def reposition(self, width=None, height=None):
        if not self.visible and width is None:
            return
        if width is None or height is None:
            width, height = self.bounds()
        self.width = min(320, max(1, width - 32))
        self.dialog.setdimensions(self.width, self.height)
        set_rounded_color(self.background, self.width, self.height, palette.surface, 14)
        place(self.dialog, max(0, (width - self.width) // 2),
              max(0, (height - self.height) // 2), top=True)

    def hide(self):
        self.overlay.setvisibility(tg.View.GONE)
        self.visible = False
        self.on_submit = None
        self.activity.hidesoftkeyboard()

    def handle_event(self, event):
        if not self.visible:
            return False
        if event.type == tg.Event.back:
            self.hide()
            return True
        if event.type == tg.Event.click:
            action = self.events.get(event.id)
            if action == "cancel":
                self.hide()
            elif action == "submit":
                value = self.input.gettext().strip()
                if not value:
                    return True
                callback = self.on_submit
                self.hide()
                if callback is not None:
                    callback(value)
            return True
        return True
