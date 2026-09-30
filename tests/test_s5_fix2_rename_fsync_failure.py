"""S5 fix 2 巡目 (t033 / PR #259 Codex P2): rename 済み・親 dir の fsync 失敗で state.yaml を更新せず抜けない。

`durable_rename` は `os.rename` が成功した後に親 dir の fsync が失敗すると `StoreWriteError(committed=True)` を出す。
以前の `cmd_archive` / `cmd_init --force` はそれを die に写したので、mission dir は移動済みなのに
`active_missions` / `default_mission` が元の名前を指したまま残った (元が無いので再試行でも直らない)。
今は `_move_mission_dir` が警告して続行し、state.yaml の更新まで最後までやる (exit code 0)。

収束の表 (途中の 1 歩が「済んだが耐久性だけ失敗」したとき): `knowledge/state-store.md` §6.2。
"""

from __future__ import annotations

import os
import stat

import pytest

import test_plan_sh_state_store_cutover as cut
import lib_state_store as store


class FailDirFsyncAfterRename:
    """`os.rename` の後の**最初の**ディレクトリ fsync だけを EIO にする (実際の呼び出しは通す)。"""

    def __init__(self, monkeypatch):
        self.renamed = False
        self.failed = 0
        real_fsync, real_rename = os.fsync, os.rename

        def rename(src, dst):
            real_rename(src, dst)
            self.renamed = True

        def fsync(fd):
            if self.renamed and not self.failed and stat.S_ISDIR(os.fstat(fd).st_mode):
                self.failed += 1
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        monkeypatch.setattr(os, "rename", rename)
        monkeypatch.setattr(os, "fsync", fsync)


def _state(root):
    text = (root / "queue" / "state.yaml").read_text()
    return text


def test_archive_finishes_state_update_when_only_the_parent_fsync_fails(tmp_path, monkeypatch, capsys):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)
    assert slug in _state(tmp_path)
    fault = FailDirFsyncAfterRename(monkeypatch)

    ns["cmd_archive"]([slug])                                     # 例外・SystemExit なし = exit 0

    assert fault.failed == 1, "注入した fsync 失敗に届いていない"
    assert (tmp_path / "queue" / "archive" / slug).is_dir() and not (tmp_path / "queue" / "missions" / slug).exists()
    state = _state(tmp_path)
    assert slug not in state, f"state.yaml がまだ移動済みの mission を指している:\n{state}"
    err = capsys.readouterr().err
    assert "fsync" in err and "耐久性" in err, err


def test_init_force_finishes_state_update_when_only_the_parent_fsync_fails(tmp_path, monkeypatch, capsys):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)
    fault = FailDirFsyncAfterRename(monkeypatch)

    ns["cmd_init"](["Again", "--mission", slug, "--force"])

    assert fault.failed == 1
    backups = [p.name for p in (tmp_path / "queue" / "archive").iterdir()]
    assert any(n.startswith(f"{slug}.overwritten-") for n in backups), backups
    assert (tmp_path / "queue" / "missions" / slug / "mission.yaml").exists()      # 作り直された
    assert slug in _state(tmp_path)                                                # 再登録まで済んでいる
    assert "耐久性" in capsys.readouterr().err


def test_a_rename_that_really_failed_still_dies_and_leaves_state_alone(tmp_path, monkeypatch):
    ns = cut._namespace(tmp_path)
    slug = cut._seed_card(ns, tmp_path)
    before = _state(tmp_path)

    def boom(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(SystemExit) as ei:
        ns["cmd_archive"]([slug])
    assert ei.value.code != 0
    assert (tmp_path / "queue" / "missions" / slug).is_dir()
    assert _state(tmp_path) == before


def test_committed_flag_is_only_set_after_the_rename(tmp_path, monkeypatch):
    """`committed` の意味: rename の前の失敗 (fsync_dir で dst の親を作った直後など) には付かない。"""
    (tmp_path / "a" / "m").mkdir(parents=True)
    real_fsync = os.fsync
    state = {"n": 0}

    def fsync(fd):
        state["n"] += 1
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "EIO")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(store.StoreWriteError) as ei:
        store.durable_rename(tmp_path / "a" / "m", tmp_path / "b" / "m")      # b を作る fsync で落ちる (rename 前)
    assert not ei.value.committed
    assert (tmp_path / "a" / "m").exists()
    (tmp_path / "b").mkdir(exist_ok=True)
    with pytest.raises(store.StoreWriteError) as ei:
        store.durable_rename(tmp_path / "a" / "m", tmp_path / "b" / "m")      # rename 後の fsync で落ちる
    assert ei.value.committed and ei.value.op == "fsync_dir"
    assert (tmp_path / "b" / "m").exists() and not (tmp_path / "a" / "m").exists()


def test_registry_write_survives_a_parent_fsync_failure_after_the_replace(tmp_path, monkeypatch, capsys):
    """workers.yaml は置換済みで fsync だけ失敗 → 警告して続行 (assign-name.sh が名前を返さずに落ちない)。"""
    import lib_registry
    path = tmp_path / "workers.yaml"
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "EIO")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    lib_registry.write(str(path), "# h\n", ["Ren"], {"Ren": {"name": "Ren", "skills": ["bash"], "task_count": 1,
                                                             "last_active": "2026-09-30"}})
    assert "name: Ren" in path.read_text()
    assert "fsync" in capsys.readouterr().err
