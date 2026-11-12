import base64

import termuxgui as tg


_VIEW_CLASSES = {
    "frame": tg.FrameLayout,
    "image": tg.ImageView,
    "linear": tg.LinearLayout,
    "edit": tg.EditText,
    "checkbox": tg.Checkbox,
    "switch": tg.Switch,
    "spinner": tg.Spinner,
    "slider": tg.View,
    "scroll": tg.NestedScrollView,
    "horizontal_scroll": tg.HorizontalScrollView,
    "text": tg.TextView,
}
_TEXT_GRAVITY = {
    "start_top": (0, 0),
    "start_center_vertical": (0, 1),
    "start_bottom": (0, 2),
    "center_top": (1, 0),
    "center": (1, 1),
    "center_bottom": (1, 2),
    "end_top": (2, 0),
    "end_center_vertical": (2, 1),
    "end_bottom": (2, 2),
}
_LAYOUT_ALIGNMENTS = {"center", "start", "end", "top", "bottom",
                      "center_vertical", "center_horizontal", "fill"}
_INPUT_TYPES = {"text", "textMultiLine", "phone", "date", "time", "datetime", "number",
                "numberDecimal", "numberPassword", "numberSigned", "numberDecimalSigned",
                "textEmailAddress", "textPassword"}


def _node(view_type, children=(), **properties):
    result = {"type": view_type, **properties}
    if children:
        result["children"] = list(children)
    return result


def _dimension(value):
    return {"fill": tg.View.MATCH_PARENT, "wrap": tg.View.WRAP_CONTENT}.get(value, value)


def row(*children, **properties):
    return _node("row", children, **properties)


def column(*children, **properties):
    return _node("column", children, **properties)


def stack(*children, **properties):
    return _node("stack", children, **properties)


def scroll(*children, **properties):
    return _node("scroll", children, **properties)


def horizontal_scroll(*children, **properties):
    return _node("horizontal_scroll", children, **properties)


def label(text, **properties):
    return _node("text", text=text, **properties)


def text_input(text="", **properties):
    properties.setdefault("singleline", True)
    properties.setdefault("line", False)
    properties.setdefault("inputtype", "text")
    return _node("edit", text=text, **properties)


def button(text, **properties):
    properties.setdefault("clickable", True)
    return _node("button", text=text, **properties)


def checkbox(text="", checked=False, **properties):
    return _node("checkbox", text=text, checked=checked, **properties)


def switch(text="", checked=False, **properties):
    return _node("switch", text=text, checked=checked, **properties)


def spinner(options, selected=0, **properties):
    return _node("spinner", options=list(options), selected=selected, **properties)


def slider(maximum=100, progress=0, **properties):
    return _node("slider", max=maximum, progress=progress, **properties)


def icon(image, **properties):
    return _node("image", image=image, **properties)


def card(*children, **properties):
    return _node("column", children, **properties)


def spacer(width=0, height=0, **properties):
    return _node("space", width=width, height=height, **properties)


def list_view(items=(), **properties):
    return _node("list", items=list(items), **properties)


def grid(children, columns, gap=0, **properties):
    children = list(children)
    if columns < 1:
        raise ValueError("grid columns must be positive")
    rows = []
    for offset in range(0, len(children), columns):
        cells = children[offset:offset + columns]
        cells = [dict(cell, width=0, weight=1) if isinstance(cell, dict) else cell
                 for cell in cells]
        cells.extend({"type": "frame", "width": 0, "weight": 1}
                     for _ in range(columns - len(cells)))
        rows.append(row(*cells, width="MATCH_PARENT", height="WRAP_CONTENT",
                        weight=0, gap=gap))
    return column(*rows, **properties)


