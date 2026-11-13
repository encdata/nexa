import json
import os
import shutil
import tempfile


DATA_PATH = os.path.expanduser("~/.config/nexa/settings.json")
THEMES = {"dark", "light"}


def load():
    try:
        with open(DATA_PATH, encoding="utf-8") as source:
            saved = json.load(source)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    theme = saved.get("theme", "dark")
    if theme not in THEMES:
        theme = "dark"
    wallpaper = saved.get("wallpaper")
    if not isinstance(wallpaper, str) or not wallpaper:
        wallpaper = None
    return {"theme": theme, "wallpaper": wallpaper}


def save(settings):
    directory = os.path.dirname(DATA_PATH)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".settings-", delete=False) as target:
            temporary_path = target.name
            json.dump({"theme": settings["theme"], "wallpaper": settings.get("wallpaper")}, target)
        os.replace(temporary_path, DATA_PATH)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def store_wallpaper(source):
    if source is None:
        return None
    directory = os.path.dirname(DATA_PATH)
    os.makedirs(directory, exist_ok=True)
    destination = os.path.join(directory, "wallpaper")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".wallpaper-",
                                         delete=False) as target:
            temporary_path = target.name
            with open(source, "rb") as image:
                shutil.copyfileobj(image, target)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
    return destination
