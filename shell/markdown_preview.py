import html
import re

from shell import palette


_FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_ULIST = re.compile(r"^\s*[-+*]\s+(.+)$")
_OLIST = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def _inline(text):
    value = html.escape(text, quote=False)
    saved = []

    def hold(markup):
        saved.append(markup)
        return f"\x00{len(saved) - 1}\x00"

    value = re.sub(r"`([^`\n]+)`", lambda match: hold(
        f"<code>{html.escape(match.group(1), quote=False)}</code>"), value)

    def link(match):
        label = match.group(1)
        if match.group(0).startswith("!"):
            return hold(f'<span class="image-label">[image: {label}]</span>')
        return hold(f'<span class="link-label">{label}</span>')

    value = re.sub(r"!?\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", link, value)
    value = re.sub(r"\*\*(.+?)\*\*|__(.+?)__",
                   lambda match: f"<strong>{match.group(1) or match.group(2)}</strong>", value)
    value = re.sub(r"~~(.+?)~~", r"<del>\1</del>", value)
    value = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*|(?<!_)_(?!\s)(.+?)(?<!\s)_",
                   lambda match: f"<em>{match.group(1) or match.group(2)}</em>", value)
    for index, markup in enumerate(saved):
        value = value.replace(f"\x00{index}\x00", markup)
    return value


def _is_rule(line):
    return re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line) is not None


def _is_block_start(lines, index):
    line = lines[index]
    return (not line.strip() or _FENCE.match(line) or _HEADING.match(line)
            or _is_rule(line) or line.lstrip().startswith(">")
            or _ULIST.match(line) or _OLIST.match(line))


def render(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            marker, language = fence.group(1), fence.group(2).strip()
            code = []
            index += 1
            while index < len(lines) and not re.match(
                    rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[index]):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            lang_class = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            blocks.append(f"<pre><code{lang_class}>{html.escape(chr(10).join(code))}</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _is_rule(line):
            blocks.append("<hr>")
            index += 1
            continue

        if line.lstrip().startswith(">"):
            quoted = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append(f"<blockquote><p>{_inline(' '.join(quoted))}</p></blockquote>")
            continue

        unordered = _ULIST.match(line)
        ordered = _OLIST.match(line)
        if unordered or ordered:
            tag = "ul" if unordered else "ol"
            entries = []
            while index < len(lines):
                item = _ULIST.match(lines[index]) if tag == "ul" else _OLIST.match(lines[index])
                if not item:
                    break
                entries.append(f"<li>{_inline(item.group(1))}</li>")
                index += 1
            blocks.append(f"<{tag}>{''.join(entries)}</{tag}>")
            continue

        paragraph = [line.strip()]
        index += 1
        while index < len(lines) and not _is_block_start(lines, index):
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")

    body = "\n".join(blocks) or '<p class="empty">nothing to preview</p>'
    background = f"#{palette.window_content & 0xFFFFFF:06x}"
    text = f"#{palette.text & 0xFFFFFF:06x}"
    accent = f"#{palette.accent & 0xFFFFFF:06x}"
    muted = f"#{palette.muted & 0xFFFFFF:06x}"
    surface = f"#{palette.surface & 0xFFFFFF:06x}"
    surface_light = f"#{palette.surface_light & 0xFFFFFF:06x}"
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
html, body {{ background: {background}; }}
body {{ margin: 0; padding: 20px 22px 36px; color: {text};
  font: 15px/1.65 sans-serif; overflow-wrap: anywhere; }}
h1,h2,h3,h4,h5,h6 {{ color: {text}; line-height: 1.3; margin: 1.2em 0 .55em; }}
h1 {{ font-size: 2em; border-bottom: 1px solid {surface_light}; padding-bottom: .3em; }}
h2 {{ font-size: 1.55em; border-bottom: 1px solid {surface_light}; padding-bottom: .25em; }}
p {{ margin: .75em 0; }}
.link-label {{ color: {accent}; text-decoration: underline; }}
code {{ font: .92em monospace; color: {accent}; background: {surface}; padding: .12em .35em; border-radius: 4px; }}
pre {{ overflow-x: auto; background: {surface}; border: 1px solid {surface_light}; border-radius: 6px;
  padding: 14px; line-height: 1.5; }}
pre code {{ padding: 0; background: transparent; color: {text}; white-space: pre; }}
blockquote {{ margin: 1em 0; padding: .15em 1em; color: {muted}; border-left: 3px solid {accent}; }}
ul,ol {{ padding-left: 1.6em; }} li {{ padding-left: .2em; margin: .25em 0; }}
hr {{ border: 0; border-top: 1px solid {surface_light}; margin: 1.5em 0; }}
img {{ max-width: 100%; }} .image-label {{ color: {muted}; font-style: italic; }}
.empty {{ color: {muted}; }}
</style></head><body>{body}</body></html>'''
