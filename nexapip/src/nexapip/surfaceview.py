from nexapip.view import View
from nexapip.activity import Activity
from nexapip.buffer import Buffer


class SurfaceView(View):
    """A Surface-backed view that displays frames from a shared Nexa buffer."""

    def __init__(self, activity: Activity, parent=None, keyboard: bool = False,
                 touch: bool = False):
        args = {"aid": activity.aid, "keyboard": keyboard}
        if parent is not None:
            args["parent"] = parent.id
        super().__init__(activity, activity.c.send_read_msg(
            {"method": "createSurfaceView", "params": args}))
        self.on_surface_changed = None
        self.on_touch = None
        self.on_key = None
        self._frame_buffer = None
        self.sendtouchevent(touch)

    def handleevent(self, event):
        if event.type == "surfaceChanged" and getattr(event, "id", None) == self.id:
            callback = self.on_surface_changed
            if callback is not None:
                callback(event.value["width"], event.value["height"])
        elif getattr(event, "id", None) == self.id:
            if event.type == "touch" and self.on_touch is not None:
                self.on_touch(event.value)
            elif event.type == "key" and self.on_key is not None:
                self.on_key(event.value)
        super().handleevent(event)

    def setbuffer(self, buffer):
        """Displays a Buffer on this surface. Call Buffer.blit() before presenting."""
        self.a.c.send_msg({"method": "setBuffer", "params": {
            "aid": self.a.aid, "id": self.id, "bid": buffer.bid}})

    def present_shm(self, source, width: int, height: int, stride: int,
                    pixel_format: str = "ARGB8888"):
        """Copies a Wayland shm frame into the hardware-backed surface view."""
        if pixel_format not in ("ARGB8888", "XRGB8888"):
            raise ValueError("surface frames must use ARGB8888 or XRGB8888")
        if width <= 0 or height <= 0 or stride < width * 4:
            raise ValueError("invalid shm frame dimensions or stride")

        pixels = memoryview(source).cast("B")
        row_bytes = width * 4
        required = (height - 1) * stride + row_bytes
        if pixels.nbytes < required:
            raise ValueError("shm frame is smaller than its dimensions and stride")

        if self._frame_buffer is None or (self._frame_buffer.w, self._frame_buffer.h) != (width, height):
            if self._frame_buffer is not None:
                self._frame_buffer.remove()
            self._frame_buffer = Buffer(self.a.c, width, height)

        target = self._frame_buffer.mem
        for y in range(height):
            src_start = y * stride
            dst_start = y * row_bytes
            target[dst_start:dst_start + row_bytes] = pixels[src_start:src_start + row_bytes]
            if pixel_format == "XRGB8888":
                for offset in range(dst_start + 3, dst_start + row_bytes, 4):
                    target[offset] = 0xff

        self._frame_buffer.blit()
        self.setbuffer(self._frame_buffer)

    def close(self):
        if self._frame_buffer is not None:
            self._frame_buffer.remove()
            self._frame_buffer = None
        self.delete()
