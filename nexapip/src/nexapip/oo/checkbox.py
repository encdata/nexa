from typing import Optional, Literal

import nexapip as tg

from nexapip.oo.view import View


class Checkbox(View, tg.Checkbox):
    """This represents a CheckBox."""

    def __init__(self, activity: tg.Activity, text: str, parent: Optional[View] = None, checked: bool = False,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.Checkbox.__init__(self, activity, text, parent, checked, visibility)
        View.__init__(self, activity, self.id, parent)
