"""原子的書き込み (`atomic_write_text` / `atomic_remove`) の単体テスト (原案 STATE-03 / §10.1)。

保証: 書き込みのどの段が失敗しても、元のファイルか完全な新ファイルのどちらかが読める。
失敗は `StoreWriteError` (None / False / 成功に潰さない)。tmp は必ず消す。mode は引き継ぐ。
親ディレクトリを fsync する (replace と unlink の後)。
"""

from __future__ import annotations

import errno
import os
import stat

import pytest

import state_store_scenarios  # noqa: F401  (scripts/ を sys.path に足す)
import lib_state_store as store


def _tmps(d):
    return [p.name for p in d.iterdir() if ".tmp." in p.name]


def test_write_new_file_and_replace_existing(tmp_path):
    p = tmp_path / "a.md"
    store.atomic_write_text(p, "one\n")
    assert p.read_text() == "one\n"
    store.atomic_write_text(p, "two\n")
    assert p.read_text() == "two\n"
    assert _tmps(tmp_path) == []


def test_bytes_are_exact_utf8_no_newline_translation(tmp_path):
    p = tmp_path / "a"
    text = "日本語\r\nと\n混在\r"
    store.atomic_write_text(p, text)
    assert p.read_bytes() == text.encode("utf-8")


def test_creates_missing_parent_directories(tmp_path):
    p = tmp_path / "x" / "y" / "z.md"
    store.atomic_write_text(p, "hi")
    assert p.read_text() == "hi"


def test_new_file_default_mode_and_explicit_mode(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    store.atomic_write_text(a, "x")
    store.atomic_write_text(b, "x", mode=0o600)
    assert stat.S_IMODE(a.stat().st_mode) == 0o644
    assert stat.S_IMODE(b.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o640, 0o664, 0o600, 0o755])
def test_existing_mode_is_preserved(tmp_path, mode):
    p = tmp_path / "a"
    p.write_text("old")
    os.chmod(p, mode)
    store.atomic_write_text(p, "new", mode=0o600)      # mode 引数より既存の mode が勝つ
    assert stat.S_IMODE(p.stat().st_mode) == mode
    assert p.read_text() == "new"


def _fail_nth(monkeypatch, name, nth, err=errno.EIO):
    """`store.<name>` の nth 回目 (0 始まり) の呼び出しだけ OSError にする。"""
    real = getattr(store, name)
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) - 1 == nth:
            raise OSError(err, os.strerror(err))
        return real(*a, **kw)
    monkeypatch.setattr(store, name, flaky)
    return calls


@pytest.mark.parametrize("name,nth,op,new_in_place", [
    ("_sys_write", 0, "write", False),
    ("_sys_fsync", 0, "fsync", False),           # file の fsync
    ("_sys_replace", 0, "replace", False),
    ("_sys_fsync", 1, "fsync_dir", True),        # 置換の後の親 dir の fsync
])
def test_each_stage_failure_keeps_a_readable_file_and_leaves_no_tmp(
        tmp_path, monkeypatch, name, nth, op, new_in_place):
    p = tmp_path / "a.md"
    p.write_text("ORIGINAL")
    _fail_nth(monkeypatch, name, nth)
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_write_text(p, "NEW")
    assert ei.value.op == op and ei.value.errno == errno.EIO and ei.value.path == str(p)
    # 元のファイルか完全な新ファイルのどちらか (途中のものは読めない)
    assert p.read_text() == ("NEW" if new_in_place else "ORIGINAL")
    assert _tmps(tmp_path) == []


def test_failure_on_a_new_file_leaves_nothing_behind(tmp_path, monkeypatch):
    p = tmp_path / "new.md"
    _fail_nth(monkeypatch, "_sys_fsync", 0)
    with pytest.raises(store.StoreWriteError):
        store.atomic_write_text(p, "NEW")
    assert not p.exists() and _tmps(tmp_path) == []


