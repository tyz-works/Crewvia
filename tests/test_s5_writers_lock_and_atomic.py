"""S5 (vNext 01a / t020): ロック外・非原子的だった書き手を lib に寄せたことのテスト。

書き手ごとに、ロックと原子性の観点で 1 つずつ (受入条件)。どれも**修正前のコードで赤**になる
(赤の実証は `tests/red_proof_s5_lib_writers.sh`。欠陥を戻した複製に同じテストを走らせる)。

| 書き手 | 直したこと | テスト |
|---|---|---|
| verifier-dispatcher `update_task_fields` | card 全体をロックなしで読み書き → `plan.sh verifying` (ロックの中で読み直す) | 1・2 |
| hooks/pre-compact.sh | card を in-place・ロックなしで上書き → `plan.sh snapshot` | 2・3 |
| plan.sh `_apply_risk_flags` | ロックが外れた後に card を読んで書く → verdict と同じトランザクションの中 | 4 |
| `.crewvia-env` | `open('w')` (source される途中で読まれうる) → `atomic_write_text` | 5 |
| `queue/.taskvia-map.json` | 2 人が読んでから `open('w')` (lost update) → `locked_update_json` | 6 |
| `registry/workers.yaml` | `lib_registry.write` が `open('w')`・assign-name.sh がロックなしで初期化 | 7 |
| archive / init --force の退避 | `shutil.move` (親 dir を fsync しない) → `durable_rename` | 8 |
| `parse_opts()` | 本文の引数を検査する前に queue の骨組みを作る | 9 |
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
import textwrap
import time

import pytest

import test_plan_sh_state_store_cutover as cut   # Sandbox / _namespace / _seed_card (モジュールごと import。scripts/ を sys.path に足す)
import lib_state_store as store
import task_graph_publisher_harness as harness
from fixture_tree import copy_plan_tree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
MISSION = "m-s5"

#: 旧 hooks/pre-compact.sh の python 部分 (S5 より前の実コードそのまま)。card 全体をロックなしで読み、
#: in-place で書き戻す。これが「本当に危険だった」ことの対照。
OLD_PRE_COMPACT = textwrap.dedent('''\
    import sys, re
    task_file = sys.argv[1]
    new_section = sys.argv[2]
    with open(task_file, 'r') as f:
        content = f.read()
    pattern = r'## Pre-Compact Snapshot\\n[\\s\\S]*?(?=\\n## |\\Z)'
    if re.search(pattern, content):
        updated = re.sub(pattern, new_section.rstrip(), content)
    else:
        updated = content.rstrip('\\n') + '\\n\\n' + new_section
    STEP_BEFORE_WRITE()
    with open(task_file, 'w') as f:
        f.write(updated)
''')

SECTION = "## Pre-Compact Snapshot\n\n- trigger: auto\n- agent: Ren\n"


@pytest.fixture
def sb(tmp_path):
    box = cut.Sandbox(tmp_path)
    box.run("init", "S5 mission", "--mission", MISSION)
    return box


def _add(sb, title="t", skills="bash"):
    sb.run("add", title, "--skills", skills, "--mission", MISSION)


def _card_path(sb, tid="t001"):
    return sb.queue / "missions" / MISSION / "tasks" / f"{tid}.md"


def _status(sb, tid="t001"):
    m = re.search(r"^status: (.*)$", _card_path(sb, tid).read_text(), re.MULTILINE)
    return m.group(1)


def _mark_ready(sb, tid="t001"):
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", tid, "--mission", MISSION)
    sb.run("ready-for-verification", tid, "--mission", MISSION, agent="Ren")


# ---------------------------------------------------------------------------
# 1. verifier-dispatcher: ロックの中で読み直し、動いた card を巻き戻さない
# ---------------------------------------------------------------------------

def test_verifying_refuses_a_card_that_moved_on_and_writes_nothing(sb):
    """旧 `update_task_fields` は読んだ時点の内容を丸ごと書き戻した: 読んだ後に verify-result が
    `verified` にしても、`status: verifying` が上書きした。今は元の status が違えば拒否 (exit 2)。"""
    _add(sb)
    _mark_ready(sb)
    sb.run("verify-result", "t001", "pass", "--mission", MISSION, agent="Ren")
    assert _status(sb) == "verified"
    before = _card_path(sb).read_bytes()
    rows_before = len(sb.audit_rows())

    p = sb.run("verifying", "t001", "--verifier", "Wei", "--mission", MISSION, expect=2)

    assert "受け付けるのは" in p.stderr
    assert _card_path(sb).read_bytes() == before
    assert len(sb.audit_rows()) == rows_before          # 拒否は監査ログの行も作らない


def test_verifying_moves_ready_to_verifying_and_records_the_verifier(sb):
    _add(sb)
    _mark_ready(sb)
    sb.run("verifying", "t001", "--verifier", "Wei", "--mission", MISSION, agent="verifier-dispatcher")
    text = _card_path(sb).read_text()
    assert "status: verifying" in text and "verifier: Wei" in text
    row = sb.audit_rows()[-1]
    assert (row["op"], row["from_status"], row["to_status"], row["actor"]) == \
        ("verifying", "ready_for_verification", "verifying", "verifier-dispatcher")


def test_verifying_rejects_a_verifier_name_that_is_not_an_identifier(sb):
    _add(sb)
    _mark_ready(sb)
    before = _card_path(sb).read_bytes()
    sb.run("verifying", "t001", "--verifier", "a\nstatus: done", "--mission", MISSION, expect=2)
    assert _card_path(sb).read_bytes() == before


def test_the_old_read_modify_write_really_did_revert_a_concurrent_transition(sb):
    """対照: 旧アルゴリズム (読む → [その間に verify-result] → 書き戻す) は verified を verifying に戻す。
    上の拒否テストが「守っているもの」が本物の危険だったことを示す。"""
    _add(sb)
    _mark_ready(sb)
    card = _card_path(sb)
    stale_text = card.read_text()                                     # 旧 dispatcher が読んだ内容
    sb.run("verify-result", "t001", "pass", "--mission", MISSION, agent="Ren")
    assert _status(sb) == "verified"
    card.write_text(stale_text.replace("status: ready_for_verification", "status: verifying"))   # 旧の書き戻し
    assert _status(sb) == "verifying"                                  # 巻き戻った (これが旧の実害)


# ---------------------------------------------------------------------------
# 2. pre-compact: 別のコマンドとの同時更新で失われない / done を巻き戻さない
# ---------------------------------------------------------------------------

def _popen_plan(sb, *args, stdin_text=None):
    env = dict(sb.env, AGENT_NAME="Ren")
    p = subprocess.Popen([str(sb.plan), *args], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    p.pending_input = stdin_text or ""            # communicate(input=) で渡す (先に close すると flush に失敗する)
    return p


def test_concurrent_snapshots_and_done_never_resurrect_in_progress(sb):
    """独立プロセス・実 flock。snapshot を 8 本走らせながら done を 1 本走らせる。
    旧 hook (ロックなしの read-modify-write) では、done が終わった後に古い in_progress の内容が書き戻された。"""
    _add(sb)
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001", "--mission", MISSION)
    procs = [_popen_plan(sb, "snapshot", "t001", "--section-file", "-", "--mission", MISSION,
                         stdin_text=SECTION.replace("auto", f"auto-{i}")) for i in range(4)]
    done = _popen_plan(sb, "done", "t001", "finished", "--no-pr", "s5 test", "--mission", MISSION)
    procs += [_popen_plan(sb, "snapshot", "t001", "--section-file", "-", "--mission", MISSION,
                          stdin_text=SECTION.replace("auto", f"late-{i}")) for i in range(4)]
    for p in procs + [done]:
        out, err = p.communicate(input=p.pending_input, timeout=120)
        assert p.returncode == 0, f"{out}\n{err}"

    text = _card_path(sb).read_text()
    assert _status(sb) == "done", text                       # in_progress が復活していない
    assert text.count("## Pre-Compact Snapshot") == 1        # 節は 1 つ (重複して積まれていない)
    assert "finished" in text                                # done の Result が残っている
    rows = sb.audit_rows()
    assert sum(1 for r in rows if r["op"] == "snapshot") == 8
    assert sum(1 for r in rows if r["op"] == "done") == 1


def test_snapshot_does_not_touch_the_frontmatter_or_other_sections(sb):
    _add(sb)
    sb.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001", "--mission", MISSION)
    before = _card_path(sb).read_text()
    sb.run("snapshot", "t001", "--section-file", "-", "--mission", MISSION, agent="Ren", stdin=SECTION)
    after = _card_path(sb).read_text()
    assert after.split("\n---\n", 1)[0] == before.split("\n---\n", 1)[0]          # frontmatter は同じ
    assert after.startswith(before.rstrip("\n"))                                    # 元の本文は先頭にそのまま
    assert after.endswith(SECTION)
    # 2 回目は差し替え (積み増さない)
    sb.run("snapshot", "t001", "--section-file", "-", "--mission", MISSION, agent="Ren",
           stdin="## Pre-Compact Snapshot\n\n- trigger: manual\n")
    again = _card_path(sb).read_text()
    assert again.count("## Pre-Compact Snapshot") == 1 and "trigger: manual" in again and "trigger: auto" not in again


def test_snapshot_refuses_a_section_that_is_not_the_snapshot_heading(sb):
    _add(sb)
    before = _card_path(sb).read_bytes()
    sb.run("snapshot", "t001", "--section-file", "-", "--mission", MISSION, stdin="## Something else\n", expect=2)
    assert _card_path(sb).read_bytes() == before


def test_snapshot_without_mission_refuses_an_ambiguous_task_id(sb):
    """旧 hook は `find ... -path "*/tasks/t001.md" | head -1` で、別の mission の同じ tNNN に書きえた
    (不変条件 2 の族)。--mission が無く複数に当たるなら、書かずに拒否する。"""
    sb.run("init", "Other mission", "--mission", "m-other")
    sb.run("add", "x", "--skills", "bash", "--mission", "m-other")
    _add(sb)
    before = {m: (sb.queue / "missions" / m / "tasks" / "t001.md").read_bytes() for m in (MISSION, "m-other")}
    p = sb.run("snapshot", "t001", "--section-file", "-", stdin=SECTION, expect=1)
    assert "multiple missions" in p.stderr or "multiple missions" in p.stdout
    after = {m: (sb.queue / "missions" / m / "tasks" / "t001.md").read_bytes() for m in (MISSION, "m-other")}
    assert after == before


def test_the_hook_calls_plan_sh_snapshot_and_falls_back_to_a_log_when_refused(tmp_path):
    """hooks/pre-compact.sh の実物を、隔離コピーの plan.sh に向けて走らせる。card は plan.sh 経由で書かれ、
    書けなかった (task が無い) ときはログ 1 行に落ちる。"""
    box = cut.Sandbox(tmp_path)
    box.run("init", "hook mission", "--mission", MISSION)
    _add(box)
    box.run("pull", "--agent", "Ren", "--skills", "bash", "--task", "t001", "--mission", MISSION)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-compact.sh").write_bytes((REPO_ROOT / "hooks" / "pre-compact.sh").read_bytes())
    os.chmod(hooks / "pre-compact.sh", 0o755)
    env = dict(box.env, CREWVIA_TASK_ID="t001", CREWVIA_MISSION_SLUG=MISSION, CREWVIA_AGENT_NAME="Ren",
               AGENT_NAME="Ren")
    payload = json.dumps({"trigger": "manual", "custom_instructions": "keep `x` and $(y) > z"})
    p = subprocess.run(["bash", str(hooks / "pre-compact.sh")], input=payload, env=env, text=True,
                       capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr
    text = _card_path(box).read_text()
    assert "## Pre-Compact Snapshot" in text and "trigger: manual" in text
    assert "keep `x` and $(y) > z" in text                           # 記号がシェルに解釈されず、そのまま入る
    assert any(r["op"] == "snapshot" for r in box.audit_rows())      # 監査ログに乗った = lib を通った

    env["CREWVIA_TASK_ID"] = "t999"
    p = subprocess.run(["bash", str(hooks / "pre-compact.sh")], input=payload, env=env, text=True,
                       capture_output=True, timeout=120)
    assert p.returncode == 0                                        # hook は失敗しても compaction を止めない
    log = (box.queue / "pre-compact-fallback.log").read_text()
    assert "task_id=t999" in log


def _kill_child_at(root, kill_at: int, action) -> tuple[bool, int]:
    """fork した子で `action()` を走らせ、lib の `FAULT_HOOK` が k 番目に呼ばれた所で自分に SIGKILL する。
    戻り値: (SIGKILL で死んだか, 呼ばれた点の数)。子は SIGKILL か os._exit でしか終わらない。"""
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
            action()
            os.write(w, str(calls[0]).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    _pid, status = os.waitpid(pid, 0)
    data = os.read(r, 32)
    os.close(r)
    return os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, int(data or 0)


def test_killing_snapshot_at_every_write_point_leaves_the_old_or_the_whole_new_card(tmp_path):
    """強制終了の全点 (lib の atomic_write_text の各段 + 監査ログ) で、card は「元のバイト」か「完全な新しいバイト」。
    旧 hook の `open(task_file, 'w')` は truncate した時点で落ちると空の card が残った (次のテスト)。"""
    def fresh(root):
        root.mkdir()
        ns = cut._namespace(root)
        slug = cut._seed_card(ns, root)
        ns["SUBCOMMAND"] = "snapshot"
        section_file = root / "section.txt"
        section_file.write_text(SECTION)
        return ns, slug, section_file

    ref = tmp_path / "ref"
    ns, slug, section_file = fresh(ref)
    card = ref / "queue" / "missions" / slug / "tasks" / "t001.md"
    old = card.read_bytes()
    ns["cmd_snapshot"](["t001", "--section-file", str(section_file), "--mission", slug])
    new = card.read_bytes()
    assert old != new

    points = 0
    for k in range(1, 40):
        root = tmp_path / f"k{k}"
        ns, slug, section_file = fresh(root)
        card = root / "queue" / "missions" / slug / "tasks" / "t001.md"
        killed, calls = _kill_child_at(
            root, k, lambda: ns["cmd_snapshot"](["t001", "--section-file", str(section_file), "--mission", slug]))
        if not killed:
            points = k - 1
            assert calls == points
            assert card.read_bytes() == new
            break
        data = card.read_bytes()
        assert data in (old, new), f"k={k}: 半端な card が残った: {data!r}"
        leftovers = [p.name for p in card.parent.iterdir() if re.fullmatch(r"t\d+\.md", p.name)]
        assert leftovers == ["t001.md"]
    assert points >= 6, f"落とせる点が {points} 個しか無い — 注入口が壊れている"


def test_the_old_in_place_write_leaves_an_empty_card_when_killed_after_truncate(tmp_path):
    """対照: 旧 hook の書き方 (`open(task_file, 'w')`) は、開いた直後に落ちると card が空になる。
    上のテストが「元か新のどちらか」を assert することの意味 (旧はそれを満たせない) を示す。"""
    card = tmp_path / "t001.md"
    card.write_text("---\nid: t001\nstatus: in_progress\n---\n\nbody\n")
    script = OLD_PRE_COMPACT.replace(
        "STEP_BEFORE_WRITE()", "pass").replace(
        "    f.write(updated)", "    os.kill(os.getpid(), 9)\n    f.write(updated)").replace(
        "import sys, re", "import sys, re, os")
    p = subprocess.run([sys.executable, "-c", script, str(card), SECTION], capture_output=True, timeout=30)
    assert p.returncode == -signal.SIGKILL
    assert card.read_bytes() == b""                                  # truncate されたまま = card が消えた


# ---------------------------------------------------------------------------
# 4. risk flags: 読みも書きも verdict と同じトランザクションの中
# ---------------------------------------------------------------------------

def _review_file(root, task_id, mode):
    path = root / "plan_review.md"
    path.write_text(f"# Review\n\n## Risk Flags\n\n- task: {task_id}\n  recommended_mode: {mode}\n\n## Other\n")
    return path


def test_risk_flag_upgrade_outside_a_transaction_fails_loudly(tmp_path):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)
    with pytest.raises(RuntimeError):
        ns["_apply_risk_flag_upgrades"](slug, [("t001", "strict")])


def test_risk_flag_upgrade_rereads_the_card_inside_the_lock_and_keeps_a_concurrent_transition(tmp_path):
    """旧: card を**ロックの前に**読み (`load_task`)、別のロックで書いた。読んでから書くまでの間に done が
    進めた status を、古い内容で巻き戻した。新: 読みも書きも同じトランザクションの中。
    parse (card に触れない) → その間に done → apply、の順で、done が残ることを確かめる。"""
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)                       # t001: in_progress
    upgrades = ns["_parse_risk_flags"](str(_review_file(tmp_path, "t001", "strict")))
    assert upgrades == [("t001", "strict")]

    def _finish():
        meta, body = ns["load_task"](slug, "t001")
        meta["status"] = "done"
        ns["save_task"](slug, "t001", meta, body)

    ns["with_lock"](_finish)                                  # verdict の前に done が入った
    ns["with_lock"](lambda: ns["_apply_risk_flag_upgrades"](slug, upgrades))

    meta, _body = ns["load_task"](slug, "t001")
    assert meta["status"] == "done"                           # in_progress に戻っていない
    assert (meta.get("verification") or {}).get("mode") == "strict"
    rows = [json.loads(line) for f in (tmp_path / "queue" / "audit").glob("*.jsonl")
            for line in f.read_text().splitlines()]
    assert rows and all(r["result"] == "ok" for r in rows)


def test_risk_flag_parsing_never_touches_a_card(tmp_path):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)
    card = tmp_path / "queue" / "missions" / slug / "tasks" / "t001.md"
    before = card.read_bytes()
    ns["_parse_risk_flags"](str(_review_file(tmp_path, "t001", "strict")))
    assert card.read_bytes() == before
    # 構造: cmd_review は verdict と risk flags を 1 つの with_lock にしている
    src = harness.plan_python_source(SCRIPTS / "plan.sh")
    tree = ast.parse(src)
    review = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cmd_review")
    inner = next(n for n in ast.walk(review) if isinstance(n, ast.FunctionDef) and n.name == "_do_verdict")
    assert any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_apply_risk_flag_upgrades"
               for n in ast.walk(inner)), "risk flags の適用が verdict のトランザクションの外に出た"


# ---------------------------------------------------------------------------
# 5. .crewvia-env: 原子的
# ---------------------------------------------------------------------------

def test_crewvia_env_is_written_with_atomic_write_text_not_open_w():
    """`plan.sh pull` が worktree に置く `.crewvia-env` は Worker が source する。途中まで書かれた内容を
    読まれると mission を取り違える。書き方が `open('w')` に戻ったら赤 (構造)。挙動は下の fault 注入で見る。"""
    src = harness.plan_python_source(SCRIPTS / "plan.sh")
    pull = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "cmd_pull")
    calls = [ast.unparse(n) for n in ast.walk(pull) if isinstance(n, ast.Call)]
    assert any("atomic_write_text(env_file" in c for c in calls), calls
    assert not any(c.startswith("open(") and "env_file" in c for c in calls)


def test_a_write_killed_midway_never_leaves_a_partial_env_file(tmp_path):
    env_file = tmp_path / ".crewvia-env"
    old = "export CREWVIA_MISSION_SLUG=old\n"
    new = "export CREWVIA_MISSION_SLUG=m-new\nexport CREWVIA_TASK_ID=t009\nexport CREWVIA_TASK_SLUG=x\n"
    env_file.write_text(old)
    survivors = set()
    for k in range(1, 20):
        env_file.write_text(old)
        killed, calls = _kill_child_at(tmp_path, k, lambda: store.atomic_write_text(env_file, new))
        survivors.add(env_file.read_text())
        assert env_file.read_text() in (old, new)
        if not killed:
            break
    assert survivors == {old, new}


# ---------------------------------------------------------------------------
# 6. queue/.taskvia-map.json: 専用ロック + 読み直し
# ---------------------------------------------------------------------------

WRITER = textwrap.dedent('''\
    import sys, time, random
    sys.path.insert(0, sys.argv[1])
    from lib_state_store import locked_update_json
    path, prefix, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
    for i in range(n):
        def fn(d, i=i):
            d[f"{prefix}:{i}"] = {"registered": True, "status": "pending"}
            time.sleep(random.random() * 0.004)     # 読んでから書くまでの窓を広げる
            return d
        locked_update_json(path, path + ".lock", fn, on_unreadable="reset")
''')


def test_two_writers_of_the_taskvia_map_lose_no_entries(tmp_path):
    """独立プロセス 2 本 × 30 件。旧 (両者とも読んでから `open('w')`) は互いの項目を消す。"""
    path = tmp_path / ".taskvia-map.json"
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(SCRIPTS), str(path), prefix, "30"])
             for prefix in ("plan", "sync")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    data = json.loads(path.read_text())
    assert len(data) == 60, f"{60 - len(data)} 件失われた"


def test_plan_sh_taskvia_map_update_uses_the_lock_and_keeps_other_entries(tmp_path):
    ns = cut._namespace(tmp_path)
    path = tmp_path / "queue" / ".taskvia-map.json"
    path.write_text(json.dumps({"other:t001": {"registered": True, "status": "done"}}))
    ns["_taskvia_map_update"]("m1", "t002", "pending")
    ns["_taskvia_map_update_status"]("m1", "t002", "running")
    data = json.loads(path.read_text())
    assert data["other:t001"]["status"] == "done"
    assert data["m1:t002"] == {"registered": True, "status": "running"}
    assert (tmp_path / "queue" / ".taskvia-map.json.lock").exists()


def test_an_unreadable_taskvia_map_is_rebuilt_not_fatal(tmp_path):
    """キャッシュなので、壊れていれば空から作り直す (次の同期が冪等に埋める)。同期の副産物で task を止めない。"""
    ns = cut._namespace(tmp_path)
    path = tmp_path / "queue" / ".taskvia-map.json"
    path.write_text("{not json")
    ns["_taskvia_map_update"]("m1", "t001", "pending")
    assert json.loads(path.read_text()) == {"m1:t001": {"registered": True, "status": "pending"}}


def test_locked_update_json_default_still_refuses_an_unreadable_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{not json")
    with pytest.raises(store.StoreReadError):
        store.locked_update_json(path, str(path) + ".lock", lambda d: d)
    assert path.read_text() == "{not json"                    # 空に潰して書き潰さない


def test_taskvia_sync_merges_only_the_entries_it_changed(tmp_path):
    """taskvia-sync.sh の `save_map` は、同期の最初に読んだ map 全体ではなく、この実行で変えた項目だけを
    ロックの下で読み直した map に重ねる。同期の間に plan.sh が足した項目を消さない。"""
    src = (SCRIPTS / "taskvia-sync.sh").read_text()
    block = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.DOTALL).group(1)
    tree = ast.parse(block)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "save_map")
    ns: dict = {"locked_update_json": store.locked_update_json, "StoreError": store.StoreError, "sys": sys}
    exec(compile(ast.Module([fn], []), "taskvia-sync.sh:save_map", "exec"), ns)

    path = tmp_path / ".taskvia-map.json"
    path.write_text(json.dumps({"added-by-plan:t001": {"registered": True, "status": "pending"}}))
    ns["save_map"](str(path), {"synced:t002": {"registered": True, "status": "done"}})
    data = json.loads(path.read_text())
    assert set(data) == {"added-by-plan:t001", "synced:t002"}


# ---------------------------------------------------------------------------
# 7. registry/workers.yaml
# ---------------------------------------------------------------------------

REGISTRY_HEADER = "# workers\n"


def _registry_args(names_and_counts):
    order = [n for n, _ in names_and_counts]
    by_name = {n: {"name": n, "skills": ["bash"], "task_count": c, "last_active": "2026-09-30"}
               for n, c in names_and_counts}
    return REGISTRY_HEADER, order, by_name


def test_registry_write_killed_at_every_point_leaves_the_old_or_the_whole_new_file(tmp_path):
    import lib_registry
    path = tmp_path / "workers.yaml"
    old_args, new_args = _registry_args([("Ren", 1)]), _registry_args([("Ren", 2), ("Sora", 0)])
    lib_registry.write(str(path), *old_args)
    old = path.read_bytes()
    lib_registry.write(str(path), *new_args)
    new = path.read_bytes()
    assert old != new
    points = 0
    for k in range(1, 30):
        lib_registry.write(str(path), *old_args)
        killed, calls = _kill_child_at(tmp_path, k, lambda: lib_registry.write(str(path), *new_args))
        data = path.read_bytes()
        assert data in (old, new), f"k={k}: 半端な workers.yaml: {data!r}"
        if not killed:
            points = calls
            break
    assert points >= 6


def test_assign_name_no_longer_initialises_the_registry_outside_the_lock():
    text = (SCRIPTS / "assign-name.sh").read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("workers: []" in ln for ln in code)
    assert not any(re.search(r">\s*\"?\$REGISTRY_YAML", ln) for ln in code)


def test_concurrent_assign_name_starts_on_a_missing_registry_lose_no_worker(tmp_path):
    """workers.yaml が無い状態で assign-name.sh を同時に 6 本起動する。旧はロックの外で `printf 'workers: []' >`
    と初期化し、先に書いた Worker の項目を後発の初期化が消しえた。今は初期化が無く、最初の write が
    ロックの中・原子的。"""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    for src in [SCRIPTS / "assign-name.sh", *sorted(SCRIPTS.glob("lib_*"))]:
        if src.is_file():
            (root / "scripts" / src.name).write_bytes(src.read_bytes())
    os.chmod(root / "scripts" / "assign-name.sh", 0o755)
    (root / "config" / "worker-names.yaml").write_bytes((REPO_ROOT / "config" / "worker-names.yaml").read_bytes())
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    procs = [subprocess.Popen(["bash", str(root / "scripts" / "assign-name.sh"), "--skills", f"skill{i}", "--exclude",
                               "none"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0] * 6, outs
    import lib_registry
    _header, order, _by = lib_registry.parse(str(root / "registry" / "workers.yaml"))
    assert len(order) == len(set(order)) == 6, order            # 6 人とも残っている


# ---------------------------------------------------------------------------
# 8. rename の耐久性: 元と先の両方の親 dir を fsync
# ---------------------------------------------------------------------------

def _dir_fsyncs_after_rename(events, src_parent, dst_parent):
    """rename の後に、先の親 dir・元の親 dir の順に fsync が来ているか。"""
    seen_rename = False
    got = []
    for ev in events:
        if ev[0] == "rename":
            seen_rename = True
        elif seen_rename and ev[0] == "fsync" and ev[1] == "dir":
            got.append(ev[2])
    return dst_parent in got and src_parent in got


class RenameRecorder:
    """`os.fsync` と `os.rename` を記録する (実際の呼び出しは通す)。`shutil.move` も lib の `_sys_rename` も
    最後は `os.rename` を呼ぶので、新旧どちらの形も同じ述語で見られる。"""

    def __init__(self, monkeypatch):
        self.events = []
        real_fsync, real_rename = os.fsync, os.rename

        def fsync(fd):
            self.events.append(("fsync", "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file",
                                os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        def rename(src, dst):
            self.events.append(("rename", os.fspath(src), os.fspath(dst)))
            return real_rename(src, dst)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "rename", rename)


def test_archive_and_init_force_fsync_both_parent_directories_after_the_rename(tmp_path, monkeypatch):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)                   # queue/missions/<slug>/
    queue = tmp_path / "queue"
    rec = RenameRecorder(monkeypatch)

    ns["cmd_archive"]([slug])                             # queue/missions/<slug> → queue/archive/<slug>
    src_parent = os.path.realpath(queue / "missions")
    dst_parent = os.path.realpath(queue / "archive")
    assert (queue / "archive" / slug).is_dir() and not (queue / "missions" / slug).exists()
    assert _dir_fsyncs_after_rename(rec.events, src_parent, dst_parent), rec.events


def test_shutil_move_shape_fails_the_same_predicate(tmp_path, monkeypatch):
    """陽性対照: 旧 (`shutil.move`) の形は同じ述語を満たさない —— 述語が「親 dir の fsync が無い」を見分ける。"""
    rec = RenameRecorder(monkeypatch)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "m").mkdir()
    import shutil
    shutil.move(str(tmp_path / "a" / "m"), str(tmp_path / "b" / "m"))
    assert any(ev[0] == "rename" for ev in rec.events), "rename が記録されていない: 述語が空回りしている"
    assert not _dir_fsyncs_after_rename(rec.events, os.path.realpath(tmp_path / "a"),
                                        os.path.realpath(tmp_path / "b"))


def test_durable_rename_refuses_an_existing_destination_and_leaves_both(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f").write_text("s")
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / "f").write_text("d")
    with pytest.raises(store.StoreWriteError):
        store.durable_rename(tmp_path / "src", tmp_path / "dst")
    assert (tmp_path / "src" / "f").read_text() == "s" and (tmp_path / "dst" / "f").read_text() == "d"


def test_durable_rename_killed_at_every_point_leaves_the_directory_in_exactly_one_place(tmp_path):
    for k in range(1, 20):
        root = tmp_path / f"k{k}"
        (root / "missions" / "m").mkdir(parents=True)
        (root / "missions" / "m" / "f").write_text("x")
        killed, calls = _kill_child_at(root, k, lambda: store.durable_rename(root / "missions" / "m",
                                                                              root / "archive" / "m"))
        here, there = (root / "missions" / "m").exists(), (root / "archive" / "m").exists()
        assert here != there, f"k={k}: 二重 / 消失 (missions={here}, archive={there})"
        if not killed:
            assert there and calls >= 3
            break


# ---------------------------------------------------------------------------
# 9. parse_opts は、本文の引数を検査する前に queue の骨組みを作らない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ("done", "t001", "--result-file", "/no/such/file", "--mission", MISSION),
    ("done", "t001", "--result-file", "-", "--mission", MISSION),          # stdin が空
    ("needs-director", "t001", "--result-file", "/no/such/file", "--mission", MISSION),
    ("verify-result", "t001", "pass", "--notes-file", "/no/such/file", "--mission", MISSION),
])
def test_a_rejected_body_argument_leaves_no_queue_skeleton(tmp_path, args):
    box = cut.Sandbox(tmp_path)
    assert not (box.queue / "missions").exists() and not (box.queue / "archive").exists()
    box.run(*args, expect=2, stdin="")
    assert not (box.queue / "missions").exists(), "拒否された呼び出しが queue/missions を作った"
    assert not (box.queue / "archive").exists(), "拒否された呼び出しが queue/archive を作った"


def test_the_skeleton_is_still_created_by_a_command_that_writes(tmp_path):
    box = cut.Sandbox(tmp_path)
    box.run("init", "first")
    assert (box.queue / "missions").is_dir() and (box.queue / "archive").is_dir()
