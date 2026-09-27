#!/usr/bin/env python3
"""読み取りの失敗を「空」で表さないこと (t018 / Codex 9 巡目 P1)。

## この PR で 8 回出た型

同じ形の欠陥が、この PR の中だけで 8 回出た —— 循環 / 空配列 / 重複 id /
デコード失敗 / 走査失敗 / stat 失敗 / **state.yaml 読み取り失敗** /
**新しい wrapper のデコード漏れ**。どれも

> 観測の失敗 (あるいは局所の破綻) を、全体の終端的な結論に落とす

という 1 つの型である。直すたびに次の入口が出ているので、t018 では
サイトごとの修正をやめた。

## P1 —— 直前の修正が作った穴

t017 は `dispatcher.load_state()` にガードを足し、読めなければ `{}` を返す
ようにした。ところが `dispatch()` は

```python
if not active_missions:
    shutdown_idle_workers()      # ← 全 idle Worker の退役
```

を持っている。したがって `EACCES` / `EIO` / 種類違いの `state.yaml` が、
**pending の仕事が残っているのに idle Worker の退役を認可する**。
「空」と「読めなかった」を同じ値で返していたことが、そのまま穴になった。

## 閉じ方

`Unreadable` —— 空の入れ物として振る舞わない値 —— を返す。`bool()` / `len()` /
`in` / `[]` / 反復 / `.get()` はすべて `TypeError` になるので、
「読めなかったものを空として扱う」コードは **書けても実行した瞬間に落ちる**。
そのうえで `dispatch()` は `is_unreadable()` で明示的に分岐し、そのサイクルを
割り当ても退役も含めて丸ごと見送る。

赤の実証は `tests/red_proof_unguarded_reads.sh`。

    python3 -m pytest tests/test_failure_is_not_empty.py -v
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lib_task_cards  # noqa: E402
from lib_task_cards import Unreadable, is_missing, is_unreadable  # noqa: E402

from test_guarded_reads_on_direct_paths import (  # noqa: E402
    _deadline, _dispatcher_sandbox, _EmptyMux, _replace_with_fifo,
)
from test_task_card_identity import load_dispatcher_namespace  # noqa: E402
from test_unobservable_is_not_empty import MISSION  # noqa: E402


# ===========================================================================
# 1. Unreadable は「空の入れ物」として振る舞わない
# ===========================================================================

@pytest.mark.parametrize("op,call", [
    ("bool()", lambda u: bool(u)),
    ("not", lambda u: not u),
    ("len()", lambda u: len(u)),
    ("iter()", lambda u: list(u)),
    ("in", lambda u: "x" in u),
    ("[]", lambda u: u["x"]),
    (".get()", lambda u: u.get("active_missions")),
    (".splitlines()", lambda u: u.splitlines()),
    (".strip()", lambda u: u.strip()),
])
def test_treating_a_failure_as_empty_raises(op, call):
    """「空として扱う」書き方が、実行した瞬間に落ちること。

    ここが t018 の中心である。`None` / `{}` / `[]` で返していると、
    `if not state:` も `state.get(...)` も **静かに通って** しまい、次に
    その結論を使うのが `shutdown_idle_workers()` だった、というのが
    Codex 9 巡目 P1 の形だった。
    """
    u = Unreadable("/queue/state.yaml", "EACCES")
    with pytest.raises(TypeError) as exc:
        call(u)
    assert "/queue/state.yaml" in str(exc.value)
    assert "EACCES" in str(exc.value)


def test_missing_and_unreadable_are_different_answers():
    """`ENOENT` (本当に無い) と、それ以外 (観測できなかった) を分けること。

    `Path.exists()` を分岐の材料にできないのは、`EACCES` で stat できない
    ときも False になるからである —— 「まだ無いのが普通」は `ENOENT` の話で
    しかない (knowledge/empty-vs-unobservable.md §5)。
    """
    absent = lib_task_cards.read_regular_text_or_unreadable("/nonexistent/x")
    assert is_unreadable(absent) and is_missing(absent)

    denied = Unreadable("/queue/state.yaml", "read error", errno=13)
    assert is_unreadable(denied) and not is_missing(denied)


def test_a_real_file_still_comes_back_as_text(tmp_path):
    """逆向きの担保 —— 普通のファイルはこれまで通り中身で返ること。"""
    f = tmp_path / "ok.txt"
    f.write_text("hello\n")
    assert lib_task_cards.read_regular_text_or_unreadable(f) == "hello\n"


def test_an_empty_file_is_empty_not_unreadable(tmp_path):
    """**0 バイトのファイルは「空」であって「読めなかった」ではない。**

    区別が要るのはこちら向きでもある。空を `Unreadable` に丸めると、今度は
    正常な空ファイルで毎サイクル警告が出て、本当の失敗が埋もれる
    (memory: empty-is-a-normal-state-not-an-edge-case)。
    """
    f = tmp_path / "empty.txt"
    f.write_text("")
    got = lib_task_cards.read_regular_text_or_unreadable(f)
    assert got == "" and not is_unreadable(got)


# ===========================================================================
# 2. P1 —— 読めない state.yaml が退役を認可しないこと
# ===========================================================================

def _unreadable_state(root: pathlib.Path, how: str) -> None:
    """`state.yaml` を「読めない」状態にする。

    Codex は reader が None を返す形でこの経路を再現した。こちらは本物の
    ファイルシステムの状態でやる —— 注入点を本番コードに作らないため
    (memory: microsecond-race-fix-needs-structural-test)。
    """
    path = root / "queue" / "state.yaml"
    if how == "fifo":
        _replace_with_fifo(path)
    elif how == "eacces":
        os.chmod(path, 0o000)
    else:                                       # pragma: no cover
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["fifo", "eacces"])
def test_an_unreadable_state_does_not_retire_idle_workers(tmp_path, how):
    """**指摘の再現 (Codex 9 巡目 P1)**。

    RED (`load_state()` が `{}` を返す t017 の形): `active_missions` が空に
    なり、`dispatch()` が `shutdown_idle_workers()` を呼ぶ —— pending の
    仕事が残っているのに、idle Worker の退役が認可される。
    """
    if how == "eacces" and os.geteuid() == 0:
        pytest.skip("root は EACCES を踏まない")

    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    _unreadable_state(root, how)

    called = []
    ns["shutdown_idle_workers"] = lambda: called.append("shutdown")
    ns["_mux"] = _EmptyMux()

    try:
        with _deadline(15):
            ns["dispatch"]()
    finally:
        if how == "eacces":
            os.chmod(root / "queue" / "state.yaml", 0o644)

    assert called == [], (
        "state.yaml を観測できないのに、全 idle Worker の退役が走った "
        "(観測の失敗を破壊の許可に使っている)")


@pytest.mark.parametrize("how", ["fifo", "eacces"])
def test_an_unreadable_workers_file_concludes_nothing_this_cycle(tmp_path, how):
    """`workers.yaml` を観測できないサイクルは、**何も結論しない**こと。

    「誰が居るか」が分からないまま進むと、そのサイクルの結論はすべて
    「Worker が 1 人もいない」という観測の失敗の上に建つ。倒す先は
    state.yaml と同じで、サイクルを丸ごと見送る。

    見えるところで確かめるのは `load_all_tasks()` が呼ばれないこと ——
    退役はこの経路 (active mission あり・mission は in_progress) からは
    そもそも届かないので、そこを assert にすると **欠陥を戻しても緑のまま**
    になる (実際に一度そう書いてしまい、tests/red_proof_t018.sh が
    「対照になっていない」と報告した)。
    """
    if how == "eacces" and os.geteuid() == 0:
        pytest.skip("root は EACCES を踏まない")

    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    path = root / "registry" / "workers.yaml"
    if how == "fifo":
        _replace_with_fifo(path)
    else:
        os.chmod(path, 0o000)

    reached = []
    original = ns["load_all_tasks"]
    ns["load_all_tasks"] = lambda *a, **kw: (reached.append("scan")
                                             or original(*a, **kw))
    ns["_mux"] = _EmptyMux()

    try:
        with _deadline(15):
            ns["dispatch"]()
    finally:
        if how == "eacces":
            os.chmod(path, 0o644)

    assert reached == [], "workers.yaml を観測できないのに割り当ての判定へ進んだ"


def test_a_readable_workers_file_still_reaches_the_assignment_scan(tmp_path):
    """**逆向きの担保** —— 読める registry では、これまで通り判定まで進むこと。"""
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)

    reached = []
    original = ns["load_all_tasks"]
    ns["load_all_tasks"] = lambda *a, **kw: (reached.append("scan")
                                             or original(*a, **kw))
    ns["_mux"] = _EmptyMux()

    with _deadline(15):
        ns["dispatch"]()

    assert reached == ["scan"], (
        "読める registry なのに割り当ての判定へ進まない —— "
        "観測できない側に倒しすぎて機能が死んでいる")


def test_a_readable_state_with_no_active_missions_still_retires(tmp_path):
    """**逆向きの担保**。本当に active mission がゼロなら、退役は走ること。

    これが無いと、上の 2 つは「`shutdown_idle_workers()` を呼ぶ経路を全部
    塞いだ」だけでも緑になる —— fail closed を足したつもりで機能そのものを
    黙って殺す形 (memory: regression-test-must-prove-red)。
    """
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    (root / "queue" / "state.yaml").write_text(
        "active_missions: []\ndefault_mission: null\n")

    called = []
    ns["shutdown_idle_workers"] = lambda: called.append("shutdown")
    ns["_mux"] = _EmptyMux()

    with _deadline(15):
        ns["dispatch"]()

    assert called == ["shutdown"], (
        "active mission が本当にゼロなのに退役が走らない —— "
        "観測できない側に倒しすぎて機能が死んでいる")


def test_a_missing_state_file_is_still_read_as_zero_missions(tmp_path):
    """`ENOENT` は「本当に無い」なので、これまで通り `{}` であること。"""
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)
    (root / "queue" / "state.yaml").unlink()

    with _deadline(10):
        assert ns["load_state"]() == {}


# ===========================================================================
# 3. publish_agents —— 観測できない Worker 一覧を撤去の根拠にしない
# ===========================================================================

def test_publish_agents_does_not_delete_everyone_on_an_unreadable_registry(
        tmp_path, monkeypatch):
    """`workers.yaml` が読めないとき、Taskvia から全員を DELETE しないこと。

    `publish_agents()` は「前回いて今回いない」エージェントを DELETE する。
    Worker 一覧を空に潰すと、**観測の失敗がそのまま撤去の根拠**になる
    (memory: evidence-for-destructive-decisions)。
    """
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)

    monkeypatch.setenv("TASKVIA_TOKEN", "dummy")
    monkeypatch.setenv("CREWVIA_TASKVIA", "enabled")

    requests = []
    ns["urllib"].request.urlopen = lambda req, timeout=5: (
        requests.append(req) or _NullCtx())
    ns["LAST_PUBLISHED_AGENTS"] = {"Ren"}

    _replace_with_fifo(root / "registry" / "workers.yaml")
    with _deadline(15):
        ns["publish_agents"]()

    assert requests == [], (
        "workers.yaml を観測できないのに Taskvia へ publish / DELETE を出した")


def test_publish_agents_still_publishes_when_the_registry_is_readable(
        tmp_path, monkeypatch):
    """**逆向きの担保**。読める registry では、これまで通り publish すること。

    上のテストは「1 本も出ない」ことを見るので、`publish_agents()` が別の
    理由で早期 return していても緑になる。対照を置いて、確かめたい層まで
    実行が届いていることを示す (memory:
    red-proof-catches-tests-green-for-the-wrong-reason)。
    """
    root = tmp_path / "repo"
    _dispatcher_sandbox(root)
    ns = load_dispatcher_namespace(root)

    monkeypatch.setenv("TASKVIA_TOKEN", "dummy")
    monkeypatch.setenv("CREWVIA_TASKVIA", "enabled")

    requests = []
    ns["urllib"].request.urlopen = lambda req, timeout=5: (
        requests.append(req) or _NullCtx())
    ns["LAST_PUBLISHED_AGENTS"] = {"Ren"}

    with _deadline(15):
        ns["publish_agents"]()

    assert [r.get_method() for r in requests] == ["POST"], requests


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
