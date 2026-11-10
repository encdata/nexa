import termuxgui as tg


class FramebufferView:
    def __init__(self, activity, parent, width, height, on_touch=None):
        self.connection = activity.c
        self.width = int(width)
        self.height = int(height)
        if self.width < 1 or self.height < 1:
            raise ValueError("framebuffer dimensions must be positive")
        self.buffer = tg.Buffer(self.connection, self.width, self.height)
        self.view = tg.ImageView(activity, parent)
        self.view.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        self.view.setbuffer(self.buffer)
        self.on_touch = on_touch
        self.closed = False
        if on_touch is not None:
            self.view.sendtouchevent(True)

    def present(self, pixels=None):
        if self.closed:
            raise RuntimeError("framebuffer view is closed")
        if pixels is not None:
            self.buffer.mem[:] = pixels
        self.buffer.blit()
        self.view.refresh()

    def resize(self, width, height):
        if self.closed:
            raise RuntimeError("framebuffer view is closed")
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == (self.width, self.height):
            return

        buffer = tg.Buffer(self.connection, width, height)
        previous = self.buffer
        try:
            self.view.setbuffer(buffer)
        except Exception:
            buffer.remove()
            raise
        self.buffer = buffer
        self.width, self.height = width, height
        previous.remove()

    def on_window_resize(self, width, height):
        self.resize(width, height)

    def handle_event(self, event):
        if (self.on_touch is None or event.type != tg.Event.touch
                or getattr(event, "id", None) != self.view.id):
            return False
        self.on_touch(event.value)
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.view.delete()
        finally:
            self.buffer.remove()
