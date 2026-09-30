# tests/ の作業メモ（このディレクトリで作業するときだけ読み込まれる）

root の `CLAUDE.md` から移した、テスト専用の規則。設計と経緯は `knowledge/test-isolation.md` /
`knowledge/daemon-authority.md` §7-11 / §7-15 / `knowledge/empty-vs-unobservable.md`。

## テスト専用の環境変数（**本番で設定しない**）

| 変数名 | 内容 |
|---|---|
| `CREWVIA_HERDR_SOCK` | lib_mux が ping する herdr API socket のパスを上書きする（デフォルト: `~/.config/herdr/herdr.sock`）。実 herdr はこの変数を読まないため、本番で設定すると ping 先と server の bind 先が食い違う |
| `CREWVIA_MUX_TEST_ISOLATION` | テスト中であることの印。`tests/conftest.py` が `os.environ` に置くので subprocess にも継承される。これが立っている間、既定の宛先 (`crewvia`) や接頭辞なしのペイン名を名指しする mux verb は `MuxTestIsolationError` で拒否される (2026-09-23 の本番 dispatcher 乗っ取り事故の再発防止。`knowledge/daemon-authority.md` §7-11)。**セッションが作った宛先 (`crewvia-pytest-<pid>-<hex>`) は pytest の終了時に片付けられる**（`tests/pytest_workspace_sweep.py`。以前は herdr に空の workspace が 1 回ごとに溜まっていた） |
| `CREWVIA_PYTEST_WORKSPACE_SWEEP` | pytest 終了時の**残骸掃除**の停止スイッチ。`0` のときだけ、死んだ pid の `crewvia-pytest-<pid>-<hex>` を何も見ず何も消さない（既定は有効）。**止まるのは残骸掃除だけ**で、セッション自身の宛先の後始末は止まらない（自分の label だけを消すので危険が無く、止めると元の漏れに戻る）。消すのは「形が合う・pid が `ESRCH`・live な pane が無い」の AND を満たす宛先だけ。pytest を起動するシェルの env に足すだけで効く（デーモンの再起動は不要）。`knowledge/daemon-authority.md` §7-15 |
| `CREWVIA_MUX_PANE_PREFIX` | ペイン名の名前空間。**本番は空 (no-op)**。設定すると `spawn("dispatcher")` が `<prefix>dispatcher` に解決され、本番のペイン名そのものが到達不能になる |

- 本番宛先名（`Sora-director` 等）の拒否は `CREWVIA_MUX_TEST_ISOLATION=1` のときだけ働く
  （`_guard_test_isolation`）。環境変数なしなら本番の Director に実際にキーが届くので、mux を試すときは
  必ず自分が起動した Worker の pane に向けること。

## fixture: plan.sh / lib を一時ディレクトリへ写すとき

- 入口は `tests/fixture_tree.py`（`copy_plan_tree(root)`）と `tests/fixture_tree.sh`
  （`copy_plan_tree` / `copy_scripts_libs`）だけ。`lib_*` を glob で写すので、新しい lib は何もしなくても付いてくる。
  自分で `cp` / `shutil.copy` を書かない（`tests/test_fixture_tree_is_the_only_copier.py` が赤にする）。
- lib を上書きするスタブは helper の**後**に置く。`git-helpers.sh` / `review-plan.sh` は意図して写さない。
- registry は queue の隣に置く（`registry_dir()` 1 箇所）。実 registry を書くテストは作らない
  （`knowledge/test-isolation.md`）。

## start.sh を mux モードで走らせるテスト

- start.sh は claude を起動する前に `~/.claude.json` で cwd の trust を検査する（t021）。fake tmux で走らせるテストは
  `tests/trust_fixture.sh`（`trust_fixture_setup` / `trust_fixture_teardown`）で信頼を宣言する。開発機は
  `~/.claude.json` が crewvia を信頼しているので通ってしまい、**CI でだけ落ちる**。`CLAUDE_CONFIG_DIR` を
  使い捨ての dir に向ける方式で、本物の `~/.claude.json` は読まない・書かない（HOME は変えない）。

## 構造ガード（AST）

- queue / registry / config を開く**直接読み取り**は、`tests/test_queue_reads_go_through_the_guard.py`（`test_no_unguarded_read_remains`）が対象モジュールの
  `open()` / `read_text()` / `read_bytes()` を **AST で全部拾い**、理由付き allowlist に無ければ落とす。
  新しい直接読み取りは必ず赤になる。読みは `lib_task_cards` の入口（`read_task_card()` /
  `read_regular_text()` / `read_regular_text_or_unreadable()`）を通す。
- デーモン側 JSON 状態ストアは `test_daemon_state_reads_go_through_the_entry.py` が `json.load(s)` を AST で拾い、
  `lib_daemon_state.load_json_store` の外は allowlist に無ければ落とす。
- 構造ガードは**実際の呼び出し形**で確かめる（`str(self.plan_sh)` のような実形で緑のまま、をリテラル対照だけで
  済ませない）。allowlist の各行は理由を持ち、該当が無くなったら（直したのに残った）赤。
