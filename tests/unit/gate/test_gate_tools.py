"""The gate's own criteria must be tested: a broken criterion is worse than a red test."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from gate import floors, junit, report  # noqa: E402
from gate.run import SuiteResult  # noqa: E402
from gate.suites import CHECK, FORMAL, SUITES, for_mode  # noqa: E402


def test_floors_only_raise(tmp_path, monkeypatch):
    path = tmp_path / "floors.json"
    path.write_text(json.dumps({"_comment": "x", "unit": 10, "live": 3}), encoding="utf-8")
    monkeypatch.setattr(floors, "PATH", path)
    changed = floors.raise_to({"unit": 7, "live": 5})
    assert changed == {"live": (3, 5)}
    assert floors.load() == {"unit": 10, "live": 5}


def test_junit_counts_failures(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0">'
        '<testcase classname="tests.unit.test_x" name="test_a"/>'
        '<testcase classname="tests.unit.test_x" name="test_b"><failure message="boom">trace</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert junit.totals(xml) == {"tests": 2, "failures": 1, "errors": 0, "skipped": 0}
    assert junit.failures(xml)[0]["test"] == "tests/unit/test_x.py::test_b"


def test_report_red_when_any_suite_red_or_tree_changed(tmp_path):
    green = SuiteResult("unit", True, "1 passed", 0.1, collected=1, floor=1)
    red = SuiteResult("live", False, "missing capability", 0.1, collected=1, floor=1)
    meta = {"mode": CHECK, "tree_sha": "abc", "started_at": "t", "run_dir": "d", "seconds": 1, "tree_changed": False}
    assert report.write(tmp_path, meta, [green])["ok"] is True
    assert report.write(tmp_path, meta, [green, red])["ok"] is False
    assert report.write(tmp_path, {**meta, "tree_changed": True}, [green])["ok"] is False


def test_formal_is_superset_of_check():
    check = {s.name for s in for_mode(CHECK)}
    formal = {s.name for s in for_mode(FORMAL)}
    assert check < formal
    assert {"live", "unit"} <= check
    assert len({s.name for s in SUITES}) == len(SUITES)
