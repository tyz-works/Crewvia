"""E5 PR-2 (t001 項目 4 + t005 の Codex P1): kai-review.sh は「自分の pull の ID」だけを名乗る。読めなければ**止まる**。

- pull の JSON から `execution_id` を読めなければ、card の今の試行を読んで名乗る fallback は**無い** (pull のロックを放した後に
  Director の reset + 別の Kai-codex の再 pull が挟まると、worker 名が同じなので card の ID は置き換えの試行のもので、
  古いプロセスがそれを done / fail できてしまう。execution.md §20.7 / §20.10)。codex も gh も起動せず、done / needs-director も
  打たず、固定文言 (task id だけ。JSON の中身は出さない) を stderr に出して exit 非 0 で止まる
- 名乗れている実行は今までどおり通る
- `--skip-pull --execution X` で名乗った試行が替わっていて報告が拒否されたら、exit 3 のまま終わり、案内 (`plan.sh status` で確かめる。無条件の reset は勧めない) を
  stderr に出す (これは変えていない経路)

kai-review.sh は repo の外に置いた偽の plan.sh (名乗りなしの報告を exit 3 で拒否する) と偽の gh / codex で走らせる。
本物の plan.sh・queue・gh には触れない。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"
XID = "ex-" + "d" * 32           # この実行の試行
OTHER_XID = "ex-" + "e" * 32     # reset + 別の Kai-codex の再 pull で card に載った、置き換えの試行
SECRET = "SECRET-pull-json-body"

PLAN_STUB = """#!/bin/bash
case "$1" in
  resolve-mission) echo m-test ;;
  pull) printf '%s' "$PULL_STDOUT"
        if [ -n "$SWAP_CARD_TO" ]; then   # pull のロックを放した後に、別の Kai-codex が再 pull した形
          sed -i "s/^current_execution_id: .*/current_execution_id: $SWAP_CARD_TO/" "$CARD_FILE"
        fi ;;
  done|needs-director)
    case " $* " in
      *" --execution "*)
        if [ -n "$REFUSE_NAMED" ]; then
          echo "$1 refused-named" >> "$CALLS_FILE"
          echo "[plan.sh] error_code=EXECUTION_NOT_CURRENT" >&2
          exit 3
        fi
        echo "$@" >> "$CALLS_FILE"; exit 0 ;;
      *) echo "$1 refused" >> "$CALLS_FILE"
         echo "[plan.sh $1] m-test/t001: この task は実行中の試行があります。" >&2
         echo "[plan.sh] error_code=EXECUTION_REQUIRED" >&2
         exit 3 ;;
    esac ;;
  *) echo "$@" >> "$CALLS_FILE" ;;
