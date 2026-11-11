import os
import stat
from datetime import datetime

import termuxgui as tg

from shell import file_actions, file_associations, palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.open_with import OpenWithChooser
from shell.text_direction import is_rtl


class FilesApp:
    app_id = "files"
    name = "home"
    icon = "folder"
    preload_ui = False

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
        rtl = is_rtl(configuration)
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        toolbar = tg.LinearLayout(activity, root, vertical=False)
        toolbar.setdimensions(tg.View.MATCH_PARENT, 52)
        toolbar.setlinearlayoutparams(0)
        toolbar.setbackgroundcolor(palette.surface_light)
        up = tg.TextView(activity, "‹", toolbar)
        up.setdimensions(52, 52)
        up.setlinearlayoutparams(0)
        up.settextsize(30)
        up.settextcolor(palette.text)
        up.setgravity(1, 1)
        up.sendclickevent(True)

        path_label = tg.TextView(activity, path, toolbar)
        path_label.setheight(52)
        path_label.setlinearlayoutparams(1)
        path_label.settextsize(13)
        path_label.settextcolor(palette.text)
        path_label.setgravity(0, 1)

        scroll = tg.NestedScrollView(activity, root, fillviewport=True, nobar=True)
        scroll.setheight(0)
        scroll.setlinearlayoutparams(1)
        rows = tg.LinearLayout(activity, scroll)
        rows.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        density = configuration["density"]
        menu = ContextMenu(activity, parent, lambda: tuple(
            round(value / density) for value in parent.getdimensions()))
        def on_association_change(path_value, app_id):
            if association_changed is not None:
                association_changed(path_value, app_id)
            render()

        open_with = OpenWithChooser(
            activity, parent, lambda: tuple(
                round(value / density) for value in parent.getdimensions()), apps, open_app,
            on_association_change)
        state = {"path": path, "up_id": up.id, "path_id": path_label.id, "rows": rows,
                 "row_paths": {}, "menu": menu, "open_with": open_with,
                 "selected": None, "busy": False}

        path_label.sendlongclickevent(True)

        def render():
            rows.clearchildren()
            state["row_paths"].clear()
            path_label.settext(state["path"])
            try:
                with os.scandir(state["path"]) as directory:
                    entries = sorted(directory,
                                     key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.casefold()))
            except OSError as error:
                row = tg.TextView(activity, str(error), rows)
                row.setdimensions(tg.View.MATCH_PARENT, 52)
                row.setlinearlayoutparams(0)
                row.settextcolor(palette.muted)
                row.setgravity(0, 1)
                return

            for entry in entries:
                is_directory = entry.is_dir(follow_symlinks=False)
                row = tg.LinearLayout(activity, rows, vertical=False)
                row.setdimensions(tg.View.MATCH_PARENT, 52)
                row.setlinearlayoutparams(0)
                row.setbackgroundcolor(palette.window_content)
                row.sendclickevent(True)
                row.sendlongclickevent(True)
                def add_icon():
                    app_icon = None if is_directory else file_associations.icon_for(entry.path, apps)
                    if is_directory or app_icon is not None:
                        image = tg.ImageView(activity, row)
                        image.setdimensions(28, 28)
                        image.setimage(load("folder" if is_directory else app_icon))
                        image.setmargin(12, "left")
                        image.setmargin(4, "right")
                        image.setmargin(12, "top")
                        image.setmargin(12, "bottom")
                        image.setlinearlayoutparams(0)
                    else:
                        marker = tg.TextView(activity, "·", row)
                        marker.setdimensions(28, 52)
                        marker.settextsize(24)
                        marker.settextcolor(palette.muted)
                        marker.setgravity(1, 1)
                        marker.setlinearlayoutparams(0)

                def add_label():
                    label = tg.TextView(activity, entry.name, row)
                    label.setheight(52)
                    label.setlinearlayoutparams(1)
                    label.setmargin(12, "left")
                    label.settextcolor(palette.text)
                    label.setgravity(0, 1)

                if rtl:
                    add_label()
                    add_icon()
                else:
                    add_icon()
                    add_label()
                state["row_paths"][row.id] = (entry.path, is_directory)

        def show_menu(item=None):
            state["selected"] = item
            actions = ["paste", "refresh"] if item is None else [
                "open", "properties", "copy", "cut", "paste", "delete"]
            if item is not None and not item[1]:
                actions.insert(1, "open_with")
            menu.show(actions, 12, 60, handle_menu_action)

        def show_open_with(path_value):
            state["open_with"].show(path_value)

        def open_path(path_value):
            state["open_with"].open(path_value)

        def run_file_action(operation):
            if state["busy"]:
                return
            state["busy"] = True

            def complete(_result, error):
                state["busy"] = False
                if error is not None:
                    print(f"nexa files: {error}")
                render()

            file_actions.run_async(operation, complete)

        def handle_menu_action(action):
            if state["busy"]:
                return
            item = state["selected"]
            path_value = item[0] if item is not None else None
            try:
                if action == "refresh":
                    render()
                elif action == "open" and item is not None:
                    if item[1]:
                        state["path"] = path_value
                        render()
                    else:
                        open_path(path_value)
                elif action == "open_with" and item is not None and not item[1]:
                    show_open_with(path_value)
                elif action == "properties" and item is not None:
                    open_app("files", {"properties": path_value})
                elif action in ("copy", "cut") and item is not None:
                    getattr(file_actions, action)(path_value)
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

        render()

        def handle_event(event):
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
            if event.type == tg.Event.longClick:
                if event.id == state["path_id"]:
                    show_menu()
                    return True
                item = state["row_paths"].get(event.id)
                if item is not None:
                    show_menu(item)
                    return True
            if event.type != tg.Event.click:
                return False
            if event.id == state["up_id"]:
                parent_path = os.path.dirname(state["path"])
                if parent_path != state["path"]:
                    state["path"] = parent_path
                    render()
                return True
            item = state["row_paths"].get(event.id)
            if item is None:
                return False
            if item[1]:
                state["path"] = item[0]
                render()
            else:
                try:
                    open_path(item[0])
                except OSError as error:
                    print(f"nexa files: {error}")
            return True

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
