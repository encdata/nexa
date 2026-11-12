import os

from shell import file_associations
from shell.context_menu import ContextMenu


class OpenWithChooser:
    def __init__(self, activity, parent, bounds, apps, open_app, on_association_change=None):
        self.apps = apps
        self.open_app = open_app
        self.on_association_change = on_association_change
        self.actions = [app_id for app_id, app in apps.items()
                        if callable(getattr(app, "supports_file", None))]
        labels = {app_id: apps[app_id].name for app_id in self.actions}
        self.script_handlers = {}
        if "terminal" in apps:
            self.script_handlers = {
                "run_sh": ("sh", lambda path: path.lower().endswith(".sh")),
                "run_python": ("python", lambda path: path.lower().endswith(".py")),
            }
            labels.update({key: interpreter for key, (interpreter, _supports) in self.script_handlers.items()})
            self.actions.extend(self.script_handlers)
        self.actions.append("cancel")
        self.menu = ContextMenu(activity, parent, bounds, actions=self.actions, labels=labels)

    @property
    def visible(self):
        return self.menu.visible

    def open(self, path):
        app_id = file_associations.get(path)
        app = self.apps.get(app_id)
        if (app is not None and callable(getattr(app, "supports_file", None))
              and app.supports_file(path)):
            self.open_app(app_id, path)
        else:
            self.show(path)

    def show(self, path, x=24, y=64):
        actions = [app_id for app_id in self.actions[:-1]
                   if app_id not in self.script_handlers and self.apps[app_id].supports_file(path)]
        actions.extend(key for key, (_interpreter, supports) in self.script_handlers.items()
                       if supports(path) and os.path.isfile(path))
        actions.append("cancel")
        self.menu.show(actions, x, y,
                       lambda app_id: self._choose(path, app_id) if app_id != "cancel" else None)

    def _choose(self, path, app_id):
        handler = self.script_handlers.get(app_id)
        if handler is not None:
            interpreter = handler[0]
            self.open_app("terminal", (path, interpreter))
            return
        file_associations.set(path, app_id)
        if self.on_association_change is not None:
            self.on_association_change(path, app_id)
        self.open_app(app_id, path)

    def handle_event(self, event):
        return self.menu.handle_event(event)

    def hide(self):
        self.menu.hide()
