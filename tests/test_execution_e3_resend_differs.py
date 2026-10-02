"""01c E3 fix 4 巡目 (t034 / PR #273 Codex P2): 同じ ID・**違う中身**の再送は、exit 0 で飲まず conflict (exit 3) にする。

E3 の前は done / fail / needs-director の 2 回目はどれも exit 2 で拒否され、打ち直した Worker が気付けた。E3 の「同じ ID の再送は成功」
(execution.md §4.4) が、中身を見ずに exit 0 を返すと、`done "first" --pr 5` → `done "CORRECTED" --pr 6` が成功に見えて、card は最初のまま。
ここは各コマンド × 「同じ中身 (exit 0・何も書かない)」「違う中身 (exit 3・何も書かない・値を出さない)」を固定する。
verify-result は t033 (`test_execution_e3_resend_idempotent.py`) が同じ型で済ませている。
"""

from __future__ import annotations

import subprocess

import pytest

import pull_execution_helpers as h
from execution_e3_helpers import MISSION, SECRET, Box, last_error_code, report_argv, run, take


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def first(box, *args):
    xid = take(box)
    p = run(box, *args, "--mission", MISSION, "--execution", xid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    return xid


def resend(box, xid, *args):
    return run(box, *args, "--mission", MISSION, "--execution", xid)


def assert_same_content_is_idempotent(box, xid, *args):
    before = box.snapshot()
    p = resend(box, xid, *args)
    assert p.returncode == 0, (args, p.stdout, p.stderr)
    assert "idempotent" in p.stdout
    assert box.snapshot() == before


def assert_conflict_writes_nothing(box, xid, *args):
    before = box.snapshot()
    p = resend(box, xid, *args)
    assert p.returncode == 3, (args, p.returncode, p.stdout, p.stderr)
    assert last_error_code(p.stderr) == "EXECUTION_ALREADY_TERMINAL"
    assert box.snapshot() == before                              # 何も書かない
    assert SECRET not in p.stdout + p.stderr                     # 値を出さない (固定の文言 + 項目名だけ)
    return p


# ---------------------------------------------------------------------------
# done: --pr / --no-pr / Result 本文
# ---------------------------------------------------------------------------

def test_done_resend_with_the_same_pr_and_result_is_idempotent(box):
    xid = first(box, "done", "t001", "first", "--pr", "5")
    assert_same_content_is_idempotent(box, xid, "done", "t001", "first", "--pr", "5")


@pytest.mark.parametrize("second", [
    ("done", "t001", "first", "--pr", "6"),                       # 違う PR 番号 (同じ Result)
    ("done", "t001", "CORRECTED " + SECRET, "--pr", "5"),         # 違う Result (同じ PR 番号)
    ("done", "t001", "first", "--no-pr", "x " + SECRET),          # PR あり → PR なし
    ("done", "t001", "CORRECTED " + SECRET, "--pr", "6"),         # 両方違う
])
def test_done_resend_that_differs_after_pr_is_a_conflict(box, second):
    xid = first(box, "done", "t001", "first", "--pr", "5")
    assert_conflict_writes_nothing(box, xid, *second)
    assert box.card()["pr_number"] == 5 and "first" in box.card_text()


@pytest.mark.parametrize("second,same", [
    (("done", "t001", "first", "--no-pr", "reason A"), True),
    (("done", "t001", "first", "--no-pr", "reason   A"), True),    # 空白の畳み方は書き込みと同じ
    (("done", "t001", "first", "--no-pr", "reason B " + SECRET), False),
    (("done", "t001", "first", "--pr", "5"), False),               # PR なし → PR あり
    (("done", "t001", "second " + SECRET, "--no-pr", "reason A"), False),
])
def test_done_resend_after_no_pr(box, second, same):
    xid = first(box, "done", "t001", "first", "--no-pr", "reason A")
    if same:
        assert_same_content_is_idempotent(box, xid, *second)
    else:
        assert_conflict_writes_nothing(box, xid, *second)
        assert box.card()["no_pr_waiver"] == "reason A" and "pr_number" not in box.card()


def test_done_resend_without_pr_flags_asserts_nothing_about_the_pr(box):
    """最初の done が --pr を持たなかった (card に pr_number が無い) なら、--pr を足した再送は違う中身。持っていたなら、名乗りなしは主張が無い。"""
    xid = first(box, "done", "t001", "first")
    assert_same_content_is_idempotent(box, xid, "done", "t001", "first")
    assert_conflict_writes_nothing(box, xid, "done", "t001", "first", "--pr", "7")


def test_done_resend_result_with_a_trailing_newline_is_the_same_result(box):
    xid = first(box, "done", "t001", "line1\nline2", "--pr", "5")
    assert_same_content_is_idempotent(box, xid, "done", "t001", "line1\nline2\n", "--pr", "5")


# ---------------------------------------------------------------------------
# needs-director: reason
# ---------------------------------------------------------------------------

def test_needs_director_resend_with_the_same_reason_is_idempotent(box):
    xid = first(box, "needs-director", "t001", "reason A")
    assert_same_content_is_idempotent(box, xid, "needs-director", "t001", "reason A")


def test_needs_director_resend_with_another_reason_is_a_conflict(box):
    xid = first(box, "needs-director", "t001", "reason A")
    assert_conflict_writes_nothing(box, xid, "needs-director", "t001", "reason B " + SECRET)
    assert box.card()["needs_director_reason"] == "reason A"


def test_needs_director_long_reason_is_compared_in_full(box):
    """200 字を超える理由は先頭だけが frontmatter に入り、全文は本文の節にある。末尾だけが違う再送も違う中身。"""
    head = "x" * 260
    xid = first(box, "needs-director", "t001", head + " tail-A")
    assert_same_content_is_idempotent(box, xid, "needs-director", "t001", head + " tail-A")
    assert_conflict_writes_nothing(box, xid, "needs-director", "t001", head + " tail-B " + SECRET)


# ---------------------------------------------------------------------------
# fail: head / no-head / handoff
# ---------------------------------------------------------------------------

def two_heads():
    p = subprocess.run(["git", "-C", str(h.REPO_ROOT), "rev-list", "-n", "2", "HEAD"], capture_output=True, text=True)
    shas = p.stdout.split()
    if p.returncode != 0 or len(shas) < 2:
        pytest.skip("commit が 2 つ以上無い (浅い clone)")
    return shas


def test_fail_resend_with_the_same_no_head_is_idempotent(box):
    xid = first(box, "fail", "t001", "--no-head", "none here")
    assert_same_content_is_idempotent(box, xid, "fail", "t001", "--no-head", "none  here")


@pytest.mark.parametrize("second", [
    ("fail", "t001", "--no-head", "another " + SECRET),            # 理由が違う
    ("fail", "t001", "/abs/handoff.md", "--no-head", "none here"),  # handoff が増えた
])
def test_fail_resend_that_differs_after_no_head_is_a_conflict(box, second):
    xid = first(box, "fail", "t001", "--no-head", "none here")
    assert_conflict_writes_nothing(box, xid, *second)
    assert box.card()["fail_head_waiver"] == "none here"


def test_fail_resend_with_a_head_after_no_head_is_a_conflict(box):
    sha = two_heads()[0]
    xid = first(box, "fail", "t001", "--no-head", "none here")
    assert_conflict_writes_nothing(box, xid, "fail", "t001", "--head", sha)
    assert box.card().get("fail_head") is None


def test_fail_resend_with_the_same_head_is_idempotent_and_another_head_is_a_conflict(box):
    sha, other = two_heads()
    xid = first(box, "fail", "t001", "--head", sha)
    assert_same_content_is_idempotent(box, xid, "fail", "t001", "--head", sha)
    assert_same_content_is_idempotent(box, xid, "fail", "t001", "--head", sha[:12])      # 略称は同じ commit
    assert_conflict_writes_nothing(box, xid, "fail", "t001", "--head", other)
    assert_conflict_writes_nothing(box, xid, "fail", "t001", "--no-head", "x " + SECRET)  # head → no-head
    assert box.card()["fail_head"] == sha


def test_fail_resend_with_the_same_handoff_is_idempotent_and_without_it_is_a_conflict(box):
    xid = first(box, "fail", "t001", "/abs/handoff.md", "--no-head", "none here")
    assert_same_content_is_idempotent(box, xid, "fail", "t001", "/abs/handoff.md", "--no-head", "none here")
    assert_conflict_writes_nothing(box, xid, "fail", "t001", "--no-head", "none here")   # handoff が消えた
    assert_conflict_writes_nothing(box, xid, "fail", "t001", "/abs/other.md", "--no-head", "none here")


# ---------------------------------------------------------------------------
# 検証の連鎖の前半: 中身を持つ引数が無い / status が進んでいれば遷移の拒否 (exit 2)。exit 0 で飲む行は無い
# ---------------------------------------------------------------------------

def test_ready_for_verification_and_verifying_resends_are_refused_not_swallowed(box):
    xid = take(box)
    assert run(box, "ready-for-verification", "t001", "--mission", MISSION, "--execution", xid).returncode == 0
    before = box.snapshot()
    p = resend(box, xid, "ready-for-verification", "t001")
    assert p.returncode == 2 and last_error_code(p.stderr) == "INVALID_TRANSITION"
    assert box.snapshot() == before
    assert run(box, "verifying", "t001", "--verifier", "V1", "--mission", MISSION, "--execution", xid,
               agent="verifier-dispatcher").returncode == 0
    before = box.snapshot()
    for verifier in ("V1", "V2"):                              # 同じ verifier の再送も、違う verifier も飲まない
        p = run(box, "verifying", "t001", "--verifier", verifier, "--mission", MISSION, "--execution", xid,
                agent="verifier-dispatcher")
        assert p.returncode == 2 and last_error_code(p.stderr) == "INVALID_TRANSITION", (verifier, p.stdout, p.stderr)
        assert SECRET not in p.stdout + p.stderr
        assert box.snapshot() == before


# ---------------------------------------------------------------------------
# verify-result pass: t033 は fail / needs_human_review の notes だけを比べ、pass が漏れていた
# ---------------------------------------------------------------------------

def to_verification(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--mission", MISSION, "--execution", xid).returncode == 0
    assert run(box, "verifying", "t001", "--verifier", "V1", "--mission", MISSION, "--execution", xid,
               agent="verifier-dispatcher").returncode == 0


def test_verify_result_pass_resend_with_other_notes_is_a_conflict(box):
    xid = take(box)
    to_verification(box, xid)
    ok = run(box, "verify-result", "t001", "pass", "--notes", "notes A", "--mission", MISSION, "--execution", xid, agent="V1")
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    # 最初の再送で回復が枠を片付ける (verify-result pass は枠を残す。R-2 が次の呼び出しで消す)。それを済ませてから比べる
    assert run(box, "verify-result", "t001", "pass", "--notes", "notes A", "--mission", MISSION, "--execution", xid,
               agent="V1").returncode == 0
    before = box.snapshot()
    same = run(box, "verify-result", "t001", "pass", "--notes", "notes A", "--mission", MISSION, "--execution", xid, agent="V1")
    assert same.returncode == 0 and "idempotent" in same.stdout and box.snapshot() == before
    p = run(box, "verify-result", "t001", "pass", "--notes", "notes B " + SECRET, "--mission", MISSION, "--execution", xid,
            agent="V1")
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_ALREADY_TERMINAL", (p.stdout, p.stderr)
    assert SECRET not in p.stdout + p.stderr
    assert box.snapshot() == before
