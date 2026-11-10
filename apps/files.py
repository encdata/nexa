import os

import termuxgui as tg

from shell import file_actions, palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.text_direction import is_rtl


class FilesApp:
    app_id = "files"
    name = "home"
    icon = "folder"

    def get_title(self, context=None):
        if context is None:
            return self.name
        return os.path.basename(os.path.normpath(context)) or context

    def build(self, activity, parent, context=None):
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
        state = {"path": path, "up_id": up.id, "path_id": path_label.id, "rows": rows,
                 "row_paths": {}, "menu": menu, "selected": None, "busy": False}

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
                    if is_directory:
                        image = tg.ImageView(activity, row)
                        image.setdimensions(28, 28)
                        image.setimage(load("folder"))
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
                "open", "copy", "cut", "paste", "delete"]
            menu.show(actions, 12, 60, handle_menu_action)

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
                        file_actions.open_file(path_value)
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
            if state["menu"].handle_event(event):
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
                    file_actions.open_file(item[0])
                except OSError as error:
                    print(f"nexa files: {error}")
            return True

        return handle_event


APP = FilesApp()
