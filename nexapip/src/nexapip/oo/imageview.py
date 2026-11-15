from typing import Optional, Literal

import nexapip as tg

from nexapip.oo.view import View


class ImageView(View, tg.ImageView):
    """This represents an ImageView."""

    def __init__(self, activity: tg.Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None):
        tg.ImageView.__init__(self, activity, parent, visibility)
        View.__init__(self, activity, self.id, parent)
