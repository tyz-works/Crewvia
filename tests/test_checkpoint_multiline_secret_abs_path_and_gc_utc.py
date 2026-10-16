"""PR #307 Codex 3 巡目 (t052): 複数行の秘密 (鍵のブロック) の途中の変更・相対の registry_dir・gc の時刻 (夏時間)。

1. 鍵のブロックの途中だけが変わると BEGIN が hunk に入らず、鍵の行が生で残った。変更のあったファイルは全体 (HEAD 側・worktree 側) を見て、
   鍵の印があるファイル (全体を見られないものも) の hunk は patch に入れない。ペインの 200 行・untracked の切り詰めも同じ規則。
2. `take()` は相対の registry_dir を絶対にしてから作る・返す・再利用する。
3. `checkpoint_gc._taken_at_epoch` は UTC の解析 (`mktime` はローカル時刻で、夏時間でずれた)。
テストは pytest の中で tmp_path に偽の鍵を作る (Bash では作れない)。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import checkpoint_gc as gc  # noqa: E402
import lib_checkpoint as ck  # noqa: E402
from test_checkpoint_lib import MISSION, TASK, XID, git, take, wt  # noqa: E402,F401  (使い捨て repo の道具)

BEGIN = "-----BEGIN RSA PRIVATE " + "KEY-----"
END = "-----END RSA PRIVATE " + "KEY-----"
# 行ごとに違う中身 (8 文字以上の部分文字列で「生で残った」を判定する)
BODY = [f"{chr(65 + i)}{i:02d}" + "qZ7xK2mWp9" * 4 + f"L{i}" for i in range(12)]
FRAGMENTS = [line[3:20] for line in BODY]


def key_block(lines=BODY):
    return "\n".join([BEGIN, *lines, END]) + "\n"


def everything(d):
    out = b""
    for root, dirs, files in os.walk(d):
        out += os.path.relpath(root, d).encode() + b"\n"
        for n in dirs + files:
            out += n.encode() + b"\n"
        for f in files:
            out += pathlib.Path(root, f).read_bytes() + b"\n"
    return out


def assert_no_secret(d, fragments=FRAGMENTS):
    blob = everything(d)
    for frag in fragments:
        assert frag.encode() not in blob, f"鍵の一部 {frag!r} が保存物に生で出た"


def meta_of(d):
    return json.loads((pathlib.Path(d) / "meta.json").read_text())


def commit_config(wt):
    (wt / "config.txt").write_text("header 1\nheader 2\nheader 3\nheader 4\n" + key_block() + "footer 1\nfooter 2\nfooter 3\nfooter 4\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "add config with a key block")


def edit_line(wt, index, new):
    lines = (wt / "config.txt").read_text().split("\n")
    lines[index] = new
    (wt / "config.txt").write_text("\n".join(lines))


# config.txt: 0-3 header, 4 BEGIN, 5-16 body, 17 END, 18-21 footer
@pytest.mark.parametrize("where, index", [("begin line", 4), ("first body line", 5), ("middle body line", 10),
                                          ("last body line", 16), ("end line", 17), ("outside the block", 19)])
def test_changing_any_line_of_a_file_that_holds_a_key_block_saves_no_hunk_of_it(wt, tmp_path, where, index):
    commit_config(wt)
    edit_line(wt, index, f"CHANGED-{where.replace(' ', '-')}")
    (wt / "a.txt").write_text("one\ntwo\nplain change\n")                    # 鍵の無いファイルの hunk は今までどおり残る
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    patch = (d / "dirty.patch").read_text()
    assert "plain change" in patch and "config.txt" not in patch
    meta = meta_of(d)
    assert meta["secret_block_files_dropped"] == 1
    assert meta["files"]["dirty_patch"]["secret_block_files_dropped"] == 1 and not meta["files"]["dirty_patch"]["withheld"]


def test_adding_a_key_block_to_a_tracked_file_drops_that_file(wt, tmp_path):
    (wt / "a.txt").write_text("one\ntwo\n" + key_block())                    # worktree 側にだけ鍵がある
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 1


def test_removing_a_key_block_drops_that_file(wt, tmp_path):
    commit_config(wt)
    (wt / "config.txt").write_text("header 1\nheader 2\nheader 3\nheader 4\nfooter 1\nfooter 2\nfooter 3\nfooter 4\n")   # HEAD 側にだけ鍵がある
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 1


def test_deleting_a_file_that_held_a_key_block_drops_it(wt, tmp_path):
    commit_config(wt)
    (wt / "config.txt").unlink()
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 1


def test_renaming_a_file_that_holds_a_key_block_drops_both_names(wt, tmp_path):
    commit_config(wt)
    git(wt, "mv", "config.txt", "moved.txt")
    (wt / "moved.txt").write_text((wt / "moved.txt").read_text().replace("footer 2", "footer TWO"))
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 2


def test_a_tracked_file_without_a_key_block_keeps_its_hunk_and_the_count_is_zero(wt, tmp_path):
    (wt / "a.txt").write_text("one\nTWO CHANGED\n")
    d = pathlib.Path(take(wt, tmp_path))
    assert "TWO CHANGED" in (d / "dirty.patch").read_text()
    assert meta_of(d)["secret_block_files_dropped"] == 0


def test_the_dropped_file_is_also_absent_from_the_binary_listing(wt, tmp_path):
    (wt / "blob.bin").write_bytes(b"\x00" + key_block().encode() + b"\x00")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "binary with key")
    (wt / "blob.bin").write_bytes(b"\x00changed\x00" + key_block().encode())
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert not (d / "binary-files.txt").exists() or "blob.bin" not in (d / "binary-files.txt").read_text()


def test_a_file_that_cannot_be_read_whole_is_dropped_not_guessed(wt, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root は 000 でも読める")
    (wt / "a.txt").write_text("one\ntwo\nchanged but unreadable\n")
    os.chmod(wt / "a.txt", 0)
    try:
        d = pathlib.Path(take(wt, tmp_path))
    finally:
        os.chmod(wt / "a.txt", 0o644)
    assert not (d / "dirty.patch").exists() or "changed but unreadable" not in (d / "dirty.patch").read_text()
    assert meta_of(d)["secret_block_files_dropped"] == 1


def test_a_file_too_large_to_scan_whole_is_dropped(wt, tmp_path, monkeypatch):
    monkeypatch.setattr(ck, "SCAN_FILE_LIMIT", 64)
    (wt / "a.txt").write_text("one\ntwo\n" + "x" * 200 + "\n")
    d = pathlib.Path(take(wt, tmp_path))
    assert meta_of(d)["secret_block_files_dropped"] == 1
    assert not (d / "dirty.patch").exists() or "xxxx" not in (d / "dirty.patch").read_text()


def test_too_many_dropped_files_withhold_the_whole_patch(wt, tmp_path, monkeypatch):
    monkeypatch.setattr(ck, "MAX_BLOCK_PATHS", 1)
    (wt / "a.txt").write_text(key_block())
    (wt / "b.dat").write_text(key_block())
    (wt / "c.txt").write_text("not tracked but fine\n")
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert not (d / "dirty.patch").exists()
    info = meta_of(d)["files"]["dirty_patch"]
    assert info["withheld"] is True and info["saved"] is False


def test_untracked_file_cut_at_the_limit_inside_a_key_is_not_saved_at_all(wt, tmp_path):
    filler = ("filler line of ordinary text\n" * 2250)
    (wt / "big.txt").write_text(filler + key_block())
    assert len(filler) < ck.UNTRACKED_FILE_LIMIT < len(filler + key_block())
    (wt / "small.txt").write_text("head\n" + key_block() + "tail\n")           # 上限以内は全体を redaction
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert not (d / "untracked" / "big.txt").exists()
    assert "[REDACTED" in (d / "untracked" / "small.txt").read_text()
    assert any(e["path"] == "big.txt" for e in meta_of(d)["files"]["untracked"]["listed_only"])


def pane_with_key(before=150, body=None, after=200):
    body = body or BODY
    return "\n".join([f"pane before {i}" for i in range(before)] + [BEGIN, *body, END] + [f"pane after {i}" for i in range(after)])


def test_pane_tail_that_starts_inside_a_key_shows_none_of_it(wt, tmp_path):
    body = [f"{chr(65 + i % 26)}{i:03d}" + "pW3nR8tY5u" * 4 for i in range(300)]
    fragments = [line[4:22] for line in body]
    text = "\n".join([BEGIN, *body, END] + [f"after {i}" for i in range(40)])      # 末尾 200 行の先頭は鍵の途中
    assert len(text.splitlines()) - 40 > ck.PANE_LINES   # 末尾 200 行は鍵の本体の中から始まる
    d = pathlib.Path(take(wt, tmp_path, pane_text=text))
    assert_no_secret(d, fragments)
    assert "after 39" in (d / "pane.txt").read_text()
    assert meta_of(d)["redactions"] >= 1


def test_pane_tail_inside_an_unterminated_key_shows_none_of_it(wt, tmp_path):
    """END が画面の下にも無い鍵: 200 行の窓には BEGIN も END も見えない。切る前の全体で redaction しないと生で残る。"""
    body = [f"{chr(65 + i % 26)}{i:03d}" + "kD4mZ9vQ1s" * 4 for i in range(400)]
    d = pathlib.Path(take(wt, tmp_path, pane_text="\n".join(["before", BEGIN, *body])))
    assert_no_secret(d, [line[4:22] for line in body])
    assert meta_of(d)["redactions"] >= 1


def test_pane_whose_begin_scrolled_away_hides_everything_up_to_the_end_marker(wt, tmp_path):
    text = "\n".join(BODY + [END] + [f"after {i}" for i in range(5)])               # BEGIN が画面の外
    d = pathlib.Path(take(wt, tmp_path, pane_text=text))
    assert_no_secret(d)
    assert "after 4" in (d / "pane.txt").read_text()


def test_redact_hides_a_stray_end_marker_prefix_and_a_normal_block_as_before():
    out, n = ck.redact("\n".join(BODY[:3] + [END, "keep me"]))
    assert "keep me" in out and BODY[0][3:20] not in out and n == 1
    out2, n2 = ck.redact("a\n" + key_block() + "b\n")
    assert out2.startswith("a\n[REDACTED:1]") and out2.endswith("b\n") and n2 == 1


def test_a_non_utf8_file_name_is_still_excluded_from_the_patch(wt, tmp_path):
    name = os.fsdecode(b"cfg-\xff\xfe.txt")
    (wt / name).write_text("h1\nh2\nh3\nh4\n" + key_block() + "f1\nf2\nf3\nf4\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "non-utf8 name")
    text = (wt / name).read_text().split("\n")
    text[10] = "CHANGED"
    (wt / name).write_text("\n".join(text))
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 1


@pytest.mark.parametrize("n", [64 * 1024, 1024 * 1024])
def test_redact_of_only_end_markers_is_linear(n):
    text = (END + "\n") * (n // 30)
    t0 = time.perf_counter()
    out, count = ck.redact(text)
    assert count == n // 30 and time.perf_counter() - t0 < 5
    big = time.perf_counter()
    ck.redact((END + "\n") * (16 * n // 30))
    assert time.perf_counter() - big < 20


# ---------------------------------------------------------------------------------------------- 相対の registry_dir

def test_relative_registry_dir_is_made_absolute_and_the_result_verifies_and_rerun_is_idempotent(wt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kw = dict(registry_dir="registry", queue_dir="queue", mission=MISSION, task=TASK, execution_id=XID, worktree=str(wt), seconds=30)
    d = ck.take(**kw)
    assert os.path.isabs(d) and d == str(tmp_path / "registry" / "checkpoints" / MISSION / TASK / XID)
    assert ck.verify(d, XID)["execution_id"] == XID
    assert ck.take(**kw) == d                                                   # 同じ take の再実行は exists_mismatch にならない
    assert ck.take(**{**kw, "registry_dir": "./registry/../registry"}) == d


def test_cli_take_with_a_relative_registry_dir_prints_an_absolute_verifiable_path(wt, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    argv = ["take", "--registry-dir", "registry", "--queue-dir", "queue", "--mission", MISSION, "--task", TASK, "--execution", XID]
    (tmp_path / "queue" / "missions" / MISSION / "executions").mkdir(parents=True)
    (tmp_path / "queue" / "missions" / MISSION / "executions" / f"{XID}.json").write_text(
        json.dumps({"git": {"worktree": str(wt)}}))
    assert ck.main(argv) == 0
    printed = capsys.readouterr().out.strip()
    assert os.path.isabs(printed) and ck.main(["verify", printed, "--execution", XID]) == 0
    capsys.readouterr()
    assert ck.main(argv) == 0 and capsys.readouterr().out.strip() == printed


def test_relative_registry_dir_take_then_plan_sh_retire_checkpoint_passes(wt, tmp_path, monkeypatch):
    from execution_e3_helpers import MISSION as M, Box, run
    box = Box(tmp_path / "root", tasks=("t001",))
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    xid = json.loads(p.stdout)["execution_id"]
    monkeypatch.chdir(box.root)
    d = ck.take(registry_dir="registry", queue_dir=str(box.queue), mission=M, task="t001", execution_id=xid, worktree=str(wt), seconds=30)
    r = run(box, "retire", "t001", "--agent", "Ren", "--execution", xid, "--mission", M, "--checkpoint", d, agent="watchdog")
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------------------------- gc の時刻 (夏時間)

TZS = ["UTC", "America/New_York", "Asia/Tokyo", "Europe/London", "Australia/Lord_Howe"]
# 夏時間の中・外・切り替えの日を含む
STAMPS = ["2026-07-01T12:00:00Z", "2026-01-15T00:00:00Z", "2026-03-08T07:00:00Z", "2026-11-01T05:30:00Z", "2026-10-25T00:59:59Z"]


@pytest.fixture
def tz():
    old = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()

    yield set_tz
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def utc_epoch(stamp):
    import datetime
    return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()


@pytest.mark.parametrize("zone", TZS)
def test_taken_at_is_parsed_as_utc_whatever_the_local_time_zone_is(tz, zone):
    tz(zone)
    for stamp in STAMPS:
        assert gc._taken_at_epoch(stamp) == utc_epoch(stamp), (zone, stamp)
    assert gc._taken_at_epoch("not a time") is None and gc._taken_at_epoch(None) is None


@pytest.mark.parametrize("zone", TZS)
def test_the_thirty_day_boundary_gives_the_same_verdict_in_every_time_zone(tz, tmp_path, zone):
    from test_checkpoint_gc import Env
    results = {}
    for name in (zone,):
        tz(name)
        env = Env(tmp_path / name.replace("/", "_"))
        env.card("t001", "done")
        d = env.checkpoint()
        meta_path = pathlib.Path(d) / "meta.json"
        for stamp in STAMPS:
            meta = json.loads(meta_path.read_text())
            meta["taken_at"] = stamp
            meta_path.write_text(json.dumps(meta))
            base = utc_epoch(stamp)
            young, _ = gc.scan(str(env.registry), str(env.queue), now=base + 30 * 86400 - 1)
            exact, _ = gc.scan(str(env.registry), str(env.queue), now=base + 30 * 86400)
            results[stamp] = ((young[0].action, young[0].reason), exact[0].action)
    for stamp, got in results.items():
        assert got == ((gc.KEEP, gc.R_TOO_NEW), gc.REMOVE), (zone, stamp, got)
