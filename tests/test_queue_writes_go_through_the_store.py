#!/usr/bin/env python3
"""lib (`lib_state_store`) を通らない queue / registry への書き込みが**増えたら CI が赤**になること (S5 / t020)。

設計: `knowledge/state-store.md` §6。読み取り側の先例 `tests/test_queue_reads_go_through_the_guard.py` と同じ
向き —— **allowlist**。表 (`ALLOWED_WRITES`) に載っていない書き込みが 1 つでもあれば落ちる (denylist は
新しい書き方で必ず穴が開く)。読み取りの表とは別ファイル。検出器は `tests/queue_write_scan.py`。

## 表の作り

`(ファイル名, 関数名) → (件数, 理由)`。件数は**その関数で検出した書き込みの数**で、増えても減っても落ちる:

* 増えた = 新しい書き込みが足された。lib を通せ、通せない理由があるなら理由つきで表に足す
* 減った / 該当が無い = 表が古い (直したのに残った)。表から外す
* 理由の欄に「未調査」は書けない (`test_every_row_has_a_real_reason`)

bash の書き込みは関数を持たないので `<bash>`、モジュール直下の python は `<module>`。

## 空虚な PASS を防ぐ

1. 検査した書き込みの**件数を出し**、下限を assert する (`test_the_scan_is_not_vacuous`)
2. 死んだ行を落とす (`test_no_allowlist_row_is_dead`)
3. 陽性対照: **本物のコードから切り出した形**を検出器が拾う (`test_positive_controls_*`)。S5 で直した
   旧コード (pre-compact の `open(task_file, 'w')`・verifier-dispatcher の `open(tmp,'w')`+`os.replace`・
   `printf > $REGISTRY_YAML`・taskvia map の `open(map_path, 'w')` ...) を含む
4. worktree で走らせても対象が 0 件にならない (`test_scan_targets_exist_in_a_worktree`。除外をパス文字列で
   判定しない —— memory: registry-dir-single-definition-and-vacuous-static-guards)

## 閉じないもの

`tests/queue_write_scan.py` の docstring を参照 (`exec` / `getattr(os, name)` / `subprocess` で起こした別プログラム)。
これは「うっかり足す」を止める補助で、敵対的な迂回を防ぐ境界ではない。

    python3 -m pytest tests/test_queue_writes_go_through_the_store.py -v
"""

from __future__ import annotations

import collections
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import queue_write_scan as scan  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 書き込みの唯一の入口。ここの中の書き込みが「lib を通る」の実体なので、検査の対象から外す
#: (下限だけ `test_the_scan_is_not_vacuous` で見る)。
STORE_LIB = 'lib_state_store.py'


def scan_targets() -> list[pathlib.Path]:
    """検査するファイル。**ディレクトリの glob** で決める (列挙した一覧は、足されたファイルを見落とす)。
    テスト (`test_*` / `tests/`) は対象外 —— 本番の queue / registry を書くコードではない。"""
    found: list[pathlib.Path] = []
    for pattern in ('scripts/*.py', 'scripts/*.sh', 'scripts/bin/*', 'scripts/shared/*.sh',
                    'hooks/*.py', 'hooks/*.sh', 'crewvia', 'crewvia-stop'):
        found.extend(REPO_ROOT.glob(pattern))
    return sorted(p for p in found if p.is_file() and not p.name.startswith('test_'))


def collect() -> list[scan.Site]:
    sites: list[scan.Site] = []
    for path in scan_targets():
        sites.extend(scan.scan_file(path))
    return sites


# ---------------------------------------------------------------------------
# 理由 (同じ理由を何行も書かないための名前。**どれも中身のある理由**)
# ---------------------------------------------------------------------------

R_LOCK = ("flock 用のロックファイル (`open('a+')` / `O_CREAT`) とその dir。中身を持たず、正本ではない。"
          "ロックの順序は queue/.lock が最も外側 (state-store.md §3.2)")
R_LOG = ("追記だけのログ / 観察記録。正本ではなく、壊れても判定に入らない (§5.1・§5.3 の「寄せない」)。"
         "queue/.lock に入れると観察の書き込みで正本のロックが混む")
R_MARK = ("再生成できる印 (heartbeat / activity / notification / grace marker / all-done 印)。毎回書き直されるか、"
          "無い = 通常状態。壊れても次の周期で直る (§5.3。lock を足すと全 tool 呼び出しが queue を待つ)")
R_DAEMON_JSON = ("registry/daemons/ のデーモン側 JSON 状態ストア。不変条件 7: 消してよい台帳・状態で、消えても"
                 "再生成される (無い = 再通知 / 再判定)。書き方は tmp+replace か、単純な上書き (§5.3)")
