#!/usr/bin/env python3
"""`pull` が受理するカードと、`dispatcher` がスケジュールするカードを一致させる。

## 背景 (Codex 5 巡目 P2)

t013 で「task の識別子はファイル名であって frontmatter の `id` 欄ではない」と
決め、食い違うカードを `[破損]` として保留する規則を入れた。**が、入ったのは
`plan.sh` の `list_tasks()` だけだった。** `dispatcher.sh` は
`list_tasks_for_mission()` / `load_all_tasks()` で frontmatter の id を直接読んで
いたので、同じ queue を 2 つの別のコードが別の規則で読む状態になった。

そのズレは 3 つの形で出る。

1. **id 行の無いカードで dispatch サイクルが落ちる。** `plan.sh` はファイル名を
   識別子にするので普通に受理し、`plan.sh status` にも ready と出る。しかし
   `load_all_tasks()` は `m['id']` で引くので `KeyError` になり、**その mission
   だけでなく全 mission の割り当てが止まる**。「pull はできるのに誰にも
   割り当てられない」という、いちばん理由の見えない止まり方である。
2. **id を名乗り替えたコピーが dispatcher からは選ばれる。** `pull` は
   `[破損]` として保留するのに、dispatcher は frontmatter の `status: pending` を
   そのまま信じて Worker を起こす。
3. **同じカードが「満たされた依存」に数えられる。** `status: done` を名乗る
   コピーが `done_ids` に入り、下流が動き出す。

さらに、**読めないカードの扱いも食い違っていた**。`plan.sh` の parser は
壊れた行で例外を投げ、呼び出し側が `[破損]` に隔離する。`dispatcher` の parser は
読めない行を黙って捨てるので、半分だけ読めた dict が `status: pending` を持って
いれば **そのまま dispatch される**。

## このファイルが固定すること

`plan.sh` と `dispatcher.sh` が **同じ queue から同じ task 集合を導く**こと。
片方だけ直せる形が残っているかぎり「今は一致している」は保証ではないので、
規則そのものは `scripts/lib_task_cards.py` ただ 1 つから来る (F-2b と同じ型の
問題なので、`lib_dep_rules.py` と同じ解き方をする)。

## 隔離方針

- dispatcher は **heredoc に埋め込まれた本物の python を exec()** して見る
  (`tests/test_orphan_daemon_guard.py` と同じ方式)。コピー実装を置くと、本物を
  直してもテストは緑のままになる。
- `lib_mux` は fake に差し替え、`# --- CYCLE ENTRY POINT ---` 以降を切り落とす。
  本番の mux / queue / 通知キャッシュには一切触らない。
- queue は毎回 `tmp_path` の使い捨て。

実行方法:
  python3 -m pytest tests/test_task_card_identity.py -v
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"
PLAN_SH = SCRIPTS_DIR / "plan.sh"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from task_graph_publisher_harness import load_plan_namespace  # noqa: E402

MISSION = "m-fixture"


# ---------------------------------------------------------------------------
# カードの fixture
# ---------------------------------------------------------------------------

def _card(task_id: str, status: str = "pending", *, declared_id: str | None = "",
          title: str | None = None, skills: str = "[code]") -> str:
    """1 枚のカード。`declared_id` が '' ならファイル名と同じ id 行を書く。

    `declared_id=None` は **id 行そのものが無い**カード (一番ありふれた壊れ方の
    片方)。それ以外の文字列は「コピーして id 行を直し忘れた」カードになる。
    """
    lines = ["---"]
    if declared_id is not None:
        lines.append(f"id: {declared_id or task_id}")
    lines += [
        f"title: {title or ('task ' + task_id)}",
        f"status: {status}",
        f"skills: {skills}",
        "blocked_by: []",
        "priority: medium",
        "worker: null",
        "---",
        "",
        "## Description",
        "",
        "fixture",
        "",
        "## Result",
        "",
    ]
    return "\n".join(lines) + "\n"


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


class Queue:
    """使い捨ての queue。plan.sh と dispatcher の両方に同じものを読ませる。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.queue = root / "queue"
        self.tasks = self.queue / "missions" / MISSION / "tasks"
        self.tasks.mkdir(parents=True)
        (self.queue / "archive").mkdir(parents=True)
        (self.queue / "missions" / MISSION / "mission.yaml").write_text(
            _mission_yaml(MISSION))
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")

    def write(self, task_id: str, text: str) -> pathlib.Path:
        path = self.tasks / f"{task_id}.md"
        path.write_text(text)
        return path

    def write_raw(self, task_id: str, data: bytes) -> pathlib.Path:
        """バイト列をそのまま置く。UTF-8 として読めないカードを作るため。

        `write_text()` では作れない —— 壊れたカードは *書き手が正しく書けなかった*
        結果として生まれるもので (途中で切れた書き込み、別エンコーディングの
        貼り付け、バイナリの取り違え)、テキストとして表現できるとはかぎらない。
        """
        path = self.tasks / f"{task_id}.md"
        path.write_bytes(data)
        return path

    def add(self, task_id: str, **kwargs) -> pathlib.Path:
        return self.write(task_id, _card(task_id, **kwargs))


