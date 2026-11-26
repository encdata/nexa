from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import CancelledError
import logging
from threading import Event

from apps import discover
from shell.desktop_state import load_home_items, load_positions
from shell.icons import preload
from shell import palette
from shell.personalization import load as load_personalization
from shell.wallpaper import load as load_wallpaper
from shell.x86_runtime import (ensure_installed, ensure_node_runtime, node_runtime_path,
                               ready as x86_runtime_ready)

logger = logging.getLogger("nexa.startup")


def load(update, configuration, cancelled=None):
    if cancelled is None:
        cancelled = Event()

    def check_cancelled():
        if cancelled.is_set():
            raise CancelledError()

    runtime_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-runtime-check")
    core_runtime_future = None
    completed = False
    try:
        update("loading apps", 10)
        logger.info("loading personalization and discovering apps")
        personalization = load_personalization()
        palette.set_theme(personalization["theme"])

        logger.info("checking the x86 runtime while discovering apps")
        core_runtime_future = runtime_worker.submit(
            ensure_installed, update, False, cancelled)
        apps = discover()
        check_cancelled()
        needs_node = any(getattr(app, "requires_x86_node", False)
                         for app in apps.values())

        if needs_node:
            def ensure_electron_runtime():
                core_runtime_future.result()
                ensure_node_runtime(update, cancelled)
                return True

            runtime_future = runtime_worker.submit(ensure_electron_runtime)
        else:
            runtime_future = core_runtime_future
        x86_runtime_error = None
        electron_node_runtime_ready = node_runtime_path() is not None

        check_cancelled()
        update("reading desktop layout", 30)
        logger.info("loading desktop layout")
        positions = load_positions()
        home_items = load_home_items()

        check_cancelled()
        update("loading icon assets", 50)
        logger.info("preloading desktop and app icons")
        preload({"apps", "calc", "calender", "clock", "close", "cursor", "folder", "home",
                 "maximize", "minimize", "nexa", "settings", "terminal"}
                | {app.icon for app in apps.values()})

        check_cancelled()
        update("loading wallpaper", 68)
        logger.info("loading wallpaper")
        try:
            wallpaper = load_wallpaper(
                configuration["screenwidth"], configuration["screenheight"],
                configuration["density"], personalization["wallpaper"])
        except (OSError, ValueError):
            personalization["wallpaper"] = None
            wallpaper = load_wallpaper(
                configuration["screenwidth"], configuration["screenheight"],
                configuration["density"])

        try:
            needs_node = runtime_future.result()
            electron_node_runtime_ready = (not needs_node or node_runtime_path() is not None)
            runtime_ready = True
            logger.info("x86 runtime check passed")
        except CancelledError:
            raise
        except Exception as error:
            runtime_ready = x86_runtime_ready()
            electron_node_runtime_ready = node_runtime_path() is not None
            x86_runtime_error = str(error)
            logger.exception("x86 runtime setup failed: %s", error)

        check_cancelled()
        update("starting desktop", 72)
        logger.info("boot prep complete; starting desktop shell")
        startup = {"apps": apps, "positions": positions, "home_items": home_items,
                   "wallpaper": wallpaper, "personalization": personalization,
                   "configuration": configuration,
                   "x86_runtime_ready": runtime_ready,
                   "electron_node_runtime_ready": electron_node_runtime_ready,
                   "x86_runtime_error": x86_runtime_error}
        completed = True
        return startup
    finally:
        if not completed:
            cancelled.set()
        if cancelled.is_set() and core_runtime_future is not None:
            core_runtime_future.cancel()
        runtime_worker.shutdown(wait=True, cancel_futures=True)
