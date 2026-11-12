import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.surface import add_rounded, set_rounded_color


class ContextMenu:
    width = 224
    row_height = 44
    actions = ("open", "open_with", "open_terminal", "new_file", "new_folder",
               "copy", "cut", "paste", "rename", "delete", "properties", "refresh",
               "realign")
    labels = {"open_with": "open with", "open_terminal": "open in terminal",
              "new_file": "new file", "new_folder": "new folder",
              "realign": "realign icons"}

    def __init__(self, activity, parent, bounds, actions=None, labels=None):
        self.activity = activity
        self.bounds = bounds
        self.actions = tuple(actions) if actions is not None else type(self).actions
        self.labels = {**type(self).labels, **(labels or {})}
        self.on_action = None
        self.visible_actions = ()
        self.disabled_actions = set()
        self.anchor = (0, 0)
        self.events = {}
        self.height = self.row_height * len(self.actions) + 8
        self.position = (0, 0)
        self.view = tg.FrameLayout(activity, parent)
        self.view.setdimensions(self.width, self.height)
        self.background = add_rounded(activity, self.view, self.width, self.height,
                                      palette.surface, 12)
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
            row.sendclickevent(True)
            row.settextsize(14)
            row.settextcolor(palette.text)
            row.setgravity(0, 1)
            self.events[row.id] = action
            self.rows[action] = row
        self.visible = False
        self.view.setvisibility(tg.View.GONE)

    def show(self, actions, x, y, on_action, disabled=()):
        actions = tuple(actions)
        self.visible_actions = actions
        self.disabled_actions = set(disabled)
        self.anchor = (x, y)
        self.on_action = on_action
        for action, row in self.rows.items():
            row.setvisibility(tg.View.VISIBLE if action in actions else tg.View.GONE)
            row.settextcolor(palette.muted if action in self.disabled_actions else palette.text)
        height = max(1, len(actions)) * self.row_height + 8
        self.height = height
        self.view.setdimensions(self.width, height)
        set_rounded_color(self.background, self.width, height, palette.surface, 12)
        width, screen_height = self.bounds()
        x = min(max(0, round(x)), max(0, width - self.width))
        y = min(max(0, round(y)), max(0, screen_height - height))
        self.position = (x, y)
        place(self.view, x, y, top=True)
        self.view.setvisibility(tg.View.VISIBLE)
        self.visible = True

    def reposition(self):
        if self.visible:
            self.show(self.visible_actions, *self.anchor, self.on_action,
                      disabled=self.disabled_actions)

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
        if action in self.disabled_actions:
            return True
        if self.on_action is not None:
            callback = self.on_action
            self.hide()
            callback(action)
        return True