class Layout(dict):
    def __init__(self, activity, views, callbacks, long_clicks, text_events,
                 selection_events, progress_events, states, all_views=None):
        super().__init__(views)
        self.activity = activity
        self._keys_by_id = {view.id: key for key, view in views.items()}
        tracked_views = views.values() if all_views is None else all_views
        self._views_by_id = {view.id: view for view in tracked_views}
        self._callbacks = callbacks
        self._long_clicks = long_clicks
        self._text_events = text_events
        self._selection_events = selection_events
        self._progress_events = progress_events
        self._states = states

    def key_for(self, event_or_id):
        view_id = getattr(event_or_id, "id", event_or_id)
        return self._keys_by_id.get(view_id)

    def handle_event(self, event):
        if event.type == tg.Event.click:
            callbacks = self._callbacks
        elif event.type == tg.Event.longClick:
            callbacks = self._long_clicks
        elif event.type == tg.Event.text:
            callbacks = self._text_events
        elif event.type == tg.Event.itemselected:
            callbacks = self._selection_events
        elif event.type == "progress":
            callbacks = self._progress_events
        else:
            return False
        callback = callbacks.get(event.id)
        view = self._views_by_id.get(event.id)
        if callback is None and view is None:
            return False
        handle = getattr(view, "handleevent", None)
        if handle is not None:
            handle(event)
        if callback is not None:
            callback(event)
        return True

    def attach(self, key, view, on_click=None, on_long_click=None, on_text=None,
               on_item_selected=None, on_progress=None):
        if key in self or view.id in self._keys_by_id:
            raise ValueError(f"duplicate layout key or view: {key}")
        self[key] = view
        self._keys_by_id[view.id] = key
        self._views_by_id[view.id] = view
        if on_click is not None:
            if not callable(on_click):
                raise TypeError("on_click must be callable")
            view.sendclickevent(True)
            self._callbacks[view.id] = on_click
        if on_long_click is not None:
            if not callable(on_long_click):
                raise TypeError("on_long_click must be callable")
            view.sendlongclickevent(True)
            self._long_clicks[view.id] = on_long_click
        if on_text is not None:
            if not callable(on_text):
                raise TypeError("on_text must be callable")
            view.sendtextevent(True)
            self._text_events[view.id] = on_text
        if on_item_selected is not None:
            if not callable(on_item_selected):
                raise TypeError("on_item_selected must be callable")
            self._selection_events[view.id] = on_item_selected
        if on_progress is not None:
            if not callable(on_progress):
                raise TypeError("on_progress must be callable")
            self._progress_events[view.id] = on_progress
        return view

    def remove(self, key, delete=True):
        view = self.pop(key)
        self._keys_by_id.pop(view.id, None)
        self._views_by_id.pop(view.id, None)
        self._callbacks.pop(view.id, None)
        self._long_clicks.pop(view.id, None)
        self._text_events.pop(view.id, None)
        self._selection_events.pop(view.id, None)
        self._progress_events.pop(view.id, None)
        if delete:
            close = getattr(view, "close", None)
            if close is not None:
                close()
            view.delete()
        return view

    def close(self):
        closed = set()
        for view in self._views_by_id.values():
            if view.id in closed:
                continue
            closed.add(view.id)
            close = getattr(view, "close", None)
            if close is not None:
                close()

    def set(self, key, properties=None, **changes):
        view = self[key]
        values = dict(properties or {})
        values.update(changes)
        for name, value in values.items():
            if name == "text":
                view.settext(value)
            elif name == "textSize":
                view.settextsize(value)
            elif name == "textColor":
                view.settextcolor(value)
            elif name == "background":
                view.setbackgroundcolor(value)
            elif name == "image":
                view.setimage(value)
            elif name == "visibility":
                view.setvisibility(value)
            elif name == "clickable":
                view.setclickable(value)
            elif name == "enabled":
                self.activity.c.send_msg({"method": "setEnabled", "params": {
                    "aid": self.activity.aid, "id": view.id, "enabled": value,
                }})
            elif name in ("width", "height"):
                getattr(view, f"set{name}")(_dimension(value))
            elif name == "dimensions":
                view.setdimensions(*(_dimension(part) for part in value))
            elif name == "weight":
                view.setlinearlayoutparams(value)
            elif name == "margins":
                for direction, amount in value.items():
                    view.setmargin(amount, direction)
            elif name == "padding":
                _set_padding(self.activity, view, value)
            elif name == "gravity":
                horizontal, vertical = _text_gravity[value]
                view.setgravity(horizontal, vertical)
            elif name == "scaleType":
                self.activity.c.send_msg({"method": "setImageScaleType", "params": {
                    "aid": self.activity.aid, "id": view.id, "scaleType": value,
                }})
            elif name == "align":
                if value not in _LAYOUT_ALIGNMENTS:
                    raise ValueError(f"unsupported layout alignment: {value}")
                self.activity.c.send_msg({"method": "setLayoutGravity", "params": {
                    "aid": self.activity.aid, "id": view.id, "align": value,
                }})
            elif name == "checked":
                view.setchecked(value)
            elif name == "options":
                view.setlist(value)
            elif name == "selected":
                view.selectitem(value)
            elif name == "progress":
                self.activity.c.send_msg({"method": "setProgress", "params": {
                    "aid": self.activity.aid, "id": view.id, "progress": value,
                }})
            else:
                raise ValueError(f"unsupported layout update property: {name}")

    def set_state(self, key, state):
        try:
            properties = self._states[key][state]
        except KeyError as error:
            raise ValueError(f"unknown state {state!r} for layout view {key!r}") from error
        self.set(key, properties)


