from typing import Optional, Literal

import nexapip as tg

from nexapip.activity import Activity
from nexapip.oo.view import View


class Button(View, tg.Button):
    """This represents a Button."""

    def __init__(self, activity: Activity, text: str, parent: Optional[View] = None, allcaps: bool = False,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.Button.__init__(self, activity, text, parent, allcaps, visibility)
        View.__init__(self, activity, self.id, parent)
