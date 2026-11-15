_THEMES = {
    "dark": {
        "background": 0xFF10131A,
        "surface": 0xFF202630,
        "surface_light": 0xFF2B3440,
        "window_border": 0xFF8290A3,
        "window_content": 0xFF2B3440,
        "accent": 0xFF45D6C5,
        "text": 0xFFE8EDF2,
        "muted": 0xFF9AA8B5,
    },
    "light": {
        "background": 0xFFF4F6F8,
        "surface": 0xFFFFFFFF,
        "surface_light": 0xFFE7EBEF,
        "window_border": 0xFF8A96A3,
        "window_content": 0xFFF4F6F8,
        "accent": 0xFF087F72,
        "text": 0xFF17212B,
        "muted": 0xFF596775,
    },
}


def set_theme(theme):
    if theme not in _THEMES:
        theme = "dark"
    globals().update(_THEMES[theme])
    globals()["current_theme"] = theme
    return theme


current_theme = set_theme("dark")
