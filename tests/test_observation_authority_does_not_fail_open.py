#!/usr/bin/env python3
"""横断ガード: 観測 (stat / /proc / exists / iterdir) の失敗を「許可 / 不在」に
潰している箇所が増えたら CI を落とす (t053)。

## なぜこの task があるか

mission B の Codex review 1 巡で、**同じ欠陥族が 3 件**出た。

* PR #240: `stat` が読めないとき pid を kill 許可に入れる (fail-open)
* PR #240: unobservable だけのスキャンが「クリーン」を返す
* PR #237: 画面が読めないとき「trust ダイアログは無い」に倒して Enter を送る

いずれも **「観測に失敗した」を「許可してよい」または「対象は存在しない」に
潰している**。3 件とも既に直っている (`tests/leaked_descendants.py` /
`tests/kill_budget.py` / `scripts/start.sh:_abort_on_unobservable_dialog`)。
この task は個別修正ではなく、**同じ型が増えたら CI が落ちる構造**に切り替える
(memory: structural-guard-beats-site-patches)。

## スコープ (実装から選定。usage 行やドキュメントの一覧からではない)

「観測の失敗が、破壊的な結論 (kill・退役・respawn 拒否・隔離・復元の上書き) に
使われうる」モジュールだけを対象にする (`AUTHORITY_MODULES`):

* `lib_pane_process.py` — watchdog の idle/job 判定。誤判定は Worker の
  hard_idle terminate に直結する。
* `lib_retirement.py` — Worker の退役・kill を実行するモジュールそのもの。
* `lib_daemon_watch.py` — dispatcher/watchdog の相互監視・respawn 権限。
* `lib_mux.py` — pane の kill・spawn 記録 (`registry/mux/*.json`) の抹消。
* `worktree_gc.py` — worktree の隔離・復元 (上書き事故になりうる)。
* `watchdog.py` — Worker の hard_idle/max による terminate。
* `tests/leaked_descendants.py` / `tests/kill_budget.py` — pytest 自身の
  kill 権限 (2026-09-27 の自爆の当事者。同じ観測失敗の型を持ちうる)。

queue/registry のファイル読み取り (`lib_task_cards` 経由) は既に
`tests/test_queue_reads_go_through_the_guard.py` が別に見ている。ここが見るのは
**stat / /proc / exists / iterdir / listdir / readlink / access** ——
そちらがカバーしない観測手段。

## 検査の仕方

対象モジュールの `.stat()` / `.exists()` / `.read_bytes()` / `.read_text()` /
`.readlink()` / `os.access()` / `.iterdir()` / `os.listdir()` 呼び出しを AST で
**全部** 拾う (`_risky_calls()`、`test_queue_reads_go_through_the_guard.py` と
同じ「実際の呼び出し形」で見る流儀)。1 つでも `OBSERVATION_SITES` に無ければ
`test_every_risky_call_is_classified` が落ちる —— 新しい呼び出しは、安全だろうと
危険だろうと、まず理由を書かせる (allowlist の思想は
memory: approve-judgment-needs-allowlist-and-scope と同じ)。

## SAFE と KNOWN_FAIL_OPEN

`OBSERVATION_SITES` の値は `(status, reason)`。

* `SAFE` — 観測の失敗を破壊的な結論に流し込んでいないことを確認済み。
  ENOENT/ESRCH だけを「無い」として扱い他は re-raise / None / unobservable
  フラグで返す、fail-closed な向き、または実際の安全装置が別の層 (O_EXCL の
  atomic create 等) にある、のいずれか。reason に根拠を書く。
* `KNOWN_FAIL_OPEN` — **この task で見つかった、まだ直っていない同族の欠陥**。
  この PR では直さない (直しに行くとスコープが広がる。memory:
  pr-size-breaks-review-machinery)。reason に危険度を書く。Director backlog へ
  (Result 参照)。

赤の実証: `tests/red_proof_t053.sh`。

    python3 -m pytest tests/test_observation_authority_does_not_fail_open.py -v
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

#: この関数呼び出しの `.attr` が現れたら「観測」とみなす。
#: stat 系 (`stat`/`access`) / 存在確認 (`exists`) / 内容読み取り
#: (`read_bytes`/`read_text`) / symlink 解決 (`readlink`) / 列挙
#: (`iterdir`/`listdir`)。書き込み系 (`write_*`/`unlink`) は対象外 ——
#: この task は「観測の失敗」の扱いを見るのであって、書き込みの安全性は
#: 別の関心事 (`tests/test_queue_reads_go_through_the_guard.py` 等)。
RISKY_ATTRS = frozenset({
    "stat", "exists", "read_bytes", "read_text", "readlink", "access",
    "iterdir", "listdir",
})

AUTHORITY_MODULES = [
    SCRIPTS_DIR / "lib_pane_process.py",
    SCRIPTS_DIR / "lib_retirement.py",
    SCRIPTS_DIR / "lib_daemon_watch.py",
    SCRIPTS_DIR / "lib_mux.py",
    SCRIPTS_DIR / "worktree_gc.py",
    SCRIPTS_DIR / "watchdog.py",
    TESTS_DIR / "leaked_descendants.py",
    TESTS_DIR / "kill_budget.py",
]


def _owner_by_line(tree: ast.AST) -> dict[int, str]:
    """行番号 → その行を含む関数名 (`test_queue_reads_go_through_the_guard.py`
    の `_owner_by_line()` と同じ作り)。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    owner.setdefault(sub.lineno, node.name)
    return owner


