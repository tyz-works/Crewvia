"""S4 (vNext 01a / t016): card を正本・assignment / .identity を projection とし、**ロックを取ったとき**に食い違いを作り直す。

受入条件 (t016):

1. **赤の実証**: pull / done (と reset / needs-director / add / 退避) の各書き込みの途中で落とした状態を作り、
   次の `plan.sh` の呼び出しで一貫した状態へ収束する (修正前は食い違いが残る)。crash 注入で 20 回反復。
2. **進行中の正しい pull / done を巻き戻さない** (並行する独立プロセスで): 回復は何も直してはいけない。
3. 持ち越し (S2 の Codex #257 3 巡目 P2 ×2): `ASSIGNMENT_HOLDING_STATUSES` の全部で worker なし・identity の欠け / 壊れを
   finding にする (report-only)。
4. Director 追記: archive の中の所有 (in_progress の card を持ったまま退避された mission)。

やり方: plan.sh の python 本体を**本物のまま**名前空間に読み込み (`tests/task_graph_publisher_harness.py`)、fork した子の中で
`lib_state_store.FAULT_HOOK` の k 番目に自分へ SIGKILL を送る。その後に**本物の plan.sh (subprocess)** を打って収束を見る。
設計: `knowledge/state-store.md` §2.2 / §2.3 / §2.5 / §2.6。
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import shutil
import signal
import subprocess
import sys
import threading

import pytest

import state_store_scenarios as sc       # scripts/ を sys.path に足す
import lib_state_store as store
import lib_task_cards as cards
import lib_task_status as status_lib
from fixture_tree import copy_plan_tree
import task_graph_publisher_harness as harness

HOLDING = status_lib.ASSIGNMENT_HOLDING_STATUSES


# ---------------------------------------------------------------------------
# 隔離した plan.sh と、queue の読み取り
# ---------------------------------------------------------------------------

class Box:
    """`root/scripts` (plan.sh の隔離コピー) + `root/queue` + `root/registry`。本番には触れない。"""

    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)
        self.plan = copy_plan_tree(self.root)
        self.queue = self.root / "queue"
        self.queue.mkdir(exist_ok=True)
        (self.root / "registry").mkdir(exist_ok=True)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "C.UTF-8",
                    "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(self.root),
                    "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}

    def run(self, *args, agent=None, expect=0, stdin=None):
        env = dict(self.env)
        if agent:
            env["AGENT_NAME"] = agent
        p = subprocess.run([str(self.plan), *args], env=env, capture_output=True, text=True,
                           timeout=120, input=stdin)
        if expect is not None:
            assert p.returncode == expect, f"plan.sh {args}: rc={p.returncode}\n{p.stdout}\n{p.stderr}"
        return p

    def clone(self, dest: pathlib.Path) -> "Box":
        shutil.copytree(self.root, dest, symlinks=True)
        return Box(dest)

    def namespace(self) -> dict:
        return harness.load_plan_namespace(self.plan, str(self.queue), str(self.root))

    @property
    def slug(self) -> str:
        return sorted(p.name for p in (self.queue / "missions").iterdir())[0]

    # -- 読み取り ---------------------------------------------------------------
    def cards(self, base="missions"):
        out = {}
        for mdir in sorted((self.queue / base).glob("*")):
            for f in sorted((mdir / "tasks").glob("t*.md")):
                meta, body = cards.parse_frontmatter(f.read_text(), source=str(f))
                out[(mdir.name, f.stem)] = meta
        return out

    def card(self, tid, slug=None):
        return self.cards()[(slug or self.slug, tid)]

    def slots(self):
        adir = self.queue / "assignments"
        found = {}
        if adir.is_dir():
            for f in sorted(adir.iterdir()):
                if f.name.endswith((".identity", ".restarting")) or f.name.startswith("."):
                    continue
                found[f.name] = f.read_text().strip()
        return found

    def identity(self, agent):
        return json.loads((self.queue / "assignments" / f"{agent}.identity").read_text())

    def audit_rows(self):
        rows = []
        for f in sorted((self.queue / "audit").glob("transitions-*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        return rows

    def snapshot(self):
        """queue 全体のバイト列 (`.lock` と監査ログは除く)。"""
        snap = {}
        for p in sorted(self.queue.rglob("*")):
            rel = p.relative_to(self.queue)
            if p.is_file() and rel.parts[0] != "audit" and p.name != ".lock":
                snap[str(rel)] = p.read_bytes()
        return snap


def problems(box: Box) -> list[str]:
    """card = 正本と projection の食い違いの一覧 (設計 §2.1 の projection の定義そのもの)。空 = 一貫している。"""
    out = []
    by = box.cards()
    slots = box.slots()
    for (slug, tid), meta in by.items():
        worker = meta.get("worker")
        if meta.get("status") == "in_progress" and worker:
            if slots.get(worker) != f"{slug}:{tid}":
                out.append(f"in_progress_without_slot:{slug}/{tid}")
                continue
            try:
                ident = box.identity(worker)
            except (OSError, ValueError):
                out.append(f"identity_missing_or_broken:{worker}")
                continue
            if str(ident.get("started_at")) != str(meta.get("started_at")):
                out.append(f"identity_generation_mismatch:{worker}")
    for worker, text in slots.items():
        slug, _, tid = text.partition(":")
        meta = by.get((slug, tid))
        if meta is None or meta.get("worker") != worker or meta.get("status") not in HOLDING:
            out.append(f"orphan_slot:{worker}")
    return out


# ---------------------------------------------------------------------------
# 強制終了 (fork した子で FAULT_HOOK の k 番目に SIGKILL)
# ---------------------------------------------------------------------------

def _kill_child_at(kill_at: int, action) -> tuple[bool, int]:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            calls = [0]

            def hook(_point, _path):
                calls[0] += 1
                if calls[0] == kill_at:
                    os.kill(os.getpid(), signal.SIGKILL)

            store.FAULT_HOOK = hook
            try:
                action()
            except SystemExit:
                pass
            os.write(w, str(calls[0]).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    _pid, st = os.waitpid(pid, 0)
    data = os.read(r, 32)
    os.close(r)
    return os.WIFSIGNALED(st) and os.WTERMSIG(st) == signal.SIGKILL, int(data or 0)


def crash_command(box: Box, k: int, argv: list[str], agent: str | None = None) -> tuple[bool, int]:
    """`plan.sh <argv>` を `box` の queue に対して走らせ、lib の書き込み点の k 番目で落とす。
    戻り値: (落ちたか, 呼ばれた点の数)。k が点の数を超えれば最後まで走る (= 落ちない)。"""
    ns = box.namespace()
    sub = argv[0]
    ns["SUBCOMMAND"] = sub

    def action():
        if agent:
            os.environ["AGENT_NAME"] = agent
        ns["cmd_" + sub.replace("-", "_")](argv[1:])

    return _kill_child_at(k, action)


# ---------------------------------------------------------------------------
# 種 (seed): 本物の plan.sh で作る。sweep は seed を cp して使う (毎回 init / add をしない)
# ---------------------------------------------------------------------------

def seed_pending(root, tasks=1, extra=()):
    box = Box(root)
    box.run("init", "recover fixture")
    for i in range(tasks):
        box.run("add", f"T{i + 1}", "--skills", "bash", "--deliverable", "none", *extra)
    return box


def seed_running(root, agent="Ren", tasks=2):
    """t001 を agent が pull 済み (card in_progress + assignment + identity)。t002 は t001 に依存。"""
    box = Box(root)
    box.run("init", "recover fixture")
    box.run("add", "T1", "--skills", "bash", "--deliverable", "none")
    for i in range(1, tasks):
        box.run("add", f"T{i + 1}", "--skills", "bash", "--deliverable", "none", "--blocked-by", "t001")
    box.run("pull", "--agent", agent, "--skills", "bash", "--task", "t001")
    assert problems(box) == []
    return box


def seed_pr_chain(root, agent="Ren"):
    """t001 (deliverable: pr・agent が pull 済み) → t002 (codex-review) と t003 (review)。"""
    box = Box(root)
    box.run("init", "recover fixture")
    box.run("add", "T1", "--skills", "bash", "--deliverable", "pr")
    box.run("add", "T2", "--skills", "codex-review", "--blocked-by", "t001", "--deliverable", "none")
    box.run("add", "T3", "--skills", "review", "--blocked-by", "t001", "--deliverable", "none")
    box.run("update", "t002", "--status", "blocked", expect=None)
    box.run("pull", "--agent", agent, "--skills", "bash", "--task", "t001")
    return box


def sweep(base: pathlib.Path, seed_box: Box, argv, agent=None, max_points=60):
    """k = 1, 2, ... の各点で `argv` を落とした状態の Box を順に返す (落ちなくなったら終わる)。
    戻り値は `[(k, box)]`。呼ばれた点の数の下限は呼び出し側が assert する。"""
    boxes = []
    for k in range(1, max_points):
        box = seed_box.clone(base / f"k{k:02d}")
        killed, calls = crash_command(box, k, argv, agent)
        if not killed:
            assert calls == k - 1
            return boxes, k - 1
        boxes.append((k, box))
    raise AssertionError("点が多すぎる — 注入口が止まらない")


# ---------------------------------------------------------------------------
# 1. pull の各点で落ちる
# ---------------------------------------------------------------------------

PULL_NEXT = {
    "same-task": (["pull", "--agent", "Ren", "--skills", "bash", "--task", "t001"], None),
    "auto": (["pull", "--agent", "Ren", "--skills", "bash"], None),
    "update-other-field": (["update", "t001", "--priority", "medium"], None),
}


def test_pull_killed_at_every_write_point_converges_on_the_next_plan_sh_call(tmp_path):
    seed = seed_pending(tmp_path / "seed")
    boxes, points = sweep(tmp_path, seed, ["pull", "--agent", "Ren", "--skills", "bash", "--task", "t001"])
    assert points >= 15, f"落とせる点が {points} 個しか無い — 注入口が壊れている"
    diverged_before = 0
    for k, box in boxes:
        before = problems(box)
        diverged_before += bool(before)
        for name, (argv, agent) in PULL_NEXT.items():
            trial = box.clone(tmp_path / f"k{k:02d}-{name}")
            trial.run(*argv, agent=agent, expect=None)
            assert problems(trial) == [], f"k={k} next={name}: {problems(trial)} (before: {before})"
            # 回復した後も「pull が起きたか / 起きなかったか」のどちらかで、card が壊れていない
            assert trial.card("t001")["status"] in ("pending", "in_progress")
    # 陽性対照: 落とした点のうち、回復前に食い違いが実際に残っていた点がある (テストが空回りしていない)
    assert diverged_before >= 2, "どの点でも食い違いが作れていない — 注入が効いていない"


# ---------------------------------------------------------------------------
# 2. done / fail / needs-director / reset の各点で落ちる
# ---------------------------------------------------------------------------

DONE = ["done", "t001", "finished", "--no-pr", "no pr in this fixture"]
DONE_NEXT = {
    "same-args": (DONE, "Ren"),
    "reset": (["update", "t001", "--reset"], None),
    "needs-director": (["needs-director", "t001", "why"], "Ren"),
    "fail": (["fail", "t001", "--no-head", "fixture"], "Ren"),
}


def test_done_killed_at_every_write_point_converges_whatever_comes_next(tmp_path):
    seed = seed_running(tmp_path / "seed")
    boxes, points = sweep(tmp_path, seed, DONE, agent="Ren")
    assert points >= 20, f"落とせる点が {points} 個しか無い — 注入口が壊れている"
    diverged_before = 0
    for k, box in boxes:
        before = problems(box)
        diverged_before += bool(before)
        for name, (argv, agent) in DONE_NEXT.items():
            trial = box.clone(tmp_path / f"k{k:02d}-{name}")
            trial.run(*argv, agent=agent, expect=None)
            assert problems(trial) == [], f"k={k} next={name}: {problems(trial)} (before: {before})"
    # D3 の後・D4 の前 (card は done・枠が残る) を作れている
    assert diverged_before >= 1, "done の途中で落ちた食い違いが 1 つも作れていない"


RESET = ["update", "t001", "--reset"]
RESET_NEXT = {
    "same-args": (RESET, None),
    "other-worker-pulls": (["pull", "--agent", "Haruto", "--skills", "bash", "--task", "t001"], None),
    "old-owner-reports-done": (DONE, "Ren"),
    "old-owner-pulls-again": (["pull", "--agent", "Ren", "--skills", "bash", "--task", "t001"], None),
}


def test_reset_killed_between_card_and_assignment_is_found_by_reverse_lookup(tmp_path):
    """card (pending・worker null) を書いた後・枠を撤去する前で落ちると、card から旧所有者が分からない。
    どの次の操作でも、逆引き (枠のうちこの card を指すもの) で旧所有者の枠が片付く (設計 §2.5)。"""
    seed = seed_running(tmp_path / "seed")
    boxes, points = sweep(tmp_path, seed, RESET)
    assert points >= 12
    stuck_before = 0
    for k, box in boxes:
        before = problems(box)
        stuck_before += any(p.startswith("orphan_slot") for p in before)
        for name, (argv, agent) in RESET_NEXT.items():
            trial = box.clone(tmp_path / f"k{k:02d}-{name}")
            trial.run(*argv, agent=agent, expect=None)
            assert problems(trial) == [], f"k={k} next={name}: {problems(trial)} (before: {before})"
    assert stuck_before >= 1, "「card は reset 済み・旧所有者の枠が残る」を作れていない"


NEEDS_DIRECTOR = ["needs-director", "t001", "fixture reason"]
NEEDS_DIRECTOR_NEXT = {
    "same-args": (NEEDS_DIRECTOR, "Ren"),
    "reset": (RESET, None),
    "done": (DONE, "Ren"),
}


def test_needs_director_killed_between_card_and_assignment_converges(tmp_path):
    seed = seed_running(tmp_path / "seed")
    boxes, points = sweep(tmp_path, seed, NEEDS_DIRECTOR, agent="Ren")
    assert points >= 12
    stuck_before = 0
    for k, box in boxes:
        before = problems(box)
        stuck_before += bool(before)
        for name, (argv, agent) in NEEDS_DIRECTOR_NEXT.items():
            trial = box.clone(tmp_path / f"k{k:02d}-{name}")
            trial.run(*argv, agent=agent, expect=None)
            assert problems(trial) == [], f"k={k} next={name}: {problems(trial)} (before: {before})"
    assert stuck_before >= 1


# ---------------------------------------------------------------------------
# 3. 20 回反復: 場面 × 落とす点 × 次の操作 を seed 固定の乱数で選ぶ
# ---------------------------------------------------------------------------

def test_random_crash_injection_converges_20_times(tmp_path):
    seeds = {
        "pull": (seed_pending(tmp_path / "seed-pull"), ["pull", "--agent", "Ren", "--skills", "bash", "--task", "t001"],
                 None, PULL_NEXT),
        "done": (seed_running(tmp_path / "seed-done"), DONE, "Ren", DONE_NEXT),
        "reset": (seed_running(tmp_path / "seed-reset"), RESET, None, RESET_NEXT),
        "needs-director": (seed_running(tmp_path / "seed-nd"), NEEDS_DIRECTOR, "Ren", NEEDS_DIRECTOR_NEXT),
    }
    repaired = 0
    for i in range(20):
        rng = random.Random(1000 + i)
        scenario = sorted(seeds)[i % len(seeds)]
        seed, argv, agent, nexts = seeds[scenario]
        box = seed.clone(tmp_path / f"it{i:02d}")
        killed, points = crash_command(box, 10 ** 6, argv, agent)          # 点の数を数える (落とさない)
        assert not killed and points >= 10
        box = seed.clone(tmp_path / f"it{i:02d}-crashed")
        k = rng.randint(1, points)
        killed, _ = crash_command(box, k, argv, agent)
        assert killed, f"iteration {i}: k={k}/{points} で落ちなかった"
        name = rng.choice(sorted(nexts))
        nargv, nagent = nexts[name]
        box.run(*nargv, agent=nagent, expect=None)
        assert problems(box) == [], f"iteration {i} ({scenario}, k={k}/{points}, next={name}): {problems(box)}"
        repaired += sum(1 for r in box.audit_rows() if str(r["result"]).startswith("repaired:"))
        # 回復の行は固定の形だけ (Result・理由の本文は出ない)
        for r in box.audit_rows():
            # 01c E3: 落ちた done が既にコミットしていた後の done の再実行は、遷移の拒否の行 (`refused:`。何も書かない) を残す
            assert r["result"] == "ok" or r["result"].startswith(("repaired:", "reported:", "refused:")), r
    assert repaired >= 1, "20 回のどこでも回復が走っていない — 場面の選び方が空回りしている"


# ---------------------------------------------------------------------------
# 4. 進行中の正しい pull / done を巻き戻さない (並行する独立プロセス)
# ---------------------------------------------------------------------------

def test_concurrent_recovery_never_rewrites_a_correct_in_flight_transaction(tmp_path):
    """4 人の Worker が pull → done を繰り返す間、別プロセスが同じ card を名指しする plan.sh (= 回復が走る)
    を打ち続ける。何も落ちていないので、**回復は 1 件も修復してはいけない** (修復 = 進行中の正しい遷移の巻き戻し)。"""
    box = Box(tmp_path / "c")
    box.run("init", "concurrent fixture")
    workers = ["Ren", "Haruto", "Mika", "Sora"]
    per_worker = 3
    for i in range(len(workers) * per_worker):
        box.run("add", f"T{i + 1}", "--skills", "bash", "--deliverable", "none")
    stop = threading.Event()
    errors: list[str] = []

    def worker(name, ids):
        for tid in ids:
            p = box.run("pull", "--agent", name, "--skills", "bash", "--task", tid, agent=name, expect=None)
            if p.returncode != 0:
                errors.append(f"pull {name} {tid}: {p.stderr}")
                return
            p = box.run("done", tid, "ok", "--no-pr", "fixture", agent=name, expect=None)
            if p.returncode != 0:
                errors.append(f"done {name} {tid}: {p.stderr}")
                return

    def hammer():
        i = 0
        while not stop.is_set():
            tid = f"t{i % (len(workers) * per_worker) + 1:03d}"
            box.run("update", tid, "--priority", "medium" if i % 2 else "high", expect=None)
            box.run("reap-orphan-assignment", "Nobody", "--no-wait", expect=None)
            box.run("store-check", expect=None)
            i += 1

    threads = [threading.Thread(target=worker, args=(w, [f"t{j * len(workers) + n + 1:03d}" for j in range(per_worker)]))
               for n, w in enumerate(workers)]
    h = threading.Thread(target=hammer)
    h.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    stop.set()
    h.join(timeout=120)
    assert errors == []
    assert all(m["status"] == "done" for m in box.cards().values())
    assert box.slots() == {}
    assert problems(box) == []
    repaired = [r for r in box.audit_rows() if str(r["result"]).startswith("repaired:")]
    assert repaired == [], f"回復が進行中の正しい遷移を修復した: {repaired}"
    # 陽性対照: 回復の入口は本当に走っている (どの pull / done も名指しの card で回復を通る)。監査ログの本体の行は
    # pull が 2 行 (01c E2: reserve の行と start の行。今までは 1 行) + done が 1 行 = 1 回の pull・done の組で 3 行
    ops = [r["op"] for r in box.audit_rows() if r["op"] in ("pull", "done")]
    assert ops.count("pull") == len(workers) * per_worker * 2 and ops.count("done") == len(workers) * per_worker
    assert len(ops) == len(workers) * per_worker * 3


def test_recovery_does_not_remove_a_successors_assignment(tmp_path):
    """同じ名前 (Ren) が同じ card を pull し直した後 (世代 G2)、名指しの card を打つ全コマンドの回復は枠を消さない。"""
    box = seed_running(tmp_path / "s")
    g1 = box.identity("Ren")["started_at"]
    box.run("update", "t001", "--reset")
    box.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    g2 = box.identity("Ren")["started_at"]
    assert g1 != g2
    box.run("update", "t001", "--priority", "medium")
    assert box.slots() == {"Ren": f"{box.slug}:t001"}
    assert box.identity("Ren")["started_at"] == g2
    assert problems(box) == []
    assert [r for r in box.audit_rows() if r["op"] == "recover"] == []


# ---------------------------------------------------------------------------
# 5. done の順序 (D0 / D1 / D2 / D3) と、違う番号での再実行
# ---------------------------------------------------------------------------

def _pr_states(box: Box):
    c = box.cards()
    s = box.slug
    return c[(s, "t001")], c[(s, "t002")], c[(s, "t003")]


def test_done_with_pr_never_leaves_a_dependent_number_the_card_does_not_carry(tmp_path):
    """`done --pr 123` を各点で落とす。**派生値 (依存先の pr_number) は、正本 (自分の card) が同じ番号を持った後にしか
    書かれない**、かつ「card は done なのに依存先に番号が無い」状態は作られない (旧順 = done の後に伝播 は後者を作った)。"""
    seed = seed_pr_chain(tmp_path / "seed")
    argv = ["done", "t001", "finished", "--pr", "123"]
    boxes, points = sweep(tmp_path, seed, argv, agent="Ren")
    assert points >= 25
    for k, box in boxes:
        t1, t2, t3 = _pr_states(box)
        for dep in (t2, t3):
            if dep.get("pr_number") is not None:
                assert t1.get("pr_number") == dep["pr_number"], f"k={k}: 派生値が正本より先に書かれた"
        if t1["status"] == "done":
            assert t2.get("pr_number") == 123 and t3.get("pr_number") == 123, f"k={k}: done なのに伝播が無い"
    # 番号の永続化 (D1) だけ済んで依存先が空の点がある = 順序が実際に D1 → D2 → D3
    assert any(_pr_states(b)[0].get("pr_number") == 123 and _pr_states(b)[0]["status"] != "done"
               for _k, b in boxes)


def test_done_after_a_crash_with_a_different_pr_number_is_refused_and_leaves_one_number(tmp_path):
    seed = seed_pr_chain(tmp_path / "seed")
    boxes, points = sweep(tmp_path, seed, ["done", "t001", "finished", "--pr", "123"], agent="Ren")
    checked = 0
    for k, box in boxes:
        t1, t2, t3 = _pr_states(box)
        if t1["status"] == "done":
            continue
        trial = box.clone(tmp_path / f"k{k:02d}-other-number")
        p = trial.run("done", "t001", "finished", "--pr", "456", agent="Ren", expect=None)
        n1, n2, n3 = _pr_states(trial)
        if t1.get("pr_number") == 123 or t2.get("pr_number") == 123 or t3.get("pr_number") == 123:
            checked += 1
            assert p.returncode != 0, f"k={k}: 違う番号の再実行が通った (正本 123・派生値 123 が残っているのに)"
            assert n1["status"] != "done"
        else:
            assert p.returncode == 0, p.stderr
        # どちらの場合も、番号は 1 つに揃っている
        nums = {x.get("pr_number") for x in (n1, n2, n3) if x.get("pr_number") is not None}
        assert len(nums) <= 1, f"k={k}: 番号が割れた: {nums}"
        assert problems(trial) == []
    assert checked >= 2, "番号が途中まで書かれた点を作れていない"


def test_done_is_refused_when_a_dependent_carries_a_different_number_and_writes_nothing(tmp_path):
    """D0 (a): Director が自分の card の番号を書き換えた (やり直しの PR)。依存先には古い番号が残る。"""
    box = seed_pr_chain(tmp_path / "s")
    box.run("done", "t001", "finished", "--pr", "123", agent="Ren", expect=0)
    box.run("update", "t001", "--reset")
    box.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    box.run("update", "t001", "--pr-number", "456")
    before = box.snapshot()
    p = box.run("done", "t001", "finished again", "--pr", "456", agent="Ren", expect=3)
    assert "pr_number=123" in p.stderr and "update t002 --pr-number 456" in p.stderr, p.stderr
    assert box.snapshot() == before, "拒否は 1 バイトも書かない"
    # 出口: 依存先を直せば通る
    box.run("update", "t002", "--pr-number", "456")
    box.run("update", "t003", "--pr-number", "456")
    box.run("done", "t001", "finished again", "--pr", "456", agent="Ren", expect=0)
    t1, t2, t3 = _pr_states(box)
    assert (t1["status"], t1["pr_number"], t2["pr_number"], t3["pr_number"]) == ("done", 456, 456, 456)


def test_done_no_pr_is_refused_when_the_card_already_carries_a_number(tmp_path):
    """D0 (b): 自分の card に pr_number があるのに --no-pr。伝わった番号と食い違ったまま確定させない。"""
    box = seed_pr_chain(tmp_path / "s")
    box.run("update", "t001", "--pr-number", "123")
    before = box.snapshot()
    p = box.run("done", "t001", "finished", "--no-pr", "no pr", agent="Ren", expect=3)
    assert "pr_number=123" in p.stderr and "--pr-number null" in p.stderr, p.stderr
    assert box.snapshot() == before
    box.run("update", "t001", "--pr-number", "null")                     # 出口
    box.run("done", "t001", "finished", "--no-pr", "no pr", agent="Ren", expect=0)


# ---------------------------------------------------------------------------
# 6. add (R-3) と 退避 (R-4)
# ---------------------------------------------------------------------------

def test_add_killed_between_card_and_next_task_id_does_not_overwrite_the_card_on_the_next_add(tmp_path):
    seed = seed_pending(tmp_path / "seed", tasks=1)
    boxes, points = sweep(tmp_path, seed, ["add", "SECOND", "--skills", "bash", "--deliverable", "none"])
    assert points >= 12
    leftover_cases = 0
    for k, box in boxes:
        titles_before = {tid: m["title"] for (_s, tid), m in box.cards().items()}
        box.run("add", "THIRD", "--skills", "bash", "--deliverable", "none")
        titles_after = {tid: m["title"] for (_s, tid), m in box.cards().items()}
        # 前の card は 1 枚も上書きされていない (旧: 同じ tNNN を黙って上書きした)
        for tid, title in titles_before.items():
            assert titles_after[tid] == title, f"k={k}: {tid} が上書きされた"
        assert "THIRD" in titles_after.values()
        leftover_cases += "SECOND" in titles_before.values() and len(titles_before) == 2 and \
            any(r["result"] == "repaired:R-3" for r in box.audit_rows())
    assert leftover_cases >= 1, "「card は書けたが next_task_id が進んでいない」点を作れていない"


def test_a_mission_moved_out_but_still_active_is_dropped_from_state_by_the_next_call(tmp_path):
    seed = seed_pending(tmp_path / "seed", tasks=1)
    seed.run("init", "second mission", "--mission", "m-second")
    slug = seed.slug
    boxes, points = sweep(tmp_path, seed, ["archive", slug])
    assert points >= 6
    stale = 0
    for k, box in boxes:
        state = (box.queue / "state.yaml").read_text()
        moved = (box.queue / "archive" / slug).is_dir() and not (box.queue / "missions" / slug).exists()
        stale += moved and slug in state
        for argv in (["init", "third mission", "--mission", "m-third"], ["archive", slug]):
            trial = box.clone(tmp_path / f"k{k:02d}-{argv[0]}")
            trial.run(*argv, expect=None)
            if moved:
                assert slug not in (trial.queue / "state.yaml").read_text(), f"k={k} {argv[0]}: active に残った"
    assert stale >= 1, "「退避済みなのに state.yaml が元の名前を指す」点を作れていない"


# ---------------------------------------------------------------------------
# 7. reap-orphan-assignment の集合 = R-2 の集合 / archive の中の所有 / store-check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,expect_reaped", [
    ("needs_director", True),          # S4 で足した: 撤去するコマンド自身が書く status
    ("pending", True),                 # worker の無い pending (reset の残骸)
    ("done", True),
    ("in_progress", False),
    ("blocked", False),
    ("ready_for_verification", False),
])
def test_reap_orphan_assignment_uses_the_same_set_as_the_recovery(tmp_path, state, expect_reaped):
    box = seed_running(tmp_path / "s", tasks=1)
    meta_path = box.queue / "missions" / box.slug / "tasks" / "t001.md"
    text = meta_path.read_text()
    new_worker = "null" if state == "pending" else "Ren"
    text = text.replace("status: in_progress", f"status: {state}").replace("worker: Ren", f"worker: {new_worker}")
    if state == "blocked":
        text = text.replace(f"status: {state}", f"status: {state}\nblocked_reason: fixture")
    if state == "needs_director":
        text = text.replace(f"status: {state}", f"status: {state}\nneeds_director_reason: fixture")
    meta_path.write_text(text)
    p = box.run("reap-orphan-assignment", "Ren", "--no-wait", expect=None)
    if expect_reaped:
        assert p.returncode == 0, p.stderr
        assert box.slots() == {}
    else:
        assert p.returncode == 3, p.stderr
        assert box.slots() == {"Ren": f"{box.slug}:t001"}


def _make_archived_owner(box: Box, worker="Ren", slug="m-old", tid="t001", status="in_progress"):
    """archive/ に `worker` が in_progress の card を持つ mission を置く (退避は status を検査しない)。"""
    src = box.queue / "missions"
    box.run("init", "old mission", "--mission", slug)
    box.run("add", "OLD", "--skills", "bash", "--deliverable", "none", "--mission", slug)
    box.run("update", tid, "--status", status, "--worker", worker, "--mission", slug)
    box.run("archive", slug)
    assert (box.queue / "archive" / slug).is_dir() and not (src / slug).exists()


def _lose_the_slot(box: Box, worker="Ren"):
    (box.queue / "assignments" / worker).unlink()


def test_r1_does_not_write_when_the_owner_also_has_an_in_progress_card_in_the_archive(tmp_path):
    """Director 追記: archive は status を検査しないので、A の in_progress card が退避された mission に残りうる。
    missions/ だけを数えると残る 1 枚を A の全てと読んで枠を書く (重複を見逃す)。archive/ も所有の証拠に含める。"""
    box = seed_running(tmp_path / "s", tasks=1)
    _make_archived_owner(box)
    _lose_the_slot(box)
    p = box.run("update", "t001", "--priority", "medium", expect=0)
    assert not (box.queue / "assignments" / "Ren").exists(), "重複を見逃して枠を書いた"
    assert "duplicate_owner" in p.stderr and "archive/m-old/t001" in p.stderr, p.stderr
    rows = [r for r in box.audit_rows() if r["op"] == "recover"]
    assert [r["result"] for r in rows] == ["reported:duplicate_owner"]
    # 対照: archive に持ち主の card が無ければ書く (R-1)
    box2 = seed_running(tmp_path / "s2", tasks=1)
    _lose_the_slot(box2)
    box2.run("update", "t001", "--priority", "medium", expect=0)
    assert box2.slots() == {"Ren": f"{box2.slug}:t001"}
    assert [r["result"] for r in box2.audit_rows() if r["op"] == "recover"] == ["repaired:R-1"]


def test_r1_does_not_write_when_an_archived_card_of_the_owner_is_unreadable(tmp_path):
    box = seed_running(tmp_path / "s", tasks=1)
    _make_archived_owner(box, worker="Kai", status="done")
    (box.queue / "archive" / "m-old" / "tasks" / "t001.md").write_text("not a card\n")
    _lose_the_slot(box)
    p = box.run("update", "t001", "--priority", "medium", expect=0)
    assert not (box.queue / "assignments" / "Ren").exists()
    assert "owner_unprovable" in p.stderr, p.stderr


def test_recovery_removes_an_orphan_slot_that_points_into_the_archive(tmp_path):
    """Director が手で退避した「通常 Worker の孤児 assignment」(done 後も残った・退避された mission の task を指す)。
    枠が指す card を missions/ に見つけられなくても、archive/ の card が決着済みなら R-2 が消す。"""
    box = seed_running(tmp_path / "s", tasks=1)
    box.run("done", "t001", "x", "--no-pr", "x")                      # 01c E3: AGENT_NAME が無くても card の worker (Ren) の枠が外れる
    assert box.slots() == {}
    (box.queue / "assignments" / "Ren").write_text(f"{box.slug}:t001\n")   # done の後に残った枠 (旧コード・回復前) を再現する
    assert box.slots() == {"Ren": f"{box.slug}:t001"}
    slug = box.slug
    box.run("archive", slug)
    assert box.slots() == {"Ren": f"{slug}:t001"}
    box.run("init", "next mission", "--mission", "m-next")
    box.run("add", "N", "--skills", "bash", "--deliverable", "none", "--mission", "m-next")
    box.run("update", "t001", "--priority", "high", "--mission", "m-next", agent="Ren")   # 呼び出し元の枠 = Ren
    assert box.slots() == {}
    assert [r["result"] for r in box.audit_rows() if r["op"] == "recover"] == ["repaired:R-2"]


def test_store_check_lists_without_writing_and_repairs_nothing(tmp_path):
    box = seed_running(tmp_path / "s", tasks=1)
    _lose_the_slot(box)
    before = box.snapshot()
    audit_before = len(box.audit_rows())
    p = box.run("store-check")
    assert "would-repair:R-1" in p.stdout and "mission=" in p.stdout and "task=t001" in p.stdout, p.stdout
    # 枠の本体だけ失った状態 (= pull が identity の後・assignment の前で落ちた形): R-1 と、本体の無い .identity
    assert "orphan_identity" in p.stdout and "2 finding(s)" in p.stdout, p.stdout
    assert "2 回連続" in p.stdout
    assert box.snapshot() == before and len(box.audit_rows()) == audit_before, "store-check が書いた"
    # 修復は次のロック取得で行われる
    box.run("update", "t001", "--priority", "medium")
    assert problems(box) == []
    assert "0 finding(s)" in box.run("store-check").stdout


def test_store_check_reports_everything_the_recovery_would_not_touch(tmp_path):
    box = seed_running(tmp_path / "s", tasks=2)
    box.run("add", "T3", "--skills", "bash", "--deliverable", "none")
    # 表に無い食い違い: 2 枚の in_progress (Ren) と、worker の無い ready_for_verification
    box.run("update", "t003", "--status", "in_progress", "--worker", "Ren")
    box.run("update", "t002", "--status", "ready_for_verification")
    out = box.run("store-check").stdout
    assert "reported:holding_without_worker" in out and "task=t002" in out, out
    only = box.run("store-check", "--mission", box.slug).stdout
    assert "task=t002" in only


# ---------------------------------------------------------------------------
# 8. S2 から持ち越し: ASSIGNMENT_HOLDING_STATUSES の全部 × {worker なし・identity 欠け・identity 壊れ}
# ---------------------------------------------------------------------------

M = "m-carry"


def _lib_queue(tmp_path, status, worker, identity):
    """lib だけで作る隔離 queue。`identity`: 'ok' / 'missing' / 'broken'。"""
    q = tmp_path / "q"
    q.mkdir()
    with store.transaction(q, op="seed", actor="test") as t:
        t.write_state({"active_missions": [M], "default_mission": M})
        t.write_mission(M, {"title": "m", "slug": M, "status": "in_progress", "created_at": "2026-09-30T00:00:00Z",
                            "completed_at": None, "next_task_id": 2})
        t.write_card(M, "t001", sc.card("t001", status, worker, sc.GEN if worker else None), sc.body())
        if worker:
            t.publish_assignment(worker, M, "t001", sc.GEN)
    ident = q / "assignments" / f"{worker}.identity"
    if worker and identity == "missing":
        ident.unlink()
    elif worker and identity == "broken":
        ident.write_text("{not json")
    return q


@pytest.mark.parametrize("status", sorted(HOLDING))
def test_diagnose_reports_a_holding_card_without_a_worker(tmp_path, status):
    q = _lib_queue(tmp_path, status, None, "ok")
    kinds = sorted(f.kind for f in store.diagnose(q))
    assert kinds and all(k.startswith("reported:") for k in kinds), kinds
    assert any(k in ("reported:in_progress_without_worker", "reported:holding_without_worker") for k in kinds)


@pytest.mark.parametrize("identity", ["missing", "broken"])
@pytest.mark.parametrize("status", sorted(HOLDING))
def test_diagnose_reports_a_broken_identity_for_every_assignment_holding_status(tmp_path, status, identity):
    q = _lib_queue(tmp_path, status, "Ren", identity)
    kinds = [f.kind for f in store.diagnose(q)]
    assert "reported:assignment_unverifiable" in kinds, kinds
    # report-only: 診断は何も書かない・修復は要求しない
    assert not any(k.startswith("would-repair") for k in kinds)


@pytest.mark.parametrize("status", sorted(HOLDING))
def test_diagnose_reports_nothing_for_a_consistent_holding_card(tmp_path, status):
    q = _lib_queue(tmp_path, status, "Ren", "ok")
    assert store.diagnose(q) == []
