from typing import Optional, Literal

from nexapip.activity import Activity
from nexapip.view import View
from nexapip.viewgroup import ViewGroup


class LinearLayout(ViewGroup):
    """This represents a LinearLayout."""

    def __init__(self, activity: Activity, parent: Optional[View] = None, vertical: bool = True,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        args = {"aid": activity.aid, "vertical": vertical, "visibility": visibility}
        if parent is not None:
            args["parent"] = parent.id
        ViewGroup.__init__(self, activity, activity.c.send_read_msg({"method": "createLinearLayout", "params": args}))
