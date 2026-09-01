"""
JSON-with-comments support.

freqtrade's config.json uses rapidjson's permissive mode, which allows
`// line comments` — plain json.load() rejects them.
"""

import json


def strip_json_comments(text: str) -> str:
    """Strip `// line comments` outside string literals. String-aware so a
    "//" inside a quoted value (e.g. a URL) is left untouched."""
    out = []
    in_string = False
    escape = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                break
            out.append("\n")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_json_with_comments(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.loads(strip_json_comments(fh.read()))
