from functools import lru_cache
from math import ceil, sqrt
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


@lru_cache(maxsize=32)
def _rounded_png(width, height, color, radius):
    scale = 2
    pixel_width, pixel_height = width * scale, height * scale
    radius = min(radius * scale, pixel_width // 2, pixel_height // 2)
    if radius == 0:
        return _solid_png(pixel_width, pixel_height, color)
    red, green, blue, alpha = ((color >> 16) & 255, (color >> 8) & 255,
                               color & 255, (color >> 24) & 255)
    rgba = bytes((red, green, blue, alpha))
    transparent = b"\0\0\0\0"
    rows = bytearray()
    for y in range(pixel_height):
        if y < radius:
            distance_y = radius - y - 0.5
        elif y >= pixel_height - radius:
            distance_y = y + 0.5 - (pixel_height - radius)
        else:
            distance_y = 0
        inset = max(0, min(pixel_width // 2,
                           ceil(radius - sqrt(max(0, radius ** 2 - distance_y ** 2)) - 0.5)))
        rows.extend(b"\0" + transparent * inset + rgba * (pixel_width - inset * 2)
                    + transparent * inset)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">2I5B", pixel_width, pixel_height, 8, 6, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(rows))
    return image + chunk(b"IEND", b"")


def set_color(image, width, height, color):
    image.setdimensions(width, height)
    image.setimage(_solid_png(width, height, color))


def add(activity, parent, width, height, color):
    image = tg.ImageView(activity, parent)
    set_color(image, width, height, color)
    return image


def set_rounded_color(image, width, height, color, radius=14):
    image.setdimensions(width, height)
    image.setimage(_rounded_png(width, height, color, radius))


def add_rounded(activity, parent, width, height, color, radius=14):
    image = tg.ImageView(activity, parent)
    set_rounded_color(image, width, height, color, radius)
    return image


def set_image_scale_type(image, scale_type):
    image.a.c.send_msg({"method": "setImageScaleType", "params": {
        "aid": image.a.aid, "id": image.id, "scaleType": scale_type,
    }})
