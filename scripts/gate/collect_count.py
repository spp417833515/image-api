#!/usr/bin/env python3
"""只数不跑 —— 用 pytest 自己的收集结果报数,输出 JSON。

## 为什么不 scrape「N tests collected」那行

那是给人看的文本,格式随 pytest 版本变;且同一行在有无 deselect 时长相不同
(`7088/7092 tests collected (4 deselected)` vs `792 tests collected`)。
门禁拿正则去读它,等于把判据建在别人的排版上。

## 为什么不用 junit-xml

实测:`--collect-only --junit-xml=x.xml` 确实会写文件,但 `tests="0"` ——
junit 记的是**跑过的用例**,收集期一个都没跑。所以 junit 能给「跑完的数」,
给不了「只收集的数」。

## 判据落在哪

`pytest_collection_finish` 拿到的 `session.items` —— 这是 pytest 自己算完
deselect(`-m "not live"` 等)之后的**最终选中集**,和真正会跑的那批**同一个对象**。
数它,不猜它。
"""

from __future__ import annotations

import json
import sys

import pytest


class _Counter:
    """收集完成时记一笔。用 collection_finish 而不是 modifyitems:

    后者会被多个插件依次调用(deselect 就发生在其中一个里),在它里面数容易数到
    「还没被 deselect 掉」的中间态;collection_finish 在全部 modifyitems 之后,拿到的是终态。
    """

    def __init__(self) -> None:
        self.n: int | None = None

    def pytest_collection_finish(self, session) -> None:
        self.n = len(session.items)


def main(argv: list[str]) -> int:
    counter = _Counter()
    code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", *argv], plugins=[counter])
    ok = counter.n is not None and int(code) == 0
    print(json.dumps({"collected": counter.n, "pytest_exit": int(code), "ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