- 静的検査（`scripts/test_registry_lock.sh` 等）は worktree では除外判定が全件に当たり必ず PASS する。
  検査件数を出し、fixture の lib は helper 経由にする。
- **書き込み側**は `tests/test_queue_writes_go_through_the_store.py`（検出器 `tests/queue_write_scan.py`。S5 / t020）。
  lib（`lib_state_store`）を通らない書き込みが**増えても減っても**赤（表は `(ファイル, 関数) → (件数, 理由)`）。python は AST
  （`.sh` の python ヒアドキュメントは**全ブロック**）、bash は字句（変数は解決できないので書き先の語で絞らず全部拾う）。
  検査件数の下限・死んだ行・陽性/陰性対照（本物のコードから切り出した形）を持つ。読み取りの表とは混ぜない。
  検出器を触ったら `python3 -m pytest tests/test_queue_writes_go_through_the_store.py` の陽性対照が全部通ること。
  bash の字句解析は `_lex_line` の 1 か所（heredoc の開始と書き込みの検出が同じ引用符の理解を使う）。**コメント・引用符の中の
  `<<X` は開始ではない**（誤認すると終端語が現れず、その行から末尾までが未検査になる — 旧 S5 は `pre-tool-use.sh` 434 行 /
  `git-helpers.sh` 104 行を見ていなかった）。`test_no_target_has_an_unclosed_heredoc_or_quote` が全対象で「閉じない heredoc / 引用符 0 件」を
  assert する。赤の実証: `bash tests/red_proof_queue_write_scan_heredoc.sh`（約 5 秒）。

## State Store (`lib_state_store`) のテスト

- crash 注入は `tests/state_store_scenarios.py`（seed・場面・収束の検査）。lib の書き込みの各段で `FAULT_HOOK` が呼ばれ、
  fork した子が k 番目で自分に SIGKILL を送る（**子は SIGKILL か `os._exit` でしか終わらない** — pytest の後始末に戻らない）。
  点の数の**下限**を assert する（注入口が壊れて 0 点で PASS しない）。全点 × 20 回。
- 並行は**独立プロセス**（`tests/state_store_worker.py`）・実 flock。mock のロックで済ませない。
- red proof は `tests/red_proof_t008.sh`（lib の保証を 1 つずつ壊した複製）。**壊した複製は `scripts/` `tests/` だけを写し、
  本番の worktree・queue には触れない**。約 4 分。
- **plan.sh の cutover（S3 / t012）のテスト**: `tests/test_plan_sh_state_store_cutover.py`（監査ログ・親 dir fsync の順序・kill・
  `queue/.lock` の共存）。plan.sh の python 本体を `tests/task_graph_publisher_harness.load_plan_namespace()` で
  **本物のまま**名前空間に読み込み、本番の `with_lock` / `save_task` 等を呼ぶ。親 dir の fsync は kill では再現しない
  （電源断相当が要る）ので、`os.fsync` / `os.replace` / `os.unlink` を記録するスタブ（`Recorder`）で
  「tmp の fsync → replace → 親 dir の fsync」の**有無と順序**を見る（陽性対照: 旧 `_atomic_write` の形を通すと述語が満たされない）。
- **互換性テスト**: `tests/test_plan_sh_compat_s3.py` が `tests/plan_sh_compat_scenario.py`（固定 fixture の 39 段）を今の plan.sh で走らせ、
  golden（`tests/fixtures/plan_sh_compat_s3.golden.json`。**cutover 前 a1f6957 の plan.sh で作った**）と exit code / stdout / stderr /
  queue の全ファイルを比べる。外から見える挙動を**意図して**変えたら、`knowledge/state-store.md` §4.1 の表に足してから golden を作り直す
  （作り直し: `git archive <旧 sha> | tar -x -C <dir>` → `python3 tests/plan_sh_compat_scenario.py <dir> <golden>`）。
  監査ログ（`queue/audit/`）と `.lock` は比べない。直列化の golden は `tests/fixtures/state_store_serialization_golden.json`
  （JSON の `sort_keys` を使わない — meta の key の並びが出力に効く）。

- **S5（t020）の書き手のテスト**: `tests/test_s5_writers_lock_and_atomic.py`（verifier-dispatcher の `verifying`・pre-compact の
  `snapshot`・risk flags・`.crewvia-env`・taskvia map・workers.yaml・rename の耐久性・`parse_opts` の骨組み）。
  強制終了は `FAULT_HOOK` を fork した子の中で k 番目に SIGKILL（`_kill_child_at`）。並行は独立プロセス・実 flock。
  **旧コードの危険が本物だった対照**を各所に置く（旧 pre-compact は truncate した時点で落ちると空の card が残る等）。
  赤の実証は `tests/red_proof_s5_lib_writers.sh`（12 ケース。欠陥を戻した複製に同じテストを走らせる。約 4 分）。

