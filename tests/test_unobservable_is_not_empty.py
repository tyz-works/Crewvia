#!/usr/bin/env python3
"""「観測できなかった」を「そこに何も無い」と読まない (Codex 7 巡目 P1 / P2)。

## なぜ 1 つのファイルにまとめてあるのか

2 件の指摘は別々の関数に出たが、**倒れ方が同じ**である。

* **P1** —— `os.listdir()` が失敗すると `list_task_cards()` は `[]` を返す。
  `cmd_done()` / `cmd_verify_result()` の完了判定は
  `all(status in TERMINAL for tasks)` なので、**空リストには True になる**。
  tasks ディレクトリが「書き込み・実行は可、読み取り不可」になっていると、
  既知のファイル名を直接開く更新系は通るので、**1 枚を done にした瞬間に、
  未完了の兄弟タスクを残したまま mission 全体が done になる**。
* **P2** —— `read_task_card()` の `open()` / `read()` に上限が無い。書き手の
  いない FIFO が `tNNN.md` として置かれていると、**別 mission の健全なカードを
  1 枚更新しただけの実行が、グラフ更新の走査でそこに座り込む**。

どちらも「読めなかった」という *観測の失敗* が、そのまま *危険な側の結論*
(mission 完了 / 無期限の停止) に落ちている。前ミッションで 39 件の P1 を生んだ
`evidence-for-destructive-decisions` と同じ型なので、同じ場所で固定する。

## 赤の実証

各テストの docstring に「欠陥を戻すとどう落ちるか」を書いてある。実証の手順と
出力は `tests/red_proof_unobservable.sh` にまとめてあり、そちらを走らせると
修正前のコードに戻したときに **このファイルの該当テストだけが赤くなる** ことを
確かめられる。

実行方法:
  python3 -m pytest tests/test_unobservable_is_not_empty.py -v
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PLAN_SH = SCRIPTS_DIR / "plan.sh"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lib_task_cards  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402

MISSION = "m-fixture"
OTHER_MISSION = "m-other"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _card(task_id: str, status: str = "pending") -> str:
    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: task {task_id}\n"
        "skills: [code]\n"
        "priority: medium\n"
        f"status: {status}\n"
        "blocked_by: []\n"
        "target_dir: null\n"
        "worker: null\n"
        "started_at: null\n"
        "completed_at: null\n"
        "---\n"
        "\n## Description\n\nfixture\n\n## Result\n\n"
    )


def _mission_yaml(slug: str) -> str:
    return (
        f"title: fixture {slug}\n"
        f"slug: {slug}\n"
        "status: in_progress\n"
        'created_at: "2026-01-01T00:00:00Z"\n'
        "completed_at: null\n"
        "next_task_id: 99\n"
        "max_review_cycles: 3\n"
    )


class Sandbox:
    """CREWVIA_REPO_ROOT / CREWVIA_QUEUE を隔離した実行環境。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        copy_plan_tree(root)
        self.queue = root / "queue"
        (self.queue / "archive").mkdir(parents=True)
        self.add_mission(MISSION)
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")

    # -- queue -------------------------------------------------------------

    def add_mission(self, slug: str) -> None:
        (self.queue / "missions" / slug / "tasks").mkdir(parents=True, exist_ok=True)
        (self.queue / "missions" / slug / "mission.yaml").write_text(_mission_yaml(slug))
        state_file = self.queue / "state.yaml"
        if state_file.exists():
            state_file.write_text(state_file.read_text().replace(
                "default_mission:", f"  - {slug}\ndefault_mission:"))

    def tasks_dir(self, slug: str = MISSION) -> pathlib.Path:
        return self.queue / "missions" / slug / "tasks"

    def add_task(self, task_id: str, status: str = "pending", slug: str = MISSION):
        (self.tasks_dir(slug) / f"{task_id}.md").write_text(_card(task_id, status))

    def mission_status(self, slug: str = MISSION) -> str:
        for line in (self.queue / "missions" / slug / "mission.yaml").read_text().splitlines():
            if line.startswith("status:"):
                return line.split(":", 1)[1].strip()
        return "?"

    # -- running -----------------------------------------------------------

    def env(self, **overrides):
        env = dict(os.environ)
        env.update(
            CREWVIA_REPO_ROOT=str(self.root),
            CREWVIA_QUEUE=str(self.queue),
            CREWVIA_TASKVIA="disabled",
            TASKVIA_URL="",
            TASKVIA_TOKEN="",
        )
        env.pop("AGENT_NAME", None)
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def run(self, *args, env=None, timeout=60):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "plan.sh"), *args],
            env=env if env is not None else self.env(),
            capture_output=True, text=True, timeout=timeout,
        )


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path / "repo")


