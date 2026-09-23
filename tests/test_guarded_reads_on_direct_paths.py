#!/usr/bin/env python3
"""**固定パスで開く読み取りにも、種類のガードを当てる** (Codex 8 巡目 P2, t017)。

## 指摘

t016 の FIFO ガードは `lib_task_cards` 経由の読み取り —— つまり `tasks/` を
**列挙して** 読む経路 —— しか守っていない。同じ queue のファイルを
**固定パスで直接開く** 経路が 2 つ残っていた。

* `dispatcher.sh` の `publish_agents()` —— `task_file.read_text()`。
  `dispatch()` より前に走るので、Taskvia 有効時に「生きた Worker が割り当て
  られたカード」が FIFO に置き換わると、**全 mission の dispatch が無期限に
  止まる**。
* `plan.sh` の `load_task()` —— `open(path)`。`with_lock()` の内側で呼ばれる
  ので、**キューロックを握ったまま**止まる。止まった側は plan.sh 全体を
  待たせる。

どちらも t016 で塞いだのと同じ穴で、塞ぎ方も同じ: `O_NONBLOCK` で開いて
`fstat` で通常ファイルであることを確かめ、それ以外は **待たずに** 断る。
待ち時間に上限を付けるのではなく種類で弾くのは、待てば読めるものが 1 つも
無いからである (`knowledge/empty-vs-unobservable.md` §4)。

## 閉じていないもの

`plan.sh` の `load_state()` には **同じ判定を入れていない**。理由は
`knowledge/empty-vs-unobservable.md` §4 にある明示的な取引で、そこが
`tests/test_retirement.py` の
`_pull_parked_inside_the_queue_lock()` —— Codex 6 巡目 P1 の回帰テストを
成立させている唯一の停止点 —— だからである。閉じないことを、ここにも書いて
おく (memory: and-condition-beats-unforgeable-evidence)。

    python3 -m pytest tests/test_guarded_reads_on_direct_paths.py -v
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_task_cards  # noqa: E402

from test_task_card_identity import load_dispatcher_namespace  # noqa: E402
from test_unobservable_is_not_empty import MISSION, Sandbox  # noqa: E402


# ---------------------------------------------------------------------------
# 「止まらない」ことの測り方
# ---------------------------------------------------------------------------
#
# ブロックは「遅い」ではなく「返らない」なので、経過時間で当て込まない。
# in-process の呼び出しには `SIGALRM` で締切を置き、subprocess には
# `subprocess.run(timeout=)` を置く。どちらも **落ちたら赤** になる。

class _Blocked(BaseException):
    """締切までに返らなかった = ブロックした。`Exception` を継承しない
    (本番コードの広い `except Exception` に飲まれないため)。"""


class _deadline:
    def __init__(self, seconds: int):
        self.seconds = seconds

    def __enter__(self):
        def _fire(_signum, _frame):
            raise _Blocked(f"{self.seconds}s 以内に返らなかった")
        self.previous = signal.signal(signal.SIGALRM, _fire)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.previous)
        return False


def _replace_with_fifo(path: pathlib.Path) -> pathlib.Path:
    """通常ファイルを、**書き手のいない** FIFO に置き換える。

    書き手がいないので `open(O_RDONLY)` は永久に返らない。これが「上限の無い
    `open()`」の害そのものである。
    """
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    return path


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path / "repo")


# ===========================================================================
# P2-a: dispatcher の publish_agents()
# ===========================================================================

def _dispatcher_sandbox(root: pathlib.Path, *, worker="Ren", task_id="t001"):
    """`publish_agents()` がカードの読み取りまで到達する最小構成を作る。

    到達の条件は 4 つ —— worker が registry に居る / heartbeat が新しい /
    assignment がある / そのカードが存在する。1 つでも欠けると読み取りの手前で
    `continue` してしまい、**何も試していないのに緑**になる。
    """
    root.mkdir(parents=True)
    (root / ".git").mkdir()                      # repo_identity_ok() を通すため
    (root / "registry" / "mux").mkdir(parents=True)

    queue = root / "queue"
    (queue / "missions" / MISSION / "tasks").mkdir(parents=True)
    (queue / "assignments").mkdir(parents=True)
    (queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")
    (queue / "missions" / MISSION / "mission.yaml").write_text(
        f"title: fixture\nslug: {MISSION}\nstatus: in_progress\n")

    registry = root / "registry"
    (registry / "heartbeats").mkdir(parents=True)
    (registry / "workers.yaml").write_text(
        "workers:\n"
        f'  - name: "{worker}"\n'
        "    skills: [code]\n"
        "    task_count: 0\n"
        "    role: worker\n")
    (registry / "heartbeats" / worker).write_text("alive\n")

    card = queue / "missions" / MISSION / "tasks" / f"{task_id}.md"
    card.write_text(
        f"---\nid: {task_id}\ntitle: real title\nskills: [code]\n"
        f"status: in_progress\nblocked_by: []\n---\n\n## Description\n\nx\n")
    (queue / "assignments" / worker).write_text(f"{MISSION}:{task_id}")
    return card


def _publish_env(monkeypatch):
    """Taskvia を「有効だが送り先は即座に失敗する」状態にする。

    カードの読み取りは POST より **前** なので、送信が落ちても確かめたい層は
    通る。実在するサーバーを向けないのは、外に出さないため。
    """
    monkeypatch.setenv("TASKVIA_TOKEN", "fixture-token")
    monkeypatch.setenv("TASKVIA_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("CREWVIA_TASKVIA", raising=False)


def test_publish_agents_does_not_block_on_a_fifo_card(tmp_path, monkeypatch):
    """**指摘の再現**。割り当て済みカードが FIFO だと `publish_agents()` が返らない。

    RED (`task_file.read_text()` のまま): `open()` に上限が無いので、書き手の
    いない FIFO の前で座り込む。`publish_agents()` は `dispatch()` より **前** に
    走るため、止まるのはその mission ではなく **全 mission の割り当て**である。
    """
    root = tmp_path / "repo"
    card = _dispatcher_sandbox(root)
    _publish_env(monkeypatch)
    ns = load_dispatcher_namespace(root)
    _replace_with_fifo(card)

    with _deadline(10):
        ns["publish_agents"]()          # 返ること自体が主張


def test_publish_agents_still_reports_the_title_of_a_regular_card(tmp_path,
                                                                 monkeypatch):
    """逆向きの担保 —— 普通のカードの title はこれまで通り読めること。

    これが無いと「読み取りをやめた」だけで上のテストが緑になる。
    """
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    _publish_env(monkeypatch)
    ns = load_dispatcher_namespace(root)

    posted = []

    def _capture(req, timeout=None):
        posted.append(json.loads(req.data.decode()))
        return _NullResponse()

    # `monkeypatch` で当てるのは、これが **プロセス全体で共有される** module
    # 属性だから。テストの終わりに必ず戻らないと、後続のテストが黙って
    # このスタブを使う。
    monkeypatch.setattr("urllib.request.urlopen", _capture)

    with _deadline(10):
        ns["publish_agents"]()

    titles = [p.get("current_task_title") for p in posted if p.get("name") == "Ren"]
    assert titles == ["real title"], f"カードの title が読めていない: {posted}"


class _EmptyMux:
    """ペインが 1 つも無い mux。`list()` だけ答えられれば足りる。"""

    def list(self, *a, **kw):
        return []

    def __getattr__(self, _name):
        return lambda *a, **kw: None


class _NullResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ===========================================================================
# P2-a2: 常駐デーモンが読む queue / registry の固定パス
# ===========================================================================
#
# カードと同じ穴が `state.yaml` / `workers.yaml` / `mission.yaml` にも空いて
# いた。こちらはデーモンなので、止まるとサイクルごと止まる —— dispatcher なら
# 全 mission の割り当て、watchdog なら **全 Worker の生存監視** である。
#
# 倒す先はどれも「何もしない」側にしてある: 割り当てない / 退役させない /
# 監視対象ゼロ。読めなかったことを、割り当てや終了の許可に使わない。
#
# t018: `{}` で返すのはやめ、`Unreadable` を返す。「読めなかった」を「空」と
# 同じ形で返していたことが、`dispatch()` の `if not active_missions:
# shutdown_idle_workers()` に落ちる経路を作っていた (Codex 9 巡目 P1)。

def test_dispatcher_load_state_does_not_block_on_a_fifo(tmp_path):
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    _replace_with_fifo(root / "queue" / "state.yaml")

    with _deadline(10):
        state = ns["load_state"]()
    assert lib_task_cards.is_unreadable(state), state


def test_dispatcher_load_workers_does_not_block_on_a_fifo(tmp_path):
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    _replace_with_fifo(root / "registry" / "workers.yaml")

    with _deadline(10):
        workers = ns["load_workers"]()
    assert lib_task_cards.is_unreadable(workers), workers


def test_dispatcher_still_reads_a_regular_state_and_workers_file(tmp_path):
    """逆向きの担保 —— 普通のファイルはこれまで通り読めること。"""
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)

    assert ns["load_state"]().get("active_missions") == [MISSION]
    assert "Ren" in ns["load_workers"]()


def test_dispatch_does_not_call_a_mission_done_on_an_unreadable_mission_yaml(
        tmp_path):
    """読めなかった mission を「完了」に数えないこと。

    この判定の先には `shutdown_idle_workers()` —— 全 idle Worker の退役 ——
    がある。観測の失敗をそこへ落とすと、mission.yaml を読めなかっただけで
    Worker が片付けられる (memory: evidence-for-destructive-decisions)。

    RED (`mfile.read_text()` のまま): そもそも `dispatch()` が返らない。
    """
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    _replace_with_fifo(root / "queue" / "missions" / MISSION / "mission.yaml")

    called = []
    ns["shutdown_idle_workers"] = lambda: called.append("shutdown")
    # 共有の `_FakeMux` は全メソッドが None を返すので、`dispatch()` を最後まで
    # 通すには「ペインは 1 つも無い」と答えられる必要がある (確かめたい層では
    # ないので、いちばん薄い形で埋める)。
    ns["_mux"] = _EmptyMux()

    with _deadline(15):
        ns["dispatch"]()

    assert called == [], "読めない mission.yaml で全 idle Worker の退役が走った"


def test_watchdog_load_active_tasks_does_not_block_on_a_fifo(tmp_path):
    """watchdog が止まると、**どの Worker も監視されない**。"""
    import watchdog

    queue = tmp_path / "queue"
    queue.mkdir()
    _replace_with_fifo(queue / "state.yaml")

    with _deadline(10):
        assert watchdog.load_active_tasks(queue) == []


def test_watchdog_still_reads_a_regular_state_file(tmp_path):
    """逆向きの担保 —— 普通の state.yaml からは in_progress の task が出ること。"""
    import watchdog

    queue = tmp_path / "queue"
    tasks = queue / "missions" / MISSION / "tasks"
    tasks.mkdir(parents=True)
    (queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")
    (tasks / "t001.md").write_text(
        "---\nid: t001\ntitle: t\nskills: [code]\nstatus: in_progress\n"
        "blocked_by: []\nworker: Ren\n---\n\n## Description\n\nx\n")

    found = watchdog.load_active_tasks(queue)

    assert [(slug, tid) for slug, tid, _ in found] == [(MISSION, "t001")]


# ===========================================================================
# P2-a3: verifier-dispatcher —— 読んでから書き戻す経路
# ===========================================================================

def _load_verifier_namespace(root: pathlib.Path) -> dict:
    """`verifier-dispatcher.sh` の本物の python を、`dispatch()` の手前まで。

    `dispatcher.sh` と違って entry point の印が無いので、末尾の `dispatch()`
    呼び出しで切る。切れなかったら `assert` で止まる —— 黙って cycle を
    走らせない。
    """
    import types

    script = (REPO_ROOT / "scripts" / "verifier-dispatcher.sh").read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", script, re.DOTALL)
    assert m, "python ヒアドキュメント (PYEOF) が見つからない"
    src, n = re.subn(r"\ndispatch\(\)\n*$", "\n", m.group(1))
    assert n == 1, "末尾の dispatch() が見つからない — 構造が変わった?"

    fake = types.ModuleType("lib_mux")
    fake.Mux = _FakeMuxModule
    saved = sys.modules.get("lib_mux")
    sys.modules["lib_mux"] = fake
    argv = sys.argv
    sys.argv = ["verifier-embedded", str(root / "queue"), str(root / "registry"),
                str(root / "notify-cache.json"), "300"]
    try:
        ns: dict = {"__name__": "verifier_under_test"}
        exec(compile(src, "verifier-dispatcher.sh (embedded, test)", "exec"), ns)
        return ns
    finally:
        sys.argv = argv
        if saved is None:
            del sys.modules["lib_mux"]
        else:
            sys.modules["lib_mux"] = saved


class _FakeMuxModule:
    def __init__(self, *a, **kw):
        pass

    def list(self, *a, **kw):
        return []

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def test_verifier_does_not_block_writing_back_a_fifo_card(tmp_path):
    """**読んでから書き戻す**経路にこそ、種類のガードが要る。

    ガードが無いと 2 つ壊れる —— 読みで無期限に止まるか、止まらなかった場合は
    `os.replace()` が置き換えるのが *別の何か* になる。

    RED (`task_path.read_text()` のまま): `_deadline` が発火する。
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "registry").mkdir()
    (root / "queue" / "missions" / MISSION / "tasks").mkdir(parents=True)
    ns = _load_verifier_namespace(root)

    card = root / "queue" / "missions" / MISSION / "tasks" / "t001.md"
    _replace_with_fifo(card)

    with _deadline(10):
        with pytest.raises(Exception):
            ns["update_task_fields"](card, {"status": "verifying"})

    assert card.is_fifo(), "書き戻しが FIFO を置き換えてしまった"