def test_mkstemp_failure_is_an_error_and_original_survives(tmp_path, monkeypatch):
    p = tmp_path / "a"
    p.write_text("ORIGINAL")

    def boom(*a, **kw):
        raise OSError(errno.ENOSPC, "no space")
    monkeypatch.setattr(store.tempfile, "mkstemp", boom)
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_write_text(p, "NEW")
    assert ei.value.op == "mkstemp" and ei.value.errno == errno.ENOSPC
    assert p.read_text() == "ORIGINAL"


def test_serialize_failure_never_touches_the_directory(tmp_path):
    p = tmp_path / "a"
    p.write_text("ORIGINAL")
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_write_text(p, "bad \udcff surrogate")
    assert ei.value.op == "serialize"
    assert p.read_text() == "ORIGINAL" and _tmps(tmp_path) == []


def test_interrupt_between_stages_still_cleans_the_tmp(tmp_path):
    p = tmp_path / "a"
    p.write_text("ORIGINAL")

    def hook(point, path):
        if point == "atomic:synced":
            raise KeyboardInterrupt
    store.FAULT_HOOK = hook
    try:
        with pytest.raises(KeyboardInterrupt):
            store.atomic_write_text(p, "NEW")
    finally:
        store.FAULT_HOOK = None
    assert p.read_text() == "ORIGINAL" and _tmps(tmp_path) == []


def test_parent_directory_is_fsynced_after_replace_and_after_unlink(tmp_path, monkeypatch):
    """親 dir の fsync が**置換の後**に、実際に親 dir の fd に対して呼ばれる (今どこにも無い保証)。"""
    real_fsync = os.fsync
    events = []

    def spy(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(kind)
        return real_fsync(fd)
    monkeypatch.setattr(store, "_sys_fsync", spy)
    real_replace = store._sys_replace
    monkeypatch.setattr(store, "_sys_replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])

    p = tmp_path / "a"
    store.atomic_write_text(p, "x")
    assert events == ["file", "replace", "dir"]

    events.clear()
    assert store.atomic_remove(p) is True
    assert events == ["dir"]


@pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOTSUP])
def test_dir_fsync_unsupported_filesystem_is_tolerated_but_other_errors_are_not(tmp_path, monkeypatch, err):
    real = os.fsync

    def only_dirs_unsupported(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, "unsupported")
        return real(fd)
    monkeypatch.setattr(store, "_sys_fsync", only_dirs_unsupported)
    store.atomic_write_text(tmp_path / "ok", "x")           # 黙って続行してよい (この 2 つだけ)
    assert (tmp_path / "ok").read_text() == "x"


def test_dir_fsync_eio_is_raised_not_swallowed(tmp_path, monkeypatch):
    real = os.fsync

    def eio_on_dir(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "io")
        return real(fd)
    monkeypatch.setattr(store, "_sys_fsync", eio_on_dir)
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_write_text(tmp_path / "a", "x")
    assert ei.value.op == "fsync_dir"


def test_remove_missing_returns_false_and_other_errors_raise(tmp_path, monkeypatch):
    assert store.atomic_remove(tmp_path / "nope") is False
    p = tmp_path / "a"
    p.write_text("x")

    def eperm(path):
        raise OSError(errno.EPERM, "denied")
    monkeypatch.setattr(store, "_sys_unlink", eperm)
    with pytest.raises(store.StoreWriteError) as ei:
        store.atomic_remove(p)
    assert ei.value.op == "unlink" and p.exists()


def test_tmp_name_never_matches_a_task_card_name():
    import lib_task_cards as cards
    for name in (".t001.md.tmp.abc123", ".state.yaml.tmp.x"):
        assert not cards.TASK_FILENAME_RE.fullmatch(name)


def test_write_into_unwritable_directory_is_an_error_not_a_silent_false(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root は権限で止まらない")
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        with pytest.raises(store.StoreWriteError):
            store.atomic_write_text(d / "a", "x")
    finally:
        os.chmod(d, 0o700)