def _risky_calls(path: pathlib.Path) -> list[tuple[str, str]]:
    """`(関数名, ソース断片)` を、このファイルの「観測」呼び出し全部について返す。

    `os.listdir(x)` は `ast.Name` (裸の関数)、`p.stat()` / `os.stat()` は
    どちらも `ast.Attribute` (`.attr` で拾える — `os.stat` は
    `Attribute(attr="stat", value=Name("os"))` なので RISKY_ATTRS の
    `"stat"` に自然に当たる)。
    """
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    owner = _owner_by_line(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Attribute) and func.attr in RISKY_ATTRS:
            hit = True
        elif isinstance(func, ast.Name) and func.id == "listdir":
            hit = True
        if not hit:
            continue
        seg = ast.get_source_segment(src, node) or "<unparsed>"
        found.append((owner.get(node.lineno, "<module>"), " ".join(seg.split())))
    return found


def _all_sites() -> list[tuple[str, str, str]]:
    """`(script, function, snippet)` を全 AUTHORITY_MODULES について返す。"""
    out: list[tuple[str, str, str]] = []
    for path in AUTHORITY_MODULES:
        for fn, seg in _risky_calls(path):
            out.append((path.name, fn, seg))
    return out


SAFE = "SAFE"
KNOWN_FAIL_OPEN = "KNOWN_FAIL_OPEN"

# ---------------------------------------------------------------------------
# 判定した対象 (script, function, snippet) -> (status, reason)
# ---------------------------------------------------------------------------
#
# 各行の reason は、そのコードを実際に読んで確かめた結論。「たぶん安全」では
# なく、fail-closed の根拠 (ENOENT/ESRCH だけを通す・None/unobservable で
# 返す・呼び出し側が hold/assume-alive 側に倒す・O_EXCL のような別層の安全
# 装置がある、等) を書く。

