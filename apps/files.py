import os
import stat
from datetime import datetime

import termuxgui as tg

from shell import file_actions, file_associations, palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.open_with import OpenWithChooser
from shell.recycler_view import RecyclerListView
from shell.text_prompt import TextPrompt
from shell.text_direction import force_ltr, is_rtl


def _format_size(size):
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024


class FilesApp:
    app_id = "files"
    name = "File Explorer"
    icon = "folder"
    preload_ui = True
    build_threaded = True

    def get_title(self, context=None):
        if context is None:
            return self.name
        if isinstance(context, dict) and "properties" in context:
            name = os.path.basename(context["properties"].rstrip(os.sep)) or context["properties"]
            return f"{name} - properties"
        return os.path.basename(os.path.normpath(context)) or context

    def build(self, activity, parent, context=None):
        return self._build(activity, parent, context, {}, None)

    def build_with_services(self, activity, parent, context, services):
        return self._build(activity, parent, context, services["apps"], services["open_app"],
                           services.get("association_changed"))

    def _build(self, activity, parent, context, apps, open_app, association_changed=None):
        if isinstance(context, dict) and "properties" in context:
            return self._build_properties(
                activity, parent, context["properties"], apps, association_changed)
        path = os.path.abspath(context or os.path.expanduser("~"))
        configuration = activity.getconfiguration()
        root = tg.LinearLayout(activity, parent)
        force_ltr(root)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        toolbar = tg.LinearLayout(activity, root, vertical=False)
        toolbar.setdimensions(tg.View.MATCH_PARENT, 48)
        toolbar.setlinearlayoutparams(0)
        toolbar.setbackgroundcolor(palette.surface_light)

        def add_button(parent_view, label, width=40):
            button = tg.TextView(activity, label, parent_view)
            button.setdimensions(width, 40)
            button.setlinearlayoutparams(0)
            button.setmargin(2, "left")
            button.setmargin(2, "right")
            button.setbackgroundcolor(palette.surface)
            button.settextsize(15)
            button.settextcolor(palette.text)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            return button

        back = add_button(toolbar, "‹", 36)
        forward = add_button(toolbar, "›", 36)
        up = add_button(toolbar, "↑", 36)

        path_label = tg.TextView(activity, path, toolbar)
        path_label.setheight(40)
        path_label.setlinearlayoutparams(1)
        path_label.setmargin(8, "left")
        path_label.setmargin(8, "right")
        path_label.settextsize(13)
        path_label.settextcolor(palette.text)
        path_label.setgravity(0, 1)
        refresh = add_button(toolbar, "↻", 36)

        search_bar = tg.LinearLayout(activity, root)
        search_bar.setdimensions(tg.View.MATCH_PARENT, 44)
        search_bar.setlinearlayoutparams(0)
        search_bar.setbackgroundcolor(palette.surface_light)
        search = tg.EditText(activity, "", search_bar, singleline=True, line=False,
                             inputtype="text")
        search.setdimensions(0, 38)
        search.setlinearlayoutparams(1)
        search.setmargin(10, "left")
        search.setmargin(6, "right")
        search.setbackgroundcolor(palette.window_content)
        search.settextcolor(palette.text)
        search.settextsize(13)
        search.sendtextevent(True)

        body = tg.LinearLayout(activity, root, vertical=False)
        body.setdimensions(tg.View.MATCH_PARENT, 0)
        body.setlinearlayoutparams(1)
        navigation = tg.LinearLayout(activity, body)
        navigation.setdimensions(64, tg.View.MATCH_PARENT)
        navigation.setbackgroundcolor(palette.surface)
        content = tg.LinearLayout(activity, body)
        content.setdimensions(0, tg.View.MATCH_PARENT)
        content.setlinearlayoutparams(1)
        content.setbackgroundcolor(palette.window_content)

        rows = RecyclerListView(activity, content)
        rows.setdimensions(tg.View.MATCH_PARENT, 0)
        rows.setlinearlayoutparams(1)

        status = tg.TextView(activity, "", content)
        status.setdimensions(tg.View.MATCH_PARENT, 24)
        status.setlinearlayoutparams(0)
        status.setmargin(10, "left")
        status.settextsize(11)
        status.settextcolor(palette.muted)
        status.setgravity(0, 1)

        density = configuration["density"]
        menu = ContextMenu(activity, parent, lambda: tuple(
            round(value / density) for value in parent.getdimensions()))
        prompt = TextPrompt(activity, parent, lambda: tuple(
            round(value / density) for value in parent.getdimensions()))
        def on_association_change(path_value, app_id):
            if association_changed is not None:
                association_changed(path_value, app_id)
            render()

        open_with = OpenWithChooser(
            activity, parent, lambda: tuple(
                round(value / density) for value in parent.getdimensions()), apps, open_app,
            on_association_change)
        state = {"path": path, "back_id": back.id, "forward_id": forward.id,
                 "up_id": up.id, "refresh_id": refresh.id, "search_id": search.id,
                 "path_id": path_label.id, "rows": rows,
                 "display_entries": [], "display_by_path": {},
                 "menu": menu, "open_with": open_with,
                 "selected": None, "busy": False, "query": "", "entries": [],
                 "history": [path], "history_index": 0, "scan_id": 0,
                 "loading": False, "error": None, "closed": False, "nav_paths": {},
                 "render_id": 0, "prompt": prompt}

        shared = os.path.expanduser("~/storage/shared")
        locations = [("home", os.path.expanduser("~"))]
        for label, folder in (("desktop", "Desktop"), ("documents", "Documents"),
                              ("downloads", "Download"), ("pictures", "Pictures"),
                              ("music", "Music"), ("videos", "Movies")):
            target = os.path.join(shared, folder)
            if os.path.isdir(target):
                locations.append((label, target))
        for label, target in locations:
            row = tg.LinearLayout(activity, navigation, vertical=False)
            row.setdimensions(tg.View.MATCH_PARENT, 32)
            row.setlinearlayoutparams(0)
            row.setmargin(2, "left")
            row.setmargin(2, "right")
            row.sendclickevent(True)
            button = tg.TextView(activity, label, row)
            button.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            button.setmargin(4, "left")
            button.settextsize(12)
            button.settextcolor(palette.text)
            button.setgravity(0, 1)
            state["nav_paths"][row.id] = target

        path_label.sendlongclickevent(True)

        def render():
            state["render_id"] += 1
            render_id = state["render_id"]
            state["display_entries"] = []
            state["display_by_path"] = {}
            rows.setitems([])
            path_label.settext(state["path"])
            if state["loading"]:
                status.settext("loading folder…")
                return
            if state["error"] is not None:
                status.settext("couldn't load folder")
                return

            entries = [entry for entry in state["entries"]
                       if not state["query"] or state["query"] in entry[1].casefold()]
            state["display_entries"] = entries
            state["display_by_path"] = {entry[0]: entry for entry in entries}
            status.settext(f"preparing {len(entries)} items…")

            def prepare_items():
                items = []
                for entry_path, entry_name, is_directory, size, modified in entries:
                    if state["closed"] or render_id != state["render_id"]:
                        return None
                    app_icon = None if is_directory else file_associations.icon_for(
                        entry_path, apps, "window")
                    icon = load("folder" if is_directory else app_icon)
                    date = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
                    detail = f"file folder · {date}" if is_directory else f"{_format_size(size)} · {date}"
                    items.append({
                        "key": entry_path,
                        "text": entry_name,
                        "subtitle": detail,
                        "marker": "",
                        "image": icon,
                    })
                return items

            def prepared(items, error):
                if state["closed"] or render_id != state["render_id"]:
                    return
                if error is not None:
                    status.settext(f"couldn't prepare folder: {error}")
                    return
                rows.setitems(items or [])
                status.settext(f"{len(entries)} items")

            file_actions.run_background(prepare_items, prepared)

        def scan_directory(directory):
            with os.scandir(directory) as source:
                entries = []
                for entry in source:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    info = entry.stat(follow_symlinks=False)
                    entries.append((entry.path, entry.name, is_directory,
                                    info.st_size, info.st_mtime))
            return sorted(entries, key=lambda item: (not item[2], item[1].casefold()))

        def load_directory(directory=None, add_history=False):
            directory = os.path.abspath(directory or state["path"])
            if add_history and directory != state["path"]:
                state["history"] = state["history"][:state["history_index"] + 1]
                state["history"].append(directory)
                state["history_index"] += 1
            state["path"] = directory
            state["query"] = ""
            search.settext("")
            state["entries"] = []
            state["error"] = None
            state["loading"] = True
            state["scan_id"] += 1
            scan_id = state["scan_id"]
            render()

            def scanned(entries, error):
                if state["closed"] or scan_id != state["scan_id"]:
                    return
                state["loading"] = False
                state["entries"] = entries or []
                state["error"] = error
                render()

            file_actions.run_background(lambda: scan_directory(directory), scanned)

        def show_menu(item=None):
            state["selected"] = item
            if item is None:
                actions = ["new_file", "new_folder", "open_terminal", "paste", "refresh"]
            else:
                actions = ["open", "open_terminal"]
                if not item[1]:
                    actions.append("open_with")
                actions.extend(("copy", "cut", "paste", "rename", "delete", "properties"))
            disabled = () if file_actions.has_clipboard_item() else ("paste",)
            menu.show(actions, 12, 60, handle_menu_action, disabled=disabled)

        def entry_from_event(event):
            key = event.value.get("key")
            if key is not None:
                return state["display_by_path"].get(key)
            position = event.value.get("position", -1)
            if 0 <= position < len(state["display_entries"]):
                return state["display_entries"][position]
            return None

        def show_open_with(path_value):
            state["open_with"].show(path_value)

        def open_path(path_value):
            state["open_with"].open(path_value)

        def run_file_action(operation):
            if state["busy"]:
                status.settext("wait for the current file operation")
                return
            state["busy"] = True

            def complete(_result, error):
                if state["closed"]:
                    return
                state["busy"] = False
                if error is not None:
                    status.settext(f"operation failed: {error}")
                load_directory()

            file_actions.run_async(operation, complete)

        def handle_menu_action(action):
            if state["busy"]:
                status.settext("wait for the current file operation")
                return
            item = state["selected"]
            path_value = item[0] if item is not None else None
            try:
                if action == "refresh":
                    load_directory()
                elif action in ("new_file", "new_folder"):
                    directory = state["path"]
                    title, initial = (("new file", "New file.txt") if action == "new_file"
                                      else ("new folder", "New folder"))
                    create = file_actions.create_file if action == "new_file" else file_actions.create_folder
                    state["prompt"].show(
                        title, initial,
                        lambda name: run_file_action(lambda: create(directory, name)))
                elif action == "rename" and item is not None:
                    source = path_value
                    state["prompt"].show(
                        "rename", os.path.basename(source),
                        lambda name: run_file_action(lambda: file_actions.rename(source, name)))
                elif action == "open_terminal":
                    directory = (state["path"] if item is None or item[1]
                                 else os.path.dirname(path_value))
                    if open_app is not None:
                        open_app("terminal", directory)
                elif action == "open" and item is not None:
                    if item[1]:
                        load_directory(path_value, add_history=True)
                    else:
                        open_path(path_value)
                elif action == "open_with" and item is not None and not item[1]:
                    show_open_with(path_value)
                elif action == "properties" and item is not None:
                    open_app("files", {"properties": path_value})
                elif action in ("copy", "cut") and item is not None:
                    getattr(file_actions, action)(path_value)
                    status.settext(f"{action} {os.path.basename(path_value)}")
                elif action == "delete" and item is not None:
                    run_file_action(lambda: file_actions.delete(path_value))
                elif action == "paste":
                    destination = state["path"]
                    if item is not None:
                        destination = path_value if item[1] else os.path.dirname(path_value)
                    if file_actions.has_clipboard_item():
                        source = file_actions.clipboard["path"]
                        is_cut = file_actions.clipboard["cut"]
                        run_file_action(
                            lambda: file_actions.paste_item(destination, source, is_cut))
            except (OSError, ValueError) as error:
                print(f"nexa files: {error}")

        load_directory()

        def handle_event(event):
            if state["prompt"].handle_event(event):
                return True
            if state["open_with"].handle_event(event):
                return True
            if state["menu"].handle_event(event):
                return True
            if event.type == tg.Event.back and state["open_with"].visible:
                state["open_with"].hide()
                return True
            if event.type == tg.Event.back and state["menu"].visible:
                state["menu"].hide()
                return True
            if event.type == tg.Event.back and state["history_index"] > 0:
                state["history_index"] -= 1
                load_directory(state["history"][state["history_index"]])
                return True
            if event.type == tg.Event.text and event.id == state["search_id"]:
                state["query"] = search.gettext().strip().casefold()
                render()
                return True
            if event.type == tg.Event.longClick:
                if event.id == state["path_id"]:
                    show_menu()
                    return True
                if event.id == rows.id:
                    entry = entry_from_event(event)
                    if entry is not None:
                        show_menu((entry[0], entry[2]))
                    return True
            if event.type != tg.Event.click:
                return False
            if event.id == state["back_id"]:
                if state["history_index"] > 0:
                    state["history_index"] -= 1
                    load_directory(state["history"][state["history_index"]])
                return True
            if event.id == state["forward_id"]:
                if state["history_index"] + 1 < len(state["history"]):
                    state["history_index"] += 1
                    load_directory(state["history"][state["history_index"]])
                return True
            if event.id == state["up_id"]:
                parent_path = os.path.dirname(state["path"])
                if parent_path != state["path"]:
                    load_directory(parent_path, add_history=True)
                return True
            if event.id == state["refresh_id"]:
                load_directory()
                return True
            target = state["nav_paths"].get(event.id)
            if target is not None:
                load_directory(target, add_history=True)
                return True
            if event.id != rows.id:
                return False
            entry = entry_from_event(event)
            if entry is None:
                return True
            item = (entry[0], entry[2])
            if item[1]:
                load_directory(item[0], add_history=True)
            else:
                try:
                    open_path(item[0])
                except OSError as error:
                    print(f"nexa files: {error}")
            return True

        def close():
            state["closed"] = True
            state["scan_id"] += 1
            state["prompt"].hide()
            rows.close()

        handle_event.close = close
        handle_event.set_context = lambda directory: load_directory(directory)
        handle_event.on_window_resize = lambda _width, _height: state["prompt"].reposition()
        return handle_event

    def _build_properties(self, activity, parent, path, apps, association_changed):
        path = os.path.abspath(path)
        rtl = is_rtl(activity.getconfiguration())
        try:
            info = os.stat(path)
        except OSError as error:
            root = tg.LinearLayout(activity, parent)
            root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            root.setbackgroundcolor(palette.window_content)
            message = tg.TextView(activity, f"couldn't read properties: {error}", root)
            message.setdimensions(tg.View.MATCH_PARENT, 64)
            message.setlinearlayoutparams(0)
            message.settextcolor(palette.text)
            message.setgravity(1, 1)
            return None
        is_directory = stat.S_ISDIR(info.st_mode)
        icon_name = "folder" if is_directory else file_associations.icon_for(path, apps, "window")
        name = os.path.basename(path.rstrip(os.sep)) or path
        extension = os.path.splitext(name)[1]
        file_type = "File folder" if is_directory else f"{extension[1:].upper()} file" if extension else "File"
        associated_app = apps.get(file_associations.get(path))

        def format_size(size):
            value = float(size)
            for unit in ("bytes", "KB", "MB", "GB", "TB"):
                if value < 1024 or unit == "TB":
                    return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
                value /= 1024

        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        tab_bar = tg.LinearLayout(activity, root, vertical=False)
        tab_bar.setdimensions(tg.View.MATCH_PARENT, 44)
        tab_bar.setlinearlayoutparams(0)
        pages = tg.FrameLayout(activity, root)
        pages.setdimensions(tg.View.MATCH_PARENT, 0)
        pages.setlinearlayoutparams(1)
        page_buttons = {}
        tab_buttons = {}
        page_views = {}
        open_with_rows = {}
        tab_names = ["General", "Details"]
        if not is_directory:
            tab_names.append("Opens with")

        def make_page(title):
            page = tg.LinearLayout(activity, pages)
            page.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            page.setbackgroundcolor(palette.window_content)
            page.setvisibility(tg.View.GONE)
            scroll = tg.NestedScrollView(activity, page, fillviewport=True, nobar=True)
            scroll.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
            scroll.setbackgroundcolor(palette.window_content)
            content = tg.LinearLayout(activity, scroll)
            content.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
            page_views[title] = page
            return content

        def add_header(content):
            header = tg.LinearLayout(activity, content, vertical=False)
            header.setdimensions(tg.View.MATCH_PARENT, 88)
            header.setlinearlayoutparams(0)

            def add_icon():
                icon = tg.ImageView(activity, header)
                icon.setdimensions(56, 56)
                icon.setimage(load(icon_name))
                icon.setmargin(16, "left")
                icon.setmargin(16, "right")
                icon.setmargin(16, "top")
                icon.setmargin(16, "bottom")
                icon.setlinearlayoutparams(0)

            def add_filename():
                filename = tg.TextView(activity, name, header)
                filename.setheight(88)
                filename.setlinearlayoutparams(1)
                filename.setmargin(8, "left")
                filename.setmargin(12, "right")
                filename.settextsize(18)
                filename.settextcolor(palette.text)
                filename.setgravity(0, 1)

            if rtl:
                add_filename()
                add_icon()
            else:
                add_icon()
                add_filename()

            divider = tg.LinearLayout(activity, content)
            divider.setdimensions(tg.View.MATCH_PARENT, 1)
            divider.setlinearlayoutparams(0)
            divider.setbackgroundcolor(palette.surface_light)

        def add_detail(content, label, value):
            row = tg.LinearLayout(activity, content)
            row.setdimensions(tg.View.MATCH_PARENT, 64)
            row.setlinearlayoutparams(0)
            row.setmargin(12, "left")
            row.setmargin(12, "right")
            title = tg.TextView(activity, label, row)
            title.setdimensions(tg.View.MATCH_PARENT, 26)
            title.setlinearlayoutparams(0)
            title.settextsize(12)
            title.settextcolor(palette.muted)
            title.setgravity(0, 1)
            value_view = tg.TextView(activity, value, row)
            value_view.setdimensions(tg.View.MATCH_PARENT, 38)
            value_view.setlinearlayoutparams(0)
            value_view.settextsize(14)
            value_view.settextcolor(palette.text)
            value_view.setgravity(0, 1)
            return value_view

        general = make_page("General")
        add_header(general)
        add_detail(general, "type", file_type)
        if not is_directory:
            general_open_with = add_detail(
                general, "opens with", associated_app.name if associated_app is not None else "choose an app")
        add_detail(general, "location", os.path.dirname(path) or os.sep)
        add_detail(general, "size", "—" if is_directory else format_size(info.st_size))

        details = make_page("Details")
        add_header(details)
        add_detail(details, "name", name)
        add_detail(details, "type", file_type)
        add_detail(details, "modified", datetime.fromtimestamp(info.st_mtime).strftime("%x %X"))
        add_detail(details, "accessed", datetime.fromtimestamp(info.st_atime).strftime("%x %X"))

        if not is_directory:
            open_page = make_page("Opens with")
            add_header(open_page)
            add_detail(open_page, "file type", extension or "no extension")
            current_app_label = add_detail(
                open_page, "current app", associated_app.name if associated_app is not None else "not set")
            for app_id, app in apps.items():
                supports_file = getattr(app, "supports_file", None)
                if not callable(supports_file) or not supports_file(path):
                    continue
                choice = tg.TextView(activity, app.name, open_page)
                choice.setdimensions(tg.View.MATCH_PARENT, 48)
                choice.setlinearlayoutparams(0)
                choice.setmargin(12, "left")
                choice.setmargin(12, "right")
                choice.setbackgroundcolor(palette.surface_light)
                choice.settextcolor(palette.text)
                choice.settextsize(14)
                choice.setgravity(0, 1)
                choice.sendclickevent(True)
                open_with_rows[choice.id] = app_id

        for title in tab_names:
            button = tg.TextView(activity, title, tab_bar)
            button.setheight(44)
            button.setlinearlayoutparams(1)
            button.settextsize(13)
            button.settextcolor(palette.text)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            page_buttons[button.id] = title
            tab_buttons[button.id] = button

        def select_page(title):
            for name_value, page in page_views.items():
                page.setvisibility(tg.View.VISIBLE if name_value == title else tg.View.GONE)
            for button_id, button_title in page_buttons.items():
                tab_buttons[button_id].setbackgroundcolor(
                    palette.accent if button_title == title else palette.surface_light)

        select_page("General")

        def handle_properties_event(event):
            if event.type != tg.Event.click:
                return False
            title = page_buttons.get(event.id)
            if title is not None:
                select_page(title)
                return True
            app_id = open_with_rows.get(event.id)
            if app_id is not None:
                file_associations.set(path, app_id)
                if association_changed is not None:
                    association_changed(path, app_id)
                app_name = apps[app_id].name
                general_open_with.settext(app_name)
                current_app_label.settext(app_name)
                return True
            return False

        return handle_properties_event


APP = FilesApp()
