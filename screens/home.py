import termuxgui as tg
from time import monotonic, sleep

from shell.desktop import Desktop
from shell.taskbar import Taskbar
from shell.window_manager import WindowManager
from shell.pointer import Pointer
from shell.text_direction import is_rtl
from shell.wallpaper import add as add_wallpaper


class DesktopShell:
    def __init__(self, activity, startup, parent, progress, show_progress):
        self.activity = activity
        self.apps = startup["apps"]
        config = startup["configuration"]
        self.screen_width = config["screenwidth"]
        self.screen_height = config["screenheight"]
        self.density = config["density"]
        self.keyboard_hidden = config.get("keyboardHidden", True)
        self.next_wallpaper_resize_check = 0
        self.rtl = is_rtl(config)
        self.root = tg.FrameLayout(activity, parent)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.root.setbackgroundcolor(0xFF10131A)
        self.root.sendtouchevent(True)
        show_progress()
        previous_size = None
        root_size = (0, 0)
        for _ in range(5):
            measured_size = tuple(self.root.getdimensions())
            if measured_size[0] > 0 and measured_size[1] > 0:
                root_size = measured_size
                if measured_size == previous_size:
                    break
            previous_size = measured_size
            sleep(0.04)
        root_width, root_height = root_size
        if root_width > 0 and root_height > 0:
            self.screen_width = round(root_width / self.density)
            self.screen_height = round(root_height / self.density)
        self.wallpaper = add_wallpaper(activity, self.root, startup["wallpaper"], self.density)
        self.wallpaper.resize(self.screen_width, self.screen_height)
        self.windows = WindowManager(activity, self.root, self.apps, self.screen_width, self.screen_height,
                                     self.density, rtl=self.rtl)
        self.windows.on_open_app = self.open_app
        self.desktop = Desktop(activity, self.root, self.apps, self.open_app,
                               self.screen_width, self.screen_height, self.density,
                               startup["positions"], startup["home_items"], progress)
        self.windows.on_association_change = self.desktop.refresh_associated_icons
        self.taskbar = Taskbar(activity, self.root, self.apps, self.open_app,
                               self.windows.activate_from_taskbar,
                               self.screen_width, self.screen_height, rtl=self.rtl)
        self.windows.on_active_change = self.taskbar.set_active_window
        self.windows.on_close = self.taskbar.remove_window
        self.pointer = Pointer(activity, self.root, self.density)
        self.desktop.pointer = self.pointer
        self.windows.pointer = self.pointer

    def open_app(self, app_id, context=None):
        window_id = self.windows.open_app(app_id, context)
        if window_id is not None:
            self.taskbar.add_window(window_id, self.apps[app_id], self.windows.windows[window_id]["title"])
            self.taskbar.set_active_window(self.windows.active_window)

    def handle_event(self, event):
        if event.type == tg.Event.config:
            width = event.value.get("screenwidth", self.screen_width)
            height = event.value.get("screenheight", self.screen_height)
            self._resize_screen(width, height)
            return True
        if (event.type == tg.Event.touch
                and event.id == self.root.id
                and event.value.get("action") == tg.Event.touch_down):
            self.taskbar.dismiss_menu()
        return (self.windows.handle_event(event)
                or self.desktop.handle_event(event)
                or self.taskbar.handle_event(event))

    def tick(self):
        now = monotonic()
        if now >= self.next_wallpaper_resize_check:
            self.next_wallpaper_resize_check = now + 0.3
            config = self.activity.getconfiguration()
            self.keyboard_hidden = config.get("keyboardHidden", self.keyboard_hidden)
            self._resize_screen(config.get("screenwidth", self.screen_width),
                                config.get("screenheight", self.screen_height))
        self.windows.tick()

    def _resize_screen(self, width, height):
        if (width, height) == (self.screen_width, self.screen_height):
            return
        self.screen_width, self.screen_height = width, height
        self.wallpaper.resize(width, height)
        self.windows.resize(width, height)
        self.desktop.resize(width, height)
        self.taskbar.resize(width, height)

    def close(self):
        self.desktop.close()
        self.windows.shutdown()


def show(activity, startup, parent, progress, show_progress):
    return DesktopShell(activity, startup, parent, progress, show_progress)
