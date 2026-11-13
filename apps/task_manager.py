import os
import queue
import signal
import threading
import time

import termuxgui as tg

from shell import palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.recycler_view import RecyclerListView
from shell.text_direction import force_ltr, is_rtl


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
    preload_ui = True
    build_threaded = True

    def build(self, activity, parent, context=None):
        configuration = activity.getconfiguration()
        density = configuration["density"]
        rtl = is_rtl(configuration)
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        force_ltr(root)
        root.setbackgroundcolor(palette.window_content)

        top = tg.LinearLayout(activity, root, vertical=False)
        top.setdimensions(tg.View.MATCH_PARENT, 76)
        top.setlinearlayoutparams(0)

        def add_usage_card(title, detail, left_margin, right_margin):
            card = tg.LinearLayout(activity, top, vertical=False)
            card.setdimensions(0, 68)
            card.setlinearlayoutparams(1)
            card.setmargin(left_margin, "left")
            card.setmargin(right_margin, "right")
            card.setmargin(3, "top")
            card.setmargin(3, "bottom")
            card.setbackgroundcolor(palette.surface)

            gauge_holder = tg.FrameLayout(activity, card)
            gauge_holder.setdimensions(52, 52)
            gauge_holder.setlinearlayoutparams(0)
            gauge_holder.setmargin(8, "left")
            gauge_holder.setmargin(6, "right")
            gauge_id = activity.c.send_read_msg({"method": "createProgressBar", "params": {
                "aid": activity.aid, "parent": gauge_holder.id, "circular": True,
                "progressColor": palette.accent, "trackColor": palette.surface_light,
            }})
            gauge = tg.View(activity, gauge_id)
            gauge.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            percent = tg.TextView(activity, "0%", gauge_holder)
            percent.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            percent.settextsize(10)
            percent.settextcolor(palette.text)
            percent.setgravity(1, 1)

            details = tg.LinearLayout(activity, card)
            details.setdimensions(0, 68)
            details.setlinearlayoutparams(1)
            details.setmargin(4, "left")
            details.setmargin(8, "right")
            caption = tg.TextView(activity, title, details)
            caption.setdimensions(tg.View.MATCH_PARENT, 28)
            caption.setlinearlayoutparams(0)
            caption.settextsize(11)
            caption.settextcolor(palette.muted)
            caption.setgravity(0, 1)
            value = tg.TextView(activity, detail, details)
            value.setdimensions(tg.View.MATCH_PARENT, 40)
            value.setlinearlayoutparams(0)
            value.settextsize(14)
            value.settextcolor(palette.text)
            value.setgravity(0, 1)
            return gauge, percent, value

        def set_gauge(gauge, percent_label, value):
            value = min(100, max(0, round(value)))
            activity.c.send_msg({"method": "setProgress", "params": {
                "aid": activity.aid, "id": gauge.id, "progress": value,
            }})
            percent_label.settext(f"{value}%")

        cpu_gauge, cpu_percent, cpu_summary = add_usage_card(
            "cpu usage", "termux processes", 6, 3)
        memory_gauge, memory_percent, memory_summary = add_usage_card(
            "ram used", "device memory", 3, 6)

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
        columns = tg.LinearLayout(activity, process_page, vertical=False)
        columns.setdimensions(tg.View.MATCH_PARENT, 34)
        columns.setlinearlayoutparams(0)
        sort_actions = {}
        sort_headers = {}
        for label, key, width in (("name", "name", 0), ("cpu", "cpu", 56),
                                  ("memory", "memory", 72)):
            header = tg.TextView(activity, label, columns)
            header.setdimensions(width, 34)
            header.setlinearlayoutparams(1 if key == "name" else 0)
            header.setmargin(10 if key == "name" else 0, "left")
            header.settextsize(11)
            header.settextcolor(palette.muted)
            header.setgravity(0 if key == "name" else 1, 1)
            header.sendclickevent(True)
            sort_actions[header.id] = key
            sort_headers[key] = header
        process_rows = RecyclerListView(activity, process_page)
        force_ltr(process_rows)
        process_rows.setdimensions(tg.View.MATCH_PARENT, 0)
        process_rows.setlinearlayoutparams(1)

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
        state = {"snapshot": None, "selected": None, "active_page": process_page,
                 "sort_by": "name", "sort_descending": False,
                 "display_processes": {}}
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

        sampler = None

        def start_sampler():
            nonlocal sampler
            if sampler is not None:
                return
            sampler = threading.Thread(target=sample_processes,
                                       name="nexa-process-sampler", daemon=True)
            sampler.start()

        def render(snapshot, keep_scroll=True):
            key = state["sort_by"]
            processes = sorted(snapshot["processes"],
                               key=lambda process: (process[key] if key != "name"
                                                    else process[key].casefold(), process["pid"]),
                               reverse=state["sort_descending"])
            for sort_key, header in sort_headers.items():
                arrow = (" ↓" if state["sort_descending"] else " ↑") if sort_key == key else ""
                header.settext(sort_key + arrow)
            query = state.get("query", "")
            visible = [process for process in processes
                       if query in process["name"].casefold() or query in str(process["pid"])]
            display_processes = {}
            items = []
            for process in visible:
                process_key = f"{process['pid']}:{process['start_time']}"
                display_processes[process_key] = process
                items.append({
                    "key": process_key,
                    "text": process["name"],
                    "subtitle": f"pid {process['pid']} · {process['state']}",
                    "cpu": f"{process['cpu']:.1f}%",
                    "memory": _format_size(process["memory"]),
                    "image": load("terminal"),
                    "background": (palette.surface_light
                                   if state["selected"] == process_key else palette.surface),
                })
            state["display_processes"] = display_processes
            process_rows.setitems(items, keep_scroll=keep_scroll)
            count_label.settext(f"{len(visible)} of {len(processes)} processes")
            cpu = sum(process["cpu"] for process in processes)
            total = snapshot["total_memory"]
            used = snapshot["used_memory"]
            set_gauge(cpu_gauge, cpu_percent, cpu)
            cpu_summary.settext(f"{len(processes)} processes")
            memory_percent_used = used / total * 100 if total else 0
            set_gauge(memory_gauge, memory_percent, memory_percent_used)
            memory_summary.settext(_format_size(used))
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

        def process_key_from_event(event):
            key = event.value.get("key")
            if key is not None:
                return key
            position = event.value.get("position", -1)
            keys = tuple(state["display_processes"])
            return keys[position] if 0 <= position < len(keys) else None

        def handle_event(event):
            if menu.handle_event(event):
                return True
            if event.type == tg.Event.back and menu.visible:
                menu.hide()
                return True
            if event.type == tg.Event.text and event.id == search.id:
                state["query"] = search.gettext().strip().casefold()
                if state["snapshot"] is not None:
                    render(state["snapshot"], keep_scroll=False)
                return True
            if event.type == tg.Event.click:
                if event.id == process_rows.id:
                    process_key = process_key_from_event(event)
                    if process_key not in state["display_processes"]:
                        return True
                    state["selected"] = process_key
                    render(state["snapshot"])
                    return True
                sort_by = sort_actions.get(event.id)
                if sort_by is not None:
                    if state["sort_by"] == sort_by:
                        state["sort_descending"] = not state["sort_descending"]
                    else:
                        state["sort_by"] = sort_by
                        state["sort_descending"] = sort_by != "name"
                    if state["snapshot"] is not None:
                        render(state["snapshot"], keep_scroll=False)
                    return True
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
            if event.type == tg.Event.longClick:
                if event.id == process_rows.id:
                    process_key = process_key_from_event(event)
                    if process_key in state["display_processes"]:
                        state["selected"] = process_key
                        render(state["snapshot"])
                        menu.show(("end_task",), 16, 64, end_task)
                    return True
            return False

        def end_task(action):
            if action != "end_task":
                return
            process = state["display_processes"].get(state["selected"])
            if process is None:
                return
            pid = process["pid"]
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
            process_rows.close()

        handle_event.on_tick = on_tick
        handle_event.on_window_open = start_sampler
        handle_event.close = close
        return handle_event


APP = TaskManagerApp()
