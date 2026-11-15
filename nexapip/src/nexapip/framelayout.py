from typing import Optional, Literal

from nexapip.view import View
from nexapip.activity import Activity
from nexapip.viewgroup import ViewGroup


class FrameLayout(ViewGroup):
    """This represents a FrameLayout."""

    def __init__(self, activity: Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        args = {"aid": activity.aid, "visibility": visibility}
        if parent is not None:
            args["parent"] = parent.id
        ViewGroup.__init__(self, activity, activity.c.send_read_msg({"method": "createFrameLayout", "params": args}))