R_TMP = "queue でも registry でもない一時ファイル (/tmp や mktemp)。落ちる先はその 1 回の実行だけ"
R_DIR = ("空の dir の作成だけ (state を持たない)。中身を書く側が durable な書き込みを担う。"
         "queue の dir 作成は plan.sh の `_durable_makedirs` → lib の `ensure_dir` を通す (§4.2)")

#: (ファイル名, 関数名) → (件数, 理由)
ALLOWED_WRITES: dict[tuple[str, str], tuple[int, str]] = {
    # ---- registry/daemons ------------------------------------------------------------------------
    ('lib_daemon_state.py', 'write_told_atomic'): (4, R_DAEMON_JSON + "。「伝えた」台帳 (tmp+replace)。t010"),
    ('lib_daemon_state.py', 'told_lock'): (2, R_LOCK + "。台帳の書き手 2 人 (dispatcher / watchdog) を直列化する"),
    ('lib_daemon_watch.py', '__post_init__'): (1, R_DIR + " (registry/daemons)"),
    ('lib_daemon_watch.py', '_remove_marker'): (1, R_DAEMON_JSON + "。再起動マーカーの撤去 (無い = 何も起きない)"),
    ('lib_daemon_watch.py', 'daemon_lock'): (2, R_LOCK + "。デーモンの単一起動ロック"),
    ('lib_daemon_watch.sh', '<bash>'): (4, R_MARK + "。デーモンの heartbeat (tmp → mv。書き手 1 者で、"
                                          "壊れても次の周期で直る)"),
    ('lib_review_refusal.py', 'record'): (4, R_DAEMON_JSON + "。codex-review の拒否記録 (tmp+replace)"),
    ('lib_review_refusal.py', 'clear'): (1, R_DAEMON_JSON + "。拒否記録の撤去 (消してよい = 復旧手順)"),
    ('dispatcher.sh', '_mark_drift_checked'): (3, R_DAEMON_JSON + "。main checkout ずれ確認の周期印 (tmp+replace)"),
    ('dispatcher.sh', 'save_told'): (3, R_DAEMON_JSON + "。「伝えた」台帳 (tmp+replace)"),
    ('dispatcher.sh', '_save_state_entry'): (2, R_DAEMON_JSON + "。Worker 状態の遷移記録"),
    ('dispatcher.sh', '_save_job_since'): (3, R_DAEMON_JSON + "。Rule 5 の job 上限タイマー"),
    ('dispatcher.sh', '_save_usage_limit'): (3, R_DAEMON_JSON + "。利用枠切れの記録"),
    ('dispatcher.sh', '_spawn_time_fallback'): (2, R_DAEMON_JSON + "。spawn 時刻のフォールバック記録"),
    ('dispatcher.sh', 'record_notify'): (1, R_DAEMON_JSON + "。通知スロットルの cache (/tmp)"),
    ('dispatcher.sh', 'forget_notify'): (1, R_DAEMON_JSON + "。通知スロットルの cache (/tmp)"),
    ('dispatcher.sh', 'check_rule5'): (1, R_DAEMON_JSON + "。通知スロットルの cache (/tmp)"),
    ('dispatcher.sh', 'set_all_done_state'): (2, R_MARK + "。all-done の印 (touch / unlink)"),
    ('dispatcher.sh', 'sweep_spawn_grace_markers'): (1, R_MARK + "。spawn 猶予マーカーの掃除"),
    ('dispatcher.sh', 'tmux_kill_window'): (1, R_MARK + "。first-seen 印の撤去"),
    ('dispatcher.sh', 'spawn_kai_review'): (2, R_LOG + "。codex-review の spawn ログ"),
    ('dispatcher.sh', 'log'): (1, R_LOG),
    ('dispatcher.sh', '<module>'): (1, R_DIR + " (ログ dir)"),
    ('dispatcher.sh', '<bash>'): (4, R_LOG + "。dispatcher.log への追記と registry/logs dir の作成"),
    ('verifier-dispatcher.sh', 'record_notify'): (1, R_DAEMON_JSON + "。通知スロットルの cache (/tmp)"),
    ('verifier-dispatcher.sh', 'log'): (1, R_LOG),
    ('verifier-dispatcher.sh', '<bash>'): (2, R_LOG + "。verifier-dispatcher.log への追記と registry dir の作成"),
    ('watchdog.py', 'run'): (1, R_DIR + " (registry)"),
    ('watchdog.py', '_log'): (2, R_LOG),
    ('watchdog.py', '_log_observation'): (1, R_LOG + "。watchdog-observations.jsonl"),
    ('watchdog.py', '_leave_legacy_log_pointer'): (1, R_LOG + "。旧 logs/ に 1 行ポインタを残すだけの移行コード"),
    # ---- registry/mux ・ retirements ・ workers ------------------------------------------------------
    ('lib_mux.py', '_pane_record_lock'): (2, R_LOCK + "。registry/mux/.records.lock (mux 抽象の spawn 記録の専用ロック)"),
    ('lib_mux.py', 'write_pane_record'): (2, "registry/mux/<name>.json (spawn 記録)。mux 抽象が sweep の規則ごと唯一の定義を持つ。"
                                             "専用ロックの中で書く (§5.3 の「寄せない」)"),
    ('lib_mux.py', 'drop_pane_record'): (1, "registry/mux/<name>.json の撤去。`expect=` で判定した記録だけを、同じロックの区間で消す"
                                            "(scripts/CLAUDE.md「mux」)"),
    ('lib_registry.py', 'with_lock'): (2, R_LOCK + "。registry/.workers.lock (workers.yaml の read-modify-write を直列化)。"
                                             "workers.yaml **本体**の書き込みは S5 で `atomic_write_text` に寄った"),
    ('lib_retirement.py', 'queue_transaction'): (2, R_LOCK + "。queue/.lock を nonblocking で取る (lib_state_store の"
                                                              " transaction と同じファイル。§3.2)"),
    ('lib_retirement.py', 'write_json_atomic'): (4, "registry/retirements/ の marker (tmp+replace)。退役は lib_retirement が"
                                                     "唯一の定義を持つプロトコル (R1: 意図を先に永続化)。書き方を変えると"
                                                     " knowledge/daemon-authority.md の論証をやり直す (§5.3)"),
    ('lib_retirement.py', 'write_json_exclusive'): (5, "registry/retirements/ の marker (O_EXCL / link で確保)。同上"),
    ('lib_retirement.py', 'unlink_quiet'): (1, "registry/retirements/ の marker の撤去。同上"),
    ('lib_worker_target.py', 'write_record'): (5, "registry/workers/<name>/ の TARGET_DIR 記録 (tmp+replace)。書き手 1 者 (§5.3)"),
    ('lib_worker_target.py', 'sweep_stale_records'): (2, "registry/workers/<name>/ の古い記録の掃除。書き手 1 者 (§5.3)"),
    ('run_verification_checks.py', 'run_checks'): (3, "registry/verification/<task>/<cycle>.json。サイクル番号で新しい名前を"
                                                      "作る追記専用の検証結果 (書き手 1 者・既存を書き換えない)"),
    # ---- plan.sh の残り (queue の正本ではない) ---------------------------------------------------------
    ('plan.sh', 'task_graph_pending_lock'): (2, R_LOCK + "。registry/task-graph/ の専用ロック"),
    ('plan.sh', 'acquire_task_graph_lock'): (2, R_LOCK + "。registry/task-graph/ の専用ロック"),
    ('plan.sh', '_mark_task_graph_pending'): (2, R_MARK + "。task-graph 再生成の要求印 (registry/task-graph)"),
    ('plan.sh', '_clear_task_graph_pending'): (1, R_MARK + "。task-graph 再生成の要求印の撤去"),
    ('plan.sh', '_append_knowledge_director'): (3, R_LOG + "。knowledge/director.md への追記 (queue の外)"),
    ('plan.sh', '_reserve_unique_path'): (1, "handoffs/ の名前確保 (O_EXCL)。既に安全な形 (§5.3 handoffs/*)"),
    ('plan.sh', '_set_aside_stale_handoff'): (1, "handoffs/ への退避 (O_EXCL で確保した名前への replace)。既に安全な形 (§5.3)"),
    ('plan.sh', 'cmd_done'): (1, "Worker の settings.json (target_dir/.claude/crewvia-worker-*.json) の撤去。queue の外"),
    ('plan.sh', 'cmd_review'): (1, "plan_review.verdict の撤去。書き手は review-plan.sh と対の 1 者で、run_id で鮮度を確かめる読み手"
                                   "がいる (§5.1 の「寄せない」)"),
    ('plan.sh', '<bash>'): (5, R_DIR + "。`dashboard` (読み取り専用の TUI) は検証を通った後にだけ queue の骨組みを作る"
                                 " (t006 QA)。他は TUI の一時ファイル (" + R_TMP + ")"),
    # ---- 入口のスクリプト・hooks ---------------------------------------------------------------------
    ('git-helpers.sh', '<bash>'): (1, R_DIR + "。git worktree の親 dir (`mkdir -p \"$(dirname \"$worktree_path\")\"`)。queue の外。"
                                      "コメント中の `<<X` が走査を打ち切っていたときは見えなかった (t035)"),
    ('kai-review.sh', '<bash>'): (6, R_MARK + "。reviewer の heartbeat と、review の一時ファイル / worktree (" + R_TMP + ")"),
    ('review-plan.sh', '<bash>'): (6, "plan_review.md / plan_review.verdict の tmp+mv と reviewer ログ。書き手 1 者で run_id で鮮度を"
                                     "確かめる読み手がいる (§5.1 の「寄せない」)"),
    ('start.sh', '<bash>'): (7, "Worker の settings / prompt の一時ファイルと refusals.log (registry/start-sh)。queue の状態を書かない。"
                                "起動時の 1 者"),
    ('start.sh', '<module>'): (4, "Worker の `.claude/settings` (target_dir 側) を書く。queue / registry の外"),
    ('benchmark-ctx.sh', '<bash>'): (11, "ベンチマーク専用 (CREWVIA_BENCH_MODE)。`assignments/<agent>.restarting` は中身のない存在だけの印"
                                          " (§5.1 の「寄せない」)"),
    ('benchmark-ctx.sh', '<module>'): (2, "ベンチマーク専用。Worker の settings を書く (queue の外)"),
    ('cleanup-target-dir.sh', '<bash>'): (2, "target_dir 側の Worker settings の撤去。queue の外"),
    ('log_to_obsidian.sh', '<bash>'): (1, R_DIR + " (~/obsidian の出力先。queue の外)"),
    ('log_to_obsidian.sh', '<module>'): (1, "~/obsidian への mission ログ。queue の外"),
    ('setup-new-env.sh', '<bash>'): (3, "セットアップ時の settings.json の書き換え (backup → sed -i → mv)。1 回きりの人手の作業で、queue の外"),
    ('sh_perm_manager.sh', '<bash>'): (18, "hook の権限設定 (settings) の backup / 復元。queue の外"),
    ('sh_perm_manager.sh', '<module>'): (2, "hook の権限設定のスナップショット。queue の外"),
    ('notification.sh', '<bash>'): (2, R_MARK + "。registry/notifications/"),
    ('post-tool-use.sh', '<bash>'): (5, R_MARK + "。heartbeat / activity / backstop の throttle 印"),
    ('pre-tool-use.sh', '<bash>'): (2, R_LOG + "。approvals.tsv"),
    ('pre-compact.sh', '<bash>'): (1, R_LOG + "。card が書けなかったときの pre-compact-fallback.log。card 本文は `plan.sh snapshot` (lib 経由) だけが書く"),
    ('lib_skill_perms.py', 'load_config'): (5, R_TMP + "。権限設定のキャッシュ (/tmp、tmp+rename)"),
    ('worktree_gc.py', 'apply_quarantine'): (1, "worktree の隔離先 dir の作成。queue の外 (削除はしない)"),
    ('worktree_gc.py', 'cmd_restore'): (1, "隔離した worktree の復元先 dir の作成。queue の外"),
}

