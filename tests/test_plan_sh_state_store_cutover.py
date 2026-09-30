"""S3 (vNext 01a / t012): plan.sh の queue への書き込みが `lib_state_store` 経由になったことのテスト。

受入条件 (t012):

1. **監査ログ**: 状態を変える全 subcommand が `queue/audit/transitions-YYYYMMDD.jsonl` に行を出す
   (修正前はログが無い = 赤)。Result・理由・本文は書かない。
2. **書き込みの途中で落ちても元のカードが残る / 親 dir の fsync**: 修正前の `_atomic_write` は親ディレクトリを
   fsync しない。プロセスの kill では再現しない (電源断相当が要る) ので、`os.fsync` / `os.replace` / `os.unlink` を
   記録するスタブで「tmp の fsync → replace → 親 dir の fsync」の**呼び出しの有無と順序**を検出する
   (Director 追記)。kill テストは「どの段で落ちても、元のカードか完全な新カードのどちらかが読める」を固定する。
3. `queue/.lock` が `lib_retirement.queue_transaction` と同じファイル (互いに排他する)。

plan.sh を**本物のまま**名前空間に読み込んで (`tests/task_graph_publisher_harness.py` と同じ方式)、本番の
`with_lock` / `save_task` / `save_mission` / `save_state` / `publish_assignment` / `retire_assignment` を呼ぶ。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys

import pytest

import state_store_scenarios as sc       # scripts/ を sys.path に足す
import lib_retirement
import lib_state_store as store
from fixture_tree import copy_plan_tree
import task_graph_publisher_harness as harness

PLAN_SH_SRC = sc.REPO_ROOT / "scripts" / "plan.sh"
SECRET_RESULT = "SECRET-RESULT-do-not-log-4f2a"
SECRET_REASON = "SECRET-REASON-do-not-log-9c1d"

AUDIT_KEYS = {"ts", "txn_id", "op", "mission", "task", "actor", "pid", "from_status", "to_status",
              "generation", "execution_id", "result", "files"}


# ---------------------------------------------------------------------------
# 隔離した plan.sh
# ---------------------------------------------------------------------------

class Sandbox:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.plan = copy_plan_tree(root)
        self.queue = root / "queue"
        self.queue.mkdir()
        (root / "registry").mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                    "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(root),
                    "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}

    def run(self, *args, agent=None, expect=0, stdin=None):
        env = dict(self.env)
        if agent:
            env["AGENT_NAME"] = agent
        p = subprocess.run([str(self.plan), *args], env=env, capture_output=True, text=True, timeout=120,
                           input=stdin)
        if expect is not None:
            assert p.returncode == expect, f"plan.sh {args}: rc={p.returncode}\n{p.stdout}\n{p.stderr}"
        return p

    @property
    def slug(self) -> str:
        return sorted(p.name for p in (self.queue / "missions").iterdir())[0]

    def card_text(self, tid: str) -> str:
        return (self.queue / "missions" / self.slug / "tasks" / f"{tid}.md").read_text()

    def audit_rows(self):
        rows = []
        for f in sorted((self.queue / "audit").glob("transitions-*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        return rows


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# 1. 監査ログ
# ---------------------------------------------------------------------------

def _field(card_text: str, name: str) -> str:
    m = re.search(rf"^{name}: (.*)$", card_text, re.MULTILINE)
    return m.group(1).strip('"') if m else ""


def _drive_every_mutating_subcommand(sb: Sandbox):
    """状態を変える subcommand を 1 通り走らせ、期待する (op, task, from, to) の列を返す。"""
    expected = []
    sb.run("init", "Audit mission")
    expected.append(("init", None, None, None))
    for title, extra in (("A", []), ("B", []), ("C", ["--blocked-by", "t002"]), ("D", [])):
        sb.run("add", title, "--skills", "bash", *extra)
    tid = {"A": "t001", "B": "t002", "C": "t003", "D": "t004"}
    for t in ("t001", "t002", "t003", "t004"):
        expected.append(("add", t, None, "pending"))

    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    expected.append(("pull", "t001", "pending", "in_progress"))
    sb.run("needs-director", "t001", SECRET_REASON, agent="Ren")
    expected.append(("needs-director", "t001", "in_progress", "needs_director"))
    sb.run("update", "t001", "--reset")
    expected.append(("update", "t001", "needs_director", "pending"))
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    expected.append(("pull", "t001", "pending", "in_progress"))
    sb.run("done", "t001", SECRET_RESULT, "--no-pr", "audit test", agent="Ren")
    expected.append(("done", "t001", "in_progress", "done"))

    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t002")
    expected.append(("pull", "t002", "pending", "in_progress"))
    sb.run("fail", "t002", "--no-head", "audit test", agent="Ren")
    expected.append(("fail", "t002", "in_progress", "failed"))
    sb.run("release-dep", "t003")
    expected.append(("release-dep", "t003", "pending", "pending"))

    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t003")
    expected.append(("pull", "t003", "pending", "in_progress"))
    sb.run("ready-for-verification", "t003", agent="Ren")
    expected.append(("ready-for-verification", "t003", "in_progress", "ready_for_verification"))
    sb.run("verifying", "t003", "--verifier", "Wei", agent="verifier-dispatcher")      # S5: verifier-dispatcher の書き込み
    expected.append(("verifying", "t003", "ready_for_verification", "verifying"))
    sb.run("snapshot", "t003", "--section-file", "-", agent="Ren",                     # S5: pre-compact hook の書き込み
           stdin=f"## Pre-Compact Snapshot\n\n- note: {SECRET_RESULT}\n")
    expected.append(("snapshot", "t003", "verifying", "verifying"))
    sb.run("verify-result", "t003", "pass", "--notes", SECRET_RESULT, agent="Ren")
    expected.append(("verify-result", "t003", "verifying", "verified"))

    # verify-result は assignment を撤去しない (Ren → t003 が verified の card を指したまま残る)。
    # 次の pull の回復 (S4 / R-2) が、その孤児の枠を消す。回復の行 (op=recover) は pull の行の前に出る
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t004")
    expected.append(("recover", "t003", "verified", "verified"))
    expected.append(("pull", "t004", "pending", "in_progress"))
    gen = _field(sb.card_text("t004"), "started_at")
    assert gen
    sb.run("retire", "t004", "--agent", "Ren", "--started-at", gen, "--outcome", "reset", "--no-wait")
    expected.append(("retire", "t004", "in_progress", "pending"))

    # 孤児の assignment: done は AGENT_NAME 無しで打つ (assignment を撤去しない) → reap が撤去する
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t004")
    expected.append(("pull", "t004", "pending", "in_progress"))
    sb.run("done", "t004", SECRET_RESULT, "--no-pr", "audit test")
    expected.append(("done", "t004", "in_progress", "done"))
    assert (sb.queue / "assignments" / "Ren").exists()
    sb.run("reap-orphan-assignment", "Ren", "--no-wait")
    expected.append(("reap-orphan-assignment", "t004", None, None))

    sb.run("update", "t002", "--status", "skipped")
    expected.append(("update", "t002", "failed", "skipped"))
    slug = sb.slug
    sb.run("archive", slug)
    expected.append(("archive", None, None, None))
    return expected


def test_every_mutating_subcommand_writes_an_audit_row(sb):
    expected = _drive_every_mutating_subcommand(sb)
    rows = sb.audit_rows()
    assert rows, "監査ログの行が 1 つも無い (queue/audit/transitions-*.jsonl)"
    got = [(r["op"], r["task"], r["from_status"], r["to_status"]) for r in rows]
    assert got == expected

    for r in rows:
        assert set(r) == AUDIT_KEYS | ({"detail"} & set(r)), r
        assert r["result"] == ("repaired:R-2" if r["op"] == "recover" else "ok")
        assert r["execution_id"] is None                 # 01c が埋める。01a では null 固定
        assert re.fullmatch(r"[0-9a-f]{32}", r["txn_id"])
        assert r["ts"].endswith("Z")
        assert isinstance(r["files"], list) and r["files"], r    # 書いたパスが 1 つは出る
        assert all(not os.path.isabs(f) for f in r["files"])
    # pull は generation (card の started_at) を出し、actor は --agent の Worker
    pulls = [r for r in rows if r["op"] == "pull"]
    assert pulls and all(r["actor"] == "Ren" and r["generation"] for r in pulls)
    # AGENT_NAME を渡した subcommand の actor はその名前
    assert [r["actor"] for r in rows if r["op"] == "done"][:1] == ["Ren"]
    # 全 QUEUE_MUTATING_SUBCOMMANDS のうち review / launch (claude を起動する) 以外を実走した
    ran = {op for op, *_ in expected if op != "recover"}
    assert ran == _mutating_subcommands() - {"review", "launch"}


def _mutating_subcommands() -> set[str]:
    src = harness.plan_python_source(PLAN_SH_SRC)
    m = re.search(r"^QUEUE_MUTATING_SUBCOMMANDS = (\{.*?\})$", src, re.DOTALL | re.MULTILINE)
    return set(ast.literal_eval(m.group(1)))


def test_audit_log_never_contains_result_reason_or_body_text(sb):
    _drive_every_mutating_subcommand(sb)
    blob = "".join(f.read_text() for f in (sb.queue / "audit").glob("*.jsonl"))
    assert blob
    for secret in (SECRET_RESULT, SECRET_REASON, "audit test"):
        assert secret not in blob
    # 陽性対照: 秘密の文言は実際に card / queue のどこかには書かれている (テストが空回りしていない)
    everything = "".join(p.read_text() for p in (sb.queue / "archive").rglob("*.md"))
    assert SECRET_RESULT in everything


def test_every_mutating_subcommand_goes_through_with_lock():
    """構造: QUEUE_MUTATING_SUBCOMMANDS の各コマンド (か、それが呼ぶ helper) は `with_lock` を通る。
    `with_lock` を通らない書き込みは監査ログにも lib のロックにも乗らない。"""
    src = harness.plan_python_source(PLAN_SH_SRC)
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    dispatch = re.search(r"^dispatch = \{(.*?)^\}$", src, re.DOTALL | re.MULTILINE).group(1)
    table = dict(re.findall(r"'([a-z-]+)': (cmd_[a-z_]+)", dispatch))
    mutating = _mutating_subcommands()
    assert mutating <= set(table), mutating - set(table)
    for sub in sorted(mutating):
        body = ast.get_source_segment(src, funcs[table[sub]])
        assert "with_lock(" in body, f"{sub} ({table[sub]}) が with_lock を通らない"
    assert len(mutating) == 17                       # 空虚でない (集合が空になっていない)


def test_writes_without_a_transaction_fail_loudly_instead_of_writing_unlocked(tmp_path):
    ns = _namespace(tmp_path)
    for name, args in (("save_task", ("s", "t001", {"id": "t001", "status": "pending"}, "b")),
                       ("save_mission", ("s", {"slug": "s"})),
                       ("save_state", ({"active_missions": []},)),
                       ("publish_assignment", ("A", "s", "t001", "g")),
                       ("classify_assignment", ("A", "s", "t001", None))):
        with pytest.raises(RuntimeError, match="with_lock"):
            ns[name](*args)
    assert not (tmp_path / "queue" / "state.yaml").exists()


def test_a_failing_audit_log_never_stops_the_transition(sb):
    sb.run("init", "Audit down")
    sb.run("add", "A", "--skills", "bash")
    shutil_target = sb.queue / "audit"
    if shutil_target.exists():
        for f in shutil_target.glob("*"):
            f.unlink()
        shutil_target.rmdir()
    shutil_target.write_text("not a directory")            # queue/audit がディレクトリでない
    p = sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    assert "[state-store warn]" in p.stderr and "audit log" in p.stderr
    assert _field(sb.card_text("t001"), "status") == "in_progress"      # 遷移は完了している
    assert (sb.queue / "assignments" / "Ren").exists()


def test_a_refused_command_writes_no_audit_row_and_no_card_change(sb):
    """`die()` (SystemExit) で抜けたトランザクションは本体の行を書かない (§4)。"""
    sb.run("init", "Refuse")
    sb.run("add", "A", "--skills", "bash")
    before_rows = len(sb.audit_rows())
    before_card = sb.card_text("t001")
    p = sb.run("done", "t001", "x", "--no-pr", "r", expect=None)      # pending でも done は通る (現状を写す)
    assert p.returncode == 0
    p2 = sb.run("done", "t001", "x", "--no-pr", "r", expect=2)        # 2 回目: done は done から拒否 (exit 2)
    rows_after = sb.audit_rows()
    assert len(rows_after) == before_rows + 1                          # 1 回目の行だけ
    assert before_card != sb.card_text("t001")
    assert "受け付けるのは" in p2.stderr


# ---------------------------------------------------------------------------
# 2. 原子的な書き込み: 親 dir の fsync の有無と順序 / 途中で落ちても元のカードが残る
# ---------------------------------------------------------------------------

def _namespace(root: pathlib.Path) -> dict:
    """plan.sh の python 本体を名前空間に読み込む (コマンドは走らせない)。本番の関数そのもの。"""
    plan = copy_plan_tree(root)
    (root / "queue").mkdir(exist_ok=True)
    (root / "registry").mkdir(exist_ok=True)
    return harness.load_plan_namespace(plan, str(root / "queue"), str(root))


class Recorder:
    """`os.fsync` / `os.replace` / `os.unlink` / `os.remove` を記録するスタブ (実際の呼び出しは通す)。"""

    def __init__(self, monkeypatch, on_event=None):
        self.events = []
        self.on_event = on_event
        real_fsync, real_replace, real_unlink, real_remove = os.fsync, os.replace, os.unlink, os.remove

        def fsync(fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            self._hit(("fsync", kind, path))
            return real_fsync(fd)

        def replace(src, dst, *a, **k):
            self._hit(("replace", os.fspath(src), os.fspath(dst)))
            return real_replace(src, dst, *a, **k)

        def unlink(path, *a, **k):
            self._hit(("unlink", os.fspath(path)))
            return real_unlink(path, *a, **k)

        def remove(path, *a, **k):
            self._hit(("unlink", os.fspath(path)))
            return real_remove(path, *a, **k)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(os, "remove", remove)

    def _hit(self, ev):
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)


def _seed_card(ns, root):
    """本番の関数で card + assignment を作る (テストの前提。記録の対象外)。"""
    slug = "m-fsync"

    def _do():
        ns["save_mission"](slug, {"title": "m", "slug": slug, "status": "in_progress",
                                  "created_at": "2026-09-30T00:00:00Z", "completed_at": None, "next_task_id": 2})
        ns["save_state"]({"active_missions": [slug], "default_mission": slug})
        ns["save_task"](slug, "t001", sc.card("t001", "in_progress", "Haruto", sc.GEN), sc.body())
        ns["publish_assignment"]("Haruto", slug, "t001", sc.GEN)

    ns["with_lock"](_do)
    return slug


def _subsequence(events, wanted):
    """`wanted` (述語の列) が `events` に**この順で**現れるか。"""
    it = iter(events)
    return all(any(pred(ev) for ev in it) for pred in wanted)


def _is_file_fsync(ev):
    return ev[0] == "fsync" and ev[1] == "file"


@pytest.mark.parametrize("target", ["card", "mission", "state", "assignment_publish", "assignment_retire"])
def test_every_queue_write_is_fsynced_in_order_file_replace_parent_dir(tmp_path, monkeypatch, target):
    ns = _namespace(tmp_path)
    slug = _seed_card(ns, tmp_path)
    queue = tmp_path / "queue"
    rec = Recorder(monkeypatch)

    def _do():
        if target == "card":
            ns["save_task"](slug, "t001", sc.card("t001", "done", "Haruto", sc.GEN), sc.body("changed"))
        elif target == "mission":
            ns["save_mission"](slug, {"title": "m2", "slug": slug, "status": "in_progress",
                                      "created_at": "2026-09-30T00:00:00Z", "completed_at": None,
                                      "next_task_id": 3})
        elif target == "state":
            ns["save_state"]({"active_missions": [slug], "default_mission": None})
        elif target == "assignment_publish":
            ns["publish_assignment"]("Sora", slug, "t001", sc.NEW_GEN)
        else:
            assert ns["retire_assignment"]("Haruto", slug, "t001", None) == "mine"

    ns["with_lock"](_do)
    dest = {"card": queue / "missions" / slug / "tasks" / "t001.md",
            "mission": queue / "missions" / slug / "mission.yaml",
            "state": queue / "state.yaml",
            "assignment_publish": queue / "assignments" / "Sora",
            "assignment_retire": queue / "assignments" / "Haruto"}[target]
    parent = os.path.realpath(dest.parent)
    if target == "assignment_retire":
        # 撤去: unlink → 親 dir の fsync (本体 → identity の順で 2 回)
        wanted = [lambda e: e[0] == "unlink" and e[1].endswith("/assignments/Haruto"),
                  lambda e: e[0] == "fsync" and e[1] == "dir" and e[2] == parent,
                  lambda e: e[0] == "unlink" and e[1].endswith("/assignments/Haruto.identity"),
                  lambda e: e[0] == "fsync" and e[1] == "dir" and e[2] == parent]
    else:
        wanted = [_is_file_fsync,
                  lambda e: e[0] == "replace" and os.path.realpath(e[2]) == os.path.realpath(dest),
                  lambda e: e[0] == "fsync" and e[1] == "dir" and e[2] == parent]
    assert _subsequence(rec.events, wanted), (
        f"{target}: tmp の fsync → replace (or unlink) → 親 dir の fsync の順で呼ばれていない\n{rec.events}")


def test_fsync_recorder_detects_a_writer_that_skips_the_parent_dir(tmp_path, monkeypatch):
    """陽性対照: 修正前の `_atomic_write` (tmp + fsync + replace だけ) を同じスタブに通すと、上の述語が満たされない。
    検出器が「親 dir の fsync が無い」を見分けられなければ、上のテストは何も守っていない。"""
    rec = Recorder(monkeypatch)
    target = tmp_path / "x.txt"
    tmp = tmp_path / "x.txt.tmp.1"
    with open(tmp, "w") as f:                         # 修正前の _atomic_write と同じ形
        f.write("data")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    wanted = [_is_file_fsync,
              lambda e: e[0] == "replace",
              lambda e: e[0] == "fsync" and e[1] == "dir"]
    assert not _subsequence(rec.events, wanted)
    assert _subsequence(rec.events, wanted[:2])       # ファイル fsync と replace は見えている (スタブは生きている)


def _apply_write(ns, slug, target):
    """本番の `save_task` / `save_state` を 1 回呼ぶ (kill テストの「落とす対象の書き込み」)。"""
    new = sc.card("t001", "done", "Haruto", sc.GEN)

    def _do():
        if target == "card":
            ns["save_task"](slug, "t001", new, sc.body("NEW BODY"))
        else:
            ns["save_state"]({"active_missions": [slug], "default_mission": None})

    ns["with_lock"](_do)


def _dest(root, slug, target):
    return (root / "queue" / "missions" / slug / "tasks" / "t001.md" if target == "card"
            else root / "queue" / "state.yaml")


def _run_child_and_kill(root, kill_kind, target):
    """子プロセスで本番の書き込みを走らせ、`kill_kind` (file / replace / dir の fsync・replace) に届いた瞬間に
    自分へ SIGKILL する。戻り値: (SIGKILL で死んだか, slug)。"""
    ns = _namespace(root)
    slug = _seed_card(ns, root)
    pid = os.fork()
    if pid == 0:                                   # 子: pytest の後始末に戻らない (os._exit)
        code = 3
        try:
            real_fsync, real_replace = os.fsync, os.replace

            def fsync(fd):
                kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                if kind == kill_kind:
                    os.kill(os.getpid(), signal.SIGKILL)
                return real_fsync(fd)

            def replace(src, dst, *a, **k):
                if kill_kind == "replace":
                    os.kill(os.getpid(), signal.SIGKILL)
                return real_replace(src, dst, *a, **k)

            os.fsync, os.replace = fsync, replace
            _apply_write(ns, slug, target)
            code = 0
        finally:
            os._exit(code)
    _pid, status = os.waitpid(pid, 0)
    return os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, slug


@pytest.mark.parametrize("kill_kind", ["file", "replace", "dir"])
@pytest.mark.parametrize("target", ["card", "state"])
def test_kill_during_a_write_leaves_the_old_or_the_whole_new_file(tmp_path, kill_kind, target):
    # 落とさずに走らせて旧 / 新のバイトを得る (別 root)
    ref = tmp_path / "ref"
    ref.mkdir()
    ns_ref = _namespace(ref)
    slug_ref = _seed_card(ns_ref, ref)
    old_bytes = _dest(ref, slug_ref, target).read_bytes()
    _apply_write(ns_ref, slug_ref, target)
    new_bytes = _dest(ref, slug_ref, target).read_bytes()
    assert old_bytes != new_bytes                       # 空虚でない: 書き込みは内容を変える

    root = tmp_path / "run"
    root.mkdir()
    killed, slug = _run_child_and_kill(root, kill_kind, target)
    assert killed, f"SIGKILL の点に届かなかった (kill_kind={kill_kind}) — スタブが呼ばれていない"
    dest = _dest(root, slug, target)
    data = dest.read_bytes()
    # 元のファイルか、完全な新ファイルのどちらか。半端なバイト列は無い。
    assert data in (old_bytes, new_bytes), data
    # replace の前 (file の fsync / replace の直前) で落ちたなら旧内容のまま、replace の後 (親 dir の fsync) なら新内容
    assert data == (new_bytes if kill_kind == "dir" else old_bytes)
    # card の列挙 (`tNNN.md`) に tmp の残骸は入らない
    if target == "card":
        assert [p.name for p in dest.parent.iterdir() if re.fullmatch(r"t\d+\.md", p.name)] == ["t001.md"]
    # ロックは落ちたプロセスと一緒に解放されている: 次のトランザクションがすぐ取れる
    with store.transaction(root / "queue", op="probe", actor="test", nonblocking=True):
        pass


# ---------------------------------------------------------------------------
# 3. queue/.lock は lib_retirement.queue_transaction と同じ (互いに排他する)
# ---------------------------------------------------------------------------

def test_plan_sh_transaction_excludes_the_retirement_queue_transaction(tmp_path):
    ns = _namespace(tmp_path)
    queue = tmp_path / "queue"
    seen = {}

    def _do():
        with lib_retirement.queue_transaction(queue) as (held, why):
            seen["held"], seen["why"] = held, why

    ns["with_lock"](_do)
    assert seen["held"] is False and "held by another process" in seen["why"]
    # 陽性対照: plan.sh のトランザクションの外では取れる
    with lib_retirement.queue_transaction(queue) as (held, _why):
        assert held is True


def test_no_wait_exits_4_with_the_same_message_when_the_lock_is_busy(sb):
    sb.run("init", "Busy")
    with store.transaction(sb.queue, op="hold", actor="test"):
        p = sb.run("reap-orphan-assignment", "Nobody", "--no-wait", expect=4)
    assert "is held by another process" in p.stderr
    assert "--no-wait なので待たずに諦めました (何も変更していません)" in p.stderr
    assert not list((sb.queue / "audit").glob("*")) or all(
        r["op"] != "reap-orphan-assignment" for r in sb.audit_rows())      # 何も書いていない
