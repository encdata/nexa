def place(view, x, y, top=False):
    view.a.c.send_msg({"method": "setViewLocation", "params": {
        "aid": view.a.aid,
        "id": view.id,
        "x": round(x),
        "y": round(y),
        "dp": True,
        "top": top,
    }})
