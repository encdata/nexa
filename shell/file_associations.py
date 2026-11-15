import json
import os
import tempfile


ASSOCIATIONS_PATH = os.path.expanduser("~/.config/nexa/file-associations.json")
_associations = None


def _load():
    global _associations
    if _associations is not None:
        return _associations
    try:
        with open(ASSOCIATIONS_PATH, encoding="utf-8") as source:
            saved = json.load(source)
    except FileNotFoundError:
        saved = {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"nexa: couldn't read file associations: {error}")
        saved = {}
    _associations = {key: value for key, value in saved.items()
                     if isinstance(key, str) and isinstance(value, str)} if isinstance(saved, dict) else {}
    return _associations


def _key(path):
    return os.path.splitext(os.path.basename(path))[1].lower()


def get(path):
    return _load().get(_key(path))


def icon_for(path, apps, fallback=None):
    app = apps.get(get(path))
    if app is None:
        return fallback
    supports_file = getattr(app, "supports_file", None)
    if not callable(supports_file) or not supports_file(path):
        return fallback
    return app.icon


def set(path, app_id):
    associations = _load()
    associations[_key(path)] = app_id
    directory = os.path.dirname(ASSOCIATIONS_PATH)
    temporary_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".file-associations-", delete=False) as target:
            temporary_path = target.name
            json.dump(associations, target, separators=(",", ":"))
        os.replace(temporary_path, ASSOCIATIONS_PATH)
    except OSError as error:
        print(f"nexa: couldn't save file association: {error}")
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
