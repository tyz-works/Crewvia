"""plan.sh board / board.html (止まっている所と依存の図) を固定する。

守ること: 止まっている各種類が上に出る・0 件の表示・読めない card を消さない・HTML エスケープ・
CREWVIA_TASK_GRAPH=0 で書かない・queue を 1 バイトも書かない・Result 等を出さない。
sandbox は test_task_graph.py のものを使う (隔離した repo root / queue)。
"""

from __future__ import annotations

import datetime
import hashlib
import re

import pytest

from test_task_graph import MISSION, sandbox  # noqa: F401  (fixture)


def _card(sb, tid, status, blocked_by=(), *, title=None, reason=None, worker=None,
          started=None, result="", reason_key="needs_director_reason"):
    lines = [
        "---", f"id: {tid}", f"title: {title or 'task ' + tid}", "skills: [code]",
        "priority: medium", f"status: {status}",
        "blocked_by: [" + ", ".join(blocked_by) + "]", "target_dir: null",
        f"worker: {worker or 'null'}", f"started_at: {started or 'null'}", "completed_at: null",
    ]
    if reason:
        lines.append(f'{reason_key}: "{reason}"')
    lines += ["---", "", "## Description", "", "fixture", "", "## Result", "", result, ""]
    d = sb.queue / "missions" / MISSION / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.md").write_text("\n".join(lines))


def _board(sb, **env):
    r = sb.run("board", env=sb.env(**env))
    return r, sb.root / "registry" / "task-graph" / "board.html"


def _stuck_section(page):
    start = page.index('id="stuck-box"')
    return page[start:page.index("依存の図", start)]