#: 検査した書き込みの総数の下限 (S5 着手時に 187 件 = 全体 194 件 - lib 自身 7 件を実測)。検出器が壊れて
#: 数件しか拾わなくなっても PASS しないための床。多少の増減で赤にならないよう余裕を持たせてある。
MIN_SCANNED_WRITES = 150
#: python ヒアドキュメントを持つ `.sh` から拾ったブロック数の下限 (先例は 1 ブロック目だけを見ていた)
MIN_PYTHON_BLOCKS = 10


def _counts(sites):
    counter = collections.Counter()
    for site in sites:
        if site.file != STORE_LIB:
            counter[(site.file, site.function)] += 1
    return counter


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def test_no_unlisted_write_remains():
    """表に無い書き込み、または件数が変わった (関数) があれば落ちる。"""
    counts = _counts(collect())
    problems = []
    for key, n in sorted(counts.items()):
        if key not in ALLOWED_WRITES:
            problems.append(f"未登録の書き込み: {key} × {n}")
        elif ALLOWED_WRITES[key][0] != n:
            problems.append(f"件数が変わった: {key} 表={ALLOWED_WRITES[key][0]} 実際={n}")
    assert not problems, (
        "lib (lib_state_store) を通らない書き込みが増えた / 減った:\n  " + "\n  ".join(problems)
        + "\n  → queue / registry の状態を書くなら lib_state_store (atomic_write_text / transaction / "
          "locked_update_json / durable_rename) を通す。通せない理由があるなら、理由つきで ALLOWED_WRITES に足す"
    )


