import json
import os
import tempfile
import time
from datetime import datetime

import termuxgui as tg

from shell import palette


DATA_PATH = os.path.expanduser("~/.config/nexa/clock.json")


def _load_state():
    try:
        with open(DATA_PATH, encoding="utf-8") as source:
            state = json.load(source)
    except (OSError, ValueError):
        state = {}
    return {
        "stopwatch_elapsed": max(0.0, float(state.get("stopwatch_elapsed", 0))),
        "stopwatch_started_at": state.get("stopwatch_started_at"),
        "timer_duration": max(1, int(state.get("timer_duration", 300))),
        "timer_deadline": state.get("timer_deadline"),
        "timer_remaining": max(0.0, float(state.get("timer_remaining", 300))),
        "laps": list(state.get("laps", []))[-8:],
        "use_24_hour": bool(state.get("use_24_hour", True)),
    }


def _save_state(state):
    directory = os.path.dirname(DATA_PATH)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".clock-", delete=False) as target:
            temporary_path = target.name
            json.dump(state, target)
        os.replace(temporary_path, DATA_PATH)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def _duration(seconds, tenths=False):
    value = max(0, int(seconds * (10 if tenths else 1)))
    tenth = value % 10 if tenths else 0
    seconds = value // 10 if tenths else value
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    return f"{clock}.{tenth}" if tenths else clock