def create_edit_text(activity, parent, text="", properties=None, **options):
    options.setdefault("singleline", True)
    options.setdefault("line", False)
    options.setdefault("inputtype", "text")
    view = tg.EditText(activity, text, parent, **options)
    if properties:
        _set_control_properties(view, properties)
    return view


def create_switch(activity, parent, text="", checked=False, properties=None):
    view = tg.Switch(activity, text, parent, checked=checked)
    if properties:
        _set_control_properties(view, properties)
    return view


def _set_padding(activity, view, padding):
    if not isinstance(padding, (int, dict)):
        raise TypeError("padding must be an integer or direction map")
    values = {"padding": padding} if isinstance(padding, int) else padding
    for direction, amount in values.items():
        if direction not in ("padding", "top", "bottom", "left", "right"):
            raise ValueError(f"unsupported padding direction: {direction}")
        params = {"aid": activity.aid, "id": view.id, "padding": amount}
        if direction != "padding":
            params["dir"] = direction
        activity.c.send_msg({"method": "setPadding", "params": params})


def _set_control_properties(view, properties):
    for name, value in properties.items():
        if name in ("width", "height"):
            getattr(view, f"set{name}")(_dimension(value))
        elif name == "dimensions":
            view.setdimensions(*(_dimension(part) for part in value))
        elif name == "weight":
            view.setlinearlayoutparams(value)
        elif name == "margins":
            for direction, amount in value.items():
                view.setmargin(amount, direction)
        elif name == "padding":
            if not isinstance(value, (int, dict)):
                raise TypeError("padding must be an integer or direction map")
            _set_padding(view.a, view, value)
        elif name == "background":
            view.setbackgroundcolor(value)
        elif name == "text":
            view.settext(value)
        elif name == "textSize":
            view.settextsize(value)
        elif name == "textColor":
            view.settextcolor(value)
        elif name == "visibility":
            view.setvisibility(value)
        elif name == "enabled":
            view.a.c.send_msg({"method": "setEnabled", "params": {
                "aid": view.a.aid, "id": view.id, "enabled": value,
            }})
        elif name == "checked":
            view.setchecked(value)
        elif name == "options":
            view.setlist(value)
        elif name == "selected":
            view.selectitem(value)
        elif name == "progress":
            view.a.c.send_msg({"method": "setProgress", "params": {
                "aid": view.a.aid, "id": view.id, "progress": value,
            }})
        elif name == "gravity":
            horizontal, vertical = _TEXT_GRAVITY[value]
            view.setgravity(horizontal, vertical)
        elif name == "align":
            if value not in _LAYOUT_ALIGNMENTS:
                raise ValueError(f"unsupported layout alignment: {value}")
            view.a.c.send_msg({"method": "setLayoutGravity", "params": {
                "aid": view.a.aid, "id": view.id, "align": value,
            }})
        else:
            raise ValueError(f"unsupported control property: {name}")


