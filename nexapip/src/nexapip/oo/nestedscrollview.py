from typing import Optional, Literal

import nexapip as tg

from nexapip.view import View
from nexapip.activity import Activity
from nexapip.oo.framelayout import FrameLayout
from nexapip.oo.viewgroup import ViewGroup


class NestedScrollView(FrameLayout, tg.NestedScrollView):
    """This represents a NestedScrollView."""

    def __init__(self, activity: Activity, parent: Optional[View] = None, fillviewport: bool = False,
                 snapping: bool = False, nobar: bool = False,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.NestedScrollView.__init__(self, activity, parent, fillviewport, snapping, nobar, visibility)
        ViewGroup.__init__(self, activity, self.id, parent)
