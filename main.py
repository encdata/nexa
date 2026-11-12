import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

import termuxgui as tg

from screens.loading import show as show_loading
from shell import file_actions
from shell.nexa_connection import NexaConnection
from shell.startup import load as load_startup


def run_connection(connection):
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
    while not startup_future.done():
        event = connection.checkevent()
        if event is not None:
            pending_events.append(event)
            if event.type == tg.Event.config:
                loading.resize(activity.getconfiguration())
            if event.type == tg.Event.destroy and event.value.get("finishing"):
                loading_active = False
                closing = True
                break
        sleep(0.01)

    if closing:
        worker.shutdown(wait=False, cancel_futures=True)
        return

    try:
        startup = startup_future.result()
    finally:
        worker.shutdown(wait=True)

    from screens.home import show as show_home

    shell = show_home(activity, startup, loading.root, update_loading, loading.show)
    del startup
    try:
        while not shell.desktop.ready or not shell.preloads_ready:
            event = pending_events.popleft() if pending_events else connection.checkevent()
            if event is not None:
                if event.type == tg.Event.destroy and event.value.get("finishing"):
                    loading_active = False
                    return
                shell.handle_event(event)
            file_actions.process_completions()
            shell.tick()
            if event is None:
                sleep(0.01)

        loading.update("ready", 100)
        loading.hide()
        loading_active = False

        while True:
            event = pending_events.popleft() if pending_events else connection.checkevent()
            file_actions.process_completions()
            shell.tick()
            if event is None:
                sleep(0.025)
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
    background = "--background-wait" in sys.argv[1:]
    marker = Path.home() / ".cache" / "nexa" / "launch"
    active_marker = marker.with_name("active")
    while True:
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
            finally:
                if background:
                    active_marker.unlink(missing_ok=True)
        if not background:
            return


if __name__ == "__main__":
    main()
