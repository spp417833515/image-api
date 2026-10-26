"""Check response bodies with the page's own validator (src/image_api/web/static/js/shapes.js), run by node.

The page rejects a whole response on the first field it does not accept, so the backend's responses are checked with
exactly that code rather than a Python copy of it. node missing is a failure, never a skip (as in the gate's js-syntax).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

SHAPES_JS = Path(__file__).resolve().parents[3] / "src/image_api/web/static/js/shapes.js"

_SCRIPT = """
import { SHAPES, problem } from %s;
let input = '';
for await (const chunk of process.stdin) input += chunk;
const checks = JSON.parse(input);
const unknown = checks.map(([name]) => name).filter(name => !(name in SHAPES));
if (unknown.length) throw new Error(`shapes.js has no shape named ${unknown}`);
console.log(JSON.stringify(checks.map(([name, body]) => problem(body, SHAPES[name], name))));
"""


def page_problems(checks: list[tuple[str, Any]]) -> list[str | None]:
    """For each (shape name, response body): the page's first complaint about it, or None if the page accepts it."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node not found: cannot run the page's shapes.js against the responses")
    script = _SCRIPT % json.dumps(SHAPES_JS.as_uri())
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps(checks),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"node could not run {SHAPES_JS}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def assert_page_accepts(checks: list[tuple[str, Any]]) -> None:
    problems = [(name, p) for (name, _), p in zip(checks, page_problems(checks), strict=True) if p]
    assert not problems, f"the page would reject these responses: {problems}"
