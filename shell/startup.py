from apps import discover
from shell.desktop_state import load_home_items, load_positions
from shell.icons import preload
from shell import palette
from shell.personalization import load as load_personalization
from shell.wallpaper import load as load_wallpaper


def load(update, configuration):
    update("loading apps", 10)
    personalization = load_personalization()
    palette.set_theme(personalization["theme"])
    apps = discover()

    update("reading desktop layout", 30)
    positions = load_positions()
    home_items = load_home_items()

    update("loading icon assets", 50)
    preload({"apps", "calc", "calender", "clock", "close", "cursor", "folder", "home",
             "maximize", "minimize", "nexa", "settings", "terminal"}
            | {app.icon for app in apps.values()})

    update("loading wallpaper", 68)
    try:
        wallpaper = load_wallpaper(configuration["screenwidth"], configuration["screenheight"],
                                   configuration["density"], personalization["wallpaper"])
    except (OSError, ValueError):
        personalization["wallpaper"] = None
        wallpaper = load_wallpaper(configuration["screenwidth"], configuration["screenheight"],
                                   configuration["density"])

    update("starting desktop", 72)
    return {"apps": apps, "positions": positions, "home_items": home_items,
            "wallpaper": wallpaper, "personalization": personalization,
            "configuration": configuration}
