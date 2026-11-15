from typing import Optional, Literal

import nexapip as tg

from nexapip.oo.view import View
from nexapip.activity import Activity
from nexapip.oo.viewgroup import ViewGroup


class SwipeRefreshLayout(ViewGroup, tg.SwipeRefreshLayout):
    """This represents a SwipeRefreshLayout."""

    def __init__(self, activity: Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.SwipeRefreshLayout.__init__(self, activity, parent, visibility)
        ViewGroup.__init__(self, activity, self.id, parent)