def _drop_read_permission(path: pathlib.Path) -> None:
    """読み取りだけを落とす (書き込み・実行は残す)。root では効かないので skip。"""
    path.chmod(0o300)
    if os.access(path, os.R_OK):
        path.chmod(0o755)
        pytest.skip("読み取り権限を落とせない環境 (root?)")


def _terminal(meta) -> bool:
    """`plan.sh` / `dispatcher.sh` の完了判定と同じ形。"""
    return meta.get("status") in {"done", "verified", "skipped", "cancelled", "failed"}


# ---------------------------------------------------------------------------
# P1 —— 走査の失敗は「空」ではない
# ---------------------------------------------------------------------------

def test_a_failed_listing_is_not_reported_as_an_empty_mission(tmp_path):
    """`os.listdir()` が失敗したら、空リストを返さないこと。

    RED: `except OSError: return []` に戻すと、この assert が落ちる —— 返り値が
    `[]` になり、`all(...)` が **True** になるからである。完了判定はこの 1 つの
    述語しか見ていないので、走査の失敗がそのまま「mission は全部終わった」に
    化ける。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t001.md").write_text(_card("t001", "done"))
    (tasks / "t002.md").write_text(_card("t002", "pending"))
    _drop_read_permission(tasks)

    try:
        cards = lib_task_cards.list_task_cards(tasks)
    finally:
        tasks.chmod(0o755)

    assert cards, "走査に失敗したのに空リストを返した (完了判定が True に化ける)"
    assert not all(_terminal(m) for m, _ in cards), (
        f"走査の失敗が「mission 完了」と読める形で返っている: {cards}")


def test_the_scan_failure_placeholder_is_neither_pending_nor_terminal(tmp_path):
    """プレースホルダは pull / dispatch にも拾われないこと。

    終端でないだけでは足りない。`pending` に化けると **中身の分からない仕事が
    Worker に割り当てられる**。`[破損]` カードと同じ、どちらでもない疑似
    ステータスであることを固定する。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t001.md").write_text(_card("t001"))
    _drop_read_permission(tasks)

    try:
        cards = lib_task_cards.list_task_cards(tasks)
    finally:
        tasks.chmod(0o755)

    (meta, _body), = cards
    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, meta
    assert meta["id"], "id の無い node は plugin がファイル全体を捨てる理由になる"
    assert meta.get("parse_error"), f"何が起きたのか残っていない: {meta}"


def test_a_missing_tasks_directory_is_still_a_genuinely_empty_mission(tmp_path):
    """逆向きの担保 —— 本当に無いときは空のまま。

    これが無いと「読めなければ全部プレースホルダ」に倒しただけで上が緑になる。
    mission を作った直後・archive 済みは `tasks/` が無いのが正常な状態で、そこに
    終端でない node を置くと **mission が二度と完了しなくなる**。
    """
    assert lib_task_cards.list_task_cards(tmp_path / "does-not-exist") == []


def test_a_tasks_path_that_is_not_a_directory_is_not_an_empty_mission(tmp_path):
    """`tasks` がディレクトリでないときも「空」ではないこと。

    RED: `if not os.path.isdir(...): return []` のままだと、`tasks` が普通の
    ファイル (壊れた復旧手順・取り違えた展開) でも「カードは 1 枚も無い」と
    読まれ、同じ完了判定を通り抜ける。`isdir()` は **存在しない** と
    **ディレクトリではない** と **stat できない** を 1 つの False に潰す。
    """
    path = tmp_path / "tasks"
    path.write_text("not a directory\n")

    cards = lib_task_cards.list_task_cards(path)

    assert cards, "tasks がディレクトリでないのに「空の mission」と読まれた"
    assert not all(_terminal(m) for m, _ in cards), cards


