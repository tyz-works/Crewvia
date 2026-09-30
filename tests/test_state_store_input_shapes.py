"""PR #257 Codex 2 巡目 P2 ×3 + QA t009 (t030): 形の違う入力で 落ちる・止まる・健全と言う、を固定する。

- P2-1 `state.yaml` の型: `active_missions` が list でない / 要素が slug でない / 空ファイル → 例外か finding
       (TypeError で落ちない・1 文字ずつに分解しない・「active が 0 件」と読まない)。
- P2-2 card の `status` が語彙の文字列でない → finding (frozenset 判定の TypeError で落ちない)。
- P2-3 監査ログ / `.lock` のパスが通常ファイルでない (FIFO 等) → ロックを持ったまま待たない。
       ハングは timeout 付きの子プロセスで赤として扱う。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import state_store_scenarios as sc
import lib_state_store as store

M, A = sc.MISSION, sc.AGENT
SCRIPTS = os.path.dirname(os.path.abspath(store.__file__))


@pytest.fixture
def q(tmp_path):
    return tmp_path / "queue"


BAD_STATE = {
    "empty_file": "",
    "only_comment": "# nothing\n",
    "no_active_key": "default_mission: m-crash\n",
    "bool": "active_missions: true\ndefault_mission: null\n",
    "scalar_string": "active_missions: mission-name\ndefault_mission: null\n",
    "unclosed_inline_list": "active_missions: [m-crash, other\ndefault_mission: null\n",
    "int_entry": "active_missions:\n  - m-crash\n  - 3\ndefault_mission: null\n",
    "nested_entry": "active_missions:\n  - [x]\ndefault_mission: null\n",
    "default_is_list": "active_missions: []\ndefault_mission: [a]\n",
}


@pytest.mark.parametrize("name", sorted(BAD_STATE))
def test_bad_state_yaml_is_an_error_or_finding_never_a_crash_or_empty(q, name):
    sc.seed(q, "archive")
    (q / "state.yaml").write_text(BAD_STATE[name])
    with pytest.raises(store.StoreReadError):
        with store.transaction(q, op="x", actor="t") as t:
            t.load_state()
    found = store.diagnose(q)                                   # TypeError で落ちない
    assert any(f.kind == "unobservable_input" and f.detail.startswith("state.yaml: ")
               for f in found), (name, found)


@pytest.mark.parametrize("name", ["empty_file", "no_active_key", "bool", "scalar_string"])
def test_r4_does_not_write_state_yaml_when_it_cannot_be_read(q, name):
    """archive 済みの mission を state から外す R-4 が、空・型違いの state.yaml を「active 0 件」と読んで
    書き戻さない (書き込み経路で空に確定させない)。"""
    sc.seed(q, "archive")
    (q / "archive" / M).mkdir(parents=True)
    (q / "state.yaml").write_text(BAD_STATE[name])
    before = (q / "state.yaml").read_text()
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(archive_slugs=(M,)))
    assert [r.result for r in reps] == ["reported:state_unreadable"]
    assert (q / "state.yaml").read_text() == before


def test_healthy_state_shapes_still_load(q):
    sc.seed(q, "archive")
    for text, expect in (("active_missions: []\ndefault_mission: null\n", []),
                         ("active_missions:\ndefault_mission: null\n", []),
                         ("active_missions:\n  - m-crash\n  - 20260930-vnext-01a-state-store\n"
                          "default_mission: m-crash\n", ["m-crash", "20260930-vnext-01a-state-store"])):
        (q / "state.yaml").write_text(text)
        with store.transaction(q, op="x", actor="t") as t:
            assert t.load_state()["active_missions"] == expect


BAD_STATUS = {"list": "[]", "mapping": "{a: 1}", "unknown": "bogus", "empty": "", "int": "3",
              "cancelled": "cancelled"}


@pytest.mark.parametrize("name", sorted(BAD_STATUS))
def test_non_vocabulary_status_is_a_finding_not_a_crash(q, name):
    sc.seed(q, "done")
    card = q / "missions" / M / "tasks" / "t001.md"
    text = card.read_text()
    lines = [f"status: {BAD_STATUS[name]}" if l.startswith("status:") else l for l in text.split("\n")]
    card.write_text("\n".join(lines))
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(cards=((M, "t001"),), agents=(A,)))   # TypeError で落ちない
        assert isinstance(reps, list)
    store.diagnose(q)                                                        # 同上
    with pytest.raises(store.CardUnreadable):
        with store.transaction(q, op="x", actor="t") as t:
            t.load_card(M, "t001")


# ---- P2-3: 通常ファイルでないパスで、ロックを持ったまま待たない ---------------------------

_CHILD = textwrap.dedent("""
    import sys, os
    sys.path.insert(0, {scripts!r})
    import lib_state_store as store
    q = {queue!r}
    try:
        with store.transaction(q, op='x', actor='t') as t:
            t.write_state({{'active_missions': [], 'default_mission': None}})
            t.record('m-crash', 't001', 'pending', 'done')
        print('TRANSITION_OK')
    except store.LockFailed:
        print('LOCK_FAILED')
""")


def _run_child(q, timeout=20):
    code = _CHILD.format(scripts=SCRIPTS, queue=str(q))
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail("ハング: 通常ファイルでないパスを開くのに queue/.lock を持ったまま待った")
    return r


def _audit_fifo(q):
    (q / "audit").mkdir(parents=True, exist_ok=True)
    p = store.audit_path(str(q))
    os.mkfifo(p)
    return p


def test_audit_log_fifo_without_reader_does_not_hang_the_transition(q):
    sc.seed(q, "done")
    _audit_fifo(q)
    r = _run_child(q)
    assert "TRANSITION_OK" in r.stdout, (r.stdout, r.stderr)
    assert "audit log" in r.stderr
    assert (q / "state.yaml").read_text().startswith("active_missions")   # 遷移は進んだ


def test_audit_log_fifo_with_a_reader_is_not_written_to(q):
    sc.seed(q, "done")
    p = _audit_fifo(q)
    rfd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    try:
        r = _run_child(q)
        assert "TRANSITION_OK" in r.stdout, (r.stdout, r.stderr)
        assert "audit log" in r.stderr
        try:
            data = os.read(rfd, 4096)
        except BlockingIOError:
            data = b""                                                    # 書き手が居ない
        assert data == b""                                                # FIFO に 1 バイトも書かない
    finally:
        os.close(rfd)


def test_audit_log_that_is_a_directory_is_a_warning_not_a_stop(q):
    sc.seed(q, "done")
    (q / "audit").mkdir(parents=True, exist_ok=True)
    os.mkdir(store.audit_path(str(q)))
    r = _run_child(q)
    assert "TRANSITION_OK" in r.stdout, (r.stdout, r.stderr)


def test_audit_failure_is_counted(q):
    sc.seed(q, "done")
    _audit_fifo(q)
    with store.transaction(q, op="x", actor="t") as t:
        t.write_state({"active_missions": [], "default_mission": None})
        t.record(M, "t001", "pending", "done")
    assert t.audit_failures >= 1


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_lock_path_that_is_not_a_regular_file_is_lock_failed_not_a_wait(q, kind):
    sc.seed(q, "done")
    lock = q / ".lock"
    if lock.exists():
        lock.unlink()
    if kind == "fifo":
        os.mkfifo(lock)
    else:
        lock.mkdir()
    r = _run_child(q)
    assert "LOCK_FAILED" in r.stdout, (r.stdout, r.stderr)
