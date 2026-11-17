from importlib.metadata import entry_points
from importlib import import_module
from importlib.util import spec_from_file_location, module_from_spec
import json
from os import PathLike
from pathlib import Path
from pkgutil import iter_modules
import re
import sys
from nexapip import APP_API_VERSION


INSTALLED_APPS = Path.home() / ".local" / "share" / "nexa" / "apps"


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
    if not isinstance(getattr(app, "build_threaded", True), bool):
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
    for directory in sorted(INSTALLED_APPS.iterdir() if INSTALLED_APPS.exists() else (),
                            key=lambda path: path.name):
        if not directory.is_dir() or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", directory.name):
            continue
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("id") != directory.name
                    or manifest.get("api_version") != APP_API_VERSION
                    or directory.name in apps):
                continue
            entrypoint = manifest.get("entrypoint", "app:APP")
            module_name, separator, object_name = entrypoint.partition(":")
            if (not separator or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module_name)
                    or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", object_name)):
                raise ValueError("invalid app entry point")
            module_path = directory.joinpath(*module_name.split("."))
            package_path = module_path / "__init__.py"
            source_path = module_path.with_suffix(".py")
            if package_path.is_file():
                module_file = package_path
                search_paths = [str(module_path)]
            elif source_path.is_file():
                module_file = source_path
                search_paths = None
            else:
                raise ValueError("app entry point is missing")
            try:
                source = json.loads((directory / "source.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                source = {}
            package_key = source.get("sha256", manifest.get("version", "0"))[:12]
            package_key = re.sub(r"[^a-zA-Z0-9_]", "_", package_key)
            module_id = (f"nexa_installed_{directory.name.replace('.', '_').replace('-', '_')}_"
                         f"{package_key}")
            spec = spec_from_file_location(module_id, module_file,
                                            submodule_search_locations=search_paths)
            if spec is None or spec.loader is None:
                raise ImportError("couldn't load app module")
            module = module_from_spec(spec)
            sys.modules[module_id] = module
            spec.loader.exec_module(module)
            app = getattr(module, object_name)
            if (getattr(app, "app_id", None) != directory.name
                    or getattr(app, "api_version", None) != APP_API_VERSION
                    or getattr(app, "name", None) != manifest.get("name")
                    or getattr(app, "version", None) != manifest.get("version")):
                raise ValueError("app identity or API version doesn't match its manifest")
            icon = manifest["icon"]
            icon_path = (directory / icon).resolve()
            if not icon_path.is_relative_to(directory.resolve()) or not icon_path.is_file():
                raise ValueError("app icon is missing or unsafe")
            app.icon = str(icon_path)
            app.version = manifest["version"]
            app.description = manifest.get("description", "")
            _register(apps, app, f"installed app {directory.name}")
        except Exception as error:
            print(f"nexa: couldn't load installed app {directory.name}: {error}")
    return apps


