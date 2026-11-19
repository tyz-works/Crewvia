"""plan.sh done は、Result に approved_head の行があるのに gate (`lib_ci_status.approved_head`) が読めない形なら拒否する。

実例: 2026-11-19 の #415 / #416 / #417 で review の Worker が approved_head を Result の 2 行目に書き、後ろに所見を続けた。
承認したのに gate は `no_approval` を返した。拒否は D0 と同じ作法 (exit 3・何も書かない・stderr の最後の行が固定コード)。

形が崩れた行 (40 桁でない・大文字の sha・`approved_head :` 等) も拒否する: gate が読まない形なので、承認のつもりで書かれていても
届かない。通すと「承認したのに no_approval」の再発になる。承認の形の行が無い Result (要修正・review 以外) は今までどおり通す。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import lib_ci_status as ci  # noqa: E402
import lib_history  # noqa: E402
from execution_e3_helpers import Box, last_error_code, run, take  # noqa: E402

SHA = "a" * 40
CODE = "APPROVED_HEAD_NOT_LAST"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def _gate_reads(box):
    return ci.approved_head(lib_history.result_section(box.card_text()))


def _done(box, xid, result, *, via_file=False, tmp_path=None):
    args = ["done", "t001"]
    if via_file:
        f = tmp_path / "result.md"
        f.write_text(result, encoding="utf-8")
        args += ["--result-file", str(f)]
    else:
        args += [result]
    return run(box, *args, "--mission", _mission(), "--no-pr", "w", "--execution", xid)


def _mission():
    from execution_e3_helpers import MISSION
    return MISSION


MISPLACED = [
    f"LGTM\napproved_head: {SHA}\n所見: 問題なし",                       # 2 行目 (実例の形)
    f"approved_head: {SHA}\n\n所見あり",                                # 先頭
    f"LGTM\napproved_head: {'A' * 40}",                                 # 大文字の sha
    f"LGTM\napproved_head: {'a' * 39}",                                 # 40 桁でない
    f"LGTM\napproved_head: {SHA[:12]}",                                 # 短縮形
    f"LGTM\nApproved_Head: {SHA}",                                      # 大文字小文字
    f"LGTM\napproved_head:{SHA}",                                       # コロンの後の空白なし
]


@pytest.mark.parametrize("result", MISPLACED)
def test_unreadable_approval_line_is_refused_and_writes_nothing(box, result):
    xid = take(box)
    before = box.snapshot()
    p = _done(box, xid, result)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert last_error_code(p.stderr) == CODE, p.stderr
    assert "最後の行" in p.stderr                                        # 出口が書いてある
    assert SHA not in p.stdout + p.stderr and "所見" not in p.stdout + p.stderr   # Result の本文・sha は出さない
    assert box.snapshot() == before                                      # 1 バイトも変わらない
    assert box.card()["status"] == "in_progress"


def test_refusal_via_result_file(box, tmp_path):
    xid = take(box)
    before = box.snapshot()
    p = _done(box, xid, f"LGTM\napproved_head: {SHA}\n続き", via_file=True, tmp_path=tmp_path)
    assert p.returncode == 3 and last_error_code(p.stderr) == CODE, p.stderr
    assert box.snapshot() == before


def test_approval_after_a_heading_is_read_by_the_gate_so_it_passes(box):
    """見出しは保存時に `### ` へ下がるので、見出しの後ろの最後の行は gate に読める (通す)。"""
    xid = take(box)
    p = _done(box, xid, f"## QA Gate\n- ok\n## 所見\nx\n\napproved_head: {SHA}")
    assert p.returncode == 0, p.stderr
    assert _gate_reads(box) == SHA


def test_approval_on_the_last_line_passes(box):
    xid = take(box)
    p = _done(box, xid, f"LGTM\n所見: 問題なし\n\napproved_head: {SHA}\n\n")
    assert p.returncode == 0, p.stderr
    assert _gate_reads(box) == SHA


def test_kai_review_shape_passes(box):
    """kai-review.sh は全文を組み立てた後に `printf '\\napproved_head: %s\\n'` を追記する (最後の非空行)。"""
    xid = take(box)
    body = "## codex review\n所見なし\n\n- P3: x\n"
    p = _done(box, xid, body + f"\napproved_head: {SHA}\n")
    assert p.returncode == 0, p.stderr
    assert _gate_reads(box) == SHA


@pytest.mark.parametrize("result", ["要修正: P1 が 2 件", "LGTM だが approved_head は書かない (承認ではない)",
                                    "- `approved_head: <sha>` を最後の行に書く、と文書にある"])
def test_result_without_an_approval_line_passes_as_before(box, result):
    xid = take(box)
    p = _done(box, xid, result)
    assert p.returncode == 0, p.stderr
    assert _gate_reads(box) is None


def test_resend_of_an_accepted_done_stays_idempotent(box):
    xid = take(box)
    r = f"LGTM\napproved_head: {SHA}"
    assert _done(box, xid, r).returncode == 0
    again = _done(box, xid, r)
    assert again.returncode == 0 and "idempotent" in again.stdout


def test_has_approval_line_is_defined_next_to_approved_head():
    assert ci.has_approval_line(f"x\napproved_head: {SHA}\ny")
    assert ci.has_approval_line("  approved_head : nope")
    assert not ci.has_approval_line("本文に approved_head: を含む行 (行頭でない)")
    assert not ci.has_approval_line(None)
