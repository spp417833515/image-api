"""Collected-count floors: one source (floors.json). They may only be raised automatically, never lowered."""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).with_name("floors.json")


def load() -> dict[str, int]:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    return {k: int(v) for k, v in data.items() if not k.startswith("_")}


def raise_to(counts: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Raise floors to the collected counts from a passing run; return {suite: (old, new)}."""
    data = json.loads(PATH.read_text(encoding="utf-8"))
    changed = {}
    for name, n in counts.items():
        old = int(data.get(name, 0))
        if n > old:
            data[name] = n
            changed[name] = (old, n)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed
