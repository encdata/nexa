import base64

import termuxgui as tg


def _encode(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


class ReusableObject:
    def __init__(self, activity, name, views):
        self.activity = activity
        self.name = name
        self.key_indices = [(view["key"], index, view["type"]) for index, view in enumerate(views)
                            if "key" in view]
        keys = [key for key, _, _ in self.key_indices]
        if len(keys) != len(set(keys)):
            raise ValueError("reusable object view keys must be unique")

        self.activity.c.send_read_msg({
            "method": "registerReusableObject",
            "params": {"aid": activity.aid, "name": name, "views": _encode(views)},
        })

    def create(self, parent, properties=None):
        ids = self.activity.c.send_read_msg({
            "method": "createReusableObject",
            "params": {
                "aid": self.activity.aid,
                "parent": parent.id,
                "name": self.name,
                "properties": _encode(properties or {}),
            },
        })
        classes = {
            "frame": tg.FrameLayout,
            "image": tg.ImageView,
            "linear": tg.LinearLayout,
            "text": tg.TextView,
        }
        views = {}
        for key, index, view_type in self.key_indices:
            view_class = classes.get(view_type)
            if view_class is None:
                raise ValueError(f"unsupported reusable object view type: {view_type}")
            view = view_class.__new__(view_class)
            tg.View.__init__(view, self.activity, ids[index])
            views[key] = view
        return views
