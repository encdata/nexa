import base64
import json
from concurrent.futures import CancelledError, ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Lock

import termuxgui as tg
from PIL import Image, ImageOps


class Wallpaper:
    def __init__(self, activity, root, prepared, density):
        self.source = prepared["source"]
        self.density = density
        self.size = prepared["size"]
        self.width = round(self.size[0] / density)
        self.height = round(self.size[1] / density)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-wallpaper")
        self._state_lock = Lock()
        self._generation = 0
        self._closed = False
        self._image_future = None
        self.view = tg.ImageView(activity, root)
        self.view.setdimensions(self.width, self.height)
        self._queue_image(prepared["image"])

    def resize(self, width, height):
        width, height = max(1, round(width)), max(1, round(height))
        if (width, height) != (self.width, self.height):
            self.view.setdimensions(width, height)
            self.width, self.height = width, height
        size = (max(1, round(width * self.density)), max(1, round(height * self.density)))
        if size == self.size:
            return
        with self._state_lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            self.size = size
            if self._image_future is not None:
                self._image_future.cancel()
            self._image_future = self._executor.submit(self._render_and_send, generation, size)

    def _queue_image(self, image):
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._image_future = self._executor.submit(self._send_image, generation, image)

    def _render_and_send(self, generation, size):
        with self._state_lock:
            if self._closed or generation != self._generation:
                return
        self._send_image(generation, _render(self.source, size))

    def _send_image(self, generation, image):
        with self._state_lock:
            if self._closed or generation != self._generation:
                return
        encoded = base64.standard_b64encode(image).decode("ascii")
        message = json.dumps({"method": "setImage", "params": {
            "aid": self.view.a.aid, "id": self.view.id, "img": encoded,
        }})
        with self._state_lock:
            if self._closed or generation != self._generation:
                return
        self.view.a.c.send_msg(message)

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            if self._image_future is not None:
                self._image_future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def tick(self):
        future = self._image_future
        if future is None or not future.done():
            return
        self._image_future = None
        try:
            future.result()
        except CancelledError:
            pass
        except Exception as error:
            print(f"nexa: wallpaper update failed: {error}")


def _render(source, size):
    image = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=90, optimize=True)
    return encoded.getvalue()


def load(width, height, density):
    path = Path(__file__).parent.parent / "assets" / "wallpapers" / "windows-11-bloom.jpg"
    with Image.open(path) as source:
        image = source.convert("RGB")
    size = (max(1, round(width * density)), max(1, round(height * density)))
    return {"source": image, "image": _render(image, size), "size": size}


def add(activity, root, prepared, density):
    return Wallpaper(activity, root, prepared, density)
