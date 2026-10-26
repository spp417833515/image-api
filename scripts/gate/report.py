"""The report: report.json (machine-readable) + summary.md (conclusion first, about 40 lines)."""

from __future__ import annotations

import json
from pathlib import Path

from .run import SuiteResult


def write(run_dir: Path, meta: dict, results: list[SuiteResult]) -> dict:
    ok = all(r.ok for r in results) and not meta.get("tree_changed")
    report = {**meta, "ok": ok, "suites": [r.to_dict() for r in results]}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(summary(report), encoding="utf-8")
    return report


def summary(report: dict) -> str:
    head = "PASS" if report["ok"] else "FAIL"
    lines = [
        f"# gate {report['mode']} · {head}",
        "",
        f"tree {report['tree_sha']} · {report['started_at']} · {report['seconds']}s",
    ]
    if report.get("tree_changed"):
        lines.append(
            "**Warning: the working tree changed during the run; this result is not cached. Re-run once edits are finished.**"
        )
    lines += ["", "| suite | result | collected/floor | seconds | notes |", "|---|---|---|---|---|"]
    for s in report["suites"]:
        counts = "—" if s["collected"] is None else f"{s['collected']}/{s['floor']}"
        lines.append(f"| {s['name']} | {'✓' if s['ok'] else '✗'} | {counts} | {s['seconds']} | {s['reason']} |")
    fails = [(s["name"], f) for s in report["suites"] for f in s["failures"]]
    if fails:
        lines += ["", "## Failures (first 10)"]
        for name, f in fails[:10]:
            lines.append(f"- [{name}] `{f['test']}`: {f['message']}")
    lines += ["", "Logs: " + ", ".join(s["log"] for s in report["suites"])]
    return "\n".join(lines) + "\n"