def _queue_digest(sb):
    h = hashlib.sha256()
    for p in sorted(sb.queue.rglob("*")):
        if p.is_file() and "audit" not in p.parts:
            h.update(str(p).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_each_kind_of_stuck_task_is_listed_above_the_graph(sandbox):
    old = (datetime.datetime.now() - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
    _card(sandbox, "t001", "needs_director", reason="Director の判断が要る")
    _card(sandbox, "t002", "failed")
    _card(sandbox, "t003", "pending", ["t002"])           # 依存先が failed → 保留
    _card(sandbox, "t004", "blocked", reason="CI 待ち", reason_key="blocked_reason")
    _card(sandbox, "t005", "in_progress", worker="Hana", started=old)
    _card(sandbox, "t006", "pending", ["t001"])           # 下流 (保留ではない)
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert page.index('id="stuck-box"') < page.index("依存の図")
    top = _stuck_section(page)
    for tid in ("t001", "t002", "t003", "t004", "t005"):
        assert f"{MISSION}/{tid}</b>" in top, tid
    assert f"{MISSION}/t006</b>" not in top
    assert "Director の判断が要る" in top
    assert "CI 待ち" in top
    assert 'data-static="4"' in top          # t005 は JS が数える分
    assert 'stuck-count">5<' in page         # 生成時点で 3 時間経っている
    assert "止まっている所はありません</div>" in top and 'id="none-msg" style="display:none"' in top
    # t001 は t006 を塞いでいる
    assert "塞いでいる下流: t006" in top


def test_nothing_stuck_says_so(sandbox):
    _card(sandbox, "t001", "done")
    _card(sandbox, "t002", "pending", ["t001"])
    _card(sandbox, "t003", "in_progress", worker="Hana",
          started=datetime.datetime.now().isoformat(timespec="seconds"))
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert 'id="none-msg" style="display:block"' in page
    assert 'stuck-count">0<' in page and 'class="stuck ok"' in page


def test_an_unreadable_card_is_shown_not_dropped(sandbox):
    _card(sandbox, "t001", "done")
    (sandbox.queue / "missions" / MISSION / "tasks" / "t002.md").write_text("---\nid: t002\n: : [\n---\n")
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert "読めない card (1)" in _stuck_section(page)
    assert "t002" in page


def test_every_string_is_escaped(sandbox):
    evil = "<script>alert(1)</script>"
    _card(sandbox, "t001", "needs_director", title=evil, reason="<img src=x onerror=1>")
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert evil not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # 自分の <script> は 1 つだけ (外部の読み込みも無い)
    assert page.count("<script") == 1
    for ext in ('src="', "href=", "url(", "@import", "://"):
        assert ext not in page, ext


def test_result_and_body_are_never_published(sandbox):
    _card(sandbox, "t001", "done", result="SECRET-RESULT-TOKEN")
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert "SECRET-RESULT-TOKEN" not in page and "fixture" not in page


def test_switch_off_writes_nothing(sandbox):
    _card(sandbox, "t001", "done")
    r, board = _board(sandbox, CREWVIA_TASK_GRAPH="0")
    assert r.returncode == 0
    assert not board.exists() and not board.parent.exists()
    r = sandbox.run("update", "t001", "--status", "done", env=sandbox.env(CREWVIA_TASK_GRAPH="0"))
    assert not board.exists()


def test_board_does_not_touch_the_queue(sandbox):
    _card(sandbox, "t001", "needs_director", reason="x")
    _card(sandbox, "t002", "pending", ["t001"])
    before = _queue_digest(sandbox)
    r, board = _board(sandbox)
    assert r.returncode == 0 and board.exists()
    assert _queue_digest(sandbox) == before


def test_queue_changes_refresh_board_next_to_tasks_json(sandbox):
    _card(sandbox, "t001", "pending")
    r = sandbox.run("update", "t001", "--status", "blocked", "--blocked-reason", "待ち")
    assert r.returncode == 0, r.stderr
    board = sandbox.graph.parent / "board.html"
    assert board.exists() and f"{MISSION}/t001</b>" in _stuck_section(board.read_text())


def test_a_failing_board_never_breaks_plan_sh(sandbox, monkeypatch):
    """board の生成が落ちても tasks.json は書かれ、終了コードは 0 のまま。"""
    _card(sandbox, "t001", "pending")
    (sandbox.root / "scripts" / "lib_task_board.py").write_text("def render_board(*a, **k):\n    raise RuntimeError('boom')\n"
                                                                   "def static_stuck_kind(t):\n    return None\n")
    r = sandbox.run("update", "t001", "--status", "blocked", "--blocked-reason", "x")  # 自動の生成
    assert r.returncode == 0
    assert sandbox.graph.exists() and "board.html の生成に失敗" in r.stderr


def test_boxes_and_edges_are_drawn(sandbox):
    _card(sandbox, "t001", "done")
    _card(sandbox, "t002", "pending", ["t001"])
    r, board = _board(sandbox)
    page = board.read_text()
    assert page.count('class="node ') == 2 and page.count("marker-end") == 1
    assert 'http-equiv="refresh"' in page and "生成:" in page


def test_reason_follows_the_current_status_not_a_stale_field(sandbox):
    """古い needs_director_reason が残っていても、今の status に対応する欄だけを理由に出す。"""
    _card(sandbox, "t001", "blocked", reason="STALE-ND", reason_key="needs_director_reason")
    p = sandbox.queue / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("---\n\n## Description", 'blocked_reason: "FRESH-BLOCKED"\n---\n\n## Description', 1))
    _card(sandbox, "t002", "failed", reason="STALE-ND2")
    _card(sandbox, "t003", "needs_director", reason="LIVE-ND")
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert "FRESH-BLOCKED" in page and "LIVE-ND" in page
    assert "STALE-ND" not in page and "STALE-ND2" not in page


@pytest.mark.parametrize("status", ["ready_for_verification", "needs_human_review"])
def test_stale_needs_director_reason_is_not_shown_after_the_card_moved_on(sandbox, status):
    """needs_director_reason は needs_director のときだけ。判断待ちに進んだ後の card では古いので出さない。"""
    _card(sandbox, "t001", status, reason="STALE-ND")
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    page = board.read_text()
    assert "STALE-ND" not in page
    assert "t001" in _stuck_section(page)  # 判断待ちの一覧には出る


def test_explicit_board_fails_loudly_when_it_cannot_write(sandbox):
    _card(sandbox, "t001", "pending")
    r, board = _board(sandbox)
    assert r.returncode == 0 and board.exists()
    (sandbox.root / "scripts" / "lib_task_board.py").write_text(
        "def render_board(*a, **k):\n    raise RuntimeError('boom')\n"
        "def static_stuck_kind(t):\n    return None\n")
    r = sandbox.run("board")
    assert r.returncode != 0
    assert r.stdout.strip() == ""
    assert "board.html を書けませんでした" in r.stderr
    # 自動の生成は今どおり本体を止めない
    r = sandbox.run("update", "t001", "--status", "blocked", "--blocked-reason", "x")
    assert r.returncode == 0, r.stderr


def test_blocked_downstream_is_listed_in_task_number_order(sandbox):
    _card(sandbox, "t001", "failed")
    _card(sandbox, "t010", "pending", ["t001"])
    _card(sandbox, "t005", "pending", ["t001"])
    _card(sandbox, "t020", "pending", ["t010"])   # 遠い順 (downstream_closure の戻り) は t020, t005, t010
    r, board = _board(sandbox)
    assert r.returncode == 0, r.stderr
    assert "t005, t010, t020" in _stuck_section(board.read_text())
