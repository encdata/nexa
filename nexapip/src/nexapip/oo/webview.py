from typing import Optional, Literal

import nexapip as tg

from nexapip.oo.view import View


class WebView(View, tg.WebView):
    """This represents a WebView."""

    def __init__(self, activity: tg.Activity, parent: Optional[View] = None,
                 visibility: Optional[Literal[0, 1, 2]] = None,
                 electron_bridge: Optional[dict] = None):
        tg.WebView.__init__(self, activity, parent, visibility, electron_bridge)
        View.__init__(self, activity, self.id, parent)