def test_no_allowlist_row_is_dead():
    """直したのに表に残った行 (該当する書き込みが 0 件) は落とす。"""
    counts = _counts(collect())
    dead = [key for key in ALLOWED_WRITES if counts.get(key, 0) == 0]
    assert not dead, f"該当する書き込みが無い行 (表から外すこと): {dead}"


def test_every_row_has_a_real_reason():
    weak = [key for key, (_n, reason) in ALLOWED_WRITES.items()
            if len(reason) < 20 or any(w in reason for w in ('未調査', 'TODO', '後で', 'あとで'))]
    assert not weak, f"理由が空・短い・未調査の行: {weak}"


def test_the_scan_is_not_vacuous():
    sites = collect()
    scanned = [s for s in sites if s.file != STORE_LIB]
    print(f"[write-guard] scanned files={len(scan_targets())} write sites={len(scanned)} "
          f"(+ {len([s for s in sites if s.file == STORE_LIB])} inside {STORE_LIB})")
    # 成功したテストの print は pytest が捨てる。CI ログに件数が残るよう conftest の summary に載せる (01a backlog 2)
    import guard_report
    guard_report.record('queue-writes', files=len(scan_targets()), write_sites=len(scanned),
                        inside_lib=len([s for s in sites if s.file == STORE_LIB]),
                        allowlist_rows=len(ALLOWED_WRITES))
    assert len(scanned) >= MIN_SCANNED_WRITES, (
        f"検査した書き込みが {len(scanned)} 件しかない (下限 {MIN_SCANNED_WRITES})。検出器が壊れている")
    inside = [s for s in sites if s.file == STORE_LIB]
    assert len(inside) >= 5, f"{STORE_LIB} 自身の書き込みが {len(inside)} 件 — lib が検査の対象から外れている"
    # 種類ごとに 1 件以上 (python の AST 側と bash の字句側の両方が生きている)
    kinds = {s.kind for s in scanned}
    for needed in ('open', 'Path.write_text', 'os.replace', 'redirect', 'command'):
        assert needed in kinds, f"{needed} を 1 件も拾っていない: 検出器のその経路が死んでいる"


