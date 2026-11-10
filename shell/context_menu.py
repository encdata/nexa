import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.surface import add as add_surface, set_color as set_surface_color


class ContextMenu:
    width = 224
    row_height = 44
    actions = ("open", "copy", "cut", "paste", "delete", "refresh", "realign")
    labels = {"realign": "realign icons"}

    def __init__(self, activity, parent, bounds):
        self.activity = activity
        self.bounds = bounds
        self.on_action = None
        self.visible_actions = ()
        self.anchor = (0, 0)
        self.events = {}
        self.height = self.row_height * len(self.actions) + 8
        self.position = (0, 0)
        self.view = tg.FrameLayout(activity, parent)
        self.view.setdimensions(self.width, self.height)
        self.view.setbackgroundcolor(palette.surface)
        self.background = add_surface(activity, self.view, self.width, self.height, palette.surface)
        self.content = tg.LinearLayout(activity, self.view)
        self.content.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.rows = {}
        for action in self.actions:
            row = tg.TextView(activity, self.labels.get(action, action), self.content)
            row.setdimensions(self.width - 12, self.row_height - 4)
            row.setlinearlayoutparams(0)
            row.setmargin(6, "left")
            row.setmargin(6, "right")
            row.setmargin(2, "top")
            row.setbackgroundcolor(palette.surface)
            row.sendclickevent(True)
            row.settextsize(14)
            row.settextcolor(palette.text)
            row.setgravity(0, 1)
            self.events[row.id] = action
            self.rows[action] = row
        self.visible = False
        self.view.setvisibility(tg.View.GONE)

    def show(self, actions, x, y, on_action):
        actions = tuple(actions)
        self.visible_actions = actions
        self.anchor = (x, y)
        self.on_action = on_action
        for action, row in self.rows.items():
            row.setvisibility(tg.View.VISIBLE if action in actions else tg.View.GONE)
        height = max(1, len(actions)) * self.row_height + 8
        self.height = height
        self.view.setdimensions(self.width, height)
        set_surface_color(self.background, self.width, height, palette.surface)
        width, screen_height = self.bounds()
        x = min(max(0, round(x)), max(0, width - self.width))
        y = min(max(0, round(y)), max(0, screen_height - height))
        self.position = (x, y)
        place(self.view, x, y, top=True)
        self.view.setvisibility(tg.View.VISIBLE)
        self.visible = True

    def reposition(self):
        if self.visible:
            self.show(self.visible_actions, *self.anchor, self.on_action)

    def hide(self):
        self.view.setvisibility(tg.View.GONE)
        self.visible = False

    def contains(self, x, y):
        left, top = self.position
        return self.visible and left <= x < left + self.width and top <= y < top + self.height

    def handle_event(self, event):
        if event.type != tg.Event.click or event.id not in self.events or not self.visible:
            return False
        action = self.events[event.id]
        if self.on_action is not None:
            callback = self.on_action
            self.hide()
            callback(action)
        return True
