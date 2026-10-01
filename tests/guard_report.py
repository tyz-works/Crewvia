"""guard_report.py — 構造ガードが**実際に検査した件数**を、成功しても CI ログに残す (vNext 01b G3 / t012。01a backlog 2)。

構造ガード (`test_queue_writes_go_through_the_store.py` / `test_git_decisions_go_through_policy.py`) は
「検査した件数の下限」を assert するが、成功したテストの `print` は pytest が捨てるので、CI ログに**件数が出なかった**
(01a の t024 で「件数が CI に出ない」が残った)。ガードは検査結果をここに `record()` し、`tests/conftest.py` の
`pytest_terminal_summary` が**成功・失敗に関わらず**最後に 1 行ずつ表示する。

    [structural-guard] git-decisions: files=58 code_blocks=132 hits=22 allowlisted=22 unlisted=0
"""

from __future__ import annotations

_REPORTS: dict[str, dict] = {}


def record(guard: str, **counts) -> None:
    """ガード名ごとに最後の記録を残す (同じガードが複数のテストから呼んでもよい。数値は上書き)。"""
    _REPORTS.setdefault(guard, {}).update(counts)


def lines() -> list[str]:
    return [f"[structural-guard] {name}: " + " ".join(f"{k}={v}" for k, v in counts.items())
            for name, counts in sorted(_REPORTS.items())]
