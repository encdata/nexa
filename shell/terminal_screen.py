from collections import deque


ANSI_COLORS = (
    0xFF000000, 0xFFCD3131, 0xFF0DBC79, 0xFFE5E510,
    0xFF2472C8, 0xFFBC3FBC, 0xFF11A8CD, 0xFFE5E5E5,
    0xFF666666, 0xFFF14C4C, 0xFF23D18B, 0xFFF5F543,
    0xFF3B8EEA, 0xFFD670D6, 0xFF29B8DB, 0xFFFFFFFF,
)


class TerminalScreen:
    DEC_GRAPHICS = {
        "`": "◆", "a": "▒", "f": "°", "g": "±", "j": "┘", "k": "┐",
        "l": "┌", "m": "└", "n": "┼", "q": "─", "t": "├", "u": "┤",
        "v": "┴", "w": "┬", "x": "│", "y": "≤", "z": "≥", "{": "π",
        "|": "≠", "}": "£", "~": "·",
    }

    def __init__(self, columns=80, rows=24, scrollback=300):
        self.scrollback = deque(maxlen=scrollback)
        self.columns = max(1, columns)
        self.rows = max(1, rows)
        self.default_fg = 0xFFE8EDF2
        self.default_bg = 0xFF171C24
        self.foreground = self.default_fg
        self.background = self.default_bg
        self.bold = False
        self.reverse = False
        self.primary = self._blank()
        self.alternate = None
        self.screen = self.primary
        self.cursor_x = 0
        self.cursor_y = 0
        self.saved_cursor = (0, 0)
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self.cursor_visible = True
        self.state = "text"
        self.sequence = ""
        self.charset = "ascii"

    def _style(self):
        fg, bg = self.foreground, self.background
        if self.bold and fg in ANSI_COLORS[:8]:
            fg = ANSI_COLORS[ANSI_COLORS.index(fg) + 8]
        if self.reverse:
            fg, bg = bg, fg
        return fg, bg, self.bold

    def _blank_cell(self):
        fg, bg, bold = self._style()
        return " ", fg, bg, bold

    def _blank_row(self):
        return [self._blank_cell() for _ in range(self.columns)]

    def _blank(self):
        return [self._blank_row() for _ in range(self.rows)]

    def feed(self, text):
        for char in text:
            if self.state == "osc":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "osc_escape"
                continue
            if self.state == "osc_escape":
                self.state = "text" if char == "\\" else "osc"
                continue
            if self.state == "charset":
                self.charset = "dec" if char == "0" else "ascii"
                self.state = "text"
                continue
            if self.state == "escape":
                self._escape(char)
                continue
            if self.state == "csi":
                if "@" <= char <= "~":
                    self._csi(char, self.sequence)
                    self.sequence = ""
                    self.state = "text"
                elif len(self.sequence) < 128:
                    self.sequence += char
                continue
            if char == "\x1b":
                self.state = "escape"
            elif char == "\r":
                self.cursor_x = 0
            elif char == "\n":
                self._line_feed()
            elif char in ("\b", "\x7f"):
                self.cursor_x = max(0, self.cursor_x - 1)
            elif char == "\t":
                self.cursor_x = min(self.columns - 1, (self.cursor_x // 8 + 1) * 8)
            elif char >= " ":
                self._put(char)

    def resize(self, columns, rows):
        columns = max(1, columns)
        rows = max(1, rows)
        if columns == self.columns and rows == self.rows:
            return
        self.columns = columns
        self.rows = rows
        self.primary = self._resize_buffer(self.primary)
        if self.alternate is not None:
            self.alternate = self._resize_buffer(self.alternate)
        self.scrollback = deque((self._resize_row(row) for row in self.scrollback),
                                maxlen=self.scrollback.maxlen)
        self.screen = self.alternate if self.alternate is not None else self.primary
        self.cursor_x = min(self.cursor_x, columns - 1)
        self.cursor_y = min(self.cursor_y, rows - 1)
        self.scroll_top = 0
        self.scroll_bottom = rows - 1

    def _resize_buffer(self, buffer):
        resized = [self._resize_row(row) for row in buffer[:self.rows]]
        resized.extend([self._blank_row() for _ in range(self.rows - len(resized))])
        return resized

    def _resize_row(self, row):
        return row[:self.columns] + [self._blank_cell() for _ in range(max(0, self.columns - len(row)))]

    def render_rows(self):
        rows = self.screen if self.alternate is not None else list(self.scrollback) + self.screen
        cursor_row = self.cursor_y + (len(self.scrollback) if self.alternate is None else 0)
        result = []
        for y, source in enumerate(rows):
            cells = source.copy()
            if self.cursor_visible and y == cursor_row:
                char, fg, bg, bold = cells[self.cursor_x]
                cells[self.cursor_x] = ("▏" if char == " " else "█", bg, fg, bold)
            runs = []
            text = []
            style = None
            for char, fg, bg, bold in cells:
                cell_style = (fg, bg, bold)
                if style is not None and cell_style != style:
                    runs.append(("".join(text), *style))
                    text = []
                style = cell_style
                text.append(char)
            if text:
                runs.append(("".join(text), *style))
            result.append(runs)
        return result

    def _escape(self, char):
        self.state = "text"
        if char == "[":
            self.state = "csi"
            self.sequence = ""
        elif char == "]":
            self.state = "osc"
        elif char == "7":
            self.saved_cursor = (self.cursor_x, self.cursor_y)
        elif char == "8":
            self.cursor_x, self.cursor_y = self.saved_cursor
        elif char == "D":
            self._line_feed()
        elif char == "M":
            self._reverse_index()
        elif char == "c":
            self.screen = self.primary
            self.alternate = None
            self.primary = self._blank()
            self.screen = self.primary
            self.cursor_x = self.cursor_y = 0
            self.scrollback.clear()
            self.foreground = self.default_fg
            self.background = self.default_bg
            self.bold = self.reverse = False
        elif char in ("(", ")"):
            self.state = "charset"

    def _csi(self, final, raw):
        private = raw.startswith("?")
        raw = raw.lstrip("?")
        params = []
        for value in raw.split(";") if raw else ():
            try:
                params.append(int(value) if value else 0)
            except ValueError:
                params.append(0)

        def value(index, default=1):
            current = params[index] if index < len(params) else 0
            return current or default

        if private and final in ("h", "l"):
            if 25 in params:
                self.cursor_visible = final == "h"
            if any(p in (47, 1047, 1049) for p in params):
                if final == "h" and self.alternate is None:
                    self.saved_cursor = (self.cursor_x, self.cursor_y)
                    self.alternate = self._blank()
                    self.screen = self.alternate
                    self.cursor_x = self.cursor_y = 0
                    self.scroll_top = 0
                    self.scroll_bottom = self.rows - 1
                elif final == "l" and self.alternate is not None:
                    self.screen = self.primary
                    self.alternate = None
                    self.cursor_x, self.cursor_y = self.saved_cursor
                    self.scroll_top = 0
                    self.scroll_bottom = self.rows - 1
            return

        count = max(1, min(value(0), max(self.columns, self.rows)))
        if final == "A":
            self.cursor_y = max(self.scroll_top, self.cursor_y - count)
        elif final == "B":
            self.cursor_y = min(self.scroll_bottom, self.cursor_y + count)
        elif final == "C":
            self.cursor_x = min(self.columns - 1, self.cursor_x + count)
        elif final == "D":
            self.cursor_x = max(0, self.cursor_x - count)
        elif final in ("E", "F"):
            self.cursor_y = max(self.scroll_top, min(self.scroll_bottom,
                                 self.cursor_y + count if final == "E" else self.cursor_y - count))
            self.cursor_x = 0
        elif final == "G":
            self.cursor_x = min(self.columns - 1, value(0) - 1)
        elif final == "d":
            self.cursor_y = min(self.rows - 1, value(0) - 1)
        elif final in ("H", "f"):
            self.cursor_y = min(self.rows - 1, value(0) - 1)
            self.cursor_x = min(self.columns - 1, value(1) - 1)
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "P":
            self._delete_chars(count)
        elif final == "@":
            self._insert_chars(count)
        elif final == "X":
            self._erase_chars(count)
        elif final == "L":
            self._insert_lines(count)
        elif final == "M":
            self._delete_lines(count)
        elif final == "S":
            self._scroll_up(count)
        elif final == "T":
            self._scroll_down(count)
        elif final == "r":
            self.scroll_top = min(self.rows - 1, value(0) - 1)
            self.scroll_bottom = min(self.rows - 1, value(1, self.rows) - 1)
            if self.scroll_bottom < self.scroll_top:
                self.scroll_top, self.scroll_bottom = 0, self.rows - 1
            self.cursor_x = self.cursor_y = 0
        elif final == "s":
            self.saved_cursor = (self.cursor_x, self.cursor_y)
        elif final == "u":
            self.cursor_x, self.cursor_y = self.saved_cursor
        elif final == "m":
            self._sgr(params)

    def _sgr(self, params):
        params = params or [0]
        index = 0
        while index < len(params):
            code = params[index]
            if code == 0:
                self.foreground, self.background = self.default_fg, self.default_bg
                self.bold = self.reverse = False
            elif code == 1:
                self.bold = True
            elif code == 22:
                self.bold = False
            elif code == 7:
                self.reverse = True
            elif code == 27:
                self.reverse = False
            elif code == 39:
                self.foreground = self.default_fg
            elif code == 49:
                self.background = self.default_bg
            elif 30 <= code <= 37:
                self.foreground = ANSI_COLORS[code - 30]
            elif 90 <= code <= 97:
                self.foreground = ANSI_COLORS[code - 90 + 8]
            elif 40 <= code <= 47:
                self.background = ANSI_COLORS[code - 40]
            elif 100 <= code <= 107:
                self.background = ANSI_COLORS[code - 100 + 8]
            elif code in (38, 48) and index + 1 < len(params):
                target = "foreground" if code == 38 else "background"
                mode = params[index + 1]
                if mode == 5 and index + 2 < len(params):
                    setattr(self, target, self._indexed_color(params[index + 2]))
                    index += 2
                elif mode == 2 and index + 4 < len(params):
                    red, green, blue = params[index + 2:index + 5]
                    setattr(self, target, 0xFF000000 | (red << 16) | (green << 8) | blue)
                    index += 4
            index += 1

    @staticmethod
    def _indexed_color(index):
        if index < 16:
            return ANSI_COLORS[max(0, index)]
        if index < 232:
            value = index - 16
            red, green, blue = value // 36, (value // 6) % 6, value % 6
            channel = lambda part: 0 if part == 0 else 55 + part * 40
            return 0xFF000000 | (channel(red) << 16) | (channel(green) << 8) | channel(blue)
        gray = 8 + max(0, min(23, index - 232)) * 10
        return 0xFF000000 | (gray << 16) | (gray << 8) | gray

    def _put(self, char):
        if self.cursor_x >= self.columns:
            self.cursor_x = 0
            self._line_feed()
        if self.charset == "dec":
            char = self.DEC_GRAPHICS.get(char, char)
        fg, bg, bold = self._style()
        self.screen[self.cursor_y][self.cursor_x] = (char, fg, bg, bold)
        self.cursor_x += 1

    def _line_feed(self):
        if self.cursor_y == self.scroll_bottom:
            self._scroll_up(1)
        else:
            self.cursor_y = min(self.rows - 1, self.cursor_y + 1)

    def _reverse_index(self):
        if self.cursor_y == self.scroll_top:
            self._scroll_down(1)
        else:
            self.cursor_y = max(0, self.cursor_y - 1)

    def _scroll_up(self, count):
        for _ in range(count):
            removed = self.screen.pop(self.scroll_top)
            self.screen.insert(self.scroll_bottom, self._blank_row())
            if self.alternate is None and self.scroll_top == 0:
                self.scrollback.append(removed)

    def _scroll_down(self, count):
        for _ in range(count):
            self.screen.pop(self.scroll_bottom)
            self.screen.insert(self.scroll_top, self._blank_row())

    def _erase_display(self, mode):
        if mode in (2, 3):
            self.screen[:] = self._blank()
            if mode == 3:
                self.scrollback.clear()
        elif mode == 0:
            self.screen[self.cursor_y][self.cursor_x:] = [self._blank_cell() for _ in range(
                self.columns - self.cursor_x)]
            for y in range(self.cursor_y + 1, self.rows):
                self.screen[y] = self._blank_row()
        elif mode == 1:
            for y in range(self.cursor_y):
                self.screen[y] = self._blank_row()
            self.screen[self.cursor_y][:self.cursor_x + 1] = [self._blank_cell() for _ in range(
                self.cursor_x + 1)]

    def _erase_line(self, mode):
        if mode == 0:
            self.screen[self.cursor_y][self.cursor_x:] = [self._blank_cell() for _ in range(
                self.columns - self.cursor_x)]
        elif mode == 1:
            self.screen[self.cursor_y][:self.cursor_x + 1] = [self._blank_cell() for _ in range(
                self.cursor_x + 1)]
        elif mode == 2:
            self.screen[self.cursor_y] = self._blank_row()

    def _erase_chars(self, count):
        end = min(self.columns, self.cursor_x + count)
        self.screen[self.cursor_y][self.cursor_x:end] = [self._blank_cell() for _ in range(end - self.cursor_x)]

    def _delete_chars(self, count):
        count = min(count, self.columns - self.cursor_x)
        row = self.screen[self.cursor_y]
        del row[self.cursor_x:self.cursor_x + count]
        row.extend(self._blank_cell() for _ in range(count))

    def _insert_chars(self, count):
        count = min(count, self.columns - self.cursor_x)
        row = self.screen[self.cursor_y]
        row[self.cursor_x:self.cursor_x] = [self._blank_cell() for _ in range(count)]
        del row[self.columns:]

    def _insert_lines(self, count):
        for _ in range(min(count, self.scroll_bottom - self.cursor_y + 1)):
            self.screen.insert(self.cursor_y, self._blank_row())
            self.screen.pop(self.scroll_bottom + 1)

    def _delete_lines(self, count):
        for _ in range(min(count, self.scroll_bottom - self.cursor_y + 1)):
            self.screen.pop(self.cursor_y)
            self.screen.insert(self.scroll_bottom, self._blank_row())
