_pages = {}
_pending_content = {}


def register_page(page_id, title, icon):
    if page_id in _pages:
        raise ValueError(f"settings page {page_id!r} is already registered")
    page = {"id": page_id, "title": title, "icon": icon,
            "content": _pending_content.pop(page_id, [])}
    _pages[page_id] = page
    return page


def register_content(page_id, builder):
    page = _pages.get(page_id)
    if not callable(builder):
        raise TypeError("settings content must be callable")
    if page is None:
        _pending_content.setdefault(page_id, []).append(builder)
    else:
        page["content"].append(builder)
    return builder


def get_pages():
    return tuple(_pages.values())
