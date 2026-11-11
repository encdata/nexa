import codecs
import os

import termuxgui as tg

from shell import palette
from shell.geometry import place
from shell.process import CommandSession
from shell.surface import add_keyboard_input
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
        self.title_view = title
        self.displayed_title = "terminal"

        self.scroll = tg.NestedScrollView(activity, root, fillviewport=True, nobar=False)
        self.scroll.setheight(0)
        self.scroll.setlinearlayoutparams(1)
        self.scroll.sendclickevent(True)
        self.output_rows = tg.LinearLayout(activity, self.scroll)
        self.output_rows.setdimensions(80 * 9, 24 * 18)
        self.output_rows.setmargin(10, "left")
        self.output_rows.setmargin(10, "right")
        self.output_rows.setmargin(8, "top")
        self.output_rows.setmargin(8, "bottom")

        input_panel = tg.LinearLayout(activity, root)
        input_panel.setdimensions(tg.View.MATCH_PARENT, 92)
        input_panel.setlinearlayoutparams(0)
        input_panel.setbackgroundcolor(palette.surface)

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
        self.keyboard_input = add_keyboard_input(activity, root)
        self.density = activity.getconfiguration()["density"]
        self.screen = TerminalScreen()
        self.rendered_rows = []
        self.row_views = []
        self.row_view_ids = set()
        self.row_view_indexes = {}
        self.mouse_pressed = False
        self.last_mouse_location = None
        self.mouse_touch_enabled = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        shell = os.environ.get("SHELL") or "/data/data/com.termux/files/usr/bin/bash"
        self.session = CommandSession(
            [shell, "-i"], cwd=os.path.expanduser("~"),
            env={"TERM": "xterm-256color", "COLORTERM": "truecolor",
                 "NO_COLOR": None, "FORCE_COLOR": "1"},
            columns=80, rows=24,
        )
        self.buttons = {interrupt.id: ("interrupt", None), enter.id: ("enter", None)}
        self.buttons.update({button.id: ("key", sequence) for button, sequence, _ in key_controls})
        self.controls = [(interrupt, "right", 64), (enter, "right", 4)]
        self.key_controls = key_controls
        self._position_controls(0)
        self._resize_output(80)
        self._render(force=True)
        self.keyboard_input.focus(forcesoft=True)
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

    def _send_key(self, value):
        text = value.get("text")
        if text is not None:
            if self.screen.bracketed_paste:
                text = f"\x1b[200~{text}\x1b[201~"
            self.session.write(text)
            return
        if value.get("action") != 0:
            return

        code = value.get("code", -1)
        codepoint = value.get("codePoint", 0)
        modifiers = value.get("modifiers", 0)
        shift = bool(modifiers & 3)
        control = bool(modifiers & 12)
        alt = bool(modifiers & 16)
        arrows = {19: "A", 20: "B", 21: "D", 22: "C"}
        if code in arrows:
            final = arrows[code]
            modifier = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if control else 0)
            if modifier > 1:
                sequence = f"\x1b[1;{modifier}{final}"
            elif self.screen.application_cursor_keys:
                sequence = f"\x1bO{final}"
            else:
                sequence = f"\x1b[{final}"
            self.session.write(sequence)
            return

        if control:
            if codepoint:
                char = chr(codepoint).lower()
            elif 29 <= code <= 54:
                char = chr(ord("a") + code - 29)
            elif code == 62:
                char = " "
            else:
                char = ""
            if "a" <= char <= "z":
                self.session.write(chr(ord(char) & 0x1F))
            elif "@" <= char <= "_":
                self.session.write(chr(ord(char) & 0x1F))
            elif char == "?":
                self.session.write("\x7f")
            return

        sequences = {
            61: "\t", 66: "\r", 67: "\x7f", 111: "\x1b", 112: "\x1b[3~",
            122: "\x1b[H", 123: "\x1b[F", 92: "\x1b[5~", 93: "\x1b[6~",
            124: "\x1b[2~", 131: "\x1bOP", 132: "\x1bOQ", 133: "\x1bOR",
            134: "\x1bOS", 135: "\x1b[15~", 136: "\x1b[17~",
            137: "\x1b[18~", 138: "\x1b[19~", 139: "\x1b[20~",
            140: "\x1b[21~", 141: "\x1b[23~", 142: "\x1b[24~",
        }
        if code == 61 and shift:
            self.session.write("\x1b[Z")
            return
        sequence = sequences.get(code)
        if sequence is not None:
            self.session.write(("\x1b" if alt else "") + sequence)
        elif codepoint > 0:
            self.session.write(("\x1b" if alt else "") + chr(codepoint))

    def handle_event(self, event):
        if event.type == tg.Event.key and event.id == self.keyboard_input.id:
            self._send_key(event.value)
            return True
        if event.type == tg.Event.touch and event.id in self.row_view_indexes:
            self._send_mouse(event)
            return True
        if event.type == tg.Event.click and (event.id == self.scroll.id
                                               or event.id in self.row_view_ids):
            self.keyboard_input.focus(forcesoft=True)
            return True
        if event.type != tg.Event.click:
            return False
        action = self.buttons.get(event.id)
        if action is None:
            return False
        action, value = action
        if action == "enter":
            self.session.write("\r")
            self.keyboard_input.focus(forcesoft=True)
            return True
        if action == "interrupt":
            self.session.write("\x03")
            self.keyboard_input.focus(forcesoft=True)
            return True
        if action == "key":
            if self.screen.application_cursor_keys and value in {
                "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D",
            }:
                value = value.replace("\x1b[", "\x1bO", 1)
            self.session.write(value)
            self.keyboard_input.focus(forcesoft=True)
            return True
        return False

    def _send_mouse(self, event):
        if not self.screen.mouse_tracking:
            return
        action = event.value.get("action")
        pointers = event.value.get("pointers", [])
        pointer = pointers[0] if pointers else None
        if action == tg.Event.touch_down and pointer is not None:
            self.mouse_pressed = True
            self.last_mouse_location = (event.id, pointer)
            self._write_mouse(0, event.id, pointer)
        elif action in (tg.Event.touch_up, tg.Event.touch_cancel):
            if self.mouse_pressed:
                view_id, pointer = self.last_mouse_location or (event.id, pointer)
                if pointer is not None:
                    self._write_mouse(0, view_id, pointer, release=True)
            self.mouse_pressed = False
            self.last_mouse_location = None
        elif action == tg.Event.touch_move and pointer is not None:
            self.last_mouse_location = (event.id, pointer)
            if self.screen.mouse_tracking == 1003 or (
                self.screen.mouse_tracking == 1002 and self.mouse_pressed
            ):
                self._write_mouse(0 if self.mouse_pressed else 3,
                                  event.id, pointer, motion=True)

    def _write_mouse(self, button, view_id, pointer, release=False, motion=False):
        row = self.row_view_indexes[view_id]
        row += int(pointer["y"] / self.density / 18)
        if self.screen.alternate is None:
            row -= len(self.screen.scrollback)
        if not 0 <= row < self.screen.rows:
            return
        column = max(0, min(self.screen.columns - 1,
                            int(pointer["x"] / self.density / 9)))
        row += 1
        column += 1
        code = button + (32 if motion else 0)
        if release and not self.screen.mouse_sgr:
            code = 3
        if self.screen.mouse_sgr:
            suffix = "m" if release else "M"
            sequence = f"\x1b[<{code};{column};{row}{suffix}"
        else:
            sequence = "\x1b[M" + "".join(chr(min(255, value + 32))
                                           for value in (code, column, row))
        self.session.write(sequence)

    def _resize_output(self, columns):
        self.output_rows.setwidth(columns * 9)

    def _render(self, force=False):
        rows = self.screen.render_rows()
        self.output_rows.setwidth(self.screen.columns * 9)
        self.output_rows.setheight(len(rows) * 18)
        if force or len(rows) != len(self.row_views):
            self.output_rows.clearchildren()
            self.row_views = []
            self.rendered_rows = []
            self.row_view_ids = set()
            self.row_view_indexes = {}
            self.mouse_touch_enabled = False
            for _ in rows:
                row = tg.TextView(self.activity, "", self.output_rows)
                row.setdimensions(self.screen.columns * 9, 18)
                row.settextsize(13)
                row.settextcolor(self.screen.default_fg)
                row.setbackgroundcolor(self.screen.default_bg)
                row.sendclickevent(True)
                self.row_views.append(row)
                self.row_view_ids.add(row.id)
                self.row_view_indexes[row.id] = len(self.row_views) - 1
                self.rendered_rows.append(None)
        changed = []
        for index, runs in enumerate(rows):
            signature = tuple(runs)
            if signature == self.rendered_rows[index]:
                continue
            row = {"id": self.row_views[index].id,
                   "text": "\u200e" + "".join(run[0] for run in runs),
                   "runs": [
                       {"text": text, "foreground": foreground,
                        "background": background, "bold": bold,
                        "italic": italic, "underline": underline,
                        "strikethrough": strikethrough}
                       for text, foreground, background, bold, italic, underline,
                       strikethrough in runs if text
                   ]}
            self.row_views[index].settext(row["text"])
            changed.append(row)
            self.rendered_rows[index] = signature
        if changed:
            self.activity.c.send_msg({
                "method": "setStyledRows",
                "params": {"aid": self.activity.aid, "rows": changed},
            })

    def on_tick(self):
        data = self.session.read_output()
        if data:
            self.screen.feed(self.decoder.decode(data))
            for response in self.screen.take_responses():
                self.session.write(response)
            if self.screen.title and self.screen.title != self.displayed_title:
                self.displayed_title = self.screen.title
                self.title_view.settext(self.displayed_title)
            self._render()
            self._sync_mouse_touch()

    def on_window_resize(self, width, height):
        columns = max(20, round(width / (8.5 * self.density)))
        rows = max(5, round((height - 136 * self.density) / (18 * self.density)))
        self.screen.resize(columns, rows)
        self.session.resize(columns, rows)
        self._resize_output(columns)
        self._render()
        self._sync_mouse_touch()
        self._position_controls(width)

    def _sync_mouse_touch(self):
        enabled = bool(self.screen.mouse_tracking)
        if enabled == self.mouse_touch_enabled:
            return
        if not enabled and self.mouse_pressed and self.last_mouse_location is not None:
            view_id, pointer = self.last_mouse_location
            self._write_mouse(0, view_id, pointer, release=True)
            self.mouse_pressed = False
            self.last_mouse_location = None
        for row in self.row_views:
            self.activity.c.send_msg({
                "method": "sendTouchEventPassthrough",
                "params": {"aid": self.activity.aid, "id": row.id, "send": enabled},
            })
        self.mouse_touch_enabled = enabled

    def close(self):
        self.session.close()


APP = TerminalApp()