def test_python_heredocs_are_all_scanned():
    """`.sh` に埋め込まれた python は 1 ブロック目だけでなく全部読む。"""
    total = 0
    for path in scan_targets():
        if path.suffix == '.sh':
            total += scan.heredoc_block_count(path.read_text(encoding='utf-8'))
    assert total >= MIN_PYTHON_BLOCKS, f"python ヒアドキュメントが {total} ブロックしか見つからない"
    # 実物で: 2 ブロック以上持つスクリプトがあり、その 2 ブロック目以降の書き込みも拾えている
    multi = [p for p in scan_targets()
             if p.suffix == '.sh' and scan.heredoc_block_count(p.read_text(encoding='utf-8')) >= 2]
    assert multi, "python ブロックを複数持つスクリプトが無い — この検査の前提が変わった (対照を作り直すこと)"


def test_scan_targets_exist_in_a_worktree():
    """worktree のパス (`.claude/worktrees/...`) で走らせても対象が 0 件にならない。"""
    targets = scan_targets()
    names = {p.name for p in targets}
    for must in ('plan.sh', 'dispatcher.sh', 'verifier-dispatcher.sh', 'taskvia-sync.sh', 'assign-name.sh',
                 'pre-compact.sh', 'post-tool-use.sh', 'lib_state_store.py', 'lib_registry.py', 'watchdog.py'):
        assert must in names, f"{must} が検査の対象に入っていない"
    assert len(targets) >= 40


def test_moved_writers_no_longer_appear():
    """S5 で lib に寄せた書き手は、表に載らず (= 検出 0 件) に済んでいる。載せて逃がしていない。"""
    counts = _counts(collect())
    moved = [
        ('taskvia-sync.sh', 'save_map'),
        ('assign-name.sh', '<bash>'),
        ('verifier-dispatcher.sh', 'update_task_fields'),
        ('verifier-dispatcher.sh', 'mark_verifying'),
        ('lib_registry.py', 'write'),
        ('plan.sh', '_taskvia_map_update'),
        ('plan.sh', '_taskvia_map_update_status'),
        ('plan.sh', 'cmd_pull'),
        ('plan.sh', 'cmd_archive'),
        ('plan.sh', 'cmd_init'),
        ('plan.sh', '_apply_risk_flags'),
        ('plan.sh', '_apply_risk_flag_upgrades'),
    ]
    still = [(k, counts[k]) for k in moved if counts.get(k)]
    assert not still, f"lib に寄せたはずの書き手がまだ書いている: {still}"
    assert ('pre-compact.sh', '<module>') not in counts, "pre-compact.sh の python 書き込みが戻っている"


