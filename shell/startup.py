from concurrent.futures import ThreadPoolExecutor

from apps import discover
from shell.desktop_state import load_home_items, load_positions
from shell.icons import preload
from shell import palette
from shell.personalization import load as load_personalization
from shell.wallpaper import load as load_wallpaper
from shell.x86_runtime import ensure_installed, ready as x86_runtime_ready


def load(update, configuration):
    update("loading apps", 10)
    runtime_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-runtime-check")
    runtime_future = runtime_worker.submit(ensure_installed, update)

    personalization = load_personalization()
    palette.set_theme(personalization["theme"])
    apps = discover()
    x86_runtime_error = None

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

    try:
        runtime_future.result()
        runtime_ready = True
    except Exception as error:
        runtime_ready = x86_runtime_ready()
        x86_runtime_error = str(error)
        print(f"nexa: couldn't install x86_64 runtime: {error}")
    finally:
        runtime_worker.shutdown(wait=True)

    update("starting desktop", 72)
    return {"apps": apps, "positions": positions, "home_items": home_items,
            "wallpaper": wallpaper, "personalization": personalization,
            "configuration": configuration,
            "x86_runtime_ready": runtime_ready,
            "x86_runtime_error": x86_runtime_error}
