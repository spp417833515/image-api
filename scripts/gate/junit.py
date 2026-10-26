"""门禁读 junit 报告：总数与失败明细（沿用 MYAI scripts/gate/junit.py）。

失败明细除了「报错首行」还带一段封顶的尾部详情。病史(2026-10-03):只留首行,
junit 报告又随门禁临时目录整体删除 —— 一条容器用例回连超时,断言里附的 docker 日志
在门禁报告里看不到,只能另外单跑去抓现场。详情尾部按行数与字符数双封顶,
防一条巨型 traceback 撑爆汇总。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

DETAIL_TAIL_LINES = 15
DETAIL_TAIL_CHARS = 2000
FIRST_LINE_CHARS = 160


def totals(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    found = False
    for ts in root.iter("testsuite"):
        found = True
        for k in counts:
            counts[k] += int(ts.get(k) or 0)
    return counts if found else None


def failures(path: Path, limit: int = 20) -> list[dict]:
    """红的用例,最多 limit 条:{test: 文件::用例, message: 报错首行, detail: 封顶尾部}。"""
    out: list[dict] = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        bad = next((c for c in tc if c.tag in ("failure", "error")), None)
        if bad is None:
            continue
        text = (bad.text or bad.get("message") or "").strip()
        first = ((bad.get("message") or text).strip().splitlines() or [""])[0]
        out.append(
            {
                "test": (tc.get("classname") or "").replace(".", "/") + ".py::" + (tc.get("name") or ""),
                "message": first[:FIRST_LINE_CHARS],
                "detail": tail(text),
            }
        )
        if len(out) >= limit:
            break
    return out


def tail(text: str, lines: int = DETAIL_TAIL_LINES, chars: int = DETAIL_TAIL_CHARS) -> str:
    """末尾 lines 行,再按字符封顶(保尾:报错的落点通常在最后)。"""
    kept = "\n".join((text or "").rstrip().splitlines()[-lines:])
    return kept if len(kept) <= chars else "…" + kept[-chars:]


__all__ = ["DETAIL_TAIL_LINES", "DETAIL_TAIL_CHARS", "totals", "failures", "tail"]