def test_verifier_still_writes_back_a_regular_card(tmp_path):
    """逆向きの担保 —— 普通のカードはこれまで通り書き換わること。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "registry").mkdir()
    tasks = root / "queue" / "missions" / MISSION / "tasks"
    tasks.mkdir(parents=True)
    ns = _load_verifier_namespace(root)

    card = tasks / "t001.md"
    card.write_text("---\nid: t001\nstatus: ready_for_verification\n"
                    "verifier: null\n---\n\n## Description\n\nx\n")

    ns["update_task_fields"](card, {"status": "verifying", "verifier": "Wei"})

    text = card.read_text()
    assert "status: verifying" in text and "verifier: Wei" in text, text


def test_verifier_load_state_does_not_block_on_a_fifo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "registry").mkdir()
    (root / "queue").mkdir()
    ns = _load_verifier_namespace(root)
    _replace_with_fifo(root / "queue" / "state.yaml")

    with _deadline(10):
        state = ns["load_state"]()
    assert lib_task_cards.is_unreadable(state), state


# ===========================================================================
# P2-b: plan.sh の load_task()
# ===========================================================================

def test_a_fifo_card_does_not_block_a_change_command(sandbox):
    """**指摘の再現**。`load_task()` が FIFO のカードで止まる。

    止まる位置は `with_lock()` の内側 —— **キューロックを握ったまま**なので、
    他の `plan.sh` も道連れになる。

    RED (`open(path)` のまま): `subprocess.run(timeout=)` が
    `TimeoutExpired` を投げる。
    """
    sandbox.add_task("t001", status="in_progress")
    _replace_with_fifo(sandbox.tasks_dir() / "t001.md")

    result = sandbox.run("needs-director", "t001", "理由", "--mission", MISSION,
                         timeout=20)

    assert result.returncode != 0, \
        f"FIFO のカードを読めたことになっている: {result.stdout}"
    combined = result.stdout + result.stderr
    assert "regular file" in combined or "通常ファイル" in combined, (
        f"種類で断ったことが伝わらない出力:\n{combined}")


def test_a_fifo_card_does_not_hold_the_queue_lock(sandbox):
    """道連れの部分を直接見る —— FIFO を踏んだ実行のあと、別の `plan.sh` が
    普通に動くこと。ロックを握ったまま止まっていれば、これも返らない。"""
    sandbox.add_task("t001", status="in_progress")
    sandbox.add_task("t002", status="pending")
    _replace_with_fifo(sandbox.tasks_dir() / "t001.md")

    sandbox.run("needs-director", "t001", "理由", "--mission", MISSION, timeout=20)
    after = sandbox.run("status", timeout=20)

    assert after.returncode == 0, \
        f"FIFO を踏んだ後の plan.sh が動かない:\n{after.stdout}\n{after.stderr}"
    assert "0/2" in after.stdout, (
        f"健全な兄弟カードまで数から落ちている (FIFO 1 枚が mission 全体を "
        f"壊していないこと):\n{after.stdout}")


def test_a_regular_card_is_still_changed_normally(sandbox):
    """逆向きの担保 —— 普通のカードはこれまで通り読めて、書き換わること。"""
    sandbox.add_task("t001", status="in_progress")

    result = sandbox.run("needs-director", "t001", "理由", "--mission", MISSION,
                         timeout=30)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "status: needs_director" in \
        (sandbox.tasks_dir() / "t001.md").read_text()


def test_a_directory_named_like_a_card_is_refused_by_a_change_command(sandbox):
    """FIFO だけを名指しで弾いていないこと。受理する側 (通常ファイル) を
    列挙する形になっていれば、ディレクトリも同じ理由で断られる
    (memory: approve-judgment-needs-allowlist-and-scope)。"""
    (sandbox.tasks_dir() / "t001.md").mkdir()

    result = sandbox.run("needs-director", "t001", "理由", "--mission", MISSION,
                         timeout=20)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "regular file" in combined or "通常ファイル" in combined, combined


# ===========================================================================
# P2-c: plan.sh の load_mission()
# ===========================================================================

def test_a_fifo_mission_yaml_does_not_block(sandbox):
    """`mission.yaml` も固定パスで開かれる。同じ穴なので同じ判定を当てる。

    `plan.sh status` は全 active mission の `mission.yaml` を読むので、1 つの
    mission の FIFO が **すべての mission の表示** を止める。
    """
    sandbox.add_task("t001", status="pending")
    _replace_with_fifo(sandbox.queue / "missions" / MISSION / "mission.yaml")

    result = sandbox.run("status", timeout=20)

    combined = result.stdout + result.stderr
    assert "regular file" in combined or "通常ファイル" in combined, (
        f"種類で断ったことが伝わらない出力 (rc={result.returncode}):\n{combined}")


def test_a_regular_mission_yaml_is_still_read(sandbox):
    """逆向きの担保 —— 普通の `mission.yaml` はこれまで通り読めること。"""
    sandbox.add_task("t001", status="pending")

    result = sandbox.run("status", timeout=20)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"{MISSION} — fixture {MISSION}" in result.stdout, (
        f"mission.yaml の title が読めていない: {result.stdout}")
