from typing import Optional, Literal

import nexapip as tg

from nexapip.activity import Activity
from nexapip.oo.viewgroup import ViewGroup


class FrameLayout(ViewGroup, tg.FrameLayout):
    """This represents a FrameLayout."""

    def __init__(self, activity: Activity, parent: Optional[ViewGroup] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.FrameLayout.__init__(self, activity, parent, visibility)
        ViewGroup.__init__(self, activity, self.id, parent)