class ClockApp:
    app_id = "clock"
    name = "Clock"
    icon = "clock"
    preload_ui = False

    def build(self, activity, parent, context=None):
        state = _load_state()
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        tabs = tg.LinearLayout(activity, root, vertical=False)
        tabs.setdimensions(tg.View.MATCH_PARENT, 48)
        tabs.setlinearlayoutparams(0)
        pages = tg.FrameLayout(activity, root)
        pages.setdimensions(tg.View.MATCH_PARENT, 0)
        pages.setlinearlayoutparams(1)

        buttons = {}
        tab_buttons = {}

        def make_button(parent_view, label, action, width=0, height=48):
            button = tg.TextView(activity, label, parent_view)
            button.setdimensions(width, height)
            button.setlinearlayoutparams(1 if width == 0 else 0)
            button.setmargin(4, "left")
            button.setmargin(4, "right")
            button.setbackgroundcolor(palette.surface_light)
            button.settextcolor(palette.text)
            button.settextsize(14)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            buttons[button.id] = action
            return button

        def make_page():
            page = tg.LinearLayout(activity, pages)
            page.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            page.setvisibility(tg.View.GONE)
            return page

        def add_space(parent_view):
            space = tg.LinearLayout(activity, parent_view)
            space.setdimensions(tg.View.MATCH_PARENT, 0)
            space.setlinearlayoutparams(1)

        def add_text(parent_view, value, height, size, color=palette.text):
            view = tg.TextView(activity, value, parent_view)
            view.setdimensions(tg.View.MATCH_PARENT, height)
            view.setlinearlayoutparams(0)
            view.settextsize(size)
            view.settextcolor(color)
            view.setgravity(1, 1)
            return view

        clock_page = make_page()
        watch_page = make_page()
        timer_page = make_page()
        for label, page in (("clock", clock_page), ("stopwatch", watch_page), ("timer", timer_page)):
            tab = make_button(tabs, label, ("tab", page), height=48)
            tab_buttons[page.id] = tab

        add_space(clock_page)
        clock_time = add_text(clock_page, "", 100, 58)
        clock_date = add_text(clock_page, "", 36, 18)
        clock_zone = add_text(clock_page, "", 30, 13, palette.muted)
        format_button = make_button(clock_page, "24-hour time", ("format",), height=44)
        format_button.setmargin(12, "top")
        add_space(clock_page)

        add_space(watch_page)
        watch_value = add_text(watch_page, "", 100, 52)
        watch_status = add_text(watch_page, "stopwatch", 32, 14, palette.muted)
        watch_controls = tg.LinearLayout(activity, watch_page, vertical=False)
        watch_controls.setdimensions(tg.View.MATCH_PARENT, 56)
        watch_controls.setlinearlayoutparams(0)
        watch_start = make_button(watch_controls, "start", ("watch_start",))
        make_button(watch_controls, "lap", ("watch_lap",))
        make_button(watch_controls, "reset", ("watch_reset",))
        lap_list = tg.LinearLayout(activity, watch_page)
        lap_list.setdimensions(tg.View.MATCH_PARENT, 0)
        lap_list.setlinearlayoutparams(1)

        add_space(timer_page)
        timer_value = add_text(timer_page, "", 100, 52)
        timer_status = add_text(timer_page, "enter a duration in seconds", 30, 13, palette.muted)
        timer_input = tg.EditText(activity, str(state["timer_duration"]), timer_page,
                                  singleline=True, inputtype="number")
        timer_input.setdimensions(140, 48)
        timer_input.setlinearlayoutparams(0)
        timer_input.setmargin(8, "top")
        timer_input.setbackgroundcolor(palette.surface)
        timer_input.settextcolor(palette.text)
        timer_input.settextsize(18)
        timer_input.setgravity(1, 1)
        timer_controls = tg.LinearLayout(activity, timer_page, vertical=False)
        timer_controls.setdimensions(tg.View.MATCH_PARENT, 56)
        timer_controls.setlinearlayoutparams(0)
        make_button(timer_controls, "start", ("timer_start",))
        make_button(timer_controls, "pause", ("timer_pause",))
        make_button(timer_controls, "reset", ("timer_reset",))
        add_space(timer_page)

        current_page = clock_page
        use_24_hour = state["use_24_hour"]
        stopwatch_running = state["stopwatch_started_at"] is not None
        timer_running = state["timer_deadline"] is not None
        next_clock_update = 0
        next_watch_update = 0
        next_timer_update = 0

        def elapsed():
            started = state["stopwatch_started_at"]
            return state["stopwatch_elapsed"] + (time.time() - started if started is not None else 0)

        def remaining():
            deadline = state["timer_deadline"]
            return max(0, deadline - time.time()) if deadline is not None else state["timer_remaining"]

        def persist():
            _save_state(state)

        def render_laps():
            lap_list.clearchildren()
            laps = state["laps"]
            for index, lap in enumerate(reversed(laps), 1):
                row = tg.TextView(activity, f"lap {len(laps) - index + 1}     {_duration(lap, True)}", lap_list)
                row.setdimensions(tg.View.MATCH_PARENT, 36)
                row.setlinearlayoutparams(0)
                row.setmargin(12, "left")
                row.settextsize(14)
                row.settextcolor(palette.text)
                row.setgravity(0, 1)

        def show_page(page):
            nonlocal current_page
            current_page = page
            for candidate in (clock_page, watch_page, timer_page):
                candidate.setvisibility(tg.View.VISIBLE if candidate == page else tg.View.GONE)
            for page_id, tab in tab_buttons.items():
                tab.setbackgroundcolor(palette.accent if page_id == page.id else palette.surface_light)

        def update_clock():
            now = datetime.now().astimezone()
            clock_time.settext(now.strftime("%H:%M:%S") if use_24_hour else now.strftime("%I:%M:%S %p"))
            clock_date.settext(now.strftime("%A, %B %d, %Y"))
            clock_zone.settext(f"{now.tzname()} · {now.strftime('UTC%z')}")
            format_button.settext("12-hour time" if use_24_hour else "24-hour time")

        def update_watch():
            watch_value.settext(_duration(elapsed(), True))

        def update_timer():
            nonlocal timer_running
            timer_value.settext(_duration(remaining()))
            if timer_running and remaining() <= 0:
                state["timer_remaining"] = 0
                state["timer_deadline"] = None
                timer_running = False
                timer_status.settext("time's up")
                persist()
            else:
                timer_status.settext("counting down" if timer_running else "paused")

        def update():
            update_clock()
            update_watch()
            watch_status.settext("running" if stopwatch_running else "paused")
            update_timer()

        def handle_event(event):
            nonlocal use_24_hour, stopwatch_running, timer_running
            if event.type != tg.Event.click:
                return False
            action = buttons.get(event.id)
            if action is None:
                return False
            if action[0] == "tab":
                show_page(action[1])
            elif action[0] == "format":
                use_24_hour = not use_24_hour
                state["use_24_hour"] = use_24_hour
                update_clock()
                persist()
            elif action[0] == "watch_start":
                if stopwatch_running:
                    state["stopwatch_elapsed"] = elapsed()
                    state["stopwatch_started_at"] = None
                    stopwatch_running = False
                else:
                    state["stopwatch_started_at"] = time.time()
                    stopwatch_running = True
                watch_status.settext("running" if stopwatch_running else "paused")
                watch_start.settext("pause" if stopwatch_running else "resume")
                update_watch()
                persist()
            elif action[0] == "watch_lap" and stopwatch_running:
                state["laps"].append(elapsed())
                state["laps"] = state["laps"][-8:]
                render_laps()
                persist()
            elif action[0] == "watch_reset":
                state["stopwatch_elapsed"] = 0
                state["stopwatch_started_at"] = None
                state["laps"] = []
                stopwatch_running = False
                watch_status.settext("paused")
                watch_start.settext("start")
                update_watch()
                render_laps()
                persist()
            elif action[0] == "timer_start":
                try:
                    duration = max(1, min(86400, int(timer_input.gettext().strip())))
                except ValueError:
                    timer_status.settext("enter seconds from 1 to 86400")
                    return True
                state["timer_duration"] = duration
                state["timer_remaining"] = duration
                state["timer_deadline"] = time.time() + duration
                timer_running = True
                timer_status.settext("counting down")
                persist()
            elif action[0] == "timer_pause":
                if timer_running:
                    state["timer_remaining"] = remaining()
                    state["timer_deadline"] = None
                    timer_running = False
                    timer_status.settext("paused")
                    persist()
            elif action[0] == "timer_reset":
                state["timer_deadline"] = None
                state["timer_remaining"] = state["timer_duration"]
                timer_running = False
                timer_status.settext("timer reset")
                persist()
            update()
            return True

        class ClockSession:
            def handle_event(self, event):
                return handle_event(event)

            def on_tick(self):
                nonlocal next_clock_update, next_watch_update, next_timer_update
                now = time.monotonic()
                if now >= next_clock_update:
                    update_clock()
                    next_clock_update = now + 1
                if stopwatch_running and now >= next_watch_update:
                    update_watch()
                    next_watch_update = now + 0.1
                if now >= next_timer_update:
                    update_timer()
                    next_timer_update = now + 1

        render_laps()
        show_page(clock_page)
        update()
        return ClockSession()


APP = ClockApp()
