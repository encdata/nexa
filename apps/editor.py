import mimetypes
import os
import tempfile

import termuxgui as tg

from shell import file_actions, palette


def _read_file(path):
    with open(path, encoding="utf-8") as source:
        return source.read()


def _write_file(path, text):
    directory = os.path.dirname(path) or "."
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".nexa-editor-", delete=False) as target:
            temporary_path = target.name
            target.write(text)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def _untitled_path():
    directory = os.path.expanduser("~")
    path = os.path.join(directory, "untitled.txt")
    index = 1
    while os.path.lexists(path):
        path = os.path.join(directory, f"untitled-{index}.txt")
        index += 1
    return path


class EditorApp:
    app_id = "editor"
    name = "Nexa Editor"
    icon = "notepad"
    preload_ui = True
    build_threaded = True

    def get_title(self, context=None):
        if context:
            return f"{os.path.basename(context)} - {self.name}"
        return self.name

    @staticmethod
    def supports_file(path):
        content_type, _encoding = mimetypes.guess_type(path)
        return content_type is None or content_type.startswith("text/") or content_type in (
            "application/json", "application/xml", "application/javascript")

    def build(self, activity, parent, context=None):
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        toolbar = tg.LinearLayout(activity, root, vertical=False)
        toolbar.setdimensions(tg.View.MATCH_PARENT, 48)
        toolbar.setlinearlayoutparams(0)
        toolbar.setbackgroundcolor(palette.surface_light)

        buttons = {}
        for action, label, width in (("new", "new", 52), ("open", "open", 56),
                                     ("save", "save", 56)):
            button = tg.TextView(activity, label, toolbar)
            button.setdimensions(width, 40)
            button.setlinearlayoutparams(0)
            button.setmargin(3, "left")
            button.setmargin(3, "right")
            button.setbackgroundcolor(palette.surface)
            button.settextcolor(palette.text)
            button.settextsize(12)
            button.setgravity(1, 1)
            button.sendclickevent(True)
            buttons[button.id] = action

        path_field = tg.EditText(activity, context or _untitled_path(), toolbar, singleline=True,
                                 line=False, inputtype="text")
        path_field.setdimensions(0, 40)
        path_field.setlinearlayoutparams(1)
        path_field.setmargin(4, "left")
        path_field.setmargin(4, "right")
        path_field.setbackgroundcolor(palette.window_content)
        path_field.settextcolor(palette.text)
        path_field.settextsize(13)

        editor = tg.EditText(activity, "", root, singleline=False, line=False,
                             inputtype="textMultiLine")
        editor.setdimensions(tg.View.MATCH_PARENT, 0)
        editor.setlinearlayoutparams(1)
        editor.setmargin(10, "left")
        editor.setmargin(10, "right")
        editor.setmargin(8, "top")
        editor.setbackgroundcolor(palette.window_content)
        editor.settextcolor(palette.text)
        editor.settextsize(15)
        editor.setgravity(0, 0)
        editor.sendtextevent(True)

        status = tg.TextView(activity, "ready", root)
        status.setdimensions(tg.View.MATCH_PARENT, 24)
        status.setlinearlayoutparams(0)
        status.setmargin(8, "left")
        status.settextsize(11)
        status.settextcolor(palette.muted)
        status.setgravity(0, 1)

        state = {"saved_text": "", "busy": False, "closed": False}

        def resolve_path():
            path = os.path.expanduser(path_field.gettext().strip())
            if not path:
                raise ValueError("enter a file path")
            if not os.path.isabs(path):
                path = os.path.join(os.path.expanduser("~"), path)
            return os.path.abspath(path)

        def set_document(path, text):
            path_field.settext(path)
            editor.settext(text)
            state["saved_text"] = text
            status.settext("opened")

        def open_file():
            if state["busy"]:
                return
            if editor.gettext() != state["saved_text"]:
                status.settext("save changes before opening another file")
                return
            try:
                path = resolve_path()
            except ValueError as error:
                status.settext(str(error))
                return
            state["busy"] = True
            status.settext("opening…")

            def opened(text, error):
                if state["closed"]:
                    return
                state["busy"] = False
                if error is not None:
                    status.settext(f"couldn't open file: {error}")
                    return
                set_document(path, text)

            file_actions.run_background(lambda: _read_file(path), opened)

        def save_file():
            if state["busy"]:
                return
            try:
                path = resolve_path()
            except ValueError as error:
                status.settext(str(error))
                return
            text = editor.gettext()
            state["busy"] = True
            status.settext("saving…")

            def saved(_result, error):
                if state["closed"]:
                    return
                state["busy"] = False
                if error is not None:
                    status.settext(f"couldn't save file: {error}")
                    return
                state["saved_text"] = text
                status.settext("saved" if editor.gettext() == text else "unsaved changes")

            file_actions.run_async(lambda: _write_file(path, text), saved)

        def new_file():
            if state["busy"]:
                return
            if editor.gettext() != state["saved_text"]:
                status.settext("save changes before creating a new file")
                return
            set_document(_untitled_path(), "")
            status.settext("new file")

        def handle_event(event):
            if event.type == tg.Event.text and event.id == editor.id:
                dirty = event.value.get("text", "") != state["saved_text"]
                status.settext("unsaved changes" if dirty else "saved")
                return True
            if event.type != tg.Event.click:
                return False
            action = buttons.get(event.id)
            if action == "new":
                new_file()
            elif action == "open":
                open_file()
            elif action == "save":
                save_file()
            else:
                return False
            return True

        if context:
            open_file()

        def close():
            state["closed"] = True

        handle_event.close = close
        return handle_event


APP = EditorApp()
