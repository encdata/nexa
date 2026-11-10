import termuxgui as tg
from time import sleep

from screens.loading import show as show_loading
from shell import file_actions
from shell.startup import load as load_startup


def main():
    with tg.Connection() as connection:
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
                if event is None:
                    sleep(0.025)
                    continue
                if event.type == tg.Event.destroy and event.value.get("finishing"):
                    break
                shell.handle_event(event)
        finally:
            shell.close()


if __name__ == "__main__":
    main()
