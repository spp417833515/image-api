"""Run a single suite and return a machine-readable result. Pass/fail comes from the exit code and junit; counts come from pytest's own collection result."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import capabilities, junit
from .suites import Suite

COLLECT = Path(__file__).with_name("collect_count.py")


@dataclass
class SuiteResult:
    name: str
    ok: bool
    reason: str  # one sentence: why it is red, or a summary of the green
    seconds: float
    collected: int | None = None
    floor: int | None = None
    totals: dict | None = None
    failures: list[dict] = field(default_factory=list)
    log: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _exec(argv: list[str], root: Path, env: dict, log: Path, timeout: int) -> int:
    with log.open("w", encoding="utf-8") as out:
        try:
            return subprocess.run(
                argv, cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            out.write(f"\n[gate] timed out after {timeout}s\n")
            return 124


def run_suite(suite: Suite, root: Path, env: dict, run_dir: Path, floor: int) -> SuiteResult:
    started = time.monotonic()
    log = run_dir / f"{suite.name}.log"

    def done(ok: bool, reason: str, **extra) -> SuiteResult:
        return SuiteResult(
            suite.name, ok, reason, round(time.monotonic() - started, 1), log=str(log.relative_to(root)), **extra
        )

    if suite.kind == "command":
        code = _exec(list(suite.args), root, env, log, suite.timeout_s)
        return done(code == 0, "passed" if code == 0 else f"exit code {code}, see the log")

    count = subprocess.run(
        [sys.executable, str(COLLECT), *suite.args], cwd=root, env=env, capture_output=True, text=True, timeout=300
    )
    try:
        collected = json.loads(count.stdout.strip().splitlines()[-1])["collected"]
    except IndexError, KeyError, json.JSONDecodeError:
        log.write_text(count.stdout + count.stderr, encoding="utf-8")
        return done(False, "collection failed (count unavailable is treated as red)", floor=floor)
    if collected is None or collected < floor:
        return done(False, f"collected {collected} < floor {floor} (tests vanished?)", collected=collected, floor=floor)
    if collected == 0:
        return done(True, "0 tests collected (floor 0)", collected=0, floor=floor)

    missing = capabilities.missing(suite.needs)
    if missing:
        log.write_text("\n".join(missing) + "\n", encoding="utf-8")
        return done(False, "missing capability: " + "; ".join(missing), collected=collected, floor=floor)

    xml = run_dir / f"{suite.name}.junit.xml"
    argv = [sys.executable, "-m", "pytest", *suite.args, "-p", "no:cacheprovider", f"--junit-xml={xml}", "-q"]
    code = _exec(argv, root, env, log, suite.timeout_s)
    totals = junit.totals(xml)
    if totals is None:
        return done(False, f"no junit report (pytest exit code {code})", collected=collected, floor=floor)
    fails = junit.failures(xml) if totals["failures"] or totals["errors"] else []
    ran = totals["tests"] - totals["skipped"]
    ok = code == 0 and not fails and totals["skipped"] == 0
    if ok:
        reason = f"{ran} passed"
    elif totals["skipped"]:
        reason = f"{totals['skipped']} skipped (skips are not allowed at acceptance)"
    else:
        reason = f"{totals['failures']} failures {totals['errors']} errors (pytest exit code {code})"
    return done(ok, reason, collected=collected, floor=floor, totals=totals, failures=fails)
