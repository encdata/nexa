import re
import os


COLORS = {
    "comment": 0xFF6A9955,
    "string": 0xFFCE9178,
    "number": 0xFFB5CEA8,
    "keyword": 0xFFC586C0,
    "builtin": 0xFF4EC9B0,
    "function": 0xFFDCDCAA,
    "constant": 0xFF4FC1FF,
    "key": 0xFF9CDCFE,
}

LANGUAGES = {
    ".json": "json",
    ".lua": "lua",
    ".py": "python",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".rs": "rust",
}

KEYWORDS = {
    "python": set("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case".split()),
    "lua": set("and break do else elseif end false for function goto if in local nil not or repeat return then true until while".split()),
    "c": set("auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while bool _Bool".split()),
    "rust": set("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while".split()),
}

BUILTINS = {
    "python": set("abs all any bool dict enumerate filter float int isinstance len list map max min print range reversed set sorted str sum tuple type zip".split()),
    "lua": set("assert collectgarbage dofile error getmetatable ipairs load next pairs pcall print rawequal rawget rawset require select setmetatable tonumber tostring type xpcall".split()),
}

_CODE_TOKEN = re.compile(
    r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/|--[^\n]*|\#[^\n]*)"
    r"|(?P<string>[rRbBfFuU]{0,2}(?:\"\"\"[\s\S]*?\"\"\"|'{3}[\s\S]*?'{3}|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'))"
    r"|(?P<number>\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?[\d_]+)?)(?:[uUlLfF]*)\b)"
    r"|(?P<decorator>@[A-Za-z_]\w*)"
    r"|(?P<identifier>[A-Za-z_]\w*)"
)


def _language(path):
    return LANGUAGES.get(os.path.splitext(path.lower())[1])


def _utf16_offsets(text):
    offsets = [0]
    offset = 0
    for character in text:
        offset += 2 if ord(character) > 0xFFFF else 1
        offsets.append(offset)
    return offsets


def _json_spans(text):
    spans = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b', text):
        token = match.group()
        if token.startswith('"'):
            color = COLORS["key"] if re.match(r'\s*:', text[match.end():]) else COLORS["string"]
        elif token in ("true", "false", "null"):
            color = COLORS["keyword"]
        else:
            color = COLORS["number"]
        spans.append((match.start(), match.end(), color))
    return spans


def _code_spans(text, language):
    spans = []
    keywords = KEYWORDS[language]
    builtins = BUILTINS.get(language, set())
    for match in _CODE_TOKEN.finditer(text):
        kind = match.lastgroup
        token = match.group()
        if kind == "comment":
            if language == "python" and not token.startswith("#"):
                continue
            if language == "lua" and not token.startswith("--"):
                continue
            if language in ("c", "rust") and not token.startswith(("//", "/*")):
                continue
            color = COLORS["comment"]
        elif kind == "string":
            color = COLORS["string"]
        elif kind == "number":
            color = COLORS["number"]
        elif kind == "decorator":
            color = COLORS["function"]
        elif token in keywords:
            color = COLORS["keyword"]
        elif token in builtins:
            color = COLORS["builtin"]
        elif token.isupper() and any(character.isalpha() for character in token):
            color = COLORS["constant"]
        elif re.match(r"\s*\(", text[match.end():]):
            color = COLORS["function"]
        else:
            continue
        spans.append((match.start(), match.end(), color))
    return spans


def highlight(activity, view, path, text):
    language = _language(path)
    if language == "json":
        spans = _json_spans(text)
    elif language is not None:
        spans = _code_spans(text, language)
    else:
        spans = []

    offsets = _utf16_offsets(text)
    activity.c.send_msg({
        "method": "setSyntaxColors",
        "params": {
            "aid": activity.aid,
            "id": view.id,
            "spans": [{"start": offsets[start], "end": offsets[end], "color": color}
                      for start, end, color in spans],
        },
    })