def _with_spacing(children, gap, vertical, justify):
    spaced = []

    def flex_space():
        return {"type": "frame", "width": 0 if not vertical else "MATCH_PARENT",
                "height": "MATCH_PARENT" if not vertical else 0, "weight": 1}

    if justify in ("center", "end", "space_evenly"):
        spaced.append(flex_space())
    for index, child in enumerate(children):
        if index:
            if justify in ("space_between", "space_evenly"):
                spaced.append(flex_space())
            else:
                spacer = {"type": "frame", "width": 0, "height": 0}
                spacer["height" if vertical else "width"] = gap
                spaced.append(spacer)
        spaced.append(child)
    if justify in ("center", "space_evenly"):
        spaced.append(flex_space())
    return spaced


def _flatten(node, parent_index, parent_id, styles, specifications, keys, padding,
             callbacks, long_clicks, text_events, selection_events,
             progress_events, lists, states):
    if isinstance(node, (list, tuple)):
        for child in node:
            _flatten(child, parent_index, parent_id, styles, specifications, keys,
                     padding, callbacks, long_clicks, text_events, selection_events,
                     progress_events, lists, states)
        return
    if not isinstance(node, dict):
        raise TypeError("layout nodes must be dictionaries")

    node = dict(node)
    children = node.pop("children", ())
    style_names = node.pop("style", ())
    if isinstance(style_names, str):
        style_names = (style_names,)
    merged = {}
    for style_name in style_names:
        try:
            merged.update(styles[style_name])
        except KeyError as error:
            raise ValueError(f"unknown layout style: {style_name}") from error
    merged.update(node)
    node = merged

    key = node.get("key")
    if key is not None:
        if key in keys:
            raise ValueError(f"duplicate layout key: {key}")
        keys[key] = len(specifications)

    on_click = node.pop("on_click", None)
    on_long_click = node.pop("on_long_click", None)
    on_text = node.pop("on_text", None)
    on_item_selected = node.pop("on_item_selected", None)
    on_progress = node.pop("on_progress", None)
    state_styles = node.pop("states", None)
    initial_state = node.pop("state", None)
    list_options = None
    if node.get("type") == "list":
        list_options = {
            "items": node.pop("items", []),
            "keep_scroll": node.pop("keep_scroll", False),
            "click": node.pop("click", on_click is not None),
            "longclick": node.pop("longclick", on_long_click is not None),
        }
    if on_click is not None:
        if not callable(on_click):
            raise TypeError("on_click must be callable")
        node["clickable"] = True
    if list_options is not None:
        node.pop("clickable", None)
    if on_long_click is not None and not callable(on_long_click):
        raise TypeError("on_long_click must be callable")
    if on_text is not None and not callable(on_text):
        raise TypeError("on_text must be callable")
    if on_item_selected is not None and not callable(on_item_selected):
        raise TypeError("on_item_selected must be callable")
    if on_progress is not None and not callable(on_progress):
        raise TypeError("on_progress must be callable")
    if on_item_selected is not None and node.get("type") != "spinner":
        raise ValueError("on_item_selected is only valid for spinner views")
    if on_progress is not None and node.get("type") != "slider":
        raise ValueError("on_progress is only valid for slider views")

    view_type = node.get("type")
    if view_type in ("switch", "checkbox"):
        node["clickable"] = True
    elif view_type == "spinner":
        options = node.get("options", ())
        selected = node.get("selected", 0)
        if not isinstance(options, (list, tuple)) or any(
                not isinstance(option, str) for option in options):
            raise TypeError("spinner options must be a list of strings")
        if not isinstance(selected, int) or selected < 0 or (options and selected >= len(options)):
            raise ValueError("spinner selection is outside its options")
    elif view_type == "slider":
        maximum = node.get("max", 100)
        progress = node.get("progress", 0)
        if not isinstance(maximum, int) or maximum < 0:
            raise ValueError("slider max must be a non-negative integer")
        if not isinstance(progress, int) or not 0 <= progress <= maximum:
            raise ValueError("slider progress must be between zero and max")
    elif view_type == "edit":
        node.setdefault("singleline", True)
        node.setdefault("line", False)
        node.setdefault("blockinput", False)
        node.setdefault("inputtype", "text")
        if node["inputtype"] not in _INPUT_TYPES:
            raise ValueError(f"unsupported edit input type: {node['inputtype']}")
    elif view_type in ("scroll", "horizontal_scroll"):
        node.setdefault("fillViewport", True)
        node.setdefault("nobar", False)
        if len(children) > 1:
            raise ValueError("scroll views can contain only one child")
    if view_type in ("row", "column"):
        vertical = view_type == "column"
        node["type"] = "linear"
        node["vertical"] = vertical
        gap = node.pop("gap", 0)
        justify = node.pop("justify", "start")
        justify = justify.replace("-", "_")
        if justify not in ("start", "center", "end", "space_between", "space_evenly"):
            raise ValueError(f"unsupported layout justification: {justify}")
        align_items = node.pop("align_items", None)
        if align_items is not None:
            if align_items not in _LAYOUT_ALIGNMENTS:
                raise ValueError(f"unsupported child alignment: {align_items}")
            children = [dict(child, align=align_items)
                        if isinstance(child, dict) else child for child in children]
        if gap or justify != "start":
            children = _with_spacing(children, gap, vertical, justify)
    elif view_type == "stack":
        node["type"] = "frame"
    elif view_type == "button":
        node["type"] = "text"
        node.setdefault("clickable", True)
        node.setdefault("gravity", "center")
    elif view_type == "space":
        node["type"] = "frame"
    elif view_type == "list":
        if children:
            raise ValueError("virtualized lists cannot contain layout children")
        node["type"] = "frame"
    elif view_type in ("fill", "wrap"):
        raise ValueError("fill and wrap are dimensions; use them for width or height")
    elif view_type not in _VIEW_CLASSES:
        raise ValueError(f"unsupported layout view type: {view_type}")
    if children and node["type"] not in ("frame", "linear", "scroll", "horizontal_scroll"):
        raise ValueError(f"{view_type} views cannot contain layout children")

    if "align" in node and node["align"] not in _LAYOUT_ALIGNMENTS:
        raise ValueError(f"unsupported layout alignment: {node['align']}")

    if node["type"] == "text" and "gravity" in node:
        if node["gravity"] not in _TEXT_GRAVITY:
            raise ValueError(f"unsupported text gravity: {node['gravity']}")

    if "flex" in node:
        node["weight"] = node.pop("flex")
    for dimension in ("width", "height"):
        if node.get(dimension) == "fill":
            node[dimension] = "MATCH_PARENT"
        elif node.get(dimension) == "wrap":
            node[dimension] = "WRAP_CONTENT"

    if "padding" in node:
        value = node.pop("padding")
        if not isinstance(value, (int, dict)):
            raise TypeError("padding must be an integer or direction map")
        padding.append((len(specifications), value))
    scale_type = node.pop("scaleType", None)
    node.pop("style", None)
    node.pop("children", None)
    if parent_index is not None:
        node["parentIndex"] = parent_index
    elif parent_id is not None:
        node["parent"] = parent_id
    image = node.get("image")
    if isinstance(image, bytes):
        node["image"] = base64.b64encode(image).decode("ascii")

    index = len(specifications)
    if state_styles is not None:
        resolved_states = {}
        for state_name, state_style in state_styles.items():
            if isinstance(state_style, str):
                try:
                    state_style = styles[state_style]
                except KeyError as error:
                    raise ValueError(f"unknown layout style: {state_style}") from error
            resolved_states[state_name] = dict(state_style)
        if initial_state is not None and initial_state not in resolved_states:
            raise ValueError(f"unknown initial layout state: {initial_state}")
        states.append((index, key, resolved_states, initial_state))
    if list_options is not None:
        if parent_index is None:
            position = sum(1 for item in specifications if item.get("parent") == parent_id)
        else:
            position = sum(1 for item in specifications
                           if item.get("parentIndex") == parent_index)
        lists.append((index, parent_index, position, node, list_options,
                      list_options["click"], list_options["longclick"]))
    if on_click is not None:
        callbacks.append((index, on_click))
    if on_long_click is not None:
        long_clicks.append((index, on_long_click))
    if on_text is not None:
        text_events.append((index, on_text))
    if on_item_selected is not None:
        selection_events.append((index, on_item_selected))
    if on_progress is not None:
        progress_events.append((index, on_progress))
    specifications.append(node)
    if scale_type is not None:
        padding.append((index, {"scaleType": scale_type}))
    _flatten(children, index, parent_id, styles, specifications, keys, padding,
             callbacks, long_clicks, text_events, selection_events,
             progress_events, lists, states)


