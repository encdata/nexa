import os
import queue
import signal
import threading
import time

import termuxgui as tg

from shell import palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.text_direction import is_rtl


def _read_memory():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            name, value, *_ = line.split()
            if name in ("MemTotal:", "MemAvailable:"):
                values[name] = int(value) * 1024
    total = values.get("MemTotal:", 0)
    available = values.get("MemAvailable:", 0)
    return total, max(0, total - available)


def _read_process(pid, cpu_percent):
    directory = f"/proc/{pid}"
    try:
        with open(f"{directory}/stat", encoding="utf-8") as source:
            raw_stat = source.read()
        close_name = raw_stat.rfind(")")
        name = raw_stat[raw_stat.find("(") + 1:close_name]
        fields = raw_stat[close_name + 2:].split()
        process_state = fields[0]
        parent_pid = int(fields[1])
        ticks = int(fields[11]) + int(fields[12])
        start_time = int(fields[19])
        with open(f"{directory}/status", encoding="utf-8") as source:
            status = source.read().splitlines()
        uid_line = next(line for line in status if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])
        rss_line = next((line for line in status if line.startswith("VmRSS:")), None)
        memory = int(rss_line.split()[1]) * 1024 if rss_line else 0
        try:
            with open(f"{directory}/cmdline", "rb") as source:
                command = source.read(4096).replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            command = ""
        command_parts = command.split()
        display_name = (" ".join((os.path.basename(command_parts[0]), *command_parts[1:]))
                        if command_parts else name)
        return {"pid": pid, "name": display_name[:64], "comm": name, "state": process_state,
                "ppid": parent_pid, "uid": uid, "memory": memory, "ticks": ticks,
                "start_time": start_time, "cpu": cpu_percent}
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _protected_pids():
    protected = {os.getpid()}
    current = os.getpid()
    while current > 1:
        process = _read_process(current, 0)
        if process is None or process["ppid"] in protected:
            break
        current = process["ppid"]
        protected.add(current)
    return protected


def _sample(previous, previous_time, protected):
    sampled_at = time.monotonic()
    total_memory, used_memory = _read_memory()
    elapsed = sampled_at - previous_time if previous_time is not None else 0
    cpu_count = os.cpu_count() or 1
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    processes = []
    current = {}
    own_uid = os.getuid()
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                process = _read_process(pid, 0)
                if process is None or process["uid"] != own_uid:
                    continue
                old = previous.get(pid)
                if old is not None and old[0] == process["start_time"] and elapsed > 0:
                    ticks = max(0, process["ticks"] - old[1])
                    process["cpu"] = ticks * 100 / (elapsed * ticks_per_second * cpu_count)
                process["protected"] = pid in protected
                current[pid] = (process["start_time"], process["ticks"])
                processes.append(process)
    except OSError:
        pass
    processes.sort(key=lambda process: (process["name"].casefold(), process["pid"]))
    return {"processes": processes, "current": current, "sampled_at": sampled_at,
            "total_memory": total_memory, "used_memory": used_memory,
            "cpu_count": cpu_count}


def _format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


