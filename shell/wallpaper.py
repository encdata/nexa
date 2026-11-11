from io import BytesIO
from pathlib import Path

import termuxgui as tg
from PIL import Image, ImageOps


class Wallpaper:
    def __init__(self, activity, root, prepared, density):
        self.source = prepared["source"]
        self.density = density
        self.size = prepared["size"]
        self.width = round(self.size[0] / density)
        self.height = round(self.size[1] / density)
        self.view = tg.ImageView(activity, root)
        self.view.setdimensions(self.width, self.height)
        self.view.setimage(prepared["image"])

    def resize(self, width, height):
        width, height = max(1, round(width)), max(1, round(height))
        self.view.setdimensions(width, height)
        self.width, self.height = width, height
        size = (max(1, round(width * self.density)), max(1, round(height * self.density)))
        if size == self.size:
            return
        self.view.setimage(_render(self.source, size))
        self.size = size


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