@pytest.fixture
def queue(tmp_path) -> Queue:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()          # repo_identity_ok() を通すため
    (root / "registry" / "mux").mkdir(parents=True)
    return Queue(root)


# ---------------------------------------------------------------------------
# dispatcher.sh の本物の python を名前空間に読み込む
# ---------------------------------------------------------------------------

class _FakeMux:
    """本番の mux に触らせないための置き換え。dispatch() は呼ばないので空で足る。"""

    def __init__(self, *a, **kw):
        pass

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def _dispatcher_source() -> str:
    text = DISPATCHER_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "dispatcher.sh の python ヒアドキュメント (PYEOF) が見つからない"
    src = m.group(1)
    src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
    assert n == 1, "cycle entry point の印が無い — dispatcher.sh の構造が変わった?"
    return src


def load_dispatcher_namespace(root: pathlib.Path) -> dict:
    """`# --- CYCLE ENTRY POINT ---` の手前までを exec() する (本物のコード)。"""
    import lib_mux as real_lib_mux

    fake = types.ModuleType("lib_mux")
    fake.Mux = _FakeMux
    fake.repo_identity_ok = real_lib_mux.repo_identity_ok
    saved = sys.modules.get("lib_mux")
    sys.modules["lib_mux"] = fake

    argv = sys.argv
    sys.argv = [
        "dispatcher-embedded",
        str(root / "queue"),
        str(root / "registry"),
        str(root / "notify-cache.json"),
        "300",
        "60",
        str(root / "dispatcher.log"),
    ]
    try:
        ns: dict = {"__name__": "dispatcher_under_test"}
        exec(compile(_dispatcher_source(), f"{DISPATCHER_SH} (embedded, test)", "exec"), ns)
        return ns
    finally:
        sys.argv = argv
        if saved is None:
            del sys.modules["lib_mux"]
        else:
            sys.modules["lib_mux"] = saved


def _cards(ns: dict, slug: str = MISSION) -> dict:
    """dispatcher が読んだカードを {id: status} にする。"""
    return {meta.get("id"): meta.get("status")
            for meta, _ in ns["list_tasks_for_mission"](slug)}


# ---------------------------------------------------------------------------
# 1. id 行の無いカードで dispatch サイクルが落ちない
# ---------------------------------------------------------------------------

def test_a_card_without_an_id_line_does_not_crash_the_dispatch_cycle(queue):
    """`load_all_tasks()` が `KeyError` を投げないこと (害の本体)。

    `plan.sh` はこのカードを普通に受理し、`plan.sh status` にも ready と出す。
    dispatcher だけが落ちると、止まっているのは **全 mission の割り当て**
    なのに、queue を見るかぎり何も壊れていないように見える。
    """
    queue.add("t001", declared_id=None)
    ns = load_dispatcher_namespace(queue.root)

    all_tasks, done_ids, statuses = ns["load_all_tasks"]([MISSION])

    assert [meta["id"] for _slug, meta in all_tasks] == ["t001"]
    assert statuses == {MISSION: {"t001": "pending"}}
    assert done_ids == {MISSION: set()}


def test_the_dispatcher_takes_the_id_from_the_filename(queue):
    """id 行が無いカードの識別子が、ファイル名から来ること。"""
    queue.add("t007", declared_id=None)
    ns = load_dispatcher_namespace(queue.root)

    assert _cards(ns) == {"t007": "pending"}


