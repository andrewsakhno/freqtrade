"""
Shared Python source snippets embedded into remote (server-side) scripts.

Each remote script is a standalone text blob piped into `ssh ... bash -s` —
there is no shared Python runtime between them, so the snippet text itself
(not a module import) is what gets duplicated across scripts. Keeping the
snippet defined once here, and interpolating it, is the single source of
truth for that duplication.
"""

# freqtrade's config.json uses rapidjson's permissive mode, which allows
# `// line comments` — plain json.load() rejects them. This snippet defines
# strip_json_comments(text) and load_json_with_comments(path), string-aware
# (a "//" inside a quoted value, e.g. a URL, is left untouched).
STRIP_JSON_COMMENTS_PY = '''
def strip_json_comments(text):
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
            elif ch == "\\\\":
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
            j = text.find("\\n", i)
            if j == -1:
                break
            out.append("\\n")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_json_with_comments(path):
    with open(path, encoding="utf-8") as fh:
        return __import__("json").loads(strip_json_comments(fh.read()))
'''
