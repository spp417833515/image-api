"""A small JS scanner for the page's static checks: comments removed, string / template-text / regex-literal contents blanked.

Template ``${...}`` code is kept and every character keeps its offset (blanked to spaces, newlines kept), so positions and
line numbers found in the result are positions in the source. Anything it cannot scan (an unterminated string, comment,
template or regex) raises ValueError: the checks fail instead of silently skipping a file.
"""

from __future__ import annotations

import re


def code_only(src: str, name: str = "?") -> str:
    """Comments removed; string, template-text and regex-literal contents blanked (``${...}`` code kept). Lines are kept."""
    out: list[str] = []
    i, n = 0, len(src)
    stack: list[int] = []  # brace depth of each open template ${...}
    prev = ""  # last significant code character, to tell a regex literal from a division

    def blank(text: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in text)

    def template_text(j: int) -> int:
        """Scan template text from j (just after ` or }); return the index after the closing ` or after ${."""
        while j < n:
            ch = src[j]
            if ch == "\\":
                out.append(blank(src[j : j + 2]))
                j += 2
            elif ch == "`":
                out.append("`")
                return j + 1
            elif src.startswith("${", j):
                out.append("${")
                stack.append(0)
                return j + 2
            else:
                out.append(blank(ch))
                j += 1
        raise ValueError(f"{name}: unterminated template literal")

    while i < n:
        ch = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(blank(src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError(f"{name}: unterminated block comment")
            out.append(blank(src[i : j + 2]))
            i = j + 2
        elif ch in "'\"":
            j = i + 1
            while j < n and src[j] != ch:
                if src[j] == "\n":
                    raise ValueError(f"{name}: unterminated string at offset {i}")
                j += 2 if src[j] == "\\" else 1
            out.append(ch + blank(src[i + 1 : j]) + ch)
            i, prev = j + 1, "a"
        elif ch == "`":
            out.append("`")
            i, prev = template_text(i + 1), "a"
        elif ch == "/" and (
            prev == ""
            or prev in "(,=:[!&|?{};+-*%<>~^"
            or re.search(r"\b(return|typeof|case|in|of)\s*$", "".join(out[-12:])[-12:])
        ):
            j, cls = i + 1, False
            while j < n and (src[j] != "/" or cls):
                if src[j] == "\n":
                    raise ValueError(f"{name}: unterminated regex literal at offset {i}")
                cls = (cls or src[j] == "[") and src[j] != "]"
                j += 2 if src[j] == "\\" else 1
            k = j + 1
            while k < n and src[k].isalpha():
                k += 1
            out.append("/" + blank(src[i + 1 : j]) + "/" + src[j + 1 : k])
            i, prev = k, "a"
        elif ch == "{":
            if stack:
                stack[-1] += 1
            out.append(ch)
            i, prev = i + 1, ch
        elif ch == "}" and stack and stack[-1] == 0:
            stack.pop()
            out.append("}")
            i, prev = template_text(i + 1), "a"
        else:
            if ch == "}" and stack:
                stack[-1] -= 1
            out.append(ch)
            if not ch.isspace():
                prev = ch
            i += 1
    if stack:
        raise ValueError(f"{name}: unterminated ${{...}} in a template literal")
    return "".join(out)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1
