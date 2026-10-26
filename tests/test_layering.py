"""Layering and structure guard: dependencies point downward only, the core package is reusable, and no giant files.

Layers (top to bottom):
  image_api (web / cli)  →  imagegen.biz  →  imagegen.logic
The composition root imagegen.factory is the only place that may wire a concrete backend (imagegen.logic.comfy) into the business layer.
The business layer depends only on the backend interface, which is how "plug in other image APIs later" is achieved.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "imagegen": ROOT / "packages/imagegen/src/imagegen",
    "image_api": ROOT / "src/image_api",
}
WEB_FRAMEWORKS = ("fastapi", "starlette", "uvicorn")
COMFY = "imagegen.logic.comfy"

# (module prefix, forbidden import prefixes, why)
RULES: list[tuple[str, tuple[str, ...], str]] = [
    (
        "imagegen.logic",
        ("imagegen.biz", "imagegen.factory", "image_api", *WEB_FRAMEWORKS),
        "the logic layer does not know who calls it",
    ),
    (
        "imagegen.biz",
        ("imagegen.factory", COMFY, "image_api", *WEB_FRAMEWORKS),
        "the business layer depends only on the backend interface, not on any concrete backend or web layer",
    ),
    (
        "imagegen",
        ("image_api", *WEB_FRAMEWORKS),
        "the core package must be importable by other projects, so it must not depend on the app or a web framework",
    ),
    (
        "image_api",
        (COMFY,),
        "the app gets its backend through imagegen.factory and does not reach into the concrete implementation",
    ),
]
# Every module outside imagegen.logic.comfy that imports a concrete backend must be the composition root.
COMFY_ALLOWED_IMPORTERS = (COMFY, "imagegen.factory")

MAX_LINES = 400
LINE_LIMITED_SUFFIXES = {".py", ".js", ".css", ".html"}


def _modules() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for top, base in SOURCES.items():
        for path in base.rglob("*.py"):
            rel = path.relative_to(base).with_suffix("")
            parts = [top, *rel.parts]
            if parts[-1] == "__init__":
                parts.pop()
            out[".".join(parts)] = path
    return out


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(module: str, path: Path) -> set[str]:
    """Absolute module names this module imports at runtime (excluding the TYPE_CHECKING block; relative imports resolved)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.If) and _is_type_checking(node):
                visit(node.orelse)
                continue
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")
                    base = base[: len(base) - (node.level - 1)]
                    target = ".".join([*base, node.module] if node.module else base)
                else:
                    target = node.module or ""
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    visit([child])

    visit(tree.body)
    return found


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


MODULES = _modules()


def test_sources_exist() -> None:
    assert MODULES, f"no source modules found: {SOURCES}"


@pytest.mark.parametrize("module", sorted(MODULES))
def test_layer_rules(module: str) -> None:
    imported = _imports(module, MODULES[module])
    problems = []
    for prefix, forbidden, why in RULES:
        if not _under(module, prefix):
            continue
        for name in sorted(imported):
            hit = next((f for f in forbidden if _under(name, f)), None)
            if hit:
                problems.append(f"{module} imports {name} (forbidden: {hit}; reason: {why})")
    if not any(_under(module, allowed) for allowed in COMFY_ALLOWED_IMPORTERS):
        problems += [
            f"{module} imports {n} (only {COMFY_ALLOWED_IMPORTERS} may import the concrete ComfyUI backend)"
            for n in sorted(imported)
            if _under(n, COMFY)
        ]
    assert not problems, "\n".join(problems)


def test_no_import_cycles() -> None:
    graph = {
        m: {t for t in MODULES if t != m and any(_under(n, t) and not _under(m, t) for n in _imports(m, p))}
        for m, p in MODULES.items()
    }
    # A package (__init__) re-exporting its submodules is not a cycle; only check edges between leaf modules.
    leaves = {m for m, p in MODULES.items() if p.name != "__init__.py"}
    edges = {m: {t for t in graph[m] if t in leaves} for m in leaves}
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def walk(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in edges[node]:
            if state.get(nxt) == 1:
                cycles.append([*stack[stack.index(nxt) :], nxt])
            elif nxt not in state:
                walk(nxt)
        stack.pop()
        state[node] = 2

    for m in sorted(leaves):
        if m not in state:
            walk(m)
    assert not cycles, "import cycles:\n" + "\n".join(" → ".join(c) for c in cycles)


def _line_limited_files() -> list[Path]:
    files = []
    for base in SOURCES.values():
        files += [p for p in base.rglob("*") if p.is_file() and p.suffix in LINE_LIMITED_SUFFIXES]
    return sorted(files)


@pytest.mark.parametrize("path", _line_limited_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_giant_files(path: Path) -> None:
    lines = len(path.read_text(encoding="utf-8").splitlines())
    assert lines <= MAX_LINES, (
        f"{path.relative_to(ROOT)} has {lines} lines (limit {MAX_LINES}); split it by responsibility"
    )
