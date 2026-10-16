"""lib_checkpoint.py (vNext 02 PR-G): 退役の直前に worktree を読み取り専用で保存する。

設計 `knowledge/vnext-02-attempt-reopen-checkpoint.md` §5.2 / §5.3。本物の git の使い捨て repo で走らせる
(git を差し替えない。textconv が起動しないことも本物の git で見る)。保存物は全部 `tmp_path` の下で、本番の
registry / queue には触れない。
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_checkpoint as ck  # noqa: E402

MISSION = "m-ck"
TASK = "t001"
XID = "ex-" + "1" * 32
OTHER_XID = "ex-" + "2" * 32

# 秘密らしい値は実行時に組み立てる (ソースに実在しそうなトークンを置かない)
GH = "gh" + "p_" + "A" * 36
SK = "sk" + "-" + "b" * 32
AWS = "AK" + "IA" + "C" * 16
SLACK = "xox" + "b-" + "1234567890-abcdefghij"
BEARER_VALUE = "d" * 40
OP_REF = "op" + "://vault/item/field"
ASSIGN_VALUE = "hunter2hunter2"
PRIV_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + "Z" * 40
PRIVATE_KEY = f"-----BEGIN RSA PRIVATE KEY-----\n{PRIV_BODY}\n-----END RSA PRIVATE KEY-----"
SECRET_VALUES = [GH, SK, AWS, SLACK, BEARER_VALUE, OP_REF, ASSIGN_VALUE, PRIV_BODY]
SECRET_TEXT = "\n".join([
    f"token = {GH}", f"key {SK}", f"aws {AWS}", f"slack {SLACK}", f"Authorization: Bearer {BEARER_VALUE}",
    f"ref {OP_REF}", f"PASSWORD={ASSIGN_VALUE}", PRIVATE_KEY, ""])


def git(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True, env=env)


@pytest.fixture
def wt(tmp_path):
    """tracked の a.txt と b.dat (NUL を含む) を commit 済みの worktree。"""
    d = tmp_path / "wt"
    d.mkdir()
    git(d, "init", "-q", "-b", "task-branch")
    (d / "a.txt").write_text("one\ntwo\n")
    (d / "b.dat").write_bytes(b"\x00BIN-ORIGINAL\x00")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


def take(wt, tmp_path, **kw):
    kw.setdefault("seconds", 30)
    return ck.take(registry_dir=tmp_path / "registry", queue_dir=tmp_path / "queue", mission=MISSION, task=TASK,
                   execution_id=XID, worktree=str(wt), **kw)


def all_saved_bytes(d):
    out = b""
    for root, _dirs, files in os.walk(d):
        for f in files:
            out += pathlib.Path(root, f).read_bytes() + b"\n" + f.encode() + b"\n"
        out += root.encode()
    return out


def meta_of(d):
    return json.loads((pathlib.Path(d) / "meta.json").read_text())


# ---------------------------------------------------------------- 内容と上限

def test_contents_modes_and_meta(wt, tmp_path):
    (wt / "a.txt").write_text("one\nTWO CHANGED\n")
    (wt / "new.txt").write_text("brand new file\n")
    d = pathlib.Path(take(wt, tmp_path, reason="hard_idle", pane_text="line1\nline2\n"))
    assert d == tmp_path / "registry" / "checkpoints" / MISSION / TASK / XID
    meta = meta_of(d)
    assert (meta["execution_id"], meta["mission"], meta["task"], meta["reason"]) == (XID, MISSION, TASK, "hard_idle")
    assert meta["branch"] == "task-branch" and len(meta["head"]) == 40 and meta["dirty"] is True
    assert meta["binary_restorable"] is False and meta["binary_files"] == 0
    assert "TWO CHANGED" in (d / "dirty.patch").read_text()
    assert (d / "untracked" / "new.txt").read_text() == "brand new file\n"
    assert "new.txt" in (d / "status.txt").read_text()
    assert (d / "pane.txt").read_text() == "line1\nline2\n"
    # 0700 / 0600
    for root, dirs, files in os.walk(d):
        for n in dirs:
            assert stat.S_IMODE(os.stat(os.path.join(root, n)).st_mode) == 0o700
        for n in files:
            assert stat.S_IMODE(os.stat(os.path.join(root, n)).st_mode) == 0o600
    assert not any(p.name.endswith(".partial") for p in d.parent.iterdir())


def test_clean_worktree_dirty_false_and_no_patch(wt, tmp_path):
    d = pathlib.Path(take(wt, tmp_path))
    assert meta_of(d)["dirty"] is False
    assert not (d / "dirty.patch").exists() and not (d / "pane.txt").exists()
    assert meta_of(d)["files"]["pane"]["saved"] is False


def test_patch_is_cut_at_one_megabyte_and_marked_truncated(wt, tmp_path):
    (wt / "a.txt").write_text("".join(f"line {i} {'x' * 60}\n" for i in range(30000)))     # 約 2MB の差分
    d = pathlib.Path(take(wt, tmp_path))
    info = meta_of(d)["files"]["dirty_patch"]
    assert info["truncated"] is True and info["original_size"] > ck.PATCH_LIMIT
    assert (d / "dirty.patch").stat().st_size <= ck.PATCH_LIMIT
    assert info["saved_size"] == (d / "dirty.patch").stat().st_size


def test_untracked_limits_per_file_and_total(wt, tmp_path):
    (wt / "small.txt").write_text("small\n")
    (wt / "big.txt").write_text("x" * (ck.UNTRACKED_FILE_LIMIT + 1))
    (wt / "exact.txt").write_text("y" * ck.UNTRACKED_FILE_LIMIT)
    d = pathlib.Path(take(wt, tmp_path))
    info = meta_of(d)["files"]["untracked"]
    assert (d / "untracked" / "small.txt").exists() and (d / "untracked" / "exact.txt").exists()
    assert not (d / "untracked" / "big.txt").exists()
    assert {"path": "big.txt", "reason": "too_large", "size": ck.UNTRACKED_FILE_LIMIT + 1} in info["listed_only"]
    # 合計 2MB: 64KB ちょうどを 40 個 (2.5MB) → 32 個目まで保存、残りは一覧だけ
    for p in (wt).glob("*.txt"):
        p.unlink()
    for i in range(40):
        (wt / f"f{i:02d}.txt").write_text(f"{i:02d}" * (ck.UNTRACKED_FILE_LIMIT // 2))
    d2 = pathlib.Path(ck.take(registry_dir=tmp_path / "registry2", queue_dir=tmp_path / "queue", mission=MISSION,
                              task=TASK, execution_id=XID, worktree=str(wt), seconds=30))
    info2 = meta_of(d2)["files"]["untracked"]
    assert info2["total_bytes"] <= ck.UNTRACKED_TOTAL_LIMIT
    assert info2["saved"] == 32 and sum(1 for e in info2["listed_only"] if e["reason"] == "total_limit") == 8


def test_symlinks_are_listed_and_never_followed(wt, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE-CONTENT-MARKER\n")
    os.symlink(outside, wt / "link.txt")
    os.symlink(tmp_path, wt / "dirlink")
    d = pathlib.Path(take(wt, tmp_path))
    assert b"OUTSIDE-CONTENT-MARKER" not in all_saved_bytes(d)
    assert not (d / "untracked" / "link.txt").exists()
    reasons = {e["path"]: e["reason"] for e in meta_of(d)["files"]["untracked"]["listed_only"]}
    assert reasons.get("link.txt") == "symlink" and reasons.get("dirlink") == "symlink"


# ---------------------------------------------------------------- バイナリ

def test_binary_content_is_never_saved_tracked_or_untracked(wt, tmp_path):
    (wt / "b.dat").write_bytes(b"\x00BIN-MODIFIED-MARKER\x00" + bytes(range(256)))      # tracked の変更
    (wt / "new.bin").write_bytes(b"\x00UNTRACKED-BIN-MARKER\x00")                         # untracked
    d = pathlib.Path(take(wt, tmp_path))
    blob = all_saved_bytes(d)
    assert b"BIN-MODIFIED-MARKER" not in blob and b"UNTRACKED-BIN-MARKER" not in blob and b"BIN-ORIGINAL" not in blob
    # `--binary` の差分は base85 で符号化されて生のマーカーでは見えない: 符号化された塊そのものが無いこと
    assert b"GIT binary patch" not in blob and b"literal " not in (d / "dirty.patch").read_bytes()
    assert "Binary files" in (d / "dirty.patch").read_text()
    lines = (d / "binary-files.txt").read_text().splitlines()
    by_name = {l.split("\t")[0]: l.split("\t") for l in lines}
    assert set(by_name) == {"b.dat", "new.bin"}
    expect = git(wt, "hash-object", "--", "b.dat").stdout.strip()
    assert by_name["b.dat"][2] == expect and int(by_name["b.dat"][1]) == (wt / "b.dat").stat().st_size
    assert meta_of(d)["binary_files"] == 2 and meta_of(d)["binary_restorable"] is False
    assert not (d / "untracked" / "new.bin").exists()


# ---------------------------------------------------------------- 秘密

SECRET_NAMES = [".env", ".env.local", "server.pem", "id.key", "cert.p12", "cert.pfx", "id_rsa", "x_dsa", "k_ed25519",
                "my-credentials.json", "app.secret", "app.secrets", "app.secret.yaml"]


def test_secret_named_files_are_not_saved_anywhere(wt, tmp_path):
    for n in SECRET_NAMES:
        (wt / n).write_text(f"FILE-{n}-CONTENT-MARKER\n")               # untracked
    (wt / "dir.credentials").mkdir()
    (wt / "dir.credentials" / "inner.txt").write_text("INNER-MARKER\n")
    (wt / "tracked.pem").write_text("TRACKED-PEM-V1\n")
    git(wt, "add", "tracked.pem")
    git(wt, "commit", "-q", "-m", "pem")
    (wt / "tracked.pem").write_text("TRACKED-PEM-V2-MARKER\n")          # tracked の変更 → patch の節を落とす
    (wt / "a.txt").write_text("one\nvisible change\n")
    d = pathlib.Path(take(wt, tmp_path))
    blob = all_saved_bytes(d)
    assert b"CONTENT-MARKER" not in blob and b"TRACKED-PEM" not in blob and b"INNER-MARKER" not in blob
    assert "visible change" in (d / "dirty.patch").read_text()
    meta = meta_of(d)
    assert meta["secret_files_excluded"] >= len(SECRET_NAMES) + 2
    assert meta["files"]["dirty_patch"]["secret_files_dropped"] == 1
    for n in SECRET_NAMES:
        assert not (d / "untracked" / n).exists()


def test_secret_contents_are_redacted_in_every_saved_file_and_counted(wt, tmp_path):
    (wt / "a.txt").write_text(SECRET_TEXT)                              # patch に載る
    (wt / "notes.txt").write_text(SECRET_TEXT)                          # untracked
    d = pathlib.Path(take(wt, tmp_path, pane_text="pane " + SECRET_TEXT))
    blob = all_saved_bytes(d).decode("utf-8", "replace")
    for v in SECRET_VALUES:
        assert v not in blob, f"生の値が残った: {v[:6]}…"
    assert "[REDACTED:" in (d / "dirty.patch").read_text()
    assert "[REDACTED:" in (d / "untracked" / "notes.txt").read_text()
    assert "[REDACTED:" in (d / "pane.txt").read_text()
    assert meta_of(d)["redactions"] >= 3 * 8


@pytest.mark.parametrize("raw", [GH, SK, AWS, SLACK, OP_REF, "github" + "_pat_" + "E" * 40, "gh" + "o_" + "F" * 30])
def test_each_token_pattern_is_replaced(raw):
    out, n = ck.redact(f"before {raw} after")
    assert raw not in out and n == 1 and out == "before [REDACTED:1] after"


def test_assignment_and_bearer_keep_the_key_and_replace_the_value():
    out, n = ck.redact(f"API_KEY: {ASSIGN_VALUE}\nGITHUB_TOKEN={ASSIGN_VALUE}\nAuthorization: Bearer {BEARER_VALUE}\n")
    assert ASSIGN_VALUE not in out and BEARER_VALUE not in out and n == 3
    import re
    assert re.search(r"API_KEY: \[REDACTED:\d+\]\n", out) and re.search(r"GITHUB_TOKEN=\[REDACTED:\d+\]\n", out)
    assert re.search(r"Authorization: Bearer \[REDACTED:\d+\]\n", out)
    assert sorted(re.findall(r"REDACTED:(\d+)", out)) == ["1", "2", "3"]       # 通し番号は重ならない


def test_private_key_block_is_replaced_and_unterminated_block_goes_to_the_end():
    out, n = ck.redact(f"x\n{PRIVATE_KEY}\ny\n")
    assert PRIV_BODY not in out and n == 1 and out.startswith("x\n[REDACTED:1]\ny")
    unterminated = f"x\n-----BEGIN PRIVATE KEY-----\n{PRIV_BODY}\nmore\n"
    out2, n2 = ck.redact(unterminated)
    assert PRIV_BODY not in out2 and "more" not in out2 and n2 == 1


@pytest.mark.parametrize("line", [
    "A" * (1024 * 1024),
    "TOKEN" * 200000,
    "sk-" * 350000,
    "Bearer " * 150000,
    "-----BEGIN PRIVATE KEY-----" * 40000,
    "ghp_" + "a" * 1000000,
    "PASSWORD=" + "z" * 1000000,
    "op://" + "q" * 1000000,
    (" " * 7 + "=") * 100000,
])
def test_redaction_is_linear_on_one_megabyte_adversarial_lines(line):
    t0 = time.monotonic()
    ck.redact(line)
    assert time.monotonic() - t0 < 5.0, "バックトラックしている疑い (1MB の 1 行が 5 秒を超えた)"


def test_a_redaction_exception_means_the_file_is_not_saved(wt, tmp_path, monkeypatch):
    (wt / "a.txt").write_text("one\nchanged\n")
    (wt / "notes.txt").write_text("hello\n")
    real = ck.redact

    def boom(text):
        if "hello" in text or "changed" in text:
            raise RuntimeError("redaction exploded")
        return real(text)

    monkeypatch.setattr(ck, "redact", boom)
    d = pathlib.Path(take(wt, tmp_path, pane_text="pane text\n"))
    assert not (d / "dirty.patch").exists() and not (d / "untracked" / "notes.txt").exists()
    blob = all_saved_bytes(d)
    assert b"changed" not in blob and b"hello" not in blob
    meta = meta_of(d)
    assert meta["redaction_failed_files"] >= 2 and meta["files"]["dirty_patch"]["saved"] is False
    assert {"path": "notes.txt", "reason": "redaction_failed"} in meta["files"]["untracked"]["listed_only"]


# ---------------------------------------------------------------- 読み取り専用 (textconv / 外部 diff を起動しない)

def test_textconv_and_external_diff_are_not_launched_by_a_real_git(wt, tmp_path):
    marker = tmp_path / "launched.marker"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    script.chmod(0o755)
    (wt / ".gitattributes").write_text("*.conv diff=evil\n")
    (wt / "x.conv").write_text("v1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "conv")
    git(wt, "config", "diff.evil.textconv", str(script))
    git(wt, "config", "diff.external", str(script))
    (wt / "x.conv").write_text("v2\n")
    # 前提の陽性対照: 素の `git diff` ならこの設定は textconv を起動する
    git(wt, "diff", "HEAD")
    assert marker.exists(), "前提が成り立たない (textconv が起動しない設定だった)"
    marker.unlink()
    d = pathlib.Path(take(wt, tmp_path))
    assert not marker.exists(), "lib_checkpoint が textconv / 外部 diff を起動した"
    assert "v2" in (d / "dirty.patch").read_text()


def test_the_worktree_is_left_exactly_as_it_was(wt, tmp_path):
    (wt / "a.txt").write_text("one\nchanged\n")
    (wt / "new.txt").write_text("n\n")

    def fingerprint():
        snap = {}
        for root, dirs, files in os.walk(wt):
            for f in files:
                p = pathlib.Path(root, f)
                st = p.lstat()
                snap[str(p.relative_to(wt))] = (st.st_mtime_ns, st.st_size, st.st_mode)
        # 観測側が index を更新しない (`status` は既定で index を refresh して書く)
        return snap, git(wt, "--no-optional-locks", "status", "--porcelain=v1").stdout

    before = fingerprint()
    take(wt, tmp_path)
    assert fingerprint() == before, "worktree (index を含む .git) が変わった"


# ---------------------------------------------------------------- 無いもの・期限・照合

def test_worktree_comes_from_the_record_and_missing_things_are_distinguished(wt, tmp_path):
    queue = tmp_path / "queue"
    rec_dir = queue / "missions" / MISSION / "executions"
    rec_dir.mkdir(parents=True)

    def write(git_field):
        (rec_dir / f"{XID}.json").write_text(json.dumps({"execution_id": XID, "git": git_field}))

    kw = dict(registry_dir=tmp_path / "registry", queue_dir=queue, mission=MISSION, task=TASK, execution_id=XID)
    with pytest.raises(ck.NothingToSave):                                # record が無い
        ck.take(**kw)
    write({"worktree": None})
    with pytest.raises(ck.NothingToSave):                                # まだ worktree を作る前に終わった
        ck.take(**kw)
    write({"worktree": str(tmp_path / "gone")})
    with pytest.raises(ck.CheckpointError) as e:                         # 記録はあるが dir が無い = 失敗
        ck.take(**kw)
    assert e.value.code == ck.E_NO_WORKTREE and not isinstance(e.value, ck.NothingToSave)
    (rec_dir / f"{XID}.json").write_text("{not json")
    with pytest.raises(ck.CheckpointError) as e2:
        ck.take(**kw)
    assert e2.value.code == ck.E_RECORD
    write({"worktree": str(wt)})
    (wt / "a.txt").write_text("from record\n")
    d = ck.take(**kw)                                                    # record の git.worktree から (ペイン無し)
    assert "from record" in (pathlib.Path(d) / "dirty.patch").read_text()
    assert not (pathlib.Path(d) / "pane.txt").exists()
    assert not list((tmp_path / "registry").rglob("*.partial"))


def test_deadline_stops_the_take_and_leaves_no_directory(wt, tmp_path):
    for i in range(5):
        (wt / f"f{i}.txt").write_text("x\n")
    ticks = iter(range(0, 10_000))
    # 時計が 1 呼び出しごとに 10 秒進む → 期限 20 秒は数手で切れる
    with pytest.raises(ck.CheckpointError) as e:
        ck.take(registry_dir=tmp_path / "registry", queue_dir=tmp_path / "q", mission=MISSION, task=TASK,
                execution_id=XID, worktree=str(wt), seconds=20, now=lambda: next(ticks) * 10.0)
    assert e.value.code == ck.E_DEADLINE
    root = tmp_path / "registry" / "checkpoints" / MISSION / TASK
    assert not (root / XID).exists() and not (root / (XID + ck.PARTIAL_SUFFIX)).exists()


def test_a_failure_midway_removes_the_partial_directory(wt, tmp_path, monkeypatch):
    (wt / "a.txt").write_text("one\nchanged\n")
    monkeypatch.setattr(ck, "_save_untracked", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ck.CheckpointError) as e:
        take(wt, tmp_path)
    assert e.value.code == ck.E_WRITE
    root = tmp_path / "registry" / "checkpoints" / MISSION / TASK
    assert not list(root.iterdir())


def test_second_take_of_the_same_attempt_reuses_the_finished_directory(wt, tmp_path):
    (wt / "a.txt").write_text("one\nfirst\n")
    d1 = take(wt, tmp_path)
    stamp = (pathlib.Path(d1) / "meta.json").read_text()
    (wt / "a.txt").write_text("one\nsecond\n")
    d2 = take(wt, tmp_path)
    assert d1 == d2 and (pathlib.Path(d2) / "meta.json").read_text() == stamp
    assert "first" in (pathlib.Path(d2) / "dirty.patch").read_text()


def test_verify_binds_the_dir_to_the_execution(wt, tmp_path):
    d = take(wt, tmp_path)
    assert ck.verify(d, XID)["execution_id"] == XID
    assert ck.verify(d)["execution_id"] == XID
    with pytest.raises(ck.CheckpointError) as e:
        ck.verify(d, OTHER_XID)                                          # 別の試行の ID を名乗る
    assert e.value.detail if hasattr(e.value, "detail") else "execution_mismatch" in str(e.value)
    assert "execution_mismatch" in str(e.value)
    # 別の試行の dir を、meta をそのままに別名で置いても通らない (dir 名と meta の ID が違う)
    other = pathlib.Path(d).parent / OTHER_XID
    os.rename(d, other)
    with pytest.raises(ck.CheckpointError) as e2:
        ck.verify(other, OTHER_XID)
    assert "dir_name_mismatch" in str(e2.value)
    for bad in (tmp_path / "nope", "relative/dir"):
        with pytest.raises(ck.CheckpointError):
            ck.verify(bad)
    link = tmp_path / "link"
    os.symlink(other, link)
    with pytest.raises(ck.CheckpointError):
        ck.verify(link)


def test_arguments_are_checked_before_anything_is_written(tmp_path):
    for kw in (dict(execution_id="ex-bad"), dict(mission="../escape"), dict(task="a/b")):
        args = dict(registry_dir=tmp_path / "r", queue_dir=tmp_path / "q", mission=MISSION, task=TASK, execution_id=XID)
        args.update(kw)
        with pytest.raises(ck.CheckpointError) as e:
            ck.take(**args)
        assert e.value.code == ck.E_ARGS
    assert not (tmp_path / "r").exists()


def test_cli_take_and_verify(wt, tmp_path):
    queue = tmp_path / "queue"
    (queue / "missions" / MISSION / "executions").mkdir(parents=True)
    (queue / "missions" / MISSION / "executions" / f"{XID}.json").write_text(
        json.dumps({"execution_id": XID, "git": {"worktree": str(wt)}}))
    (wt / "a.txt").write_text("cli\n")
    script = REPO / "scripts" / "lib_checkpoint.py"
    out = subprocess.run([sys.executable, str(script), "take", "--registry-dir", str(tmp_path / "registry"),
                          "--queue-dir", str(queue), "--mission", MISSION, "--task", TASK, "--execution", XID],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    d = out.stdout.strip()
    ok = subprocess.run([sys.executable, str(script), "verify", d, "--execution", XID], capture_output=True, text=True)
    assert ok.returncode == 0
    bad = subprocess.run([sys.executable, str(script), "verify", d, "--execution", OTHER_XID], capture_output=True,
                         text=True)
    assert bad.returncode == 1 and "verify_failed" in bad.stderr


# ---------------------------------------------------------------- セキュリティレビューの指摘 (PR #307)

@pytest.mark.parametrize("text", [
    '{"password": "hunter2 with spaces"}',
    "{'api_key': 'hunter2 with spaces'}",
    'password: "hunter2 with spaces"',
    "SECRET_KEY = 'hunter2 with spaces'",
    'config = {"token":"hunter2nospace"}',
    "PASSWD='hunter2 with spaces'",
    "export API_KEY=hunter2nospace",
])
def test_quoted_json_yaml_and_dict_forms_are_redacted(text):
    out, n = ck.redact(text)
    assert "hunter2" not in out and "with spaces" not in out and "[REDACTED:" in out and n >= 1


def test_quoted_assignment_is_linear_on_adversarial_input():
    for line in ('"password":' * 100000, "'token'='" * 100000, 'PASSWORD="' + "a" * 1000000):
        t0 = time.monotonic()
        ck.redact(line)
        assert time.monotonic() - t0 < 5.0


def test_a_repo_local_clean_filter_is_never_launched_and_the_checkpoint_is_refused(wt, tmp_path):
    marker = tmp_path / "filter.marker"
    script = tmp_path / "clean.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o755)
    (wt / ".gitattributes").write_text("*.flt filter=evil\n")
    (wt / "x.flt").write_text("v1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "flt")
    git(wt, "config", "filter.evil.clean", str(script))
    (wt / "x.flt").write_text("v2\n")
    # 陽性対照: 素の `git diff HEAD` はこの clean filter を起動する (前提)
    git(wt, "diff", "HEAD")
    assert marker.exists(), "前提が成り立たない (clean filter が起動しない設定だった)"
    marker.unlink()
    with pytest.raises(ck.CheckpointError) as e:
        take(wt, tmp_path)
    assert "local_filters" in str(e.value) and not marker.exists(), "lib_checkpoint が clean filter を起動した"
    assert not list((tmp_path / "registry").rglob("*.partial")) and not (tmp_path / "registry" / "checkpoints" / MISSION / TASK / XID).exists()


def test_global_config_filters_are_not_read(wt, tmp_path, monkeypatch):
    marker = tmp_path / "global.marker"
    script = tmp_path / "gclean.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o755)
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text(f"[filter \"evil\"]\n\tclean = {script}\n")
    (wt / ".gitattributes").write_text("*.flt filter=evil\n")
    (wt / "x.flt").write_text("v1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "flt")
    (wt / "x.flt").write_text("v2\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    d = take(wt, tmp_path)
    assert not marker.exists(), "global の filter を起動した"
    assert "v2" in (pathlib.Path(d) / "dirty.patch").read_text()


def test_a_hung_git_is_cut_at_the_deadline_even_while_reading(wt, tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nsleep 30\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    t0 = time.monotonic()
    with pytest.raises(ck.CheckpointError) as e:
        take(wt, tmp_path, seconds=1)
    assert e.value.code == ck.E_DEADLINE and time.monotonic() - t0 < 5
    assert not list((tmp_path / "registry").rglob("*.partial"))


# ---------------------------------------------------------------- セキュリティレビュー 2 巡目 (PR #307)

@pytest.mark.parametrize("text", [
    r'{"password": "ab\"cd hunter2 tail"}',
    r"{'token': 'ab\'cd hunter2 tail'}",
    '"password": "hunter2 unterminated and long',
    "SECRET='hunter2 unterminated",
])
def test_escaped_and_unterminated_quotes_do_not_leak_a_tail(text):
    out, _ = ck.redact(text)
    assert "hunter2" not in out and "tail" not in out and "unterminated" not in out and "cd" not in out.replace("REDACTED", "")


def test_quote_forms_stay_linear_on_adversarial_input():
    for line in ('PASSWORD="' + '\\"' * 500000, 'token="' * 100000, "secret='" + "\\" * 1000000):
        t0 = time.monotonic()
        ck.redact(line)
        assert time.monotonic() - t0 < 5.0


def test_a_filter_defined_through_include_or_worktree_config_is_refused(wt, tmp_path):
    marker = tmp_path / "inc.marker"
    script = tmp_path / "c.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o755)
    inc = tmp_path / "extra.cfg"
    inc.write_text(f'[filter "evil"]\n\tclean = {script}\n')
    (wt / ".gitattributes").write_text("*.flt filter=evil\n")
    (wt / "x.flt").write_text("v1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "flt")
    git(wt, "config", "include.path", str(inc))
    (wt / "x.flt").write_text("v2\n")
    with pytest.raises(ck.CheckpointError) as e:
        take(wt, tmp_path)
    assert "local_filters" in str(e.value) and not marker.exists()
    git(wt, "config", "--unset", "include.path")
    git(wt, "config", "extensions.worktreeConfig", "true")
    (wt / ".git" / "config.worktree").write_text(f'[filter "evil"]\n\tclean = {script}\n')
    with pytest.raises(ck.CheckpointError) as e2:
        take(wt, tmp_path)
    assert "local_filters" in str(e2.value) and not marker.exists()


def test_diff_and_status_do_not_descend_into_submodules(wt, tmp_path, monkeypatch):
    calls = []
    real = ck._run_git

    def spy(clock, worktree, *args, **kw):
        calls.append(args)
        return real(clock, worktree, *args, **kw)

    monkeypatch.setattr(ck, "_run_git", spy)
    take(wt, tmp_path)
    for verb in ("status", "diff"):
        seen = [a for a in calls if a and a[0] == verb]
        assert seen and all("--ignore-submodules=all" in a for a in seen), (verb, seen)