def _apply_padding(activity, view_id, padding):
    if "scaleType" in padding:
        activity.c.send_msg({"method": "setImageScaleType", "params": {
            "aid": activity.aid, "id": view_id, "scaleType": padding["scaleType"],
        }})
        return
    _set_padding(activity, tg.View(activity, view_id), padding)


def _apply_view_properties(activity, view_id, properties):
    for name, value in properties.items():
        if name == "padding":
            _set_padding(activity, tg.View(activity, view_id), value)
        elif name == "scaleType":
            activity.c.send_msg({"method": "setImageScaleType", "params": {
                "aid": activity.aid, "id": view_id, "scaleType": value,
            }})


def _compile_layout(layout, styles, parent_id):
    specifications = []
    keys = {}
    padding = []
    callbacks = []
    long_clicks = []
    text_events = []
    selection_events = []
    progress_events = []
    lists = []
    states = []
    _flatten(layout, None, parent_id, styles or {}, specifications, keys, padding,
             callbacks, long_clicks, text_events, selection_events,
             progress_events, lists, states)
    return (specifications, keys, padding, callbacks, long_clicks, text_events,
            selection_events, progress_events, lists, states)


class LayoutTemplate:
    def __init__(self, activity, name, layout, styles=None):
        from shell.reusable_object import ReusableObject

        (specifications, self._keys, self._padding, callbacks, long_clicks,
         text_events, selection_events, progress_events, lists,
         self._states) = _compile_layout(layout, styles, None)
        if lists:
            raise ValueError("layout templates cannot contain virtualized lists")

        self.activity = activity
        self._callbacks = callbacks
        self._long_clicks = long_clicks
        self._text_events = text_events
        self._selection_events = selection_events
        self._progress_events = progress_events
        self._view_names = []
        reserved = set(self._keys)
        for index, specification in enumerate(specifications):
            key = f"__nexa_view_{index}"
            while key in reserved:
                key = f"_{key}"
            reserved.add(key)
            self._view_names.append(key)
            specification["key"] = key
        self._template = ReusableObject(activity, name, specifications)

    def create(self, parent, properties=None):
        translated = {}
        instance_properties = {}
        for key, values in (properties or {}).items():
            if key not in self._keys:
                raise KeyError(f"unknown layout template key: {key}")
            index = self._keys[key]
            values = dict(values)
            dimensions = values.pop("dimensions", None)
            if dimensions is not None:
                values["width"], values["height"] = dimensions
            local = {name: values.pop(name) for name in tuple(values)
                     if name in ("padding", "scaleType")}
            translated[self._view_names[index]] = values
            if local:
                instance_properties[index] = local
        created = self._template.create(parent, translated)
        views = {key: created[self._view_names[index]] for key, index in self._keys.items()}
        all_views = [created[name] for name in self._view_names]
        ids = [created[name].id for name in self._view_names]
        click_handlers = {}
        long_click_handlers = {}
        text_handlers = {}
        selection_handlers = {}
        progress_handlers = {}
        for index, callback in self._callbacks:
            click_handlers[ids[index]] = callback
        for index, callback in self._long_clicks:
            long_click_handlers[ids[index]] = callback
            tg.View(self.activity, ids[index]).sendlongclickevent(True)
        for index, callback in self._text_events:
            text_handlers[ids[index]] = callback
            tg.View(self.activity, ids[index]).sendtextevent(True)
        for index, callback in self._selection_events:
            selection_handlers[ids[index]] = callback
        for index, callback in self._progress_events:
            progress_handlers[ids[index]] = callback
        for index, value in self._padding:
            _apply_padding(self.activity, ids[index], value)
        for index, values in instance_properties.items():
            _apply_view_properties(self.activity, ids[index], values)
        states = {key: values for _index, key, values, _initial in self._states
                  if key is not None}
        result = Layout(self.activity, views, click_handlers, long_click_handlers,
                        text_handlers, selection_handlers, progress_handlers, states,
                        all_views)
        for _index, key, _values, initial in self._states:
            if key is not None and initial is not None:
                result.set_state(key, initial)
        return result


