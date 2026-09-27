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
