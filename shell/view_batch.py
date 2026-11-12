import base64

import termuxgui as tg


def create_views(activity, parent, specifications):
    views = []
    for specification in specifications:
        item = dict(specification)
        if "parent_index" in item:
            item["parentIndex"] = item.pop("parent_index")
        else:
            item["parent"] = parent.id
        image = item.get("image")
        if isinstance(image, bytes):
            item["image"] = base64.b64encode(image).decode("ascii")
        views.append(item)

    ids = activity.c.send_read_msg({"method": "createViewBatch",
                                    "params": {"aid": activity.aid, "views": views}})
    return [tg.View(activity, view_id) for view_id in ids]


def set_image(activity, view_id, image):
    activity.c.send_msg({"method": "setImage", "params": {
        "aid": activity.aid, "id": view_id,
        "img": base64.b64encode(image).decode("ascii"),
    }})


def set_text(activity, view_id, text):
    activity.c.send_msg({"method": "setText", "params": {
        "aid": activity.aid, "id": view_id, "text": text,
    }})