# ---------------------------------------------------------------------------
# 2. id を名乗り替えたコピーは dispatcher からも保留される
# ---------------------------------------------------------------------------

def test_a_card_whose_id_contradicts_the_filename_is_never_dispatchable(queue):
    """`pull` が保留するカードを、dispatcher が pending と読まないこと。

    読んでしまうと Worker が起き、`plan.sh pull --task t002` で拒否される —
    起動 1 回分 (約 48KB の prompt) が丸ごと無駄になり、しかも理由が
    dispatcher のログにしか出ない。
    """
    queue.add("t001")
    queue.write("t002", _card("t002", declared_id="t001", title="task t002"))
    ns = load_dispatcher_namespace(queue.root)

    cards = _cards(ns)
    assert cards["t002"] != "pending", (
        f"名乗り替えたコピーが dispatch 可能に見えている: {cards}")
    assert cards["t001"] == "pending", f"健全なカードが巻き添えになった: {cards}"


def test_an_isolated_card_is_not_counted_as_a_finished_dependency(queue):
    """`done` を名乗るコピーが `done_ids` に入らないこと。

    「`t002` が done_ids に入っていない」だけを見ると、**欠陥のあるコードでも
    緑になる**。修正前の dispatcher は `t002.md` を `t001` として読むので、
    `done_ids` は `{'t001'}`、`t002` はどこにも現れない —— 名乗り替えを追認して
    カードを 1 枚消した状態こそが、いちばん見つけたい壊れ方である。

    だから見るのは「2 枚のカードが 2 枚として見えていること」と
    「そのうち done と数えられるのは健全な 1 枚だけであること」の両方。
    """
    queue.add("t001", status="done")
    queue.write("t002", _card("t002", status="done", declared_id="t001"))
    ns = load_dispatcher_namespace(queue.root)

    all_tasks, done_ids, statuses = ns["load_all_tasks"]([MISSION])

    assert sorted(meta["id"] for _slug, meta in all_tasks) == ["t001", "t002"], (
        "カードがファイル名で identify されていない (名乗り替えを追認して "
        f"1 枚消えている): {[m.get('id') for _s, m in all_tasks]}")
    assert set(statuses[MISSION]) == {"t001", "t002"}, statuses
    assert done_ids[MISSION] == {"t001"}, (
        f"隔離すべきカードが『完了した依存』に数えられている: {done_ids}")


def test_the_isolated_card_is_visible_not_dropped(queue):
    """保留したカードが、保留されたと分かる形で残ること。

    黙って落とすと「なぜ割り当てられないのか」が dispatcher 側から分からない。
    """
    queue.write("t002", _card("t002", declared_id="t001"))
    ns = load_dispatcher_namespace(queue.root)

    cards = ns["list_tasks_for_mission"](MISSION)
    assert len(cards) == 1, f"カードが黙って消えている: {cards}"
    title = cards[0][0].get("title", "")
    assert "破損" in title, f"隔離の印が出ていない: {title!r}"
    assert "id" in title.lower(), f"何が問題なのか読み取れない: {title!r}"


# ---------------------------------------------------------------------------
# 3. 読めないカードの扱いも一致する
# ---------------------------------------------------------------------------

def test_an_unparseable_card_is_isolated_not_half_read(queue):
    """壊れた frontmatter が「半分読めた pending」にならないこと。

    `plan.sh` の parser は壊れた行で例外を投げ、呼び出し側が `[破損]` に隔離する。
    黙って行を捨てる parser だと、残った `status: pending` がそのまま信じられて
    **中身の分からないカードが dispatch される**。
    """
    queue.write("t001", (
        "---\n"
        "id: t001\n"
        "this line has no colon and is not a list item\n"
        "status: pending\n"
        "skills: [code]\n"
        "---\n"
        "\n## Description\n\nfixture\n"
    ))
    ns = load_dispatcher_namespace(queue.root)

    cards = _cards(ns)
    assert cards == {"t001": "corrupted"}, (
        f"壊れたカードが隔離されていない: {cards}")


