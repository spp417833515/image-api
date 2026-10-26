"""Unit tests for the page's response checks (static/js/shapes.js), run with node on the real module.

shapes.js has no DOM dependencies, so node can import it directly. node is required (the gate's js-syntax suite needs it
too); if it is missing the tests fail rather than skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SHAPES = Path(__file__).resolve().parents[2] / "src/image_api/web/static/js/shapes.js"
RUNNER = f"""
import {{ readFileSync }} from 'node:fs';
import {{ problem, SHAPES }} from '{SHAPES.as_uri()}';
const cases = JSON.parse(readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(([shape, value]) => problem(value, SHAPES[shape], '响应'))));
"""

STEP_OK = {
    "step": 1,
    "route": "ref",
    "output": {"width": 960, "height": 960},
    "requested": None,
    "reject": None,
    "unavailable": None,
}
REJECT = {"code": "too_many_images", "message": "放多了"}
IMAGE = {"id": "img_1", "url": "/api/images/img_1", "width": 960, "height": 960, "route": "t2i", "seed": 1}
ROUTES = {k: {"available": True, "reason": None} for k in ("t2i", "ref", "mask")}
STATUS = {
    "online": True,
    "upgrading": None,
    "version": {"tag": "v0.42.5", "commit": "483dae4"},
    "last_check": None,
    "last_operation": None,
    "routes": ROUTES,
    "limits": {"max_images": 4, "size_multiple": 32, "default_size": {"width": 1024, "height": 1024}},
    "size_presets": [{"label": "1:1", "width": 1024, "height": 1024}],
}


def job(state: str, *steps: dict) -> dict:
    return {"id": "job_1", "state": state, "first_step": 1, "steps": list(steps)}


def jstep(state: str, **kw: object) -> dict:
    return {"step": 1, "route": "t2i", "state": state, "progress": None, "image": None, "error": None, **kw}


# (name, shape, value, None if valid else a substring of the reported problem)
CASES: list[tuple[str, str, object, str | None]] = [
    ("plan step with route, no reject", "plan", {"steps": [STEP_OK]}, None),
    (
        "plan step: only the step number (route and reject both missing)",
        "plan",
        {"steps": [{"step": 1}]},
        "必须正好一个为 null",
    ),
    ("plan step: route key missing, reject given", "plan", {"steps": [{"step": 1, "reject": REJECT}]}, None),
    (
        "plan step: route null, reject key missing",
        "plan",
        {"steps": [{"step": 1, "route": None}]},
        "必须正好一个为 null",
    ),
    ("plan step: route and reject both null", "plan", {"steps": [{**STEP_OK, "route": None}]}, "必须正好一个为 null"),
    (
        "plan step: route and reject both given",
        "plan",
        {"steps": [{**STEP_OK, "reject": REJECT}]},
        "必须正好一个为 null",
    ),
    ("plan step: route not in the contract", "plan", {"steps": [{**STEP_OK, "route": "inpaint"}]}, "t2i | ref | mask"),
    ("plan: no steps", "plan", {"steps": []}, "至少要有 1 项"),
    ("generate: job_id", "generate", {"job_id": "job_1"}, None),
    ("generate: no job_id", "generate", {}, "job_id"),
    ("generate: empty job_id", "generate", {"job_id": ""}, "job_id"),
    ("job: running step", "job", job("running", jstep("running", progress=0.4)), None),
    ("job: done step without image", "job", job("done", jstep("done")), "image"),
    ("job: failed step without error", "job", job("failed", jstep("failed")), "error"),
    ("job: progress above 1", "job", job("running", jstep("running", progress=1.5)), "0–1"),
    ("job: done job with a step not done", "job", job("done", jstep("running")), "不是每一步都 done"),
    ("job: done job, done step with image", "job", job("done", jstep("done", image=IMAGE)), None),
    ("status: minimal valid", "status", STATUS, None),
    (
        "status: unavailable route without a reason",
        "status",
        {**STATUS, "routes": {**ROUTES, "mask": {"available": False, "reason": None}}},
        "reason",
    ),
    (
        "history: time without a timezone",
        "history",
        {"items": [{**IMAGE, "instruction": "x", "created_at": "2026-10-26T06:02:00", "step": 1}]},
        "ISO 8601",
    ),
]


@pytest.fixture(scope="module")
def results() -> list[str | None]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node not found: the page's response checks (shapes.js) are tested with node")
    out = subprocess.run(
        [node, "--input-type=module", "-e", RUNNER],
        input=json.dumps([[shape, value] for _, shape, value, _ in CASES], ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("k", range(len(CASES)), ids=[c[0] for c in CASES])
def test_shape(results: list[str | None], k: int) -> None:
    name, _, value, expect = CASES[k]
    got = results[k]
    if expect is None:
        assert got is None, f"{name}: should be accepted, got problem {got!r} for {value}"
    else:
        assert got is not None and expect in got, f"{name}: should be rejected mentioning {expect!r}, got {got!r}"
