#!/usr/bin/env python3
"""tests/red_proof_e2_pull.py — pull の Controller 化 (01c E2 / t008) の欠陥を戻した変異で、狙ったテストが赤になる実証。

    python3 tests/red_proof_e2_pull.py            # 全部
    python3 tests/red_proof_e2_pull.py E01 E05    # 指定した変異だけ

1. リポジトリ (.git / .claude / queue / registry / logs を除く) を一時ディレクトリに写す。本番の状態には触れない
   (テストは自分で tmp の queue を作る。`CREWVIA_*` の env は外して走らせる)。
2. **変異なし**で `tests/test_pull_execution_e2.py` が全部緑であることを確かめる (対照)。
3. 変異 (scripts/ の 1 か所) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。赤は「狙ったテスト名の FAILED」
   だけ: collection error・ImportError・SyntaxError は赤と数えない (テスト対象の切り出しの失敗を赤にしない)。
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき。置換元が**ちょうど 1 回**でなければ変異自体を失敗 (BROKEN) にする。

変異の対応 (受入条件の「赤の実証: 冪等化・Execution ID の発行・二重予約の拒否」+ 必須条件):

- E01 冪等化 (N8): 再開を無効にする → 同じ Worker の再 pull が TASK_ALREADY_RESERVED になる
- E02a / E02b / E02c Execution ID の発行: JSON / `.crewvia-env` / identity に ID を入れない
- E03 二重予約の拒否: 他の Worker の予約済み task を再開 (= 奪取) できる
- E04 準備ロックを外す (設計 §6.1 の 5): 同じ予約の並行 pull が worktree に触れる
- E05 CAS を新しい欄だけにする (Codex 3 巡目 P1): 旧形式の書き手が解放した予約を start が running にする
- E06 O1: 再開が自分の古い枠を公開し直さない
- E07 O2: 時刻でない `now` を通す
- E08 start を JSON の前にコミットしない
- E09 (t031) 準備ロックの記述子を worktree を作る helper に渡さない (`pass_fds` を外す): 親の python だけが SIGKILL されると
  子孫が生きていてもロックが外れ、再試行の pull が別の helper を同時に走らせる
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
PLAN = "scripts/plan.sh"
STORE = "scripts/lib_state_store.py"
CTL = "scripts/lib_task_controller.py"
TEST = "tests/test_pull_execution_e2.py"
PARENT_KILL = "tests/test_pull_execution_e2_parent_kill.py"      # t031 (PR #271 Codex P2)

T = "test_pull_execution_e2.py"
# (id, 説明, ファイル, 置換元, 置換先, 赤になるべきテスト名 (部分一致のどれか 1 つ以上が FAILED))
MUTATIONS = [
    ("E01", "冪等化を外す (予約済みの task の再開をしない)", PLAN,
     "    if not agent or meta.get('status') != 'in_progress' or (meta.get('worker') or '') != agent:\n"
     "        return False\n"
     "    if _EXEC.fields_problem(meta) is not None:\n"
     "        return False\n",
     "    return False\n",
     ["test_a_pull_killed_before_start_is_resumed_by_the_same_worker_with_the_same_execution",
      "test_pull_killed_at_every_write_point_converges_and_never_forks_the_execution",
      "test_a_killed_preparing_pull_leaves_no_stale_lock_and_the_next_pull_resumes_the_same_execution"]),
    ("E02a", "Execution ID の発行を外す (JSON に execution_id / attempt を出さない)", PLAN,
     "    chosen_holder[0]['execution_id'] = execution_id\n"
     "    chosen_holder[0]['attempt'] = execution_holder[0]['attempt']\n",
     "",
     ["test_pull_issues_one_execution_and_every_place_agrees"]),
    ("E02b", "Execution ID の発行を外す (.crewvia-env に CREWVIA_EXECUTION_ID を書かない)", PLAN,
     "                        f'export CREWVIA_EXECUTION_ID={shlex.quote(execution_id)}\\n'\n",
     "",
     ["test_pull_issues_one_execution_and_every_place_agrees"]),
    ("E02c", "Execution ID の発行を外す (identity に execution_id を入れない)", CTL,
     "        txn.publish_assignment(agent, slug, tid, now, execution_id=xid)\n",
     "        txn.publish_assignment(agent, slug, tid, now)\n",
     ["test_pull_issues_one_execution_and_every_place_agrees"]),
    ("E03", "二重予約の拒否を外す (他の Worker の予約済み task を再開できる)", PLAN,
     "                        if agent and _is_reserved_by(meta, agent) and (meta.get('target_dir') or None) == effective_target:\n",
     "                        if agent and (meta.get('target_dir') or None) == effective_target:\n",
     ["test_a_second_pull_of_a_reserved_task_by_someone_else_is_refused_with_a_fixed_code",
      "test_someone_elses_reserved_task_is_never_taken_over_by_another_worker"]),
    ("E04", "準備ロックを外す (同じ予約の並行 pull が worktree に触れる)", STORE,
     "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    except OSError as e:\n        os.close(fd)\n"
     "        if e.errno in (_errno.EWOULDBLOCK, _errno.EAGAIN):\n            raise LockBusy(f\"prepare lock",
     "        pass\n    except OSError as e:\n        os.close(fd)\n"
     "        if e.errno in (_errno.EWOULDBLOCK, _errno.EAGAIN):\n            raise LockBusy(f\"prepare lock",
     ["test_a_second_pull_of_the_same_reservation_during_preparation_touches_nothing",
      "test_the_preparation_lock_holder_that_fails_closes_the_execution_and_the_other_pull_was_refused"]),
    ("E05", "CAS を新しい欄だけにする (旧形式の書き手との AND を外す)", PLAN,
     "    return (meta.get('status') == 'in_progress'\n"
     "            and (meta.get('worker') or '') == (agent or '')\n"
     "            and meta.get('started_at') == generation\n"
     "            and meta.get('current_execution_id') == execution_id\n",
     "    return (meta.get('current_execution_id') == execution_id\n",
     ["test_a_card_reopened_by_the_director_after_an_old_format_reset_is_not_started_by_the_stale_pull",
      "test_the_worktree_failure_path_does_not_overwrite_a_card_the_director_reopened"]),
    ("E06", "O1: 再開が自分の古い枠 (前の試行の identity) を公開し直さない", PLAN,
     "    if verdict in (ASSIGN_ABSENT, ASSIGN_SUCCESSOR):\n        publish_assignment(agent, slug, task_id, meta.get('started_at'), execution_id=execution_id)\n",
     "    if verdict in (ASSIGN_ABSENT,):\n        publish_assignment(agent, slug, task_id, meta.get('started_at'), execution_id=execution_id)\n",
     ["test_o1_a_stale_slot_of_the_previous_attempt_next_to_a_reserved_card_is_republished_by_the_resume"]),
    ("E07", "O2: 時刻でない now を通す", CTL,
     "    if not isinstance(now, str) or store._safe_generation(now) is None or not _is_iso_instant(now):\n",
     "    if not isinstance(now, str) or store._safe_generation(now) is None:\n",
     ["test_a_non_time_now_is_refused_before_anything_is_written"]),
    ("E08", "start を JSON の前にコミットしない (reserved のまま JSON を渡す)", PLAN,
     "    started, why = _pull_start(mission_slug, task_id, agent, started_holder[0], execution_id, git_context)\n",
     "    started, why = True, ''\n",
     ["test_pull_issues_one_execution_and_every_place_agrees"]),
    ("E09", "準備ロックの記述子を helper に渡さない (親だけ kill → 子孫が排他の外で動く)", PLAN,
     "                pass_fds=(prepare_lock.fileno(),),\n            )",
     "            )",
     ["test_killing_only_the_parent_python_during_worktree_creation_keeps_the_lock_while_the_helper_lives"],
     PARENT_KILL),
]


def _copy_tree(dest: pathlib.Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".claude", "queue", "registry", "logs", "__pycache__", "*.pyc",
                                    "node_modules")
    shutil.copytree(REPO, dest, ignore=ignore, dirs_exist_ok=True)


def _purge_pyc(root: pathlib.Path) -> None:
    for p in root.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(root: pathlib.Path, k_expr: str | None, tests=(TEST, PARENT_KILL)):
    _purge_pyc(root)
    cmd = [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", "-rfE"]
    if k_expr:
        cmd += ["-k", k_expr]
    r = subprocess.run(cmd, cwd=root, env=_env(), capture_output=True, text=True, timeout=1500)
    out = r.stdout + r.stderr
    # 失敗したテストごとの「最初の `E   ` 行」(assert の失敗か、例外 (KeyError 等) か。collection / import の失敗は errors 側)
    failed = {}
    for m in re.finditer(r"^FAILED \S+?::(\S+?)(?:\[\S+\])?(?: - .*)?$", out, re.M):
        failed[m.group(1)] = ""
    for name in list(failed):
        sec = re.search(rf"^_+ {re.escape(name)}(?:\[\S+\])? _+$(.*?)(?=^_{{3,}} |\Z)", out, re.M | re.S)
        first = re.search(r"^E {3}(.*)$", sec.group(1), re.M) if sec else None
        failed[name] = first.group(1).strip() if first else "?"
    errors = re.findall(r"^ERROR .*$", out, re.M)
    return r.returncode, failed, errors, out


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-e2-pull-") as tmp:
        base = pathlib.Path(tmp) / "base"
        base.mkdir()
        _copy_tree(base)
        rc, failed, errors, out = _run(base, None)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        print(f"対照 (変異なし): 緑 ({re.search(r'(\d+) passed', out).group(1)} passed)")

        for mid, desc, rel, old, new, targets, *rest in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(base, work)
            f = work / rel
            src = f.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                shutil.rmtree(work, ignore_errors=True)
                continue
            f.write_text(src.replace(old, new))
            rc, failed, errors, out = _run(work, " or ".join(targets), (rest[0],) if rest else (TEST,))
            hit = sorted(n for n in failed if any(t in n for t in targets))
            if errors or "SyntaxError" in out or "ImportError" in out or "collected 0 items" in out:
                results.append((mid, desc, "BROKEN", "collection error / import error (変異が壊れている)"))
            elif hit:
                # 赤の中身: 狙ったテストの **assert の失敗** (`AssertionError` / `assert ...`) だけを RED と数える。
                # 例外 (KeyError / TypeError 等) で落ちたものは EXC (テスト対象の切り出しの失敗かもしれない) で、RED にしない
                # pytest.fail / DID NOT RAISE (`Failed: `) はテスト自身の判定なので assert の失敗に数える
                asserts = [n for n in hit if failed[n].startswith(("AssertionError", "assert ", "Failed: "))]
                reasons = "; ".join(f"{n}: {failed[n][:110]}" for n in hit)
                results.append((mid, desc, "RED" if asserts else "EXC", reasons))
            else:
                results.append((mid, desc, "GREEN", "狙ったテストが赤にならない (留め金になっていない)"))
            shutil.rmtree(work, ignore_errors=True)

    print()
    for mid, desc, verdict, detail in results:
        print(f"{mid} {verdict:6} {desc}\n       -> {detail}")
    bad = [r for r in results if r[2] != "RED"]
    print(f"\n変異 {len(results)} 件: RED {len(results) - len(bad)} / それ以外 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