def test_a_card_that_cannot_be_read_does_not_abort_the_cycle(queue, tmp_path):
    """読めないファイル (権限) でサイクル全体が落ちないこと。

    今の dispatcher は per-file の try/except で読み取り失敗も吸収している。
    共有の読み取りに寄せる際、その耐性を落としてはならない — 1 枚のカードで
    全 mission の割り当てが止まるのは、いちばん避けたい倒れ方である。
    """
    path = queue.add("t001")
    queue.add("t002")
    path.chmod(0o000)
    if path.stat().st_mode & 0o400:      # root では chmod が効かない
        pytest.skip("読み取り権限を落とせない環境 (root?)")
    try:
        ns = load_dispatcher_namespace(queue.root)
        cards = _cards(ns)
    finally:
        path.chmod(0o644)

    assert cards.get("t002") == "pending", f"健全なカードが巻き添えになった: {cards}"
    assert cards.get("t001") == "corrupted", f"読めないカードが隔離されていない: {cards}"


# ---------------------------------------------------------------------------
# 4. 両者が同じ task 集合を導く (ズレの検出そのもの)
# ---------------------------------------------------------------------------

#: 「ありうる壊れ方」を 1 つの queue に全部並べる。ここに 1 行足すだけで、
#: 以後 pull と dispatch のズレは自動的に検出される。
AGREEMENT_CARDS = [
    ("t001", _card("t001", status="pending")),
    ("t002", _card("t002", status="done")),
    ("t003", _card("t003", status="in_progress")),
    ("t004", _card("t004", status="failed")),
    # id 行が無い — 矛盾ではないので普通のカードとして扱われる
    ("t005", _card("t005", declared_id=None)),
    # id 行が空 — 「無い」と同じ扱い (t013)
    ("t006", "---\nid:\ntitle: task t006\nstatus: pending\nskills: [code]\n"
             "blocked_by: []\n---\n\n## Description\n\nfixture\n"),
    # コピーして id 行を直し忘れた
    ("t007", _card("t007", declared_id="t001")),
    # done を名乗るコピー
    ("t008", _card("t008", status="done", declared_id="t002")),
    # frontmatter が壊れている
    ("t009", "---\nid: t009\nbroken line without a colon\nstatus: pending\n---\n\n"
             "## Description\n\nfixture\n"),
    # frontmatter の閉じが無い
    ("t010", "---\nid: t010\nstatus: pending\n\n## Description\n\nfixture\n"),
    # frontmatter が始まっていない
    ("t011", "## Description\n\nfixture\n"),
]


def test_pull_and_dispatch_derive_the_same_cards(queue):
    """同じ queue から、plan.sh と dispatcher が同じ (id, status) を導くこと.

    ここが赤くなるのは「片方だけ直した」瞬間である。規則を 1 箇所に集約しても、
    `plan.sh` 側の list_tasks() と dispatcher 側の list_tasks_for_mission() は
    別の関数として残る (入力の型も、警告の出し先も違う)。だから *結果が同じ*
    ことを直接押さえる。
    """
    for task_id, text in AGREEMENT_CARDS:
        queue.write(task_id, text)

    dispatcher_ns = load_dispatcher_namespace(queue.root)
    from_dispatcher = _cards(dispatcher_ns)

    plan_ns = load_plan_namespace(PLAN_SH, str(queue.queue), str(REPO_ROOT))
    from_plan = {meta.get("id"): meta.get("status")
                 for meta, _ in plan_ns["list_tasks"](MISSION)}

    assert from_dispatcher == from_plan, (
        "pull が受理するカードと dispatcher がスケジュールするカードがズレている\n"
        f"  plan.sh    : {from_plan}\n"
        f"  dispatcher : {from_dispatcher}"
    )
    # 対照: 全部が「破損」になって一致した、という緑ではないこと。
    assert from_plan["t001"] == "pending", from_plan
    assert from_plan["t005"] == "pending", from_plan


