import logging
from logging.handlers import RotatingFileHandler
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parent / "nexapip" / "src"))

import nexapip as tg

from screens.loading import show as show_loading
from shell import file_actions
from shell.nexa_connection import NexaConnection
from shell.startup import load as load_startup


def configure_logging():
    log_directory = Path.home() / ".cache" / "nexa" / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_directory / "nexa.log", maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    app_log = log_directory / "linux-apps.log"
    if app_log.is_file() and app_log.stat().st_size > 5 * 1024 * 1024:
        previous = log_directory / "linux-apps.log.1"
        previous.unlink(missing_ok=True)
        app_log.replace(previous)


def run_connection(connection):
    logger = logging.getLogger("nexa")
    logger.info("loader connected; starting desktop boot")
    activity = tg.Activity(connection)
    activity.settheme(
        0xFF10131A,
        0xFF10131A,
        0xFF10131A,
        0xFFE8EDF2,
        0xFF45D6C5,
    )

    configuration = activity.getconfiguration()
    loading = show_loading(activity, configuration)
    pending_events = deque()
    loading_active = True

    def update_loading(status, progress):
        if loading_active:
            loading.update(status, progress)

    closing = False
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-startup")
    startup_future = worker.submit(load_startup, update_loading, configuration)
    try:
        while not startup_future.done():
            event = connection.wait_event(0.01)
            if event is not None:
                pending_events.append(event)
                if event.type == tg.Event.config:
                    loading.resize(activity.getconfiguration())
                if event.type == tg.Event.destroy and event.value.get("finishing"):
                    loading_active = False
                    closing = True
                    break
    except EOFError:
        loading_active = False
        worker.shutdown(wait=False, cancel_futures=True)
        raise

    if closing:
        worker.shutdown(wait=False, cancel_futures=True)
        return

    try:
        startup = startup_future.result()
    except Exception:
        logger.exception("desktop boot failed while preparing startup state")
        raise
    finally:
        worker.shutdown(wait=True)

    from screens.home import show as show_home

    shell = show_home(activity, startup, loading.root, update_loading, loading.show)
    del startup
    logger.info("desktop shell created")
    try:
        while not shell.desktop.ready:
            event = pending_events.popleft() if pending_events else connection.wait_event(0.01)
            if event is not None:
                if event.type == tg.Event.destroy and event.value.get("finishing"):
                    loading_active = False
                    return
                shell.handle_event(event)
            file_actions.process_completions()
            shell.tick()

        loading.update("ready", 100)
        loading.hide()
        loading_active = False
        logger.info("desktop boot complete")

        while True:
            event = pending_events.popleft() if pending_events else connection.wait_event(0.025)
            file_actions.process_completions()
            shell.tick()
            if event is None:
                continue
            if event.type == tg.Event.destroy and event.value.get("finishing"):
                break
            shell.handle_event(event)
    finally:
        shell.close()


def wait_for_launch(marker):
    marker.parent.mkdir(parents=True, exist_ok=True)
    while not marker.exists():
        sleep(0.2)


def main():
    configure_logging()
    logging.getLogger("nexa").info("Nexa process started")
    background = "--background-wait" in sys.argv[1:]
    marker = Path.home() / ".cache" / "nexa" / "launch"
    active_marker = marker.with_name("active")
    if background:
        wait_for_launch(marker)
    while True:
        try:
            connection = NexaConnection()
            break
        except RuntimeError:
            if not background:
                raise
            sleep(1)
    with connection:
        if background:
            active_marker.touch()
            marker.unlink(missing_ok=True)
        try:
            run_connection(connection)
        except EOFError as error:
            if not background:
                raise
            logging.getLogger("nexa").info("loader connection ended: %s", error)
        finally:
            if background:
                active_marker.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
