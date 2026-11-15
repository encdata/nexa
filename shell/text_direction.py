def is_rtl(_configuration):
    return False


def force_ltr(view):
    view.a.c.send_msg({"method": "setLayoutDirection", "params": {
        "aid": view.a.aid, "id": view.id, "direction": "ltr",
    }})