esac
exit 0
"""


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_kai(tmp_path, pull_stdout, card_fields=None, *, swap_card_to="", refuse_named=False, extra_args=()):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    _exe(root / "scripts" / "plan.sh", PLAN_STUB)
    queue = tmp_path / "queue"
    card = queue / "missions" / "m-test" / "tasks" / "t001.md"
    if card_fields is not None:
        card.parent.mkdir(parents=True)
        card.write_text("---\nid: t001\ntitle: x\nstatus: in_progress\nworker: Kai-codex\n"
                        + card_fields + "---\n\n## Result\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # gh / codex が呼ばれたら calls に残す (止まる経路では 1 度も呼ばれない)。pull の後の gh は失敗して fail_needs_director に倒れる
    _exe(bindir / "gh", "#!/bin/bash\necho gh-called >> \"$CALLS_FILE\"\nexit 1\n")
    _exe(bindir / "codex", "#!/bin/bash\necho codex-called >> \"$CALLS_FILE\"\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_NAME", "CREWVIA_EXECUTION_ID")}
    env.update({"CREWVIA_REPO_ROOT": str(root), "CREWVIA_QUEUE": str(queue), "PULL_STDOUT": pull_stdout,
                "CALLS_FILE": str(tmp_path / "calls.txt"), "CARD_FILE": str(card), "SWAP_CARD_TO": swap_card_to,
                "REFUSE_NAMED": "1" if refuse_named else "", "PATH": f"{bindir}:{env['PATH']}"})
    p = subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test", *extra_args],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
    return p, calls


ACTIVE_CARD = f"current_execution_id: {XID}\nexecution_status: running\n"
UNREADABLE_PULLS = ["this is not json", '{"id": "t001"}', "", '{"execution_id": 7}', f'{{"note": "{SECRET}"']


@pytest.mark.parametrize("pull_stdout", UNREADABLE_PULLS)
@pytest.mark.parametrize("card", [None, ACTIVE_CARD])
def test_when_the_pull_json_has_no_id_the_script_stops_without_reporting_or_naming_the_card(tmp_path, pull_stdout, card):
    """card に active な自分の試行があっても (= 旧 fallback なら名乗れた形でも) 名乗らない・報告しない・codex を起動しない。"""
    p, calls = run_kai(tmp_path, pull_stdout, card)
    assert p.returncode not in (0, 3), (p.returncode, p.stdout, p.stderr)       # 成功にも「拒否された」にも見せない
    assert calls == "", calls                                                    # done / needs-director / gh / codex のどれも呼ばない
    assert "execution_id を読めませんでした" in p.stderr and "t001" in p.stderr
    assert XID not in p.stdout + p.stderr


@pytest.mark.parametrize("pull_stdout", UNREADABLE_PULLS)
def test_a_replacement_attempt_in_the_card_is_never_named_or_completed(tmp_path, pull_stdout):
    """P1 の再現: pull の後 (ロックを放した後) に reset + 別プロセスの再 pull で card が置き換えの試行になる。
    旧 fallback は card を読んで OTHER_XID を名乗り、置き換えの試行を done / needs-director できた。"""
    p, calls = run_kai(tmp_path, pull_stdout, ACTIVE_CARD, swap_card_to=OTHER_XID)
    assert calls == "", calls
    assert OTHER_XID not in calls + p.stdout + p.stderr
    assert p.returncode not in (0, 3)


def test_the_stop_message_does_not_echo_the_pull_output(tmp_path):
    p, calls = run_kai(tmp_path, f'{{"note": "{SECRET}"', ACTIVE_CARD)
    assert SECRET not in p.stdout + p.stderr


def test_a_named_run_is_unchanged(tmp_path):
    p, calls = run_kai(tmp_path, '{"execution_id": "%s"}' % XID)
    assert f"--execution {XID}" in calls and "refused" not in calls
    assert "update t001 --status pending --reset" not in p.stderr


def test_a_named_run_is_not_redirected_by_a_swapped_card(tmp_path):
    """JSON から読めた ID は固定される。pull の後に card が替わっても、名乗るのは自分の pull の ID。"""
    p, calls = run_kai(tmp_path, '{"execution_id": "%s"}' % XID, ACTIVE_CARD, swap_card_to=OTHER_XID)
    assert f"--execution {XID}" in calls and OTHER_XID not in calls


def test_a_named_report_refused_by_plan_sh_ends_with_exit_3_and_a_recovery_hint(tmp_path):
    """名乗った試行が替わっていて plan.sh が拒否 (exit 3) したとき: exit 3 のまま・card は in_progress のまま・回復手順を出す。"""
    p, calls = run_kai(tmp_path, "", None, refuse_named=True, extra_args=("--skip-pull", "--execution", XID))
    assert p.returncode == 3, (p.returncode, p.stdout, p.stderr)
    assert "needs-director refused-named" in calls
    assert "plan.sh status --mission m-test" in p.stderr, p.stderr
    # Codex P2: どの拒否でも in_progress と断定せず、無条件の reset を勧めない (別の試行が進行中・既に終わっている場合がある)
    assert "--reset" not in p.stderr and "in_progress" not in p.stderr, p.stderr


def test_skip_pull_still_reads_the_card_once_at_startup(tmp_path):
    """Director が手で再実行する経路は変えない: `--skip-pull` の起動時の 1 回は card の active な自分の試行を名乗る。"""
    p, calls = run_kai(tmp_path, "", ACTIVE_CARD, extra_args=("--skip-pull",))
    assert f"--execution {XID}" in calls and "refused" not in calls