- **S4（t016）の回復のテスト**: `tests/test_projection_recovery_on_lock.py`。plan.sh の python 本体を**本物のまま**名前空間に読み込み、
  fork した子で `FAULT_HOOK` の k 番目に SIGKILL → **本物の plan.sh（subprocess）** で次の呼び出しを打って収束を見る（pull / done / reset /
  needs-director / add / 退避の全点 × 次の操作。ランダム 20 回は seed 固定）。`problems()` が「card = 正本と projection の食い違い」の
  定義そのもの。seed の queue は 1 回だけ本物の plan.sh で作り、点ごとに `cp -a` する。並行は独立プロセス（回復は**1 件も修復してはいけない**
  = 進行中の正しい遷移を巻き戻さない）。持ち越し（worker なし・identity 欠け / 壊れ × 全 holding status）は lib の `diagnose()` を直接。
  赤の実証は `tests/red_proof_projection_recovery.sh`（13 ケース。欠陥を注入した複製に同じテストを走らせる。約 10 分）。
  **plan.sh の subcommand を足したら** `tests/test_registry_isolation.py` の `ISOLATED_INVOCATIONS` と usage 行・header（先頭 70 行に `--help` が要る）も揃える。

## 子プロセスを残さない（`tests/leaked_descendants.py` / `tests/proc_group.py`）

- plan.sh のような **bash の下で更に子を起こすもの** を `subprocess.run(timeout=)` / `Popen.kill()` で止めると、
  殺されるのは bash だけで、下の `python3 -` は孤児になる（FIFO のテストでは `wait_for_partner` で永久に待つ）。
  タイムアウトや後始末が要る実行は `proc_group.run_in_own_group()`（`Popen` なら `start_new_session=True` +
  `kill_group()`）で、**木ごと**殺す。
- 構造ガード: 各テストの後に、そのテストの間に増えて生きている **このセッションの子孫** があれば kill して、
  そのテストを ERROR にする（`conftest.py` が `install()`）。「このセッションの子孫」は環境の印
  `CREWVIA_PYTEST_SESSION` か cmdline / cwd が basetemp を指すこと。本番の plan.sh・デーモン・別セッションの
  pytest は数えない・殺さない。ガードが赤くなったら、ガードではなくそのテストの後片付けを直す。
- 設計・実測・戻し方: `knowledge/test-leaked-descendants.md`。
- **kill の関門は `tests/kill_budget.py`（判定とは別ファイル）**。自分・祖先・セッション/グループリーダー・
  **自分より古いプロセス**（テストの子孫が、テスト自身より先に生まれていることはない）は殺さない。許可が
  上限（既定 16、`CREWVIA_LEAK_KILL_BUDGET`）を超えたら **1 件も殺さない** — 「本当に N 個漏れた」ではなく
  判定が壊れている方を疑う。**このファイルは変異させない**（安全弁を壊す変異は意味を失わせる）。
- ガードの kill は `kill_all(survivors, kill=_default_kill)` の差し替え口を通す。ガード自身のテストと
  変異テストは **本物のシグナルを送らない**（`tests/test_leak_guard_self_preservation.py`）。
  `_default_kill` は `signal.pidfd_send_signal` で **観測時 (`scan()`) に束縛した pidfd 限定**に送る
  （pid 番号では送らない）— `scan()` が候補を見つけた瞬間に `os.pidfd_open` して starttime を
  再確認し、以降その pid 番号が再利用されても束縛した fd は元のプロセスにしか届かない
  （2 巡目 codex review finding 3 / memory: verify-and-destroy-must-share-one-connection）。
  pidfd を束縛できなかった survivor は pid 番号へフォールバックせず kill しない。
- **判定を壊す変異テストは PID 名前空間の中だけで走らせる**:
  `unshare -Urpf --mount-proc python3 -m pytest …`。名前空間の外の pid は `/proc` に見えず `os.kill` も
  ESRCH になるので、判定がどう壊れても外へ届かない。2026-09-27、`_belongs` の頭に `return "any", True` を
  注入した変異（G1）を素の環境で走らせ、`systemd --user` / tmux / WSL キープアライブ / n8n（uid 1000）を
  SIGKILL して WSL ごと落とした。

## red proof の作法

- 修正のテストは、**欠陥を戻して赤くなること**を実証する（`tests/red_proof_*.sh`）。期待値をテスト内に
  再実装したテストは欠陥の留め金にならない。
- 欠陥注入は `PYTHONDONTWRITEBYTECODE` + pyc の掃除つきで行う（古い pyc が緑を見せる）。
- 触りうる全 backend を隔離し、**本物に届いていない**ことを assert する。内側の `env -i` が
  `~/.local` の pytest を見失うので、偽 HOME ではなく本物の HOME + `CREWVIA_*` だけを unset する。
- 注入した欠陥が緑のままなら、打ち切り例外が backstop の `except` に飲まれている可能性を疑う。
- stacked PR では後続コミットが注入点を消して red proof が失効する（CI にも載っていない）。
- 実行時間が長いもの（約 25 分）がある。`pytest -q -q` は件数を隠す。パイプ越しの終了コードは suite の
  結果ではない。
