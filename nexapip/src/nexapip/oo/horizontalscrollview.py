from typing import Optional, Literal

import nexapip as tg

from nexapip.activity import Activity
from nexapip.oo.framelayout import FrameLayout
from nexapip.oo.viewgroup import ViewGroup


class HorizontalScrollView(FrameLayout, tg.HorizontalScrollView):
    """This represents a HorizontalScrollView."""

    def __init__(self, activity: Activity, parent: Optional[ViewGroup] = None, fillviewport: bool = False,
                 snapping: bool = False, nobar: bool = False,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.HorizontalScrollView.__init__(self, activity, parent, fillviewport, snapping, nobar, visibility)
        ViewGroup.__init__(self, activity, self.id, parent)
