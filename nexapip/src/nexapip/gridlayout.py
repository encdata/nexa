from typing import Optional, Literal

from nexapip.view import View
from nexapip.activity import Activity
from nexapip.viewgroup import ViewGroup


class GridLayout(ViewGroup):
    """This represents a GridLayout."""

    def __init__(self, activity: Activity, rows: int, cols: int, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        args = {"aid": activity.aid, "rows": rows, "cols": cols, "visibility": visibility}
        if parent is not None:
            args["parent"] = parent.id
        ViewGroup.__init__(self, activity, activity.c.send_read_msg({"method": "createGridLayout", "params": args}))