# ---------------------------------------------------------------------------
# 陽性対照 —— 本物のコードから切り出した形を、検出器が拾うこと
# ---------------------------------------------------------------------------

#: (名前, python のソース) —— S5 より前の実コード、または現行コードの書き方そのまま
PYTHON_POSITIVE = [
    ('pre-compact: open(task_file, "w") でカードを in-place に上書き',
     "with open(task_file, 'w') as f:\n    f.write(updated)\n"),
    ('verifier-dispatcher: open(tmp,"w") + os.replace (fsync なし・ロックなし)',
     "tmp = str(task_path) + f'.tmp.{os.getpid()}'\nwith open(tmp, 'w') as f:\n    f.write(new_text)\nos.replace(tmp, str(task_path))\n"),
    ('taskvia map: open(map_path, "w") + json.dump',
     "with open(map_path, 'w') as f:\n    json.dump(task_map, f, indent=2, ensure_ascii=False)\n"),
    ('lib_registry.write: open(path, "w") + writelines',
     "with open(path, 'w') as f:\n    f.writelines(out)\n"),
    ('plan.sh pull: .crewvia-env を open(env_file, "w")',
     "with open(env_file, 'w') as _ef:\n    _ef.write('x')\n"),
    ('plan.sh archive: shutil.move(src, dst)', "shutil.move(src, dst)\n"),
    ('write_text (tmp への書き込み)', "tmp.write_text(json.dumps(told))\n"),
    ('with LOG_FILE.open("a") (Path.open)', "with LOG_FILE.open('a') as f:\n    f.write('x')\n"),
    ('mode= キーワード', "open(path, mode='w')\n"),
    ('モードが変数 (定数でなければ書き込みとみなす)', "open(path, mode)\n"),
    ('os.open の書き込み系フラグ', "fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"),
    ('os.fdopen(fd, "w")', "f = os.fdopen(fd, 'w')\n"),
    ('Path.unlink', "path.unlink(missing_ok=True)\n"),
    ('os.remove', "os.remove(worker_settings)\n"),
    ('Path.replace (引数 1 個)', "tmp.replace(target)\n"),
    ('tempfile.mkstemp', "fd, tmp = tempfile.mkstemp(dir=parent)\n"),
    ('Path.touch', "ALL_DONE_STATE_FILE.touch()\n"),
    ('os.makedirs', "os.makedirs(knowledge_dir, exist_ok=True)\n"),
]

#: 書き込みではない形 (誤検出しない)
PYTHON_NEGATIVE = [
    ('open(path) (読み)', "open(path).read()\n"),
    ('open(path, "r")', "open(path, 'r')\n"),
    ('open(path, mode="rb")', "open(path, mode='rb')\n"),
    ('os.open の読み専用フラグ (lib_task_cards の実形)', "fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)\n"),
    ('str.replace(old, new)', "text = text.replace('a', 'b')\n"),
    ('datetime.replace(tzinfo=..)', "dt = dt.replace(tzinfo=timezone.utc)\n"),
    ('Path.read_text', "x = p.read_text()\n"),
    ('json.load', "d = json.load(f)\n"),
]

BASH_POSITIVE = [
    ('assign-name.sh: printf > $REGISTRY_YAML', "printf 'workers: []\\n' > \"$REGISTRY_YAML\"\n"),
    ('heartbeat: date +%s > file', 'date +%s > "${HEARTBEAT_DIR}/${AGENT_NAME}" 2>/dev/null || true\n'),
    ('touch "$HEARTBEATS_DIR/$AGENT"', 'touch "$HEARTBEATS_DIR/$AGENT"\n'),
    ('>> ログへの追記', 'echo "$msg" >> "$LOG_FILE"\n'),
    ('mv tmp → 本体', 'mv "${VERDICT_FILE}.tmp" "$VERDICT_FILE"\n'),
    ('rm -f', 'rm -f "$OUTPUT_FILE"\n'),
    ('tee', 'codex exec 2>&1 | tee "$STDERR_FILE"\n'),
    ('cp', 'cp "$SETTINGS_JSON" "$SETTINGS_JSON.bak"\n'),
    ('sed -i', 'sed -i "s|a|b|g" "$SETTINGS_JSON"\n'),
    ('mkdir -p (&& の後)', '{ mkdir -p "$dir" && printf x >> "${dir}/refusals.log"; } 2>/dev/null\n'),
    ('cat > file <<EOF', 'cat > "$tmp" <<EOF\nbody\nEOF\n'),
]

