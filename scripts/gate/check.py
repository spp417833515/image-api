"""One gate run: compute the tree sha → hit the cache, or run every suite of this mode in order → write the report.

Cache key = tree sha + mode. The tree sha covers all tracked and non-ignored files, including scripts/gate itself, so a changed tool means a new key.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from . import floors, report
from .run import run_suite
from .suites import for_mode
from .tree import tree_sha


def check(root: Path, mode: str, use_cache: bool = True) -> dict:
    gate_dir = root / ".pp/gate"
    sha = tree_sha(root, gate_dir / "tmp")
    run_dir = gate_dir / "runs" / f"{sha}-{mode}"
    cached = run_dir / "report.json"
    if use_cache and cached.is_file():
        data = json.loads(cached.read_text(encoding="utf-8"))
        data["cached"] = True
        return data

    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    tmp = gate_dir / "tmp" / sha
    tmp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}

    started = time.monotonic()
    meta = {
        "mode": mode,
        "tree_sha": sha,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_dir": str(run_dir.relative_to(root)),
    }
    bounds = floors.load()
    results = [run_suite(s, root, env, run_dir, bounds.get(s.name, 0)) for s in for_mode(mode)]
    meta["seconds"] = round(time.monotonic() - started, 1)
    meta["tree_changed"] = tree_sha(root, gate_dir / "tmp") != sha
    data = report.write(run_dir, meta, results)
    shutil.rmtree(tmp, ignore_errors=True)
    if meta["tree_changed"]:
        # The content changed mid-run: this result does not represent any one version, so do not leave it behind as a cache entry
        cached.rename(run_dir / "report.stale.json")
    data["cached"] = False
    return data
