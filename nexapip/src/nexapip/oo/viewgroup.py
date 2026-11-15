from typing import Optional

import nexapip as tg

from abc import ABC
from nexapip.oo.view import View


class ViewGroup(View, tg.ViewGroup, ABC):

    def __init__(self, activity: tg.Activity, id: int, parent: Optional['ViewGroup']):
        View.__init__(self, activity, id, parent)
        self.views = []

    def clearchildren(self):
        """Removes all child Views of this ViewGroup."""
        tg.ViewGroup.clearchildren(self)
        self.views = []