BASH_NEGATIVE = [
    ('/dev/null への捨て', 'foo >/dev/null 2>&1\n'),
    ('fd の複製', 'foo 2>&1\nbar >&2\n'),
    ('引用符の中の >', 'echo "a > b"\necho \'x >> y\'\n'),
    ('コメントの中の >', '# echo hi > file\n'),
    ('[[ 比較 ]]', 'if [[ "$a" > "$b" ]]; then :; fi\n'),
    ('算術', 'if (( n > 3 )); then :; fi\n'),
    ('複数行の文字列の中の >', 'msg="line1\n> Worker: 読め\nline3"\n'),
    ('ヒアドキュメントの本文の >', 'cat <<EOF\nx > y\nEOF\n'),
    ('読み取りリダイレクト', 'while read -r l; do :; done < "$file"\n'),
]


@pytest.mark.parametrize('name,source', PYTHON_POSITIVE, ids=[n for n, _ in PYTHON_POSITIVE])
def test_positive_controls_python(name, source):
    assert scan.python_sites(source, 'probe.py'), f"検出できない書き込みの形: {name}"


@pytest.mark.parametrize('name,source', PYTHON_NEGATIVE, ids=[n for n, _ in PYTHON_NEGATIVE])
def test_negative_controls_python(name, source):
    found = scan.python_sites(source, 'probe.py')
    assert not found, f"書き込みではないものを拾った: {name}: {found}"


@pytest.mark.parametrize('name,source', BASH_POSITIVE, ids=[n for n, _ in BASH_POSITIVE])
def test_positive_controls_bash(name, source):
    assert scan.shell_sites(source, 'probe.sh'), f"検出できない書き込みの形: {name}"


@pytest.mark.parametrize('name,source', BASH_NEGATIVE, ids=[n for n, _ in BASH_NEGATIVE])
def test_negative_controls_bash(name, source):
    found = scan.shell_sites(source, 'probe.sh')
    assert not found, f"書き込みではないものを拾った: {name}: {found}"


def test_positive_control_second_python_block_in_a_shell_script():
    """先例は python ブロックの 1 つ目だけを見ていた。2 つ目にだけある書き込みも拾う。"""
    script = (
        "python3 - <<'PYEOF'\nprint('a')\nPYEOF\n"
        "python3 - \"$x\" <<'PYEOF'\nwith open(sys.argv[1], 'w') as f:\n    f.write('y')\nPYEOF\n"
    )
    sites = scan.shell_sites(script, 'probe.sh')
    assert [s.kind for s in sites] == ['open']


def test_positive_control_adding_one_write_to_a_real_file_turns_the_guard_red():
    """本物のファイルの本文に 1 行足した版を検出器に通すと、表と食い違う (= 赤になる)。
    実際の呼び出し形で確かめる: `str(self.plan_sh)` のような実形で緑のまま、をリテラルだけで済ませない。"""
    targets = {p.name: p for p in scan_targets()}
    baseline = _counts(collect())
    for name, extra in (
        ('pre-compact.sh', 'echo "x" > "$CREWVIA_ROOT/queue/missions/m/tasks/t001.md"\n'),
        ('taskvia-sync.sh', "python3 - <<'PYEOF'\nopen('queue/.taskvia-map.json', 'w').write('{}')\nPYEOF\n"),
        ('assign-name.sh', 'printf "workers: []\\n" > "$REGISTRY_YAML"\n'),
        ('verifier-dispatcher.sh', "python3 - <<'PYEOF'\nos.replace(tmp, str(task_path))\nPYEOF\n"),
    ):
        text = targets[name].read_text(encoding='utf-8') + '\n' + extra
        mutated = collections.Counter(baseline)
        # 元のファイルぶんを引いて、足した版のぶんを入れる
        for key in [k for k in mutated if k[0] == name]:
            del mutated[key]
        for site in scan.shell_sites(text, name):
            mutated[(site.file, site.function)] += 1
        differing = [k for k in set(mutated) | set(ALLOWED_WRITES)
                     if k[0] == name and mutated.get(k, 0) != ALLOWED_WRITES.get(k, (0, ''))[0]]
        assert differing, f"{name} に書き込みを 1 行足しても、表と食い違わない (ガードが緑のまま)"


# ---------------------------------------------------------------------------
# 「開始を誤認して残りを読み飛ばす」型 (t035) —— コメント・引用符の中の `<<X`
# ---------------------------------------------------------------------------

