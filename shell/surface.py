from functools import lru_cache
import struct
import zlib

import termuxgui as tg


@lru_cache(maxsize=32)
def _solid_png(width, height, color):
    rgba = bytes(((color >> 16) & 255, (color >> 8) & 255, color & 255, (color >> 24) & 255))
    row = b"\0" + rgba * width

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(row * height))
    return image + chunk(b"IEND", b"")


def set_color(image, width, height, color):
    image.setdimensions(width, height)
    image.setimage(_solid_png(width, height, color))


def add(activity, parent, width, height, color):
    image = tg.ImageView(activity, parent)
    set_color(image, width, height, color)
    return image
