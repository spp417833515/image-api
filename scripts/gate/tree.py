"""The working tree's content sha: a temporary index holds every tracked file plus new files that are not ignored, then write-tree.

The real index is untouched. The same content always gives the same sha, so the cache can reuse a run safely; any change gives a new sha.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def tree_sha(root: Path, scratch: Path) -> str:
    scratch.mkdir(parents=True, exist_ok=True)
    index = scratch / "tree.index"
    index.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True, capture_output=True)
    out = subprocess.run(["git", "write-tree"], cwd=root, env=env, check=True, capture_output=True, text=True)
    index.unlink(missing_ok=True)
    return out.stdout.strip()