#: 書き込み 1 行を足しても検出されるべき前置き。どれも旧検出器は `<<X` を heredoc の開始と誤認し、
#: 終端語が現れないので以降を全部読み飛ばした。
HEREDOC_LOOKALIKES = [
    ('コメント中の <<X', "# see: cat <<EOF in the docs\n"),
    ('コメント中の <<-X', "foo  # <<-DONE\n"),
    ('二重引用符の中の <<X', "echo \"x <<'PYEOF'\"\n"),
    ('単引用符の中の <<X', "echo 'x <<EOF'\n"),
    ('複数行の文字列の中の <<X', "msg=\"line1\nusage: cmd <<EOF\nline3\"\n"),
    ('$(...) の中の入れ子の引用符のあとの <<X', "x=\"$(printf '%s' \"$y\" | sed 's/a/b/')\"  # <<EOF\n"),
]
TAIL_WRITE = 'echo x > "$CREWVIA_QUEUE/state.yaml"\n'


@pytest.mark.parametrize('name,prefix', HEREDOC_LOOKALIKES, ids=[n for n, _ in HEREDOC_LOOKALIKES])
def test_negative_control_heredoc_lookalike_does_not_blind_the_rest(name, prefix):
    """コメント・引用符の中の `<<X` の**次の行**の書き込みを拾う (旧検出器は 0 件だった)。"""
    sites = scan.shell_sites(prefix + TAIL_WRITE, 'probe.sh')
    assert [s.kind for s in sites] == ['redirect'], f"{name}: 後ろの書き込みを見落とした: {sites}"
    assert scan.unclosed_heredocs(prefix + TAIL_WRITE) == [], name


def test_lookalike_does_not_hide_a_later_python_block_either():
    script = ("# usage: cmd <<EOF\n"
              "python3 - <<'PYEOF'\nwith open(p, 'w') as f:\n    f.write('x')\nPYEOF\n")
    assert [s.kind for s in scan.shell_sites(script, 'probe.sh')] == ['open']


REAL_HEREDOCS = [
    ('"$(cat <<EOF ... EOF)" (コマンド置換の中は本物)',
     'msg="$(cat <<EOF\nbody > not-a-write\nEOF\n)"\n', 0),   # 開始を拾えなければ本文の `>` が 1 件に見える
    ("\"$(python3 - <<'PYEOF' ... PYEOF)\" の中の python の書き込みを拾う",
     "x=\"$(python3 - <<'PYEOF'\nopen('f', 'w').write('x')\nPYEOF\n)\"\n", 1),
    ('通常の cat <<EOF は本文を飛ばして次の行へ', 'cat <<EOF\nx > y\nEOF\n', 0),
]


@pytest.mark.parametrize('name,source,writes', REAL_HEREDOCS, ids=[n for n, _, _ in REAL_HEREDOCS])
def test_real_heredocs_are_still_recognised(name, source, writes):
    assert len(scan.shell_sites(source, 'probe.sh')) == writes, name
    assert scan.unclosed_heredocs(source) == [], name


def test_no_target_has_an_unclosed_heredoc_or_quote():
    """**ファイルの何を実際に検査したか**。閉じない heredoc / 閉じない引用符が 1 件でもあれば、その行から末尾までが
    未検査 (開始の誤認は必ずここに出る)。全対象ファイルで 0 件。"""
    total = skipped = 0
    bad = []
    for path in scan_targets():
        if path.suffix == '.py':
            continue                                     # python は AST (構文が壊れていれば ast.parse が落ちる)
        cov = scan.coverage(path.read_text(encoding='utf-8'))
        total += cov['total']
        skipped += cov['heredoc_body']
        if cov['unclosed'] or cov['open_quote_at_eof']:
            bad.append((path.name, cov['unclosed'], cov['open_quote_at_eof']))
    print(f"[write-guard] shell lines={total} heredoc-body lines skipped={skipped} "
          f"inspected as bash={total - skipped} ({100 * (total - skipped) // max(total, 1)}%)")
    assert total >= 5000, f"検査した行が {total} 行しかない — 対象が消えている"
    assert not bad, f"閉じない heredoc / 引用符 (= 以降が未検査): {bad}"


def test_positive_control_a_write_appended_to_the_previously_blind_files_is_detected():
    """旧検出器が 434 行 / 104 行を読み飛ばしていた実ファイル (`hooks/pre-tool-use.sh` /
    `scripts/git-helpers.sh`) の末尾に書き込みを足すと、表と食い違う (= 赤になる)。実物は変えない。"""
    targets = {p.name: p for p in scan_targets()}
    baseline = _counts(collect())
    for name in ('pre-tool-use.sh', 'git-helpers.sh'):
        text = targets[name].read_text(encoding='utf-8') + '\n' + TAIL_WRITE
        n_before = sum(v for k, v in baseline.items() if k[0] == name)
        n_after = len(scan.shell_sites(text, name))
        assert n_after == n_before + 1, f"{name}: 末尾に足した書き込みが検出されない ({n_before} → {n_after})"
