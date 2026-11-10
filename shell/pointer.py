import termuxgui as tg

from shell.geometry import place
from shell.icons import load


class Pointer:
    def __init__(self, activity, root, density):
        self.activity = activity
        self.density = density
        self.view = tg.ImageView(activity, root, visibility=tg.View.GONE)
        self.view.setdimensions(24, 24)
        self.view.setimage(load("cursor"))

    def move(self, x, y):
        self.view.setvisibility(tg.View.VISIBLE)
        place(self.view, x, y, top=True)

    def hide(self):
        self.view.setvisibility(tg.View.GONE)