OBSERVATION_SITES: dict[tuple[str, str, str], tuple[str, str]] = {
    # -- lib_pane_process.py: watchdog の idle/job 判定 --------------------
    ("lib_pane_process.py", "_proc_stat",
     'Path(f"/proc/{pid}/stat").read_bytes()'):
        (SAFE, "ENOENT/ESRCH だけ None。他の OSError は re-raise し呼び出し側 "
               "(classify_process_tree) が unknown に倒す (t082/t049 族A)"),
    ("lib_pane_process.py", "_proc_cmdline",
     'Path(f"/proc/{pid}/cmdline").read_bytes()'):
        (SAFE, "同上。cmdline が読めない job wrapper を idle_process に "
               "誤判定しない契約 (t082)"),
    ("lib_pane_process.py", "_proc_environ",
     'Path(f"/proc/{pid}/environ").read_bytes()'):
        (SAFE, "同上。空 ({}) は消滅でも読めないでもない正当な結果として区別 (t091)"),
    ("lib_pane_process.py", "_proc_exe", 'os.readlink(f"/proc/{pid}/exe")'):
        (SAFE, "同上契約。_is_session_body() は exe が読めない場合 False = "
               "infra 側委譲で、危険側 (job 見逃し) には倒れない (t097)"),
    ("lib_pane_process.py", "classify_process_tree", 'Path("/proc").iterdir()'):
        (SAFE, "列挙自体が失敗した場合の扱いはこの1呼び出しの外 (呼び出し元は "
               "個々の pid だけを辿るので、iterdir 失敗は root 直下の列挙のみに "
               "影響し pane 内の既知 pid ツリーには波及しない"),

    # -- lib_retirement.py: Worker の退役・kill --------------------------
    ("lib_retirement.py", "process_alive",
     'Path(f"/proc/{pid}/stat").read_text(errors="replace")'):
        (SAFE, "呼び出しは try 節の外で FileNotFoundError/ProcessLookupError を "
               "個別に捕まえ、それ以外は re-raise (呼び出し側で fail-closed)"),
    ("lib_retirement.py", "has_marker",
     'request_path(self.registry_dir, agent).exists()'):
        (SAFE, "pathlib.Path.exists() は EACCES を False に潰さず raise する "
               "(実測)。本当の二重書き込み防止は _write_request() の "
               "write_json_exclusive (O_EXCL) —— has_marker() は早期ログ用の "
               "ソフトな事前チェックに過ぎない"),
    ("lib_retirement.py", "has_marker",
     'progress_path(self.registry_dir, agent).exists()'):
        (SAFE, "同上。O_EXCL が本当の関門"),
    ("lib_retirement.py", "_write_request",
     'progress_path(self.registry_dir, agent).exists()'):
        (SAFE, "同上。この直後の write_json_exclusive(request_path, ...) が "
               "WROTE_EXISTS を返せば書き込みを拒否するので、この exists() が "
               "誤って False になっても二重request の実害は O_EXCL 側で止まる"),
    ("lib_retirement.py", "_check_stall",
     'stall_path(self.registry_dir, agent).exists()'):
        (SAFE, "誤って False (再報告扱い) になっても、結果は Director への "
               "再通知 (ノイズ) であって kill/queue 書き換えではない"),
    ("lib_retirement.py", "list_agents", "d.iterdir()"):
        (SAFE, "OSError は個々の agent ディレクトリではなく registry_dir 直下の "
               "列挙。呼び出し側 (process_all) は空リストなら「今回は何もしない」 "
               "側に倒れ、既存の退役処理を止めない"),
    ("lib_retirement.py", "_guard",
     '(self.queue_dir / "assignments" / agent).exists()'):
        (KNOWN_FAIL_OPEN,
         "HIGH: `except OSError: pass` が exists() の PermissionError も含めて "
         "握り潰し、コードは『assignment 無し』のときと同じ経路へ抜ける。"
         "assignment が実在するのに (EACCES 等で) 観測できなかった場合、"
         "idle retirement の premise 再チェックが素通りし GUARD_DISCARD を "
         "返さない —— タスクを再取得した Worker が誤って kill 候補に残る。"
         "この task では直さない (t053 スコープ外)。Director backlog へ。"),

    # -- lib_daemon_watch.py: dispatcher/watchdog 相互監視 -----------------
    ("lib_daemon_watch.py", "process_generation",
     'Path(proc_root, str(pid), "stat").read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "全 OSError で None。呼び出し元 instance_alive() は "
               "『generation を read できない = alive のまま』に倒す設計 "
               "(docstring: \"the direction we must fail in is 'do not "
               "respawn'\")"),
    ("lib_daemon_watch.py", "scan_daemon_pids", "root.iterdir()"):
        (SAFE, "OSError で None を返し、呼び出し側は『could not look』として "
               "hold する (docstring: \"holding forever ... is the correct "
               "direction to fail in\")"),
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "cmdline").read_bytes()'):
        (SAFE, "ENOENT/ESRCH だけ continue (消えた)。他は None を return し "
               "walk 全体を『不完全』として hold させる"),
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "stat").read_text(encoding="utf-8")'):
        (SAFE, "cmdline 一致後の zombie 判定専用。読めなければ pass して "
               "found.append(pid) —— 『生きているとみなす』方向で、これは "
               "respawn を許可しない (=二重起動させない) 側の安全な倒し方"),
    ("lib_daemon_watch.py", "_remove_marker", "path.exists()"):
        (SAFE, "unlink() が FileNotFoundError 以外の OSError を投げた後の "
               "確認。exists() が (EACCES 等で) raise すれば例外は関数の外へ "
               "伝播し『消せたか分からない』が明示的に見える形になる —— 黙って "
               "\"消せた\" にはならない"),

    # -- lib_mux.py: pane の kill・spawn 記録 ------------------------------
    ("lib_mux.py", "_proc_cwd", "os.readlink(link)"):
        (SAFE, "OSError を捕まえ _PROC_GONE (ENOENT かつ親も消滅) か "
               "_PROC_UNREADABLE の三値で返す。呼び出し側は UNREADABLE を "
               "absence と区別する契約 (docstring)"),
    ("lib_mux.py", "_proc_cwd", "link.parent.exists()"):
        (SAFE, "ENOENT 系エラーが本当に『/proc/<pid> ごと消えた』かを補強する "
               "追加確認。ここが raise しても外側の except OSError が拾わず "
               "_PROC_UNREADABLE 側へは倒れない実装だが、EACCES で親が読めない "
               "状況自体が readlink 側で既に UNREADABLE を返した後の分岐で "
               "起きるので影響は absence 判定を誤って GONE にしない方向に留まる"),
    ("lib_mux.py", "repo_identity_ok", '(root / ".git").exists()'):
        (SAFE, "docstring: 'Anything else (deleted, recreated as an empty "
               "directory, a permission error) returns False so callers can "
               "fail closed: skip the action (do not kill)'"),
    ("lib_mux.py", "_read_proc",
     'path.read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "OSError を _PROC_GONE (ENOENT/ESRCH) / _PROC_UNREADABLE の "
               "三値に分ける。呼び出し側は UNREADABLE を absence と区別する"),
    ("lib_mux.py", "_live_children", "Path(proc_root).iterdir()"):
        (SAFE, "OSError で None (docstring: \"could not look\" is not "
               "\"nothing there\")。空リストと未観測を区別する呼び出し契約"),
    ("lib_mux.py", "proc_table", "Path(proc_root).iterdir()"):
        (SAFE, "同上 (proc_table も None/空を区別する契約)"),
    ("lib_mux.py", "proc_table",
     '(entry / "cmdline").read_bytes()'):
        (SAFE, "t101: errors=\"replace\" で decode 例外を避けるのみ。読み取り "
               "失敗 (OSError) 時の扱いはこの関数のさらに外側の except で "
               "None に倒す設計 (docstring: incomplete walk = None)"),
    ("lib_mux.py", "proc_table",
     '(entry / "stat").read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "同上"),
    ("lib_mux.py", "reap_stale_pane_records", "os.listdir(directory)"):
        (SAFE, "FileNotFoundError は [] (無いのが普通)。他の OSError は "
               "warning を出して [] —— 記録を 1 件も落とさない (docstring: "
               "\"Every doubt keeps the record\")"),
    ("lib_mux.py", "_resolve_for_ownership", "os.readlink(current)"):
        (SAFE, "呼び出しは try/except で包まれ、読めなければ UNDECIDED を返す "
               "設計 (docstring: \"there is no fact of the matter ... "
               "UNDECIDED rather than a guess\")。UNDECIDED は kill を "
               "authorise する MINE 判定にはならない"),

    # -- worktree_gc.py: worktree の隔離・復元 -----------------------------
    ("worktree_gc.py", "cmd_restore", "os.path.exists(orig)"):
        (KNOWN_FAIL_OPEN,
         "LOW/MEDIUM: os.path.exists() は EACCES を含む全 OSError を False に "
         "潰す (pathlib.Path.exists() と異なり raise しない)。orig が実在する "
         "のに権限で観測できないと、この『既に何かある』ガードを素通りして "
         "git worktree move が orig の中へ移動してしまう (既存ディレクトリへの "
         "move は『中へ移す』動作になる、と同関数の docstring)。操作は人間が "
         "CLI から明示的に叩く復元コマンドなので影響範囲は限定的。この task "
         "では直さない。Director backlog へ。"),
    ("worktree_gc.py", "apply_quarantine", "os.path.exists(dest)"):
        (KNOWN_FAIL_OPEN,
         "LOW: 同じ os.path.exists() の EACCES 吸収。dest はこの関数が "
         "マイクロ秒精度のタイムスタンプで新規生成する隔離先パスなので、"
         "他プロセスが同じ dest を既に権限制限付きで作っている確率は極めて "
         "低いが、理論上は同じ型。git worktree move 失敗時は『削除にフォール "
         "バックせず failed にする』(族A) ため、最悪でも隔離は失敗として "
         "報告されるだけでデータ消失はしない。この task では直さない。"
         "Director backlog へ。"),
    ("worktree_gc.py", "load_target_dirs", "os.listdir(workers)"):
        (SAFE, "ENOENT だけ [] (docstring: registry/workers が無いのは普通)。"
               "他の OSError は理由文字列付きで返し、呼び出し側は全 Worker を "
               "keep にする (docstring: \"呼び出し側は全部 keep にする\")"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'stat').read_text()"):
        (SAFE, "FileNotFoundError だけ '消えた' を返す。他の (OSError, "
               "ValueError) は None (=harmless と言えない=デフォルトで keep "
               "側)。この関数自体が『keep するかどうか』の除外判定で、既定の "
               "戻り値 None が keep 側 (呼び出し元 scan_process_cwds の "
               "docstring: 読めなければ 'the cwd が worktree の中かもしれない "
               "ので取れなかった扱い')"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'cmdline').read_bytes()"):
        (SAFE, "同上 (同じ try/except ブロック内)"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'comm').read_text()"):
        (SAFE, "同上"),
    ("worktree_gc.py", "scan_process_cwds", "os.readlink(proc / pid / 'cwd')"):
        (SAFE, "ENOENT/ESRCH は継続 (消えた)。それ以外は "
               "_cwd_unreadable_but_harmless() で harmless と言えたものだけを "
               "除外し、それ以外は found に『取れなかった』側で残す "
               "(docstring: \"取れなかった扱い\")"),
    ("worktree_gc.py", "scan_process_cwds", "os.listdir(proc)"):
        (SAFE, "OSError はエラー文字列付きで [] を返し呼び出し側 (verdict "
               "判定) は keep 側に倒す (\"読めない・...は keep\"、"
               "scripts/CLAUDE.md worktree_gc.py 節)"),
    ("worktree_gc.py", "scan_process_cwds", "os.stat(proc / pid)"):
        (SAFE, "st_uid 判定用。OSError (ENOENT/ESRCH) は消滅として continue、"
               "それ以外は harmless 判定を経て『取れなかった』側に残る "
               "(_cwd_unreadable_but_harmless と同じ判断)"),
    ("worktree_gc.py", "scan_process_cwds",
     '(proc / pid / \'comm\').read_text(errors="replace")'):
        (SAFE, "cwd が読めず harmless でもないと既に確定した後の、エラー文言 "
               "用の表示専用の読み取り。失敗しても except OSError: comm = '?' "
               "で握り潰し、直後の return [], f'... を読めない ...' は不変 —— "
               "この読み取りの成否は『取れなかった扱い』という結論に影響しない"),

    # -- watchdog.py: Worker の hard_idle/max による terminate --------------
    ("watchdog.py", "load_active_tasks", "state_file.exists()"):
        (SAFE, "docstring: 読めなければ『監視対象なし』=『誰も kill しない』が "
               "この判定の安全な向き (empty-vs-unobservable.md 表 F と同じ)"),
    ("watchdog.py", "_leave_legacy_log_pointer", "legacy.exists()"):
        (SAFE, "旧 logs/ の移行コード。失敗しても watchdog の判定には入らない "
               "(ALLOWED_DIRECT_READS の同エントリと同じ理由)"),
    ("watchdog.py", "_leave_legacy_log_pointer",
     "legacy.read_text(errors=\"replace\")"):
        (SAFE, "同上"),
    ("watchdog.py", "_mtimes_since_floor", "p.stat()"):
        (SAFE, "t017 で修正済み。FileNotFoundError だけ continue、他は "
               "unobservable=True を立てて呼び出し側 (terminate 判定) が "
               "抑制する (tests/test_stat_failure_is_not_silence.py が固定)"),
    ("watchdog.py", "_notification_files", "notif_dir.iterdir()"):
        (SAFE, "t016 で修正済み。FileNotFoundError だけ [] (無いのが普通)、"
               "他は None で『観測できなかった』を呼び出し側へ渡す"),
    ("watchdog.py", "_newest_notification", "f.stat()"):
        (SAFE, "t017 で修正済み。FileNotFoundError だけ continue、他は "
               "(now, \"(unobservable)\") を返し _awaiting_human() の抑制を "
               "外さない"),

    # -- tests/leaked_descendants.py: pytest 自身の kill 権限 ---------------
    ("leaked_descendants.py", "available", '(_PROC / "self" / "stat").exists()'):
        (SAFE, "ガード全体が動くかどうかの可用性チェック。False なら "
               "install() が warning を出して guard 自体を無効化する ("
               "黙って全部通す側ではなく明示的な警告)"),
    ("leaked_descendants.py", "_read_stat",
     '(_PROC / str(pid) / "stat").read_bytes()'):
        (SAFE, "OSError で None。呼び出し元 _scan_one() は stat is None を "
               "『居るのに読めない』として (_PROC/str(pid)).exists() で "
               "再確認し unobservable 側へ倒す (kill 許可にはしない)"),
    ("leaked_descendants.py", "_read_bytes", "path.read_bytes()"):
        (SAFE, "OSError で None。_belongs() は None を observed=False として "
               "扱い、3 signal のうち 1 つでも読めなければ観測失敗に倒す"),
    ("leaked_descendants.py", "pids", "os.listdir(_PROC)"):
        (SAFE, "OSError で空集合。呼び出し元 snapshot()/scan() は "
               "差分ベースなので、列挙自体が失敗すると新規プロセスを 1 つも "
               "検出できず kill 対象が増えない方向 (fail-closed)"),
    ("leaked_descendants.py", "_belongs",
     'os.readlink(os.fsencode(base / "cwd"))'):
        (SAFE, "OSError で observed=False。3 signal (environ/cmdline/cwd) の "
               "うち 1 つでも読めなければ観測失敗として survivor 判定を "
               "保留する設計 (docstring)"),
    ("leaked_descendants.py", "_scan_one", '(_PROC / str(pid)).exists()'):
        (SAFE, "stat が None のときの再確認。exists() の結果をそのまま "
               "unobservable フラグとして返すだけで、kill 許可には使わない"),
    ("leaked_descendants.py", "_scan_one", '(_PROC / str(pid)).stat()'):
        (SAFE, "uid 判定用。OSError は『走査中に死んだだけ』として "
               "survivor=None, unobservable=False (=「居ない」であって "
               "kill 許可の根拠ではない)"),
    ("leaked_descendants.py", "_uptime", '(_PROC / "uptime").read_text()'):
        (SAFE, "boot 経過時間の補助値。読めなければ 0.0 —— age 計算がやや "
               "不正確になるだけで kill 許可/survivor 判定そのものには使わない"),

    # -- tests/kill_budget.py: 判定と独立した第二の関門 ---------------------
    ("kill_budget.py", "_ppid_and_start",
     '(_PROC / str(pid) / "stat").read_bytes()'):
        (SAFE, "OSError で None。partition() は None を『年齢を検証できない』"
               "として refused に回す (fail-open にしない、と docstring が "
               "明言)"),
}


