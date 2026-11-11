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


@lru_cache(maxsize=32)
def _rounded_png(width, height, color, radius):
    scale = 2
    pixel_width, pixel_height = width * scale, height * scale
    radius = min(radius * scale, pixel_width // 2, pixel_height // 2)
    red, green, blue, alpha = ((color >> 16) & 255, (color >> 8) & 255,
                               color & 255, (color >> 24) & 255)
    rows = bytearray()
    for y in range(pixel_height):
        rows.append(0)
        for x in range(pixel_width):
            near_x = min(max(x + 0.5, radius), pixel_width - radius)
            near_y = min(max(y + 0.5, radius), pixel_height - radius)
            inside = (x + 0.5 - near_x) ** 2 + (y + 0.5 - near_y) ** 2 <= radius ** 2
            rows.extend((red, green, blue, alpha if inside else 0))

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