def create_layout(activity, parent, layout, styles=None):
    """Build a keyed tree of native views with one createViewBatch request."""
    (specifications, keys, padding, callbacks, long_clicks, text_events,
     selection_events, progress_events, lists, states) = _compile_layout(
         layout, styles, parent.id)
    for _index, parent_index, _position, _specification, _options, _click, _longclick in lists:
        if parent_index is None:
            valid_parent = isinstance(parent, tg.LinearLayout)
        else:
            valid_parent = specifications[parent_index]["type"] == "linear"
        if not valid_parent:
            raise ValueError("virtualized lists need a row or column parent")
    ids = activity.c.send_read_msg({
        "method": "createViewBatch",
        "params": {"aid": activity.aid, "views": specifications},
    })

    all_views = []
    for index, specification in enumerate(specifications):
        view_class = _VIEW_CLASSES.get(specification["type"], tg.View)
        view = view_class.__new__(view_class)
        tg.View.__init__(view, activity, ids[index])
        all_views.append(view)
    views = {key: all_views[index] for key, index in keys.items()}

    if lists:
        from shell.recycler_view import RecyclerListView

        for index, parent_index, position, specification, options, click, longclick in reversed(lists):
            list_parent = parent if parent_index is None else tg.LinearLayout.__new__(tg.LinearLayout)
            if parent_index is not None:
                tg.View.__init__(list_parent, activity, ids[parent_index])
            view = RecyclerListView(activity, list_parent, click=click, longclick=longclick)
            view.setdimensions(specification.get("width", tg.View.WRAP_CONTENT),
                               specification.get("height", tg.View.WRAP_CONTENT))
            if "weight" in specification:
                view.setlinearlayoutparams(specification["weight"], position)
            else:
                view.setlinearlayoutparams(0, position)
            for direction, amount in specification.get("margins", {}).items():
                view.setmargin(amount, direction)
            if "background" in specification:
                view.setbackgroundcolor(specification["background"])
            if "visibility" in specification:
                view.setvisibility(specification["visibility"])
            if options["items"]:
                view.setitems(options["items"], keep_scroll=options["keep_scroll"])
            if specification.get("key") is not None:
                views[specification["key"]] = view
            all_views[index] = view
            placeholder_id = ids[index]
            ids[index] = view.id
            tg.View(activity, placeholder_id).delete()
    click_handlers = {}
    long_click_handlers = {}
    list_indices = {item[0] for item in lists}
    for index, callback in callbacks:
        click_handlers[ids[index]] = callback
    for index, callback in long_clicks:
        long_click_handlers[ids[index]] = callback
        if index not in list_indices:
            tg.View(activity, ids[index]).sendlongclickevent(True)
    text_handlers = {ids[index]: callback for index, callback in text_events}
    for index, _callback in text_events:
        tg.View(activity, ids[index]).sendtextevent(True)
    selection_handlers = {ids[index]: callback for index, callback in selection_events}
    progress_handlers = {ids[index]: callback for index, callback in progress_events}
    for index, value in padding:
        _apply_padding(activity, ids[index], value)
    states_by_key = {key: state_values for _index, key, state_values, _initial in states
                     if key is not None}
    result = Layout(activity, views, click_handlers, long_click_handlers,
                    text_handlers, selection_handlers, progress_handlers, states_by_key,
                    all_views)
    for _index, key, _state_values, initial_state in states:
        if key is not None and initial_state is not None:
            result.set_state(key, initial_state)
    return result
