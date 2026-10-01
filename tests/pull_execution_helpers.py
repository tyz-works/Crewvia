#!/usr/bin/env python3
"""tests/pull_execution_helpers.py — pull の Controller 化 (vNext 01c E2) のテスト共通の道具。

隔離した plan.sh (`copy_plan_tree`) + 隔離 queue + 使い捨ての root。**本番の queue / registry / worktree / mux には触れない**
(`CREWVIA_QUEUE` と `CREWVIA_REPO_ROOT` を両方とも `root` の下に向ける。Taskvia は `disabled`)。

git は呼ばない stub の `git-helpers.sh` を、**途中で止められる / 失敗させられる / 別のコマンドを差し込める**版に差し替える
(fixture の中の合図ファイルで操る。本番のコードにテスト用のフックを足さない):

    <root>/hold/<task>.enabled   あれば worktree 作成の途中で止まる (reached を作り、go が現れるまで待つ)
    <root>/hold/<task>.reached   止まった印
    <root>/hold/<task>.go        これが現れたら続ける
    <root>/hold/<task>.cmd       あれば、その中身を bash で実行してから続ける (旧形式の reset 等を差し込む)
    <root>/hold/<task>.fail      あれば、実行後に W5 で失敗する
    <root>/hold/<task>.parent    helper を起こした python (plan.sh の本体) の pid (親だけを kill するテスト用)
    <root>/hold/<task>.invocations  helper が走るたびに 1 行 (pid)。同時に 2 本走っていないことを数える
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import lib_execution as ex  # noqa: E402
import lib_state_store as store  # noqa: E402
import lib_task_cards as cards  # noqa: E402
import state_store_scenarios as sc  # noqa: E402
import task_graph_publisher_harness as harness  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402

MISSION = sc.MISSION
SECRET = "SENTINEL_SECRET_do_not_leak"

HELPER = r'''# crewvia test stub: git-helpers (E2 の pull のテスト用。止める / 失敗させる / 差し込む)
crewvia_create_worktree() {
  local mission_slug="${1:-}" task_id="${2:-}" task_slug="${3:-}"
  local root
  root="$(cd -P -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)" || return 1
  local hold="${root}/hold"
  local wt="${root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}"
  echo "$PPID" > "${hold}/${task_id}.parent"          # この helper を起こした plan.sh の python の pid (親だけ kill するため)
  echo "$$" >> "${hold}/${task_id}.invocations"        # helper が走った回数 (同時に 2 本走っていないことを見る)
  if [[ -e "${hold}/${task_id}.enabled" ]]; then
    : > "${hold}/${task_id}.reached"
    local n=0
    while [[ ! -e "${hold}/${task_id}.go" && $n -lt 400 ]]; do sleep 0.05; n=$((n+1)); done
  fi
  if [[ -e "${hold}/${task_id}.cmd" ]]; then
    bash "${hold}/${task_id}.cmd" >&2 || true
  fi
  if [[ -e "${hold}/${task_id}.fail" ]]; then
    echo "crewvia_create_worktree (stub): W5: injected failure" >&2
    return 1
  fi
  mkdir -p "$wt" || return 1
  echo "$wt"
}
'''


def wait_for(path, timeout=30.0):
    deadline = time.time() + timeout
    while not pathlib.Path(path).exists():
        if time.time() > deadline:
            raise AssertionError(f"{path} が {timeout}s 以内に現れなかった")
        time.sleep(0.02)


class Box:
    """`root/scripts` (plan.sh の隔離コピー) + `root/queue` + `root/registry` + `root/hold`。"""

    def __init__(self, root, tasks=("t001",), *, seed=True, seed_extra=None):
        self.root = pathlib.Path(root)
        self.plan_sh = copy_plan_tree(self.root)
        self.queue = self.root / "queue"
        (self.root / "registry").mkdir(exist_ok=True)
        (self.root / "hold").mkdir(exist_ok=True)
        (self.root / "scripts" / "git-helpers.sh").write_text(HELPER)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "C.UTF-8",
                    "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(self.root),
                    "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}
        if seed:
            self.queue.mkdir(parents=True, exist_ok=True)
            sc.seed(self.queue, "pull")            # m-crash / t001 (pending)
            with store.transaction(self.queue, op="seed", actor="test") as t:
                for tid in tasks:
                    if tid != "t001":
                        t.write_card(MISSION, tid, sc.card(tid), sc.body())
                if seed_extra:
                    seed_extra(t)

    # -- plan.sh ----------------------------------------------------------------
    def argv(self, *args):
        return [str(self.plan_sh), *args]

    def proc_env(self, agent=None):
        env = dict(self.env)
        if agent:
            env["AGENT_NAME"] = agent
        return env

    def plan(self, *args, agent=None, timeout=120):
        return subprocess.run(self.argv(*args), env=self.proc_env(agent), capture_output=True, text=True,
                              timeout=timeout)

    def popen(self, *args, agent=None):
        """独立プロセス (自分のセッション = 木ごと殺せる)。"""
        return subprocess.Popen(self.argv(*args), env=self.proc_env(agent), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    def pull(self, agent="Ren", task="t001", *, auto=False, skills="code"):
        args = ["pull", "--agent", agent, "--skills", skills, "--mission", MISSION]
        if not auto:
            args += ["--task", task]
        return self.plan(*args)

    def reset(self, tid="t001"):
        p = self.plan("update", tid, "--status", "pending", "--reset", "--mission", MISSION, agent="Director")
        assert p.returncode == 0, p.stderr

    # -- 読み取り ----------------------------------------------------------------
    def card(self, tid="t001"):
        meta, _ = cards.parse_frontmatter((self.queue / "missions" / MISSION / "tasks" / f"{tid}.md").read_text(),
                                          source=tid)
        return meta

    def card_text(self, tid="t001"):
        return (self.queue / "missions" / MISSION / "tasks" / f"{tid}.md").read_text()

    def record(self, xid):
        return json.loads((self.queue / "missions" / MISSION / "executions" / f"{xid}.json").read_text())

    def records(self):
        d = self.queue / "missions" / MISSION / "executions"
        return sorted(p.name for p in d.glob("ex-*.json")) if d.is_dir() else []

    def slot(self, agent):
        p = self.queue / "assignments" / agent
        return p.read_text().strip() if p.exists() else None

    def identity(self, agent):
        p = self.queue / "assignments" / f"{agent}.identity"
        return json.loads(p.read_text()) if p.exists() else None

    def audit_rows(self):
        rows = []
        for f in sorted((self.queue / "audit").glob("transitions-*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        return rows

    def snapshot(self, *, with_audit=False):
        """queue のバイト列 (`.lock` と準備ロックは除く。中身が空で、取るだけで作られる)。"""
        snap = {}
        for p in sorted(self.queue.rglob("*")):
            rel = p.relative_to(self.queue).as_posix()
            if not p.is_file() or p.name == ".lock" or p.name.endswith(".prepare.lock"):
                continue
            if rel.startswith("audit/") and not with_audit:
                continue
            snap[rel] = p.read_bytes()
        return snap

    def env_file(self, tid="t001"):
        slug = self.card(tid).get("task_slug") or f"task-{tid}"
        p = self.root / ".claude" / "worktrees" / MISSION / f"{tid}-{slug}" / ".crewvia-env"
        return p.read_text() if p.exists() else None

    def worktree_dir(self, tid="t001"):
        slug = self.card(tid).get("task_slug") or f"task-{tid}"
        return self.root / ".claude" / "worktrees" / MISSION / f"{tid}-{slug}"

    def store_check(self):
        p = self.plan("store-check", "--mission", MISSION)
        return p.stdout + p.stderr

    def clone(self, dest):
        shutil.copytree(self.root, dest, symlinks=True)
        return Box(dest, seed=False)

    # -- 合図 (hold) -------------------------------------------------------------
    def hold(self, tid="t001", *, cmd=None, fail=False, block=True):
        h = self.root / "hold"
        if block:
            (h / f"{tid}.enabled").write_text("")
        if cmd is not None:
            (h / f"{tid}.cmd").write_text(cmd)
        if fail:
            (h / f"{tid}.fail").write_text("")

    def reached(self, tid="t001", timeout=30.0):
        wait_for(self.root / "hold" / f"{tid}.reached", timeout)

    def go(self, tid="t001"):
        (self.root / "hold" / f"{tid}.go").write_text("")

    def unhold(self, tid="t001"):
        for suffix in ("enabled", "reached", "go", "cmd", "fail", "parent", "invocations"):
            (self.root / "hold" / f"{tid}.{suffix}").unlink(missing_ok=True)

    # -- namespace (fork した子の中で本物の cmd_pull を呼ぶ) ---------------------------
    def namespace(self):
        return harness.load_plan_namespace(self.plan_sh, str(self.queue), str(self.root))


def last_error_code(stderr):
    """stderr の**最後の行**の固定形式 (execution.md §4.4)。無ければ None。"""
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    m = re.fullmatch(r"\[plan\.sh\] error_code=([A-Z_]+)", lines[-1]) if lines else None
    return m.group(1) if m else None


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=30)


class ForkedPull:
    """fork した子で本物の `cmd_pull(argv)` を走らせている最中のもの (`wait()` で結果を受け取る)。"""

    def __init__(self, pid, rfd):
        self.pid, self._r = pid, rfd

    def wait(self):
        _pid, st = os.waitpid(self.pid, 0)
        data = os.read(self._r, 64).decode()
        os.close(self._r)
        calls_s, _, ok_s = (data or "0 0").partition(" ")
        return os.WIFSIGNALED(st) and os.WTERMSIG(st) == signal.SIGKILL, int(calls_s), bool(int(ok_s or 0))


def fork_start(box, patch, argv, agent, *, fault_kill_at=None):
    """fork した子で本物の `cmd_pull(argv)` を呼ぶ (すぐ戻る。スレッドは使わない — fork とスレッドを混ぜない)。
    `patch(ns)` で名前空間の協力者を差し替えられる (止める・落とす地点を作る。本番のコードにフックは足さない)。
    `fault_kill_at=k` は lib の書き込み点の k 番目で SIGKILL。"""
    ns = box.namespace()
    ns["SUBCOMMAND"] = "pull"
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            os.environ.update(box.proc_env(agent))
            calls = [0]

            def hook(_point, _path):
                calls[0] += 1
                if fault_kill_at is not None and calls[0] == fault_kill_at:
                    os.kill(os.getpid(), signal.SIGKILL)

            store.FAULT_HOOK = hook
            if patch is not None:
                patch(ns)
            ok = False
            try:
                ns["cmd_pull"](argv)
                ok = True
            except SystemExit:
                pass
            os.write(w, f"{calls[0]} {int(ok)}".encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    return ForkedPull(pid, r)


def fork_run(box, patch, argv, agent, *, fault_kill_at=None):
    """`fork_start` して終わるまで待つ。戻り値: (SIGKILL で落ちたか, 呼ばれた書き込み点の数, 正常終了か)。"""
    return fork_start(box, patch, argv, agent, fault_kill_at=fault_kill_at).wait()


def die_here():
    os.kill(os.getpid(), signal.SIGKILL)
