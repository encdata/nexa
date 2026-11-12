import sys
from pathlib import Path
from time import sleep

import termuxgui as tg

from screens.loading import show as show_loading
from shell import file_actions
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
    startup = load_startup(loading.update, configuration)

    from screens.home import show as show_home

    shell = show_home(activity, startup, loading.root, loading.update, loading.show)
    loading.update("ready", 100)
    loading.hide()
    del loading, startup

    try:
        while True:
            event = connection.checkevent()
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
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def main():
    background = "--background-wait" in sys.argv[1:]
    marker = Path.home() / ".cache" / "nexa" / "launch"
    while True:
        if background:
            wait_for_launch(marker)
        while True:
            try:
                connection = tg.Connection()
                break
            except RuntimeError:
                if not background:
                    raise
                sleep(1)
        with connection:
            run_connection(connection)
        if not background:
            return


if __name__ == "__main__":
    main()
