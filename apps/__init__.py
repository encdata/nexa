from importlib.metadata import entry_points
from importlib import import_module
from os import PathLike
from pkgutil import iter_modules


ENTRY_POINT_GROUP = "nexa.apps"


def _register(apps, app, source):
    required = ("app_id", "name", "icon", "build")
    missing = [name for name in required if not hasattr(app, name)]
    if missing:
        raise TypeError(f"{source} is missing app fields: {', '.join(missing)}")
    if not isinstance(app.app_id, str) or not app.app_id:
        raise TypeError(f"{source} has an invalid app_id")
    if not isinstance(app.name, str) or not app.name:
        raise TypeError(f"{source} has an invalid name")
    if not isinstance(app.icon, (str, bytes, PathLike)):
        raise TypeError(f"{source} has an invalid icon")
    if not callable(app.build):
        raise TypeError(f"{source}.build must be callable")
    if not isinstance(getattr(app, "preload_ui", False), bool):
        raise TypeError(f"{source}.preload_ui must be a bool")
    if not isinstance(getattr(app, "build_threaded", False), bool):
        raise TypeError(f"{source}.build_threaded must be a bool")
    if app.app_id in apps:
        raise ValueError(f"duplicate app id {app.app_id!r} from {source}")
    apps[app.app_id] = app


def discover():
    apps = {}
    for module in sorted(iter_modules(__path__), key=lambda item: item.name):
        if module.name.startswith("_"):
            continue
        loaded = import_module(f"{__name__}.{module.name}")
        app = getattr(loaded, "APP", None)
        if app is None:
            continue
        _register(apps, app, f"{__name__}.{module.name}.APP")

    for entry in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda item: item.name):
        _register(apps, entry.load(), f"{entry.name} ({entry.value})")
    return apps