@pytest.mark.parametrize(
    "site", _all_sites(),
    ids=lambda s: f"{s[0]}:{s[1]}:{s[2][:40]}")
def test_every_risky_call_is_classified(site):
    """観測の呼び出しは、安全だろうと危険だろうと `OBSERVATION_SITES` に載って
    いること。

    RED の作り方 —— `tests/red_proof_t053.sh` を参照。新しい観測呼び出しを
    足すと (安全に見えても) このテストがその 1 件を報告して落ちる。
    """
    assert site in OBSERVATION_SITES, (
        f"{site[0]}:{site[1]}() に、分類されていない観測呼び出しがある:\n"
        f"  {site[2]}\n\n"
        f"  stat/proc/exists/iterdir の失敗を『許可 / 不在』に潰していないか "
        f"確認し、OBSERVATION_SITES に理由付きで 1 行足すこと。\n"
        f"  安全と確認できたなら status=SAFE、まだ直っていない同族の欠陥なら "
        f"status=KNOWN_FAIL_OPEN (危険度を reason に書き、Result で "
        f"Director backlog へ)。")


def test_the_allowlist_has_no_dead_entries():
    """`OBSERVATION_SITES` に、もう存在しない呼び出しの行が残っていないこと。

    死んだ行が残ると、その関数に別の観測呼び出しが戻ってきたときに **黙って
    許可** される —— `test_queue_reads_go_through_the_guard.py` の同名テストと
    同じ理由。
    """
    live = set(_all_sites())
    dead = sorted(k for k in OBSERVATION_SITES if k not in live)
    assert not dead, (
        "OBSERVATION_SITES に、もう存在しない観測呼び出しの行が残っている:\n"
        + "\n".join(f"  {s}:{f}(): {seg}" for s, f, seg in dead)
        + "\n  直したなら、その行は消すこと。")


