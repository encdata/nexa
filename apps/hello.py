import termuxgui as tg

from shell import palette


class HelloApp:
    app_id = "hello"
    name = "hello world"
    icon = "window"

    def build(self, activity, parent, context=None):
        label = tg.TextView(activity, "hello world", parent)
        label.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        label.settextsize(28)
        label.settextcolor(palette.accent)
        label.setgravity(1, 1)


APP = HelloApp()
