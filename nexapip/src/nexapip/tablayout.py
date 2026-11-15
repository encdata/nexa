from typing import Optional, List, Literal

from nexapip.view import View
from nexapip.activity import Activity
from nexapip.horizontalscrollview import HorizontalScrollView
from nexapip.viewgroup import ViewGroup


class TabLayout(HorizontalScrollView):
    """This represents a TabLayout."""

    def __init__(self, activity: Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        args = {"aid": activity.aid, "visibility": visibility}
        if parent is not None:
            args["parent"] = parent.id
        ViewGroup.__init__(self, activity, activity.c.send_read_msg({"method": "createTabLayout", "params": args}))

    def setlist(self, list: List[str]):
        """Sets the list tabs."""
        self.a.c.send_msg({"method": "setList", "params": {"aid": self.a.aid, "id": self.id, "list": list}})

    def selecttab(self, tab: int):
        """Selects a tab in the TabLayout. tab is the index of the tab to select, starting at 0."""
        self.a.c.send_msg({"method": "selectTab", "params": {"aid": self.a.aid, "id": self.id, "tab": tab}})