def test_an_unstattable_tasks_directory_is_not_an_empty_mission(tmp_path):
    """親ディレクトリが辿れないときも「空」ではないこと。

    `os.path.isdir()` は `stat` の失敗も False にする。親から実行権限が消えた
    だけで「この mission にはカードが無い」と読まれ、完了判定を通り抜ける。
    """
    parent = tmp_path / "mission"
    tasks = parent / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "t001.md").write_text(_card("t001"))
    parent.chmod(0o600)                       # 実行権限なし = 中を辿れない
    if os.access(tasks, os.F_OK):
        parent.chmod(0o755)
        pytest.skip("実行権限を落とせない環境 (root?)")

    try:
        cards = lib_task_cards.list_task_cards(tasks)
    finally:
        parent.chmod(0o755)

    assert cards, "stat できないのに「空の mission」と読まれた"
    assert not all(_terminal(m) for m, _ in cards), cards


def test_done_does_not_complete_a_mission_it_could_not_scan(sandbox):
    """実害そのもの —— `plan.sh done` が mission 全体を done にしないこと。

    RED: 走査の失敗が `[]` を返すと、`t002` が pending のまま残っているのに
    `mission.yaml` の status が `done` になる。既知のファイル名を直接開く更新系は
    「書き込み・実行は可、読み取り不可」のディレクトリでも通るので、**この経路
    だけが生き残って完了判定に到達する**のが Codex の指摘の肝である。
    """
    sandbox.add_task("t001", "in_progress")
    sandbox.add_task("t002", "pending")
    tasks = sandbox.tasks_dir()
    _drop_read_permission(tasks)

    try:
        r = sandbox.run("done", "t001", "finished")
    finally:
        tasks.chmod(0o755)

    assert sandbox.mission_status() != "done", (
        f"走査できなかった mission を完了にした (t002 は pending のまま)\n"
        f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")


def test_verify_result_does_not_complete_a_mission_it_could_not_scan(sandbox):
    """同じ述語を持つもう 1 つの経路 (`verify-result`) も塞がっていること。

    完了判定は 2 箇所にある。片方だけ直すと、次に通る経路が残る —— この
    リポジトリが同じ欠陥を繰り返してきた形そのものなので、両方を固定する。
    """
    sandbox.add_task("t001", "ready_for_verification")
    sandbox.add_task("t002", "pending")
    tasks = sandbox.tasks_dir()
    _drop_read_permission(tasks)

    try:
        r = sandbox.run("verify-result", "t001", "pass", "--notes", "ok")
    finally:
        tasks.chmod(0o755)

    assert r.returncode == 0, (
        f"verify-result 自体が通っていない — テストの前提が崩れている\n"
        f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    assert sandbox.mission_status() != "done", (
        f"走査できなかった mission を verify-result が完了にした\n"
        f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")


# ---------------------------------------------------------------------------
# P2 —— カードの読み取りは、書き手のいない FIFO で止まらない
# ---------------------------------------------------------------------------

#: 「上限が無い」は待っても分からないので、上限を決めて超えたら失敗にする。
#: 修正前は無期限に止まるため、この秒数が何であっても赤になる。
_NO_BLOCK_SECONDS = 10


class _Timeout(BaseException):
    """`BaseException` なのは意図的。

    `read_task_card()` は「読み取り経路から例外を出さない」ために
    `except Exception` の backstop を持つ。`Exception` を継承した打ち切りは
    **その backstop に飲まれて隔離カードに化ける** ので、修正前のコードでも
    テストが緑になってしまった (memory: red-proof-catches-tests-green-for-the-
    wrong-reason)。打ち切りは backstop の外を通らなければ、測りたいものを
    測っていない。
    """


def _deadline(seconds: int):
    """`signal.alarm` で本物のブロックを打ち切る。

    「速いこと」を経過時間で測らないのは、遅いだけの失敗と区別が付かないから
    である。ここで見たいのは **ブロックするかどうか** の 1 点なので、ブロック
    したら例外で切る。
    """
    def _fire(_signum, _frame):
        raise _Timeout(f"{seconds}s 以内に返らなかった (ブロックしている)")

    class _Ctx:
        def __enter__(self):
            self.prev = signal.signal(signal.SIGALRM, _fire)
            signal.alarm(seconds)

        def __exit__(self, *exc):
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self.prev)
            return False

    return _Ctx()


def test_a_fifo_card_is_refused_instead_of_blocking(tmp_path):
    """書き手のいない FIFO のカードで止まらないこと。

    RED: `with open(path) as f: f.read()` のままだと、FIFO の `open()` が書き手を
    待って返らない。`signal.alarm` が `_Timeout` を投げてこのテストが赤くなる。

    止まる主体はグラフ更新だけではない。同じ読み取りを常駐デーモン 3 者
    (dispatcher / verifier-dispatcher / watchdog) が使うので、1 枚の FIFO で
    **全 mission の割り当てと Worker の生存監視が同時に止まる**。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    os.mkfifo(tasks / "t001.md")

    with _deadline(_NO_BLOCK_SECONDS):
        meta, _body = lib_task_cards.read_task_card(str(tasks / "t001.md"), "t001")

    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, meta
    assert meta.get("parse_error"), f"拒否した理由が残っていない: {meta}"


def test_a_fifo_card_does_not_stall_the_listing_of_its_siblings(tmp_path):
    """FIFO が 1 枚あっても、同じディレクトリの健全なカードは読めること。

    隔離は「そのカードだけが動かない」で止まらなければならない。巻き添えで
    走査ごと止まるなら、拒否できていても意味が無い。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    os.mkfifo(tasks / "t001.md")
    (tasks / "t002.md").write_text(_card("t002", "pending"))

    with _deadline(_NO_BLOCK_SECONDS):
        cards = lib_task_cards.list_task_cards(tasks)

    by_id = {m["id"]: m for m, _ in cards}
    assert by_id["t001"]["status"] == lib_task_cards.CORRUPT_TASK_STATUS
    assert by_id["t002"]["status"] == "pending", by_id["t002"]


def test_a_fifo_card_in_one_mission_does_not_freeze_a_change_in_another(sandbox):
    """実害そのもの —— 別 mission の健全なカードを 1 枚直す実行が止まらないこと。

    RED: 変更系のコマンドは commit の **後に** 全 active mission を同期で走査して
    tasks.json を作り直す。カードの読み取りに上限が無いと、無関係な mission の
    FIFO 1 枚で `plan.sh` そのものが返らなくなる。`retire --no-wait` がこれを
    踏むと、**退役を commit した後に** watchdog のタイムアウトを使い切る。

    ここでは停止スイッチ (`CREWVIA_TASK_GRAPH=0`) を **使わない**。グラフ生成を
    有効にしたまま通ることが、この修正の主張である。
    """
    sandbox.add_mission(OTHER_MISSION)
    sandbox.add_task("t001", "in_progress")
    os.mkfifo(sandbox.tasks_dir(OTHER_MISSION) / "t009.md")

    r = sandbox.run("done", "t001", "finished", "--mission", MISSION,
                    env=sandbox.env(CREWVIA_TASK_GRAPH_FILE=str(
                        sandbox.root / "registry" / "task-graph" / "tasks.json")),
                    timeout=_NO_BLOCK_SECONDS)

    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    graph = json.loads(
        (sandbox.root / "registry" / "task-graph" / "tasks.json").read_text())
    ids = {t["id"] for t in graph["tasks"]}
    assert f"{OTHER_MISSION}:t009" in ids, (
        f"拒否したカードが DAG から黙って消えた: {ids}")


def test_a_directory_named_like_a_card_is_refused_without_blocking(tmp_path):
    """FIFO だけを名指しで弾いていないこと。

    条件を「FIFO なら拒否」と書くと、次に来る種類 (ソケット・デバイス・
    ディレクトリ) で同じ穴が開く。**通常ファイルだけを受理する**形であることを
    固定する —— このリポジトリが繰り返してきた「denylist を足して閉じたつもりに
    なる」の逆側 (memory: approve-judgment-needs-allowlist-and-scope)。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t001.md").mkdir()

    with _deadline(_NO_BLOCK_SECONDS):
        cards = lib_task_cards.list_task_cards(tasks)

    (meta, _body), = cards
    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, meta


def test_a_regular_card_is_still_read_normally(tmp_path):
    """逆向きの担保 —— 普通のカードはそのまま読めること。"""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t001.md").write_text(_card("t001", "pending"))

    (meta, body), = lib_task_cards.list_task_cards(tasks)

    assert meta["status"] == "pending", meta
    assert "fixture" in body, body


def test_a_symlink_to_a_regular_card_is_still_read(tmp_path):
    """symlink 越しの通常ファイルを巻き添えにしないこと。

    `O_NOFOLLOW` のような「symlink ごと拒否」に倒すと、worktree 運用で普通に
    使われている形が黙って `[破損]` になる。見ているのは **開いた先が通常
    ファイルか** であって、経路の作り方ではない。
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    real = tmp_path / "real.md"
    real.write_text(_card("t001", "pending"))
    (tasks / "t001.md").symlink_to(real)

    (meta, _body), = lib_task_cards.list_task_cards(tasks)

    assert meta["status"] == "pending", meta
