from concurrent.futures import ThreadPoolExecutor
import logging

from apps import discover
from shell.desktop_state import load_home_items, load_positions
from shell.icons import preload
from shell import palette
from shell.personalization import load as load_personalization
from shell.wallpaper import load as load_wallpaper
from shell.x86_runtime import (ensure_installed, node_runtime_path,
                               ready as x86_runtime_ready)

logger = logging.getLogger("nexa.startup")


def load(update, configuration):
    update("loading apps", 10)
    logger.info("loading personalization and discovering apps")
    personalization = load_personalization()
    palette.set_theme(personalization["theme"])
    apps = discover()

    def ensure_startup_runtimes():
        needs_node = any(getattr(app, "requires_x86_node", False)
                         for app in apps.values())
        ensure_installed(update, needs_node)
        return needs_node

    logger.info("boot prep started; checking the x86 runtime concurrently")
    runtime_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-runtime-check")
    runtime_future = runtime_worker.submit(ensure_startup_runtimes)
    x86_runtime_error = None
    electron_node_runtime_ready = node_runtime_path() is not None

    update("reading desktop layout", 30)
    logger.info("loading desktop layout")
    positions = load_positions()
    home_items = load_home_items()

    update("loading icon assets", 50)
    logger.info("preloading desktop and app icons")
    preload({"apps", "calc", "calender", "clock", "close", "cursor", "folder", "home",
             "maximize", "minimize", "nexa", "settings", "terminal"}
            | {app.icon for app in apps.values()})

    update("loading wallpaper", 68)
    logger.info("loading wallpaper")
    try:
        wallpaper = load_wallpaper(configuration["screenwidth"], configuration["screenheight"],
                                   configuration["density"], personalization["wallpaper"])
    except (OSError, ValueError):
        personalization["wallpaper"] = None
        wallpaper = load_wallpaper(configuration["screenwidth"], configuration["screenheight"],
                                   configuration["density"])

    try:
        needs_node = runtime_future.result()
        electron_node_runtime_ready = (not needs_node or node_runtime_path() is not None)
        runtime_ready = True
        logger.info("x86 runtime check passed")
    except Exception as error:
        runtime_ready = x86_runtime_ready()
        electron_node_runtime_ready = node_runtime_path() is not None
        x86_runtime_error = str(error)
        logger.exception("x86 runtime setup failed: %s", error)
    finally:
        runtime_worker.shutdown(wait=True)

    update("starting desktop", 72)
    logger.info("boot prep complete; starting desktop shell")
    return {"apps": apps, "positions": positions, "home_items": home_items,
            "wallpaper": wallpaper, "personalization": personalization,
            "configuration": configuration,
            "x86_runtime_ready": runtime_ready,
            "electron_node_runtime_ready": electron_node_runtime_ready,
            "x86_runtime_error": x86_runtime_error}