def test_audited_call_count_has_a_floor():
    """検査した箇所の件数が 0 件や極端な減少で「空虚に PASS」しないこと。

    `AUTHORITY_MODULES` からの呼び出し形が変わって検出漏れが起きた場合、
    件数が黙って 0 に近づいて全テストが空虚に緑になる (memory:
    registry-dir-single-definition-and-vacuous-static-guards /
    coverage-table-cannot-find-what-it-omits)。2026-09-28 時点の実測は 52 件
    (8 ファイル)。10 件を割ったら検出器そのものが壊れている疑いが強い。
    """
    sites = _all_sites()
    assert len(sites) >= 10, (
        f"AUTHORITY_MODULES から検出された観測呼び出しが {len(sites)} 件しか "
        f"ない (期待は 10 件以上)。検出器 (_risky_calls / RISKY_ATTRS) が "
        f"対象モジュールの呼び出し形を見失っていないか確認すること。")


def test_terminal_summary(capsys):
    """検査した件数と内訳を出す (0 件で PASS にしないための可視化)。"""
    sites = _all_sites()
    safe = sum(1 for s in sites if OBSERVATION_SITES.get(s, (None,))[0] == SAFE)
    fail_open = sum(1 for s in sites
                    if OBSERVATION_SITES.get(s, (None,))[0] == KNOWN_FAIL_OPEN)
    print(f"[observation-authority] 検査した観測呼び出し: {len(sites)} 件 "
          f"(SAFE={safe} / KNOWN_FAIL_OPEN={fail_open}) / "
          f"対象モジュール: {len(AUTHORITY_MODULES)} 件")
    assert safe + fail_open == len(sites)
