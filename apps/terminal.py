import codecs
import os

import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.process import CommandSession
from shell.terminal_screen import TerminalScreen


class TerminalApp:
    app_id = "terminal"
    name = "Nexa Terminal"
    icon = "terminal"

    def build(self, activity, parent, context=None):
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        toolbar = tg.FrameLayout(activity, root)
        toolbar.setdimensions(tg.View.MATCH_PARENT, 44)
        toolbar.setlinearlayoutparams(0)
        toolbar.setbackgroundcolor(palette.surface_light)
        title = tg.TextView(activity, "terminal", toolbar)
        title.setdimensions(120, 44)
        title.settextsize(13)
        title.settextcolor(palette.muted)
        title.setgravity(1, 1)
        place(title, 12, 0)

        self.scroll = tg.NestedScrollView(activity, root, fillviewport=True, nobar=False)
        self.scroll.setheight(0)
        self.scroll.setlinearlayoutparams(1)
        self.output_scroll = tg.HorizontalScrollView(activity, self.scroll, nobar=True)
        self.output_scroll.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        self.output_rows = tg.LinearLayout(activity, self.output_scroll)
        self.output_rows.setdimensions(80 * 9, tg.View.WRAP_CONTENT)
        self.output_rows.setmargin(10, "left")
        self.output_rows.setmargin(10, "right")
        self.output_rows.setmargin(8, "top")
        self.output_rows.setmargin(8, "bottom")

        input_panel = tg.LinearLayout(activity, root)
        input_panel.setdimensions(tg.View.MATCH_PARENT, 140)
        input_panel.setlinearlayoutparams(0)
        input_panel.setbackgroundcolor(palette.surface)
        self.command = tg.EditText(activity, "", input_panel, singleline=True, line=False,
                                   inputtype="text")
        self.command.setdimensions(tg.View.MATCH_PARENT, 44)
        self.command.setlinearlayoutparams(0)
        self.command.setmargin(8, "left")
        self.command.setmargin(8, "right")
        self.command.setmargin(4, "top")
        self.command.setbackgroundcolor(palette.window_content)
        self.command.settextcolor(palette.text)
        self.command.settextsize(14)
        self.command.sendtextevent(True)
        self.command.sendclickevent(True)

        self.actions = tg.FrameLayout(activity, input_panel)
        self.actions.setdimensions(tg.View.MATCH_PARENT, 44)
        self.actions.setlinearlayoutparams(0)
        interrupt = self._button(activity, self.actions, "ctrl-c", palette.surface_light, palette.text)
        enter = self._button(activity, self.actions, "enter", palette.accent, palette.background)

        self.key_row = tg.FrameLayout(activity, input_panel)
        self.key_row.setdimensions(tg.View.MATCH_PARENT, 44)
        self.key_row.setlinearlayoutparams(0)
        keys = (("esc", "\x1b"), ("tab", "\t"), ("↑", "\x1b[A"),
                ("←", "\x1b[D"), ("↓", "\x1b[B"), ("→", "\x1b[C"))
        key_controls = []
        for index, (label, sequence) in enumerate(keys):
            button = self._button(activity, self.key_row, label, palette.surface_light, palette.text)
            key_controls.append((button, sequence, index))

        self.activity = activity
        self.density = activity.getconfiguration()["density"]
        self.screen = TerminalScreen()
        self.rendered_rows = []
        self.row_views = []
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.draft = ""
        shell = os.environ.get("SHELL") or "/data/data/com.termux/files/usr/bin/bash"
        self.session = CommandSession(
            [shell, "-i"], cwd=os.path.expanduser("~"),
            env={"TERM": "xterm-256color", "COLORTERM": "truecolor"},
            columns=80, rows=24,
        )
        self.buttons = {interrupt.id: ("interrupt", None), enter.id: ("enter", None)}
        self.buttons.update({button.id: ("key", sequence) for button, sequence, _ in key_controls})
        self.controls = [(interrupt, "right", 64), (enter, "right", 4)]
        self.key_controls = key_controls
        self._position_controls(0)
        self._resize_output(80)
        self._render(force=True)
        return self

    @staticmethod
    def _button(activity, parent, label, background, foreground):
        button = tg.TextView(activity, label, parent)
        button.setdimensions(56 if label == "ctrl-c" else 60 if label == "enter" else 44, 40)
        button.setbackgroundcolor(background)
        button.settextsize(12)
        button.settextcolor(foreground)
        button.setgravity(1, 1)
        button.sendclickevent(True)
        return button

    def _position_controls(self, width):
        width_dp = width / self.density if width else 420
        for button, _, right_gap in self.controls:
            button_width = 60 if button.gettext() == "enter" else 56
            place(button, max(4, width_dp - right_gap - button_width), 2)
        for button, _, index in self.key_controls:
            place(button, 8 + index * 48, 2)

    def _sync_input(self, text):
        if text == self.draft:
            return
        shared = 0
        while shared < min(len(self.draft), len(text)) and self.draft[shared] == text[shared]:
            shared += 1
        if shared == len(self.draft):
            self.session.write(text[shared:])
        elif shared == len(text):
            self.session.write("\x7f" * (len(self.draft) - shared))
        else:
            self.session.write("\x15" + text[shared:])
        self.draft = text

    def handle_event(self, event):
        if event.type == tg.Event.text and event.id == self.command.id:
            self._sync_input(event.value.get("text", self.command.gettext()))
            return True
        if event.type == tg.Event.click and event.id == self.command.id:
            self.command.focus(forcesoft=True)
            return True
        if event.type != tg.Event.click:
            return False
        action = self.buttons.get(event.id)
        if action is None:
            return False
        action, value = action
        if action == "enter":
            self._sync_input(self.command.gettext())
            self.session.write("\n")
            self.draft = ""
            self.command.settext("")
            return True
        if action == "interrupt":
            self.session.write("\x03")
            self.draft = ""
            self.command.settext("")
            return True
        if action == "key":
            self.session.write(value)
            return True
        return False

    def _resize_output(self, columns):
        self.output_rows.setwidth(columns * 9)

    def _render(self, force=False):
        rows = self.screen.render_rows()
        if force or len(rows) != len(self.row_views):
            self.output_rows.clearchildren()
            self.row_views = []
            self.rendered_rows = []
            for _ in rows:
                row = tg.FrameLayout(self.activity, self.output_rows)
                row.setdimensions(self.screen.columns * 9, 18)
                self.row_views.append(row)
                self.rendered_rows.append(None)
        for index, runs in enumerate(rows):
            signature = tuple(runs)
            if signature == self.rendered_rows[index]:
                continue
            row = self.row_views[index]
            row.clearchildren()
            column = 0
            for text, foreground, background, bold in runs:
                if not text:
                    continue
                segment = tg.TextView(self.activity, "\u200e" + text, row)
                segment.setdimensions(len(text) * 9, 18)
                segment.settextsize(14 if bold else 13)
                segment.settextcolor(foreground)
                segment.setbackgroundcolor(background)
                segment.setgravity(1, 1)
                place(segment, column * 9, 0)
                column += len(text)
            self.rendered_rows[index] = signature

    def on_tick(self):
        data = self.session.read_output()
        if data:
            self.screen.feed(self.decoder.decode(data))
            self._render()
            self.scroll.setscrollposition(0, 1_000_000, smooth=False)

    def on_window_resize(self, width, height):
        columns = max(20, round(width / (8.5 * self.density)))
        rows = max(5, round((height - 184 * self.density) / (18 * self.density)))
        self.screen.resize(columns, rows)
        self.session.resize(columns, rows)
        self._resize_output(columns)
        self._render()
        self._position_controls(width)

    def close(self):
        self.session.close()


APP = TerminalApp()