def test_pull_and_dispatch_agree_on_which_dependencies_are_finished(queue):
    """done_ids も一致すること (依存判定の入力そのもの)。

    規則 (`unmet_dependencies`) を共有しても、**入力が違えば結論は違う** —
    Codex の指摘の核心はここだった。
    """
    for task_id, text in AGREEMENT_CARDS:
        queue.write(task_id, text)

    dispatcher_ns = load_dispatcher_namespace(queue.root)
    _all, done_by_mission, _statuses = dispatcher_ns["load_all_tasks"]([MISSION])

    plan_ns = load_plan_namespace(PLAN_SH, str(queue.queue), str(REPO_ROOT))
    terminal = plan_ns["TERMINAL_STATUSES"]
    from_plan = {meta["id"] for meta, _ in plan_ns["list_tasks"](MISSION)
                 if meta.get("status") in terminal}

    assert done_by_mission[MISSION] == from_plan, (
        "「満たされた依存」の集合がズレている\n"
        f"  plan.sh    : {sorted(from_plan)}\n"
        f"  dispatcher : {sorted(done_by_mission[MISSION])}"
    )
    assert from_plan == {"t002"}, f"対照が崩れている: {sorted(from_plan)}"


# ---------------------------------------------------------------------------
# 5. 規則のコピーが戻ってこないこと
# ---------------------------------------------------------------------------
#
# 上の一致テストがあれば「今ズレている」ことは分かる。だがコピーが 2 つあれば、
# 片方だけ直せる形はいつでも戻る —— そして戻ったことに気付けるのは、誰かが
# 実際に壊れたカードを置いた日だけになる。だから形のほうも押さえる。

CARDS_PY = SCRIPTS_DIR / "lib_task_cards.py"

#: コピーが残っていないかを見に行く範囲 (lib_dep_rules の同種テストと同じ)。
_SCAN_GLOBS = ("scripts/*.sh", "scripts/*.py", "hooks/*.sh")


