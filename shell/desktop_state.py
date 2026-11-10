import json
import os


PATH = os.path.expanduser("~/.config/nexa/desktop.json")


def load_home_items():
    home = os.path.expanduser("~")
    try:
        with os.scandir(home) as entries:
            items = [(entry.path, entry.name, entry.is_dir(follow_symlinks=False))
                     for entry in entries
                     if not entry.name.startswith(".")
                     and (entry.is_dir(follow_symlinks=False)
                          or entry.is_file(follow_symlinks=False))]
    except OSError as error:
        print(f"nexa: couldn't read home items: {error}")
        return []
    return sorted(items, key=lambda item: (not item[2], item[1].casefold()))


def load_positions():
    try:
        with open(PATH, encoding="utf-8") as layout_file:
            positions = json.load(layout_file)
        return positions if isinstance(positions, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"nexa: couldn't read desktop positions: {error}")
        return {}


def save_positions(positions):
    directory = os.path.dirname(PATH)
    temporary_path = f"{PATH}.tmp"
    try:
        os.makedirs(directory, exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as layout_file:
            json.dump(positions, layout_file, separators=(",", ":"))
        os.replace(temporary_path, PATH)
    except OSError as error:
        print(f"nexa: couldn't save desktop positions: {error}")
