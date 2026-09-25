#!/usr/bin/env python3
"""
tests/test_dispatcher_retirement_exclusion.py

t025 (mission 20260921-daemon-authority-and-mutual-watch, Codex 4 巡目 P1-1 の前半)

## 直している欠陥

退役が決まった Worker に、dispatcher が新しい task を割り当ててしまう。

`retire_worker()` は marker を書くだけになった (t002) ので、Worker は
**猶予期間のあいだ生きたまま**である。その間 `queue/assignments/<agent>` は
(idle 退役なら) 空いたままなので、dispatcher の `dispatch()` はその Worker を
ただの idle Worker として扱い、次の task を送ってしまう。Worker が pull すると:

  - pane の PID も created_at も変わらないので watchdog の identity guard は
    素通りし、**新しい task を実行中の Worker が kill される**
  - 後始末は元の task しか見ないので、**新しい task が in_progress のまま
    宙に浮く**

watchdog 側の guard (現在の assignment が request の実行と同じか) は
tests/test_retirement.py の t025 群で担保しているが、それは「撃たれた後に
避ける」防御でしかない。割り当てそのものを止めるのが本命の修正で、これは
dispatcher 側にしか書けない。

## テストの張り方

`scripts/dispatcher.sh` の heredoc に埋め込まれた **本物の python を exec()**
して `dispatch()` を直接呼ぶ (tests/test_orphan_daemon_guard.py と同じ方式)。
ロジックを複製したテストは「dispatcher.sh を直したこと」を一切証明しない —
複製側だけ直しても緑になってしまう。

実 tmux / herdr には触れない (`lib_mux` はフェイクに差し替え)。queue /
registry / notify cache はすべて使い捨ての tmpdir。

実行方法:
  python3 -m pytest tests/test_dispatcher_retirement_exclusion.py -v
"""

import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_mux  # noqa: E402
import lib_retirement  # noqa: E402

DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"

AGENT = "Dispatchee"
WINDOW = f"{AGENT}-worker"
SLUG = "20260921-dispatch-retire"


class FakeMux:
    """dispatch() が触る Mux の面だけを持つフェイク。"""

    def __init__(self, windows=None):
        self.windows = list(windows or [])
        self.sent = []
        self.killed = []

    def available(self):
        return True

    def list(self, suffix=None):
        if suffix:
            return [n for n in self.windows if n.endswith(suffix)]
        return list(self.windows)

    def send(self, target, message):
        self.sent.append((target, message))
        return True

    def kill(self, target):
        self.killed.append(target)
        if target in self.windows:
            self.windows.remove(target)
        return True

    def state(self, target):
        return "unknown"  # tmux 相当 — Rule 5 はスキップされる

    def capture(self, target):
        return ""

    def pid(self, target):
        return 4242 if target in self.windows else None


def _build_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)          # repo_identity_ok() 用
    (root / "registry" / "mux").mkdir(parents=True)
    (root / "registry" / "retirements").mkdir(parents=True)
    queue = root / "queue"
    (queue / "missions" / SLUG / "tasks").mkdir(parents=True)
    (queue / "assignments").mkdir(parents=True)

    (queue / "state.yaml").write_text(
        f"active_missions:\n  - {SLUG}\ndefault_mission: {SLUG}\n")
    (queue / "missions" / SLUG / "mission.yaml").write_text(
        f'title: dispatch retire test\nslug: {SLUG}\nstatus: in_progress\n'
        f'created_at: "2026-09-21T00:00:00Z"\ncompleted_at: null\nnext_task_id: 2\n')
    (queue / "missions" / SLUG / "tasks" / "t001.md").write_text(
        "---\nid: t001\ntitle: pending work\nskills: [code]\npriority: high\n"
        "status: pending\nblocked_by: []\ntarget_dir: null\nworker: null\n"
        "started_at: null\ncompleted_at: null\n---\n\n## Description\nwork\n\n## Result\n")
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n")

    # spawn 猶予を抜けさせる (猶予中なら退役判定そのものが走らない)
    (root / "registry" / "mux" / f"{WINDOW}.firstseen").write_text("1.0")
    return root


