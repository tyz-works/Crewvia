"""PR #257 Codex 2 巡目の P2 ×2 (lib の約束) を固定する。

P2-1 **読めない入力を健全と報告しない**: `diagnose()` は queue を観測できなかった (EACCES 等) とき
     `[]` を返してはいけない。列挙・state.yaml・assignment が読めないことを finding として残す。
     ENOENT だけが「無い」(`lib_task_cards.Unreadable` と同じ作法)。
P2-2 **内容を出さない**: カードの本文・frontmatter の値・パーサ例外 (問題の行をそのまま含む) が、
     stderr・監査ログ・戻り値・例外メッセージに出ない。出るのは固定コードと安全なメタデータだけ。
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

import state_store_scenarios as sc       # scripts/ を sys.path に足す (lib より先)
import lib_state_store as store

M, A, G = sc.MISSION, sc.AGENT, sc.GEN

pytestmark_root = pytest.mark.skipif(os.geteuid() == 0, reason="root は権限で止まらない")

#: 空白と `=` を含む — 識別子 (slug / Worker 名 / 世代) の形ではないので、門を通らない値の代表。
SECRET = "SECRET RESULT TOKEN=sk-abc123"
SECRET_LINE = "SECRET-RESULT-TOKEN sk-abc123 no colon here"


@pytest.fixture
def q(tmp_path):
    return tmp_path / "queue"


@pytest.fixture
def unlock():
    """chmod 000 したものを teardown で必ず戻す (tmp_path の後始末が失敗しないように)。"""
    paths = []
    yield paths
    for p in paths:
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass


def kinds(findings):
    return sorted(f.kind for f in findings)


# ---- P2-1: 読めない入力を健全と報告しない --------------------------------------

def test_healthy_queue_has_no_findings(q):
    """陽性対照の裏 (空虚さの防止): 健全な queue では [] が返る。以下の各テストの [] ではない結果は
    「観測できなかった」から来ている。"""
    sc.seed(q, "done")
    assert store.diagnose(q) == []


@pytestmark_root
def test_unlistable_tasks_dir_is_a_finding_not_an_empty_queue(q, unlock):
    sc.seed(q, "done")
    tdir = q / "missions" / M / "tasks"
    os.chmod(tdir, 0)
    unlock.append(tdir)
    found = store.diagnose(q)
    assert found != []                                             # 今の head は []
    assert any(f.kind == "unobservable_input" and "list_error:EACCES" in f.detail
               and f"missions/{M}/tasks" in f.detail for f in found), found


@pytestmark_root
def test_unlistable_missions_dir_is_a_finding(q, unlock):
    sc.seed(q, "done")
    os.chmod(q / "missions", 0)
    unlock.append(q / "missions")
    found = store.diagnose(q)
    assert any(f.kind == "unobservable_input" and "missions: list_error:EACCES" in f.detail for f in found), found


@pytestmark_root
def test_unlistable_assignments_dir_is_a_finding(q, unlock):
    sc.seed(q, "done")
    os.chmod(q / "assignments", 0)
    unlock.append(q / "assignments")
    found = store.diagnose(q)
    assert any(f.kind == "unobservable_input" and "assignments" in f.detail and "EACCES" in f.detail
               for f in found), found


@pytestmark_root
def test_unreadable_state_yaml_is_a_finding_and_r4_never_runs_on_it(q, unlock):
    sc.seed(q, "archive")
    st = q / "state.yaml"
    os.chmod(st, 0)
    unlock.append(st)
    found = store.diagnose(q)
    assert any(f.kind == "unobservable_input" and f.detail.startswith("state.yaml: read_error:EACCES")
               for f in found), found


def test_corrupt_state_yaml_is_a_finding(q):
    sc.seed(q, "archive")
    (q / "state.yaml").write_text("this line has no colon\n")
    found = store.diagnose(q)
    assert any(f.kind == "unobservable_input" and f.detail.startswith("state.yaml: parse_error")
               for f in found), found


def test_unreadable_assignment_is_reported_even_if_no_card_refers_to_it(q):
    """`_r2` は読めない枠を黙って飛ばしていた (名指しの card が参照していない限り)。"""
    sc.seed(q, "pull")
    (q / "assignments").mkdir()
    os.mkfifo(q / "assignments" / "Ghost")                        # どの card も指さない枠
    found = store.diagnose(q)
    assert any(f.kind == "reported:assignment_unverifiable" and f.agent == "Ghost" for f in found), found
    # recover() の範囲 (呼び出し元の枠) でも報告する — 書かない・消さない
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(agents=("Ghost",)))
    assert [r.result for r in reps] == ["reported:assignment_unverifiable"]
    assert (q / "assignments" / "Ghost").exists()


@pytestmark_root
def test_r4_does_not_drop_an_active_mission_it_cannot_observe(q, unlock):
    """`os.path.lexists` は EACCES でも False → 「移動済み」と誤読して active から外す穴。"""
    sc.seed(q, "archive")
    (q / "archive" / M).mkdir(parents=True)                        # 別の理由で archive/<slug> が在る
    os.chmod(q / "missions", 0)
    unlock.append(q / "missions")
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(archive_slugs=(M,)))
    os.chmod(q / "missions", 0o755)
    assert [r.result for r in reps] == ["reported:archive_state_unobservable"]
    with store.transaction(q, op="y", actor="t") as t:
        assert t.load_state()["active_missions"] == [M]            # 外していない


@pytestmark_root
def test_r3_reports_an_unlistable_tasks_dir_instead_of_skipping(q, unlock):
    sc.seed(q, "add")
    tdir = q / "missions" / M / "tasks"
    os.chmod(tdir, 0)
    unlock.append(tdir)
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(add_missions=(M,)))
    assert [r.result for r in reps] == ["reported:tasks_dir_unreadable"]


@pytestmark_root
def test_reverse_lookup_reports_an_unlistable_assignments_dir(q, unlock):
    sc.seed(q, "done")
    os.chmod(q / "assignments", 0)
    unlock.append(q / "assignments")
    with store.transaction(q, op="x", actor="t") as t:
        reps = t.recover(store.Scope(cards=((M, "t001"),)))
    assert "reported:assignments_dir_unreadable" in [r.result for r in reps]


@pytestmark_root
def test_orphan_identity_is_not_declared_when_the_body_cannot_be_observed(q, unlock):
    sc.seed(q, "done")
    ad = q / "assignments"
    os.chmod(ad, 0o444)                                            # 一覧は取れるが lstat は通らない
    unlock.append(ad)
    kinds_seen = kinds(store.diagnose(q))
    assert "orphan_identity" not in kinds_seen                     # 観測できない = 「無い」ではない
    assert "unobservable_input" in kinds_seen


def test_missing_things_are_not_findings(q):
    """ENOENT だけが「本当に無い」。空の queue (まだ何も無い) は健全。"""
    q.mkdir()
    assert store.diagnose(q) == []


# ---- P2-2: 内容を出さない -------------------------------------------------------

def _leaks(q, capsys, extra=()):
    out = capsys.readouterr()
    audit = "".join(p.read_text() for p in (q / "audit").glob("*.jsonl")) if (q / "audit").exists() else ""
    return [name for name, text in (("stderr", out.err), ("stdout", out.out), ("audit", audit), *extra)
            if "SECRET" in text or "sk-abc123" in text]


def _card_text(extra_frontmatter_lines="", worker="null", status="in_progress", started="null", cid="t001"):
    return ("---\n" + f"id: {cid}\ntitle: x\nstatus: {status}\nworker: {worker}\nstarted_at: {started}\n"
            + extra_frontmatter_lines + "---\n\n## Description\nd\n\n## Result\nRESULT " + SECRET + "\n")


def _recover(q, scope):
    with store.transaction(q, op="next", actor="t") as t:
        return t.recover(scope)


def test_broken_frontmatter_line_never_reaches_stderr_or_audit_or_reports(q, capsys):
    sc.seed(q, "pull")
    (q / "missions" / M / "tasks" / "t001.md").write_text(_card_text(SECRET_LINE + "\n"))
    reps = _recover(q, sc.SCOPES["pull"])
    assert [r.result for r in reps] == ["reported:card_unreadable"]
    assert reps[0].detail.startswith("parse_error:line=")            # 固定コード + 行番号だけ
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []
    rows = [json.loads(line) for p in (q / "audit").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert rows and all("SECRET" not in json.dumps(r) for r in rows)


def test_load_card_error_and_its_chain_do_not_contain_the_line(q):
    sc.seed(q, "pull")
    (q / "missions" / M / "tasks" / "t001.md").write_text(_card_text(SECRET_LINE + "\n"))
    with store.transaction(q, op="x", actor="t") as t:
        with pytest.raises(store.CardUnreadable) as ei:
            t.load_card(M, "t001")
    e = ei.value
    assert "SECRET" not in str(e) and "SECRET" not in e.reason and "sk-abc123" not in repr(e)
    assert e.__cause__ is None and (e.__context__ is None or "SECRET" not in str(e.__context__))
    assert e.reason.startswith("parse_error")


@pytest.mark.parametrize("loader", ["load_mission", "load_state"])
def test_broken_mission_and_state_errors_carry_no_line(q, loader):
    sc.seed(q, "pull")
    (q / "missions" / M / "mission.yaml").write_text(SECRET_LINE + "\n")
    (q / "state.yaml").write_text(SECRET_LINE + "\n")
    with store.transaction(q, op="x", actor="t") as t:
        with pytest.raises(store.StoreReadError) as ei:
            getattr(t, loader)(*([M] if loader == "load_mission" else []))
    assert "SECRET" not in str(ei.value) and "sk-abc123" not in str(ei.value)
    assert ei.value.__cause__ is None and (ei.value.__context__ is None or "SECRET" not in str(ei.value.__context__))


def test_broken_mission_yaml_in_r3_reports_a_code_only(q, capsys):
    sc.seed(q, "add")
    (q / "missions" / M / "mission.yaml").write_text(SECRET_LINE + "\n")
    reps = _recover(q, sc.SCOPES["add"])
    assert [r.result for r in reps] == ["reported:mission_unreadable"]
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []


def test_broken_state_yaml_in_r4_reports_a_code_only(q, capsys):
    sc.seed(q, "archive")
    (q / "state.yaml").write_text(SECRET_LINE + "\n")
    reps = _recover(q, sc.SCOPES["archive"])
    assert [r.result for r in reps] == ["reported:state_unreadable"]
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []


def test_card_field_values_that_are_not_identifiers_are_never_echoed(q, capsys):
    """worker / status / started_at / id 欄の値は内容。識別子の形でなければ出さない。"""
    sc.seed(q, "pull")
    tdir = q / "missions" / M / "tasks"
    # (a) worker が Worker 名として使えない (パス区切り) → invalid_worker_name。値は出さない
    (tdir / "t001.md").write_text(_card_text(worker='"SECRET RESULT/TOKEN=sk-abc123"', started=G))
    r1 = _recover(q, sc.SCOPES["pull"])
    # (b) 未知の status の値 (holding でない) は R-1 の対象外。既知の status でも generation が識別子でない → 出さない
    (tdir / "t001.md").write_text(_card_text(worker=A, started='"SECRET RESULT TOKEN=sk-abc123"'))
    r2 = _recover(q, store.Scope(cards=((M, "t001"),)))
    # (c) id 欄がファイル名と食い違う (値は内容)
    (tdir / "t001.md").write_text(_card_text(cid='"SECRET RESULT TOKEN=sk-abc123"'))
    r3 = _recover(q, sc.SCOPES["pull"])
    assert [r.result for r in r1] == ["reported:invalid_worker_name"]
    assert "reported:card_unreadable" in [r.result for r in r3]
    assert [r.detail for r in r3 if r.result == "reported:card_unreadable"] == ["id_mismatch"]
    # (b) は generation を出さずに R-1 が書く (世代は identity ファイルに置く。監査ログの generation は None)
    assert all(r.repaired or r.result.startswith("reported:") for r in r2)
    rows = [json.loads(line) for p in (q / "audit").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert all(r["generation"] is None or "SECRET" not in r["generation"] for r in rows)
    assert _leaks(q, capsys, [("reports", repr([r1, r2, r3]))]) == []


def test_assignment_body_contents_are_never_echoed(q, capsys):
    sc.seed(q, "done")
    (q / "assignments" / A).write_text(SECRET + "\n")               # 形の違う枠 (別の Worker の残骸など)
    reps = _recover(q, sc.SCOPES["done"])
    assert "reported:assignment_malformed" in [r.result for r in reps]
    (q / "assignments" / A).write_text(f"{M}:t002\n")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "in_progress", A, G), sc.body())
        t.write_card(M, "t002", sc.card("t002", "in_progress", A, "g2"), sc.body())
    _recover(q, sc.SCOPES["done"])
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []


def test_unknown_status_value_is_redacted_in_detail_and_audit(q, capsys):
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "SECRET RESULT TOKEN=sk-abc123", A, G), sc.body())
    reps = _recover(q, sc.SCOPES["done"])
    # t030: 語彙にない status は card ごと読めない扱い (frozenset 判定で落とさない・別の状態に読まない)。
    # 値は固定コード bad_status の背後に隠れ、出ない。
    assert [r.result for r in reps] == ["reported:card_unreadable", "reported:assignment_target_unreadable"]
    assert {r.detail for r in reps} == {"bad_status"}
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []


def test_serialize_failure_message_does_not_echo_the_text(q):
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_write_text(q / "a", "prefix " + SECRET + " \udcff tail")
    assert ei.value.op == "serialize"
    assert "SECRET" not in str(ei.value) and "sk-abc123" not in str(ei.value)


def test_audit_row_is_gated_even_for_caller_supplied_values(q):
    """`record()` の値が識別子の形でなければ落とす (本文を渡す誤用があっても行に出ない)。"""
    with store.transaction(q, op="x", actor=SECRET) as t:
        t.record(SECRET, SECRET, SECRET, SECRET, SECRET, detail=SECRET)
    raw = "".join(p.read_text() for p in (q / "audit").glob("*.jsonl"))
    row = json.loads(raw)
    assert "SECRET" not in raw and "sk-abc123" not in raw
    assert row["actor"] == "unknown" and row["mission"] is None and row["generation"] is None
    assert row["detail"] == "redacted"


def test_safe_values_still_pass_the_gate(q):
    """門が正常な行まで潰していない (陽性対照): 識別子・既知の status・世代は出る。"""
    with store.transaction(q, op="pull", actor=A) as t:
        t.record(M, "t001", "pending", "in_progress", G, detail="leftover: t003")
    row = json.loads("".join(p.read_text() for p in (q / "audit").glob("*.jsonl")))
    assert (row["mission"], row["task"], row["actor"], row["from_status"], row["to_status"], row["generation"],
            row["detail"]) == (M, "t001", A, "pending", "in_progress", G, "leftover: t003")


def test_invalid_scope_names_are_reported_not_silently_skipped(q, capsys):
    """R-3 / R-4 は不正な slug を黙って飛ばしていた (観測していないのに「何も無い」と同じ形)。値は出さない。"""
    sc.seed(q, "add")
    reps = _recover(q, store.Scope(add_missions=("../" + SECRET,), archive_slugs=("a/b " + SECRET,)))
    assert [r.result for r in reps] == ["reported:invalid_scope_name"]     # 同じ code は 1 件にまとまる
    assert _leaks(q, capsys, [("reports", repr(reps))]) == []
