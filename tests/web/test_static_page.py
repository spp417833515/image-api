"""Static checks on the page (src/image_api/web/static).

The page has no build step, so a broken import or a missing file only shows up in a browser; and the page must not grow
its own copy of backend rules, transports, colours or demo leftovers. These checks catch that without a browser.
Behaviour against contract-shaped responses is checked with a real browser in line 1's self-check; JS syntax is the gate's
js-syntax suite. Anything these checks cannot parse is a failure, never a silent skip.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jsscan import code_only, line_of  # sibling helper: pytest puts this test directory on sys.path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src/image_api/web/static"
JS_DIR = STATIC / "js"
JS = sorted(STATIC.rglob("*.js"))
CSS = sorted(STATIC.rglob("*.css"))
THEME = STATIC / "css/theme.css"


CODE = {p: code_only(p.read_text(encoding="utf-8"), p.name) for p in JS}


# ---------------------------------------------------------------- files and imports
def test_index_references_existing_files() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"""(?:href|src)=["'](/static/[^"']+)["']""", html)
    assert "/static/js/main.js" in refs, refs
    missing = [r for r in refs if not (STATIC / r.removeprefix("/static/")).is_file()]
    assert not missing, f"index.html references files that do not exist: {missing}"
    others = re.findall(r"""(?:href|src)=["']((?!/static/|data:,)[^"']+)["']""", html)
    assert not others, f"index.html may only load /static/ files (no CDN, no other origin): {others}"


STATEMENT = re.compile(r"(?m)^[ \t]*(import|export)\b")
FROM_CLAUSE = re.compile(
    r"""(import|export)\s+(?P<clause>[^;'"`]*?)\s*from\s*(?P<q>['"])(?P<spec>[^'"]+)(?P=q)\s*;""", re.S
)
BARE_IMPORT = re.compile(r"""import\s*(?P<q>['"])(?P<spec>[^'"]+)(?P=q)\s*;""")
EXPORT_DECL = re.compile(r"(?m)^[ \t]*export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([\w$]+)")
EXPORT_LIST = re.compile(r"(?m)^[ \t]*export\s*\{([^}]*)\}\s*;")


def _exports(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    names = set(EXPORT_DECL.findall(src))
    for group in EXPORT_LIST.findall(src):
        names |= {part.split(" as ")[-1].strip() for part in group.split(",") if part.strip()}
    if re.search(r"(?m)^[ \t]*export\s+default\b", src):
        names.add("default")
    return names


def _resolve(spec: str, importer: Path) -> Path | None:
    if spec.startswith("/static/"):
        return STATIC / spec.removeprefix("/static/")
    if spec.startswith(("./", "../")):
        return (importer.parent / spec).resolve()
    return None


def _module_imports(path: Path) -> list[tuple[int, str, str | None]]:
    """(line, clause, spec) per import/export-from statement; an import we cannot parse fails the test."""
    src = path.read_text(encoding="utf-8")
    found = []
    for m in STATEMENT.finditer(CODE[path]):
        start = m.start(1)
        bare = BARE_IMPORT.match(src, start)
        full = FROM_CLAUSE.match(src, start)
        if bare:
            found.append((line_of(src, start), "", bare["spec"]))
        elif full:
            found.append((line_of(src, start), full["clause"], full["spec"]))
        elif m[1] == "import":
            pytest.fail(
                f"{path.name}:{line_of(src, start)}: cannot parse this import statement; write it as import {{ ... }} from './x.js';"
            )
    return found


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_imports_resolve_to_exported_names(path: Path) -> None:
    code = CODE[path]
    assert not re.search(r"\bimport\s*\(", code), (
        f"{path.name}: dynamic import() is not used by the page; load modules statically"
    )
    assert not re.search(r"\brequire\s*\(", code), f"{path.name}: require() is not available to native ES modules"
    problems = []
    for line, clause, spec in _module_imports(path):
        target = _resolve(spec, path)
        if target is None:
            problems.append(f"line {line}: '{spec}' is not a local module (no third-party libraries, no CDN)")
            continue
        if not target.is_file():
            problems.append(f"line {line}: '{spec}' does not exist")
            continue
        exported = _exports(target)
        clause = " ".join(clause.split())
        if clause.startswith("{") or clause.startswith("*") or clause == "":
            default, rest = None, clause
        else:
            default, _, rest = clause.partition(",")
            default, rest = default.strip(), rest.strip()
        if default:
            problems += (
                []
                if "default" in exported
                else [f"line {line}: default import from '{spec}', which has no export default"]
            )
        ns = re.fullmatch(r"\*\s+as\s+([\w$]+)", rest)
        if ns:
            used = set(re.findall(rf"(?<![\w$.]){re.escape(ns[1])}\.([\w$]+)", code))
            problems += [f"line {line}: {ns[1]}.{u} is not exported by '{spec}'" for u in sorted(used - exported)]
        elif rest.startswith("{") and rest.endswith("}"):
            names = [part.split(" as ")[0].strip() for part in rest[1:-1].split(",") if part.strip()]
            problems += [f"line {line}: '{nm}' is not exported by '{spec}'" for nm in names if nm not in exported]
        elif rest:
            problems.append(f"line {line}: cannot read the import clause '{clause}'")
    assert not problems, f"{path.name}: " + "; ".join(problems)


# ---------------------------------------------------------------- one transport, in one place
NETWORK = re.compile(r"(?<![\w$])(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)(?![\w$])")


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_only_api_module_talks_to_the_network(path: Path) -> None:
    code = CODE[path]
    hits = [f"{path.name}:{line_of(code, m.start())}: {m[1]}" for m in NETWORK.finditer(code)]
    if path == JS_DIR / "api.js":
        hits = [h for h in hits if not h.endswith(": fetch")]
        assert re.search(r"(?<![\w$.])fetch\s*\(", code), "api.js is the page's only fetch caller; it must call fetch"
    assert not hits, f"only js/api.js may talk to the backend, and only with fetch: {hits}"


# ---------------------------------------------------------------- no backend rules in the page
RULE_NUMBERS = {
    4,
    32,
    256,
    1024,
    1920,
    9007199254740991,
}  # max images, size multiple, size range, default size, seed range
NUMBER = re.compile(r"(?<![\w$.])(\d+(?:\.\d+)?(?:e\d+)?)(?![\w$.])")


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_no_route_rejection_or_size_rules(path: Path) -> None:
    """Limits come from /api/status, routes and rejections from /api/plan, output sizes from the plan; the page computes none."""
    code = CODE[path]
    hits = [
        f"line {line_of(code, m.start())}: literal {m[1]}" for m in NUMBER.finditer(code) if float(m[1]) in RULE_NUMBERS
    ]
    patterns = {
        r"\.length\s*(?:[<>]=?|[!=]==?)\s*(?:[2-9]|\d\d+)": "comparing a count with a limit",
        r"(?:[2-9]|\d\d+)\s*(?:[<>]=?|[!=]==?)\s*[\w$.]+\.length\b": "comparing a count with a limit",
        r"\bMath\.round\s*\(": "rounding (sizes are rounded by the backend)",
        r"%\s*\d": "modulo by a number",
        r"\bMAX_SAFE_INTEGER\b|\*\*\s*53\b|\bpow\s*\(\s*2\s*,\s*53": "the seed range",
    }
    for pat, why in patterns.items():
        hits += [f"line {line_of(code, m.start())}: {why}: {m[0]!r}" for m in re.finditer(pat, code)]
    assert not hits, (
        f"{path.name}: backend rules belong in imagegen.biz; the page reads /api/status and /api/plan: {hits}"
    )


# ---------------------------------------------------------------- demo leftovers, built-in images
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico", ".avif", ".bmp", ".apng", ".tif", ".tiff"}


def test_no_demo_leftovers_or_built_in_images() -> None:
    """The template's demo state bar, hash switching, MOCK data, backend stand-in and sample images must not ship."""
    files = [p for p in STATIC.rglob("*") if p.is_file()]
    images = [p.name for p in files if p.suffix.lower() in IMAGE_SUFFIXES]
    assert not images, f"the real page has no built-in images: {images}"
    unknown = [p.name for p in files if p.suffix not in {".html", ".css", ".js"}]
    assert not unknown, f"unexpected files under static/: {unknown}"
    banned = ["MOCK", "演示", "后端替身", "hashchange", "location.hash", "assets/", "data:image"]
    hits = [f"{p.relative_to(STATIC)}: {w}" for p in files for w in banned if w in p.read_text(encoding="utf-8")]
    assert not hits, hits


# ---------------------------------------------------------------- theme: colours, fonts, spacing, radii only in theme.css
NAMED_COLOURS = set(
    re.findall(
        r"[a-z]+",
        """aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood cadetblue
    chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey
    darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray
    darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen
    fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen
    lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime
    limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
    mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive
    olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
    powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue
    slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke
    yellow yellowgreen""",
    )
)
COLOUR_FN = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(", re.I)
DECL = re.compile(r"([-a-zA-Z]+)\s*:\s*([^;{}]+)")
SPACING = re.compile(r"^(?:padding|margin)(?:-[a-z]+)*$|^(?:row-|column-)?gap$|^border(?:-[a-z]+)*-radius$")
FONT = re.compile(r"^font(?:-family|-size)?$")
RAW_LENGTH = re.compile(r"(?<![\w.])-?\d*\.?\d+(?:px|em|rem|pt|vh|vw|ch|%)")  # also negative: margin-top:-6px


def _declarations(path: Path) -> list[tuple[int, str, str]]:
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m[0]), path.read_text(encoding="utf-8"), flags=re.S)
    out = []
    for block in re.finditer(r"\{([^{}]*)\}", text):
        for d in DECL.finditer(block[1]):
            out.append((line_of(text, block.start(1) + d.start()), d[1].lower(), d[2].strip()))
    return out


@pytest.mark.parametrize("path", [p for p in CSS if p != THEME], ids=lambda p: p.name)
def test_css_values_come_from_theme(path: Path) -> None:
    hits = []
    for line, prop, value in _declarations(path):
        if COLOUR_FN.search(value) or {w.lower() for w in re.findall(r"[a-zA-Z]+", value)} & NAMED_COLOURS:
            hits.append(f"line {line}: colour in {prop}: {value}")
        bare = re.sub(r"var\([^()]*\)", "", value)
        if SPACING.match(prop) and [
            x for x in RAW_LENGTH.findall(bare) if not (prop.endswith("radius") and x == "50%")
        ]:
            hits.append(f"line {line}: raw spacing/radius in {prop}: {value}")
        if FONT.match(prop) and bare.strip() not in ("", "inherit"):
            hits.append(f"line {line}: raw font value in {prop}: {value}")
    assert not hits, f"{path.name}: colours, fonts, spacing and radii come from css/theme.css variables: {hits}"


THEME_STYLE_PROPS = (
    r"color|background(?:Color)?|borderColor|outlineColor|fill|stroke|padding\w*|margin\w*|gap|rowGap|columnGap"
    r"|font|fontSize|fontFamily|borderRadius|border\w*Radius|cssText"
)
INLINE_THEME = re.compile(
    r"(?<![-\w])(?:color|background(?:-color)?|border(?:-[a-z]+)*-color|padding(?:-[a-z]+)*|margin(?:-[a-z]+)*"
    r"|(?:row-|column-)?gap|font(?:-size|-family)?|border(?:-[a-z]+)*-radius)\s*:"
)


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_js_sets_no_theme_values(path: Path) -> None:
    """Colours, fonts, spacing and radii come from css/theme.css; JS may only read them (cssVar) or set layout sizes."""
    src, code = path.read_text(encoding="utf-8"), CODE[path]
    found = [(m.start(), m[0]) for m in COLOUR_FN.finditer(src)]
    found += [(m.start(), m[0]) for m in re.finditer(r"(?:fillStyle|strokeStyle)\s*=\s*['\"`]", src)]
    found += [
        (m.start(), m[0])
        for m in re.finditer(r"(?:color|background|fill|stroke)\s*:\s*([a-zA-Z]+)", src)
        if m[1].lower() in NAMED_COLOURS
    ]
    found += [(m.start(), m[0]) for m in re.finditer(rf"\.style\.(?:{THEME_STYLE_PROPS})\s*=(?!=)", code)]
    found += [(m.start(), m[0]) for m in re.finditer(r"\.style\.setProperty\s*\(\s*(?!['\"`]--)", src)]
    # bracket writes and Object.assign hide the property name; the page only sets layout sizes with el.style.<prop>
    found += [(m.start(), m[0]) for m in re.finditer(r"\.style\s*\[[^\]]*\]\s*=(?!=)", src)]
    found += [(m.start(), m[0]) for m in re.finditer(r"\bObject\.assign\s*\(\s*[\w$.]+\.style\b", code)]
    for attr in re.finditer(r"""style=(["'])(.*?)\1""", src):
        found += [(attr.start(), f"style={attr[1]}{attr[2]}{attr[1]}") for _ in INLINE_THEME.finditer(attr[2])]
    hits = [f"line {line_of(src, pos)}: {what}" for pos, what in found]
    assert not hits, (
        f"{path.name}: colours, fonts, spacing and radii come from css/theme.css (read them with cssVar): {hits}"
    )


def test_scanner_blanks_strings_and_keeps_template_code() -> None:
    src = "const a = 'fetch(4)'; const b = `x ${ f(32) } y`; const r = /4/g; // fetch 1920\n/* 256 */ c(1);"
    code = code_only(src)
    assert "fetch" not in code and "1920" not in code and "256" not in code and "(4)" not in code
    assert "f(32)" in code and "c(1)" in code and len(code.splitlines()) == len(src.splitlines())