class TaskManagerApp:
    app_id = "task_manager"
    name = "Task Manager"
    icon = "apps"
    preload_ui = False

    def build(self, activity, parent, context=None):
        configuration = activity.getconfiguration()
        density = configuration["density"]
        rtl = is_rtl(configuration)
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        top = tg.LinearLayout(activity, root, vertical=False)
        top.setdimensions(tg.View.MATCH_PARENT, 76)
        top.setlinearlayoutparams(0)
        cpu_card = tg.LinearLayout(activity, top)
        cpu_card.setdimensions(0, 68)
        cpu_card.setlinearlayoutparams(1)
        cpu_card.setmargin(6, "left")
        cpu_card.setmargin(3, "top")
        cpu_card.setmargin(3, "bottom")
        cpu_card.setbackgroundcolor(palette.surface)
        cpu_caption = tg.TextView(activity, "cpu · termux", cpu_card)
        cpu_caption.setdimensions(tg.View.MATCH_PARENT, 28)
        cpu_caption.setlinearlayoutparams(0)
        cpu_caption.setmargin(12, "right" if rtl else "left")
        cpu_caption.settextsize(11)
        cpu_caption.settextcolor(palette.muted)
        cpu_caption.setgravity(0, 1)
        cpu_summary = tg.TextView(activity, "—", cpu_card)
        cpu_summary.setdimensions(tg.View.MATCH_PARENT, 40)
        cpu_summary.setlinearlayoutparams(0)
        cpu_summary.setmargin(12, "right" if rtl else "left")
        cpu_summary.settextsize(19)
        cpu_summary.settextcolor(palette.text)
        cpu_summary.setgravity(0, 1)

        memory_card = tg.LinearLayout(activity, top)
        memory_card.setdimensions(0, 68)
        memory_card.setlinearlayoutparams(1)
        memory_card.setmargin(3, "left")
        memory_card.setmargin(6, "right")
        memory_card.setmargin(3, "top")
        memory_card.setmargin(3, "bottom")
        memory_card.setbackgroundcolor(palette.surface)
        memory_caption = tg.TextView(activity, "memory · device", memory_card)
        memory_caption.setdimensions(tg.View.MATCH_PARENT, 28)
        memory_caption.setlinearlayoutparams(0)
        memory_caption.setmargin(12, "right" if rtl else "left")
        memory_caption.settextsize(11)
        memory_caption.settextcolor(palette.muted)
        memory_caption.setgravity(0, 1)
        memory_summary = tg.TextView(activity, "—", memory_card)
        memory_summary.setdimensions(tg.View.MATCH_PARENT, 40)
        memory_summary.setlinearlayoutparams(0)
        memory_summary.setmargin(12, "right" if rtl else "left")
        memory_summary.settextsize(15)
        memory_summary.settextcolor(palette.text)
        memory_summary.setgravity(0, 1)

        tabs = tg.LinearLayout(activity, root, vertical=False)
        tabs.setdimensions(tg.View.MATCH_PARENT, 44)
        tabs.setlinearlayoutparams(0)
        pages = tg.FrameLayout(activity, root)
        pages.setdimensions(tg.View.MATCH_PARENT, 0)
        pages.setlinearlayoutparams(1)

        process_page = tg.LinearLayout(activity, pages)
        process_page.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        performance_page = tg.LinearLayout(activity, pages)
        performance_page.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        performance_page.setvisibility(tg.View.GONE)

        process_header = tg.LinearLayout(activity, process_page, vertical=False)
        process_header.setdimensions(tg.View.MATCH_PARENT, 48)
        process_header.setlinearlayoutparams(0)
        count_label = tg.TextView(activity, "scanning termux processes…", process_header)
        count_label.setdimensions(0, 48)
        count_label.setlinearlayoutparams(1)
        count_label.setmargin(12, "right" if rtl else "left")
        count_label.settextsize(12)
        count_label.settextcolor(palette.muted)
        count_label.setgravity(0, 1)
        filter_label = tg.TextView(activity, "filter", process_header)
        filter_label.setdimensions(44, 48)
        filter_label.setlinearlayoutparams(0)
        filter_label.settextsize(11)
        filter_label.settextcolor(palette.muted)
        filter_label.setgravity(1, 1)
        search = tg.EditText(activity, "", process_header, singleline=True, line=False,
                             inputtype="text")
        search.setdimensions(124, 38)
        search.setlinearlayoutparams(0)
        search.setmargin(8 if rtl else 4, "left")
        search.setmargin(4 if rtl else 8, "right")
        search.setbackgroundcolor(palette.surface)
        search.settextsize(13)
        search.settextcolor(palette.text)
        search.sendtextevent(True)
        process_scroll = tg.NestedScrollView(activity, process_page, fillviewport=True, nobar=True)
        process_scroll.setheight(0)
        process_scroll.setlinearlayoutparams(1)
        process_rows = tg.LinearLayout(activity, process_scroll)
        process_rows.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)

        performance_scroll = tg.NestedScrollView(activity, performance_page, fillviewport=True, nobar=True)
        performance_scroll.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        performance_content = tg.LinearLayout(activity, performance_scroll)
        performance_content.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        performance_values = {}
        for label in ("cpu usage", "memory in use", "available memory", "logical processors", "termux processes"):
            card = tg.LinearLayout(activity, performance_content)
            card.setdimensions(tg.View.MATCH_PARENT, 66)
            card.setlinearlayoutparams(0)
            card.setmargin(8, "left")
            card.setmargin(8, "right")
            card.setmargin(4, "top")
            card.setmargin(4, "bottom")
            card.setbackgroundcolor(palette.surface)
            caption = tg.TextView(activity, label, card)
            caption.setdimensions(tg.View.MATCH_PARENT, 25)
            caption.setlinearlayoutparams(0)
            caption.setmargin(12, "right" if rtl else "left")
            caption.settextsize(11)
            caption.settextcolor(palette.muted)
            caption.setgravity(0, 1)
            value = tg.TextView(activity, "—", card)
            value.setdimensions(tg.View.MATCH_PARENT, 41)
            value.setlinearlayoutparams(0)
            value.setmargin(12, "right" if rtl else "left")
            value.settextsize(15)
            value.settextcolor(palette.text)
            value.setgravity(0, 1)
            performance_values[label] = value

        tab_actions = {}
        tab_buttons = {}
        tab_entries = (("Processes", process_page), ("Performance", performance_page))
        if rtl:
            tab_entries = tuple(reversed(tab_entries))
        for label, page in tab_entries:
            button = tg.TextView(activity, label, tabs)
            button.setheight(44)
            button.setlinearlayoutparams(1)
            button.setbackgroundcolor(palette.accent if label == "Processes" else palette.surface_light)
            button.settextcolor(palette.text)
            button.settextsize(13)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            tab_actions[button.id] = (label, page)
            tab_buttons[button.id] = button

        menu = ContextMenu(activity, parent,
                           lambda: tuple(round(value / density) for value in parent.getdimensions()),
                           actions=("end_task",), labels={"end_task": "end task"})
        row_views = {}
        state = {"snapshot": None, "selected": None, "active_page": process_page}
        results = queue.Queue(maxsize=1)
        stop = threading.Event()
        protected = _protected_pids()

        def sample_processes():
            previous = {}
            previous_time = None
            while not stop.is_set():
                try:
                    snapshot = _sample(previous, previous_time, protected)
                    previous = snapshot["current"]
                    previous_time = snapshot["sampled_at"]
                    if results.full():
                        try:
                            results.get_nowait()
                        except queue.Empty:
                            pass
                    results.put_nowait(snapshot)
                except (OSError, ValueError) as error:
                    print(f"nexa task manager: {error}")
                stop.wait(1)

        sampler = threading.Thread(target=sample_processes, name="nexa-process-sampler", daemon=True)
        sampler.start()

        def make_process_row(process):
            row = tg.LinearLayout(activity, process_rows, vertical=False)
            row.setdimensions(tg.View.MATCH_PARENT, 76)
            row.setlinearlayoutparams(0)
            row.setmargin(6, "left")
            row.setmargin(6, "right")
            row.setmargin(3, "top")
            row.setmargin(3, "bottom")
            row.setbackgroundcolor(palette.surface)
            row.sendclickevent(True)
            row.sendlongclickevent(True)
            def add_icon():
                icon = tg.ImageView(activity, row)
                icon.setdimensions(32, 32)
                icon.setimage(load("terminal"))
                icon.setmargin(12, "left")
                icon.setmargin(10, "right")
                icon.setmargin(22, "top")
                icon.setlinearlayoutparams(0)

            if not rtl:
                add_icon()
            info = tg.LinearLayout(activity, row)
            info.setdimensions(0, 76)
            info.setlinearlayoutparams(1)
            title = tg.TextView(activity, process["name"], info)
            title.setdimensions(tg.View.MATCH_PARENT, 38)
            title.setlinearlayoutparams(0)
            title.setmargin(8 if rtl else 2, "left")
            title.setmargin(2 if rtl else 8, "right")
            title.settextsize(14)
            title.settextcolor(palette.text)
            title.setgravity(0, 1)
            details = tg.TextView(activity, "", info)
            details.setdimensions(tg.View.MATCH_PARENT, 34)
            details.setlinearlayoutparams(0)
            details.setmargin(8 if rtl else 2, "left")
            details.setmargin(2 if rtl else 8, "right")
            details.settextsize(11)
            details.settextcolor(palette.muted)
            details.setgravity(0, 1)
            if rtl:
                add_icon()
            return {"view": row, "title": title, "details": details,
                    "start_time": process["start_time"]}

        def render(snapshot):
            processes = snapshot["processes"]
            live = {process["pid"] for process in processes}
            for pid in tuple(row_views):
                if pid not in live:
                    row_views.pop(pid)["view"].delete()
            for process in processes:
                pid = process["pid"]
                row = row_views.get(pid)
                if row is not None and row["start_time"] != process["start_time"]:
                    row["view"].delete()
                    row = None
                if row is None:
                    row = make_process_row(process)
                    row_views[pid] = row
                row["title"].settext(process["name"])
                row["details"].settext(
                    f"pid {pid}   cpu {process['cpu']:.1f}%   ram {_format_size(process['memory'])}   {process['state']}")
                row["view"].setbackgroundcolor(
                    palette.surface_light if state.get("selected") == pid else palette.surface)
                query = state.get("query", "")
                matches = query in process["name"].casefold() or query in str(pid)
                row["view"].setvisibility(tg.View.VISIBLE if matches else tg.View.GONE)
                row_views[pid]["process"] = process

            visible_count = sum(1 for process in processes
                               if state.get("query", "") in process["name"].casefold()
                               or state.get("query", "") in str(process["pid"]))
            count_label.settext(f"{visible_count} of {len(processes)} processes")
            cpu = sum(process["cpu"] for process in processes)
            total = snapshot["total_memory"]
            used = snapshot["used_memory"]
            cpu_summary.settext(f"{cpu:.1f}%")
            memory_summary.settext(f"{_format_size(used)} / {_format_size(total)}")
            performance_values["cpu usage"].settext(
                f"{cpu:.1f}% of total cpu capacity")
            performance_values["memory in use"].settext(
                f"{_format_size(used)} / {_format_size(total)}")
            performance_values["available memory"].settext(
                _format_size(max(0, total - used)))
            performance_values["logical processors"].settext(
                str(snapshot["cpu_count"]))
            performance_values["termux processes"].settext(
                str(len(processes)))
            state["snapshot"] = snapshot

        def handle_event(event):
            if menu.handle_event(event):
                return True
            if event.type == tg.Event.back and menu.visible:
                menu.hide()
                return True
            if event.type == tg.Event.text and event.id == search.id:
                state["query"] = search.gettext().strip().casefold()
                if state["snapshot"] is not None:
                    render(state["snapshot"])
                return True
            if event.type == tg.Event.click:
                selected_tab = tab_actions.get(event.id)
                if selected_tab is not None:
                    _label, page = selected_tab
                    process_page.setvisibility(tg.View.VISIBLE if page == process_page else tg.View.GONE)
                    performance_page.setvisibility(tg.View.VISIBLE if page == performance_page else tg.View.GONE)
                    state["active_page"] = page
                    for tab_id, (tab_label, _page) in tab_actions.items():
                        tab_buttons[tab_id].setbackgroundcolor(
                            palette.accent if tab_label == _label else palette.surface_light)
                    return True
                selected = next((pid for pid, row in row_views.items()
                                 if row["view"].id == event.id), None)
                if selected is not None:
                    state["selected"] = selected
                    row_views[selected]["view"].setbackgroundcolor(palette.surface_light)
                    return True
            if event.type == tg.Event.longClick:
                selected = next((pid for pid, row in row_views.items()
                                 if row["view"].id == event.id), None)
                if selected is not None:
                    state["selected"] = selected
                    menu.show(("end_task",), 16, 64, end_task)
                    return True
            return False

        def end_task(action):
            if action != "end_task":
                return
            pid = state["selected"]
            row = row_views.get(pid)
            process = row.get("process") if row is not None else None
            if process is None:
                return
            if process["protected"]:
                count_label.settext("can't end nexa or its terminal session")
                return
            current = _read_process(pid, 0)
            if (current is None or current["uid"] != os.getuid()
                    or current["start_time"] != process["start_time"]):
                count_label.settext("process already exited")
                return
            try:
                os.kill(pid, signal.SIGTERM)
                count_label.settext(f"sent end task to {process['name']}")
            except (ProcessLookupError, PermissionError) as error:
                count_label.settext(f"couldn't end task: {error}")

        def on_tick():
            try:
                snapshot = results.get_nowait()
            except queue.Empty:
                return
            render(snapshot)

        def close():
            stop.set()

        handle_event.on_tick = on_tick
        handle_event.close = close
        return handle_event


APP = TaskManagerApp()