def _load_dispatcher(root: Path, mux: FakeMux) -> dict:
    """dispatcher.sh の heredoc python を、cycle entry point を落として exec()。"""
    text = DISPATCHER_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF", text, re.S)
    assert m, "dispatcher.sh の python heredoc が見つからない"
    src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", m.group(1), flags=re.S)
    assert n == 1, "cycle entry point マーカーが見つからない — dispatcher.sh の構造が変わった?"

    fake_lib_mux = types.ModuleType("lib_mux")
    fake_lib_mux.Mux = lambda: mux
    fake_lib_mux.repo_identity_ok = lib_mux.repo_identity_ok
    # 差し替える前の module を覚えておき、finally で **元に戻す**。`del` だけだと本物の
    # lib_mux が sys.modules から消え、後続のテストの `import lib_mux` が別の module を
    # 新しく読み込む (test_dispatcher_notify_once.py が `lib_mux.Mux` を patch した object と
    # 食い違い、本物の mux に触れに行く)。
    previous_lib_mux = sys.modules.get("lib_mux")
    sys.modules["lib_mux"] = fake_lib_mux

    argv = [
        "dispatcher-embedded",
        str(root / "queue"),
        str(root / "registry"),
        str(root / "notify-cache.json"),
        "300",
        "60",
        str(root / "dispatcher.log"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        ns = {"__name__": "dispatcher_under_test"}
        exec(compile(src, str(DISPATCHER_SH) + " (embedded, test)", "exec"), ns)
        return ns
    finally:
        sys.argv = old_argv
        if previous_lib_mux is None:
            sys.modules.pop("lib_mux", None)
        else:
            sys.modules["lib_mux"] = previous_lib_mux


def _write_retirement_request(root: Path, mux: FakeMux) -> None:
    """dispatcher 自身が書くのと同じ経路で marker を置く。"""
    ex = lib_retirement.RetirementExecutor(
        registry_dir=root / "registry",
        repo_root=root,
        mux=mux,
        repo_identity_check=lambda: True,
        queue_dir=root / "queue",
    )
    assert ex.request(AGENT, WINDOW, "no-task"), "前提: marker が書けていること"


def _assign_messages(mux: FakeMux) -> list:
    return [msg for target, msg in mux.sent if target == WINDOW and "plan pull" in msg]


# ---------------------------------------------------------------------------


def test_idle_worker_without_a_marker_is_assigned(tmp_path):
    """前提の担保: marker が無ければ従来どおり割り当てる。

    これが無いと、下のテストは「割り当てを全部止めた」だけでも緑になる。
    """
    root = _build_repo(tmp_path)
    mux = FakeMux([WINDOW])
    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()

    assert _assign_messages(mux), f"idle Worker に task が割り当てられていない: {mux.sent}"


def test_red_worker_with_a_retirement_marker_is_not_assigned_a_task(tmp_path):
    """退役が決まった Worker を割り当て対象から外す。

    marker がある = watchdog が猶予期間を数えている最中。ここで新しい task を
    渡すと、その task を実行中の Worker が kill され、task は宙に浮く。
    """
    root = _build_repo(tmp_path)
    mux = FakeMux([WINDOW])
    _write_retirement_request(root, mux)

    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()

    assert not _assign_messages(mux), (
        f"退役中の Worker に task を割り当てた: {mux.sent}")
    assert (root / "queue" / "missions" / SLUG / "tasks" / "t001.md").read_text().count(
        "status: pending") == 1, "task は pending のまま次の Worker を待つこと"


def test_red_worker_mid_termination_is_not_assigned_a_task(tmp_path):
    """request が消費されて progress だけになった後も、割り当ててはならない。

    request は `_start()` の後も残るが、`_orphaned()` 経路や手作業で request
    だけが消えた状態は実在する。`has_marker()` が見ているのは 2 ファイルの
    どちらかなので、dispatcher 側も同じ判定を使うこと。
    """
    root = _build_repo(tmp_path)
    mux = FakeMux([WINDOW])
    _write_retirement_request(root, mux)
    # request を消して progress だけを残す (終了処理の途中)
    lib_retirement.write_json_atomic(
        lib_retirement.progress_path(root / "registry", AGENT),
        lib_retirement.build_progress(None, lib_retirement.PHASE_SIGTERM_SENT,
                                      pane_pid=4242))
    lib_retirement.unlink_quiet(lib_retirement.request_path(root / "registry", AGENT))

    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()

    assert not _assign_messages(mux), (
        f"終了処理中の Worker に task を割り当てた: {mux.sent}")


def test_marker_cleared_lets_the_worker_be_assigned_again(tmp_path):
    """保留に出口があること: marker が片付けば同じ Worker は復帰する。"""
    root = _build_repo(tmp_path)
    mux = FakeMux([WINDOW])
    _write_retirement_request(root, mux)

    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()
    assert not _assign_messages(mux)

    lib_retirement.unlink_quiet(lib_retirement.request_path(root / "registry", AGENT))
    lib_retirement.unlink_quiet(lib_retirement.progress_path(root / "registry", AGENT))

    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()
    assert _assign_messages(mux), (
        f"marker が片付いたのに Worker が復帰しない: {mux.sent}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