def _code_lines(path: pathlib.Path) -> str:
    """行頭コメントを落としたテキスト。

    探しているのは「規則のコピー」であって、規則について *書いた説明* ではない。
    落とさないと、隔離の仕組みを解説している文章 (まさにこの変更で増えるもの) が
    そのままコピーとして数えられ、テストを弱める圧力になる。
    """
    return "\n".join(
        line for line in path.read_text(errors="replace").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_card_reader_exists_as_one_module():
    """読み取りの本体が scripts/lib_task_cards.py にあること。"""
    import lib_task_cards

    assert callable(lib_task_cards.list_task_cards)
    assert callable(lib_task_cards.read_task_card)
    assert callable(lib_task_cards.isolated_task)
    assert lib_task_cards.CORRUPT_TASK_STATUS


def test_both_readers_import_the_shared_module():
    """plan.sh と dispatcher.sh が、どちらもこのモジュールから読むこと。"""
    plan_src = PLAN_SH.read_text()
    assert "lib_task_cards" in plan_src, "plan.sh が lib_task_cards を読んでいない"
    assert "_TASK_CARDS.list_task_cards(" in plan_src, (
        "plan.sh の list_tasks() が共有の読み取りを呼んでいない")

    disp_src = DISPATCHER_SH.read_text()
    assert "from lib_task_cards import" in disp_src, (
        "dispatcher.sh が lib_task_cards を import していない")
    assert "list_task_cards(MISSIONS_DIR" in disp_src, (
        "dispatcher.sh の list_tasks_for_mission() が共有の読み取りを呼んでいない")


def test_no_script_keeps_its_own_frontmatter_parser():
    """`def parse_frontmatter` が本体以外に無いこと。

    dispatcher.sh は PR #212 のあとも独自の parser を持っていた (Codex 5 巡目)。
    そちらは読めない行を黙って捨てるので、plan.sh が隔離するカードを pending と
    して読む —— 同じ queue、違う結論。
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for glob in _SCAN_GLOBS
        for path in sorted(REPO_ROOT.glob(glob))
        if path.resolve() != CARDS_PY.resolve()
        and "def parse_frontmatter(" in _code_lines(path)
    ]
    assert not offenders, (
        f"frontmatter parser のコピーが残っている: {offenders} — "
        f"scripts/lib_task_cards.py の parse_frontmatter() を使うこと")


def test_no_script_builds_an_isolated_card_by_hand():
    """`[破損]` カードの組み立てが `isolated_task()` 1 箇所だけであること。

    隔離の形 (status / title の印 / 空の blocked_by) がバラバラの場所に書かれると、
    次の 1 件で片方だけ直して穴が開く —— t013 が `enforce_task_graph_contract()` に
    ゲートを 1 つだけ置いたのと同じ理由。
    """
    import lib_task_cards

    needle = "'" + lib_task_cards.CORRUPT_TASK_STATUS + "'"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for glob in _SCAN_GLOBS
        for path in sorted(REPO_ROOT.glob(glob))
        if path.resolve() != CARDS_PY.resolve()
        and needle in _code_lines(path)
    ]
    assert not offenders, (
        f"疑似ステータスのリテラルが直書きされている: {offenders} — "
        f"lib_task_cards.CORRUPT_TASK_STATUS / isolated_task() を使うこと")


# ---------------------------------------------------------------------------
# 6. 読み取り経路の例外が、1 つ残らず隔離に落ちる (Codex 6 巡目 P1)
# ---------------------------------------------------------------------------
#
# t014 で読み取りを 1 箇所に集約したこと自体は正しい。落としたのは **広さ** の
# ほうだった。集約前、この読み取りを持っていた 5 者のハンドラはこうなっていた。
#
#   dispatcher.sh          try: parse_frontmatter(path.read_text()) except Exception
#   verifier-dispatcher.sh 同上
#   watchdog.py            同上 (except Exception: continue)
#   plan.sh                read は素通し / parse だけ except ValueError
#   taskvia-sync.sh        ハンドラ無し
#
# 集約先は `except OSError` + `except ValueError` だった。**常駐デーモン 3 者が
# 持っていた `except Exception` より狭い**。狭くなった差分がそのまま穴になり、
# 不正な UTF-8 を含むカード 1 枚で `UnicodeDecodeError` が `list_task_cards()` を
# 突き抜け、**全 mission の割り当てと Worker の生存監視が同時に止まる**。
#
# だからここで押さえるのは「UnicodeError も捕まえた」ではない。**読み取り経路から
# 例外が出てこない**ことそのもの ——「今回の 1 件を足した」で終わらせると、次に
# 誰かが読み取り経路に新しい失敗を持ち込んだ日に、同じ止まり方がもう一度出る。

UNDECODABLE_CARD = (
    b"---\n"
    b"id: t001\n"
    b"title: \xff\xfe broken encoding\n"
    b"status: pending\n"
    b"skills: [code]\n"
    b"blocked_by: []\n"
    b"---\n"
    b"\n## Description\n\nfixture\n"
)


def test_a_card_with_invalid_utf8_does_not_abort_the_dispatch_cycle(queue):
    """0xff を含むカード 1 枚で、dispatch サイクル全体が落ちないこと (害の本体)。

    落ちるのはその mission ではなく **全 mission の割り当て**である。しかも
    `plan.sh status` には何も出ない (同じ例外で status も落ちる) ので、
    「pull もできないし誰も割り当てられない、理由はどこにも出ていない」という
    いちばん理由の見えない止まり方になる。
    """
    queue.write_raw("t001", UNDECODABLE_CARD)
    queue.add("t002")
    ns = load_dispatcher_namespace(queue.root)

    all_tasks, done_ids, statuses = ns["load_all_tasks"]([MISSION])

    assert statuses[MISSION].get("t002") == "pending", (
        f"健全なカードが 1 枚の壊れたファイルに巻き添えにされた: {statuses}")
    assert statuses[MISSION].get("t001") == "corrupted", (
        f"読めなかったカードが隔離されていない: {statuses}")
    assert done_ids[MISSION] == set(), (
        f"中身の読めないカードが『完了した依存』に数えられている: {done_ids}")
    assert sorted(meta["id"] for _slug, meta in all_tasks) == ["t001", "t002"]


def test_a_card_with_invalid_utf8_does_not_abort_the_watchdog_cycle(queue):
    """同じカードで、Worker の生存監視が止まらないこと。

    dispatcher と watchdog は別のプロセスだが、読み取りは同じ 1 つの関数から
    来る —— だから穴も同時に開く。割り当てが止まったうえに、走っている Worker
    の監視まで同時に消えるのがこの欠陥のいちばん悪いところで、
    `knowledge/daemon-authority.md` の相互監視もこれは救えない (どちらの
    プロセスも生きたまま、毎サイクル同じ例外で何もせずに終わる)。
    """
    import watchdog

    queue.write_raw("t001", UNDECODABLE_CARD)
    queue.write("t002", _card("t002", status="in_progress"))

    active = watchdog.load_active_tasks(queue.queue)

    assert [task_id for _slug, task_id, _meta in active] == ["t002"], (
        f"in_progress の Worker が監視対象から消えた: {active}")


def test_pull_and_dispatch_agree_on_an_undecodable_card(queue):
    """plan.sh と dispatcher が、読めないカードについても同じ結論を出すこと。

    片方だけが落ちる形だと、`plan.sh status` で見えているものと実際に
    割り当てられるものがズレる —— t014 で閉じたはずのズレが、読み取り失敗の
    経路からもう一度開く。
    """
    queue.write_raw("t001", UNDECODABLE_CARD)
    queue.add("t002")

    dispatcher_ns = load_dispatcher_namespace(queue.root)
    from_dispatcher = _cards(dispatcher_ns)

    plan_ns = load_plan_namespace(PLAN_SH, str(queue.queue), str(REPO_ROOT))
    from_plan = {meta.get("id"): meta.get("status")
                 for meta, _ in plan_ns["list_tasks"](MISSION)}

    assert from_dispatcher == from_plan, (
        "読めないカードの扱いがズレている\n"
        f"  plan.sh    : {from_plan}\n"
        f"  dispatcher : {from_dispatcher}"
    )
    assert from_plan["t002"] == "pending", f"対照が崩れている: {from_plan}"


# --- 読み取り経路の失敗を全部並べる -----------------------------------------
#
# ここに 1 行足すだけで、その壊れ方が隔離に落ちることが以後ずっと押さえられる。
# 「隔離されない」ことが正しい欄 (id 欄の不在・空) も同じ表に置いてあるのは、
# 全部を [破損] に倒して一致させた、という緑ではないことを同時に示すため。

def _make_unreadable(queue):
    path = queue.add("t001")
    path.chmod(0o000)
    if path.stat().st_mode & 0o400:          # root では chmod が効かない
        pytest.skip("読み取り権限を落とせない環境 (root?)")


def _make_directory(queue):
    (queue.tasks / "t001.md").mkdir()


READ_PATH_FAILURES = [
    # (名前, カードの作り方, 期待する status)
    ("OSError: 読み取り権限が無い",
     _make_unreadable, "corrupted"),
    ("OSError: ファイルではなくディレクトリ",
     _make_directory, "corrupted"),
    ("UnicodeError: UTF-8 として読めないバイト列",
     lambda q: q.write_raw("t001", UNDECODABLE_CARD), "corrupted"),
    ("UnicodeError: UTF-16 で書かれたカード",
     lambda q: q.write_raw("t001", _card("t001").encode("utf-16")), "corrupted"),
    ("ValueError: frontmatter が始まっていない",
     lambda q: q.write("t001", "## Description\n\nfixture\n"), "corrupted"),
    ("ValueError: frontmatter が閉じていない",
     lambda q: q.write("t001", "---\nid: t001\nstatus: pending\n\n## Description\n"),
     "corrupted"),
    ("ValueError: 認識できない行がある",
     lambda q: q.write("t001", "---\nid: t001\nbroken line\nstatus: pending\n---\n"),
     "corrupted"),
    ("id 欄がファイル名と食い違う",
     lambda q: q.write("t001", _card("t001", declared_id="t002")), "corrupted"),
    # --- ここから下は「隔離されない」ことが正しい (t013 の設計) ---
    ("id 欄が無い — ファイル名が答えを持っているので普通のカード",
     lambda q: q.write("t001", _card("t001", declared_id=None)), "pending"),
    ("id 欄が空 — 「無い」と同じ",
     lambda q: q.write("t001", "---\nid:\ntitle: t001\nstatus: pending\n"
                               "skills: [code]\nblocked_by: []\n---\n"), "pending"),
]


@pytest.mark.parametrize(
    "builder,expected",
    [pytest.param(b, e, id=name) for name, b, e in READ_PATH_FAILURES],
)
def test_every_read_path_failure_lands_in_isolation(queue, builder, expected):
    """読み取り経路で起こりうる失敗が、1 つ残らず隔離に落ちること。

    そして **隣のカードは無傷である**こと。倒れる先は常に「そのカードだけが
    動かない」でなければならない。
    """
    builder(queue)
    queue.add("t002")
    ns = load_dispatcher_namespace(queue.root)

    cards = _cards(ns)

    assert cards.get("t001") == expected, f"t001 の扱いが違う: {cards}"
    assert cards.get("t002") == "pending", (
        f"健全なカードが巻き添えになった: {cards}")


def test_an_unexpected_exception_in_the_read_path_is_contained(queue, monkeypatch):
    """**まだ知らない** 失敗でも、サイクルが落ちないこと。

    集約前、常駐デーモン 3 者はカード単位の `except Exception` でこれを吸収して
    いた。集約先がそれより狭いかぎり、「読み取り経路に新しい失敗が増えた日」は
    そのまま「全 mission の割り当てが止まる日」になる —— 今回の P1 が
    まさにそれで、`UnicodeDecodeError` は最初からあった失敗が見えただけだった。

    だから押さえるのは特定の例外型ではなく、**例外が出てこないこと**そのもの。
    """
    import lib_task_cards

    def exploding_parse(*_a, **_kw):
        raise RuntimeError("まだ誰も想定していない読み取り失敗")

    monkeypatch.setattr(lib_task_cards, "parse_frontmatter", exploding_parse)

    queue.add("t001")
    queue.add("t002")
    ns = load_dispatcher_namespace(queue.root)

    cards = _cards(ns)

    assert cards == {"t001": "corrupted", "t002": "corrupted"}, (
        f"想定外の例外が隔離に落ちていない: {cards}")


def test_a_failing_warn_callback_does_not_abort_the_cycle():
    """**警告を出せないこと**が、割り当てを止める理由にならないこと。

    読み取り経路に残っていたもう 1 つの漏れ。`warn` は呼び出し側から渡される
    ただの callable で、dispatcher なら `log()` —— つまり **ログファイルへの
    書き込み**である。ディスクが埋まる / ログの権限が変わるだけでそれは
    例外を投げるし、そのとき壊れたカードが 1 枚あれば、警告を出そうとした瞬間に
    サイクルが落ちる。

    「壊れたカードが 1 枚あって、かつログが書けない」という、いちばん忙しい日に
    しか揃わない組み合わせで全 mission の割り当てが止まる形なので、`UnicodeError`
    と同じ扱いにする —— 出せない警告は諦め、カードの隔離だけは必ず返す。
    """
    import lib_task_cards

    def exploding_warn(_msg):
        raise OSError(28, "No space left on device")

    meta, _body = lib_task_cards.read_task_card(
        "/nonexistent/queue/missions/m/tasks/t001.md", "t001", warn=exploding_warn)

    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, meta


def test_a_failing_warn_callback_does_not_abort_the_listing_either(queue):
    """同じことを、カードを 1 枚も開けなかった場合にも言えること。

    `list_task_cards()` は `tasks/` そのものが読めないときにも警告を出す。
    そこだけ素の `warn(...)` が残っていると、漏れは塞いだつもりで残る —— これが
    「今回の 1 件を足す」で終わらせたときに必ず起きる形である。
    """
    import lib_task_cards

    queue.add("t001")
    queue.tasks.chmod(0o000)
    if os.access(queue.tasks, os.R_OK):          # root では chmod が効かない
        queue.tasks.chmod(0o755)
        pytest.skip("読み取り権限を落とせない環境 (root?)")

    def exploding_warn(_msg):
        raise OSError(28, "No space left on device")

    try:
        cards = lib_task_cards.list_task_cards(queue.tasks, warn=exploding_warn)
    finally:
        queue.tasks.chmod(0o755)

    assert cards == []


def test_an_isolated_card_says_why_it_was_isolated(queue):
    """隔離したカードに、何が起きたのかが残ること。

    `[破損]` として保留するのは「黙って消えたカードは誰にも直せない」から
    だった (t013)。理由が残っていなければ、保留にした意味がない。
    """
    queue.write_raw("t001", UNDECODABLE_CARD)
    ns = load_dispatcher_namespace(queue.root)

    meta, _body = ns["list_tasks_for_mission"](MISSION)[0]

    assert "破損" in meta.get("title", ""), meta
    reason = meta.get("parse_error") or ""
    assert reason, f"隔離の理由が残っていない: {meta}"
    assert "utf-8" in reason.lower() or "decode" in reason.lower(), (
        f"何が起きたのか読み取れない理由: {reason!r}")
