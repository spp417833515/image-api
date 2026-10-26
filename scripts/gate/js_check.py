#!/usr/bin/env python3
"""Syntax check for the page's JS: parse every .js under static/ with node as an ES module.

Why --experimental-default-type=module: verified on Node 22, plain `node --check x.js` returns 0 for a .js file
that contains import plus a syntax error (module-type detection fails and lets it through); only with the default
type pinned to module is it reported. Finding no files, or node being absent, counts as red.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src/image_api/web/static"


def main(static: Path = STATIC) -> int:
    node = shutil.which("node")
    if node is None:
        print("node not found: cannot check the page JS syntax", file=sys.stderr)
        return 1
    files = sorted(static.rglob("*.js"))
    if not files:
        print(f"no .js files under {static}", file=sys.stderr)
        return 1
    bad = 0
    for path in files:
        out = subprocess.run(
            [node, "--experimental-default-type=module", "--check", str(path)], capture_output=True, text=True
        )
        if out.returncode != 0:
            bad += 1
            print(out.stderr.strip(), file=sys.stderr)
    print(f"{len(files)} files checked, {bad} with syntax errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
