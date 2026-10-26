#!/usr/bin/env python3
"""The project's only acceptance entry point.

    uv run python scripts/gate.py check            # routine gate: static checks + unit + real ComfyUI path; red exits non-zero
    uv run python scripts/gate.py check --formal   # strict pre-merge gate: also runs a real ComfyUI upgrade/rollback (stops ComfyUI for a few minutes)
    uv run python scripts/gate.py check --no-cache # ignore the cached result for the same tree sha
    uv run python scripts/gate.py floors --raise   # raise the collected-count floors to the counts of the latest passing check

Reports go to .pp/gate/runs/<tree-sha>-<mode>/ (report.json + summary.md). Executors, reviewers and the supervisor
all run this same command; for the same tree sha the cache answers instantly, so nobody re-runs it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gate import floors  # noqa: E402
from gate.check import check  # noqa: E402
from gate.report import summary  # noqa: E402
from gate.suites import CHECK, FORMAL  # noqa: E402
from gate.tree import tree_sha  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gate.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--formal", action="store_true")
    c.add_argument("--no-cache", action="store_true")
    f = sub.add_parser("floors")
    f.add_argument("--raise", dest="raise_", action="store_true", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "check":
        data = check(ROOT, FORMAL if args.formal else CHECK, use_cache=not args.no_cache)
        print(("(cached) " if data["cached"] else "") + summary(data))
        print(f"report: {data['run_dir']}/report.json")
        return 0 if data["ok"] else 1

    sha = tree_sha(ROOT, ROOT / ".pp/gate/tmp")
    path = ROOT / ".pp/gate/runs" / f"{sha}-{CHECK}" / "report.json"
    if not path.is_file():
        print(f"no check report for the current tree {sha}; run gate.py check first", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data["ok"]:
        print("the current tree's check did not pass; floors can only be raised from a green run", file=sys.stderr)
        return 1
    changed = floors.raise_to({s["name"]: s["collected"] for s in data["suites"] if s["collected"] is not None})
    print(json.dumps(changed, ensure_ascii=False) if changed else "floors unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
