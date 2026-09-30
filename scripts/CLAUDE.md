# scripts/ の作業メモ（このディレクトリで作業するときだけ読み込まれる）

lib ごとの**破ってはいけない契約**の要約。理由・経緯・全経路の棚卸しは `knowledge/file-map.md`
（旧 root CLAUDE.md の各ファイル項）と、そこから張った設計文書にある。

## 読み取りの入口（`lib_task_cards.py` / `lib_daemon_state.py`）

- queue / registry / config のファイルを開くコードは、必ず `lib_task_cards` の入口
  （`read_task_card()` / `read_regular_text()` / `read_regular_text_or_unreadable()`）を通す。
  task カードを読むのは 5 者（plan.sh・dispatcher.sh・watchdog.py・verifier-dispatcher.sh・taskvia-sync.sh）とも
  `list_task_cards()` だけ。フォールバックは持たない（単体コピーの隔離テストでは lib ごと置く）。
- **`Unreadable` 契約**: 読み取りの失敗を `None` / `{}` / `[]` で返さない。`Unreadable` は空の入れ物として
  振る舞わず、`bool()` / `len()` / `in` / `[]` / 反復 / `.get()` がすべて `TypeError` になる。分岐は
  `is_unreadable()` / `is_missing()`（**ENOENT だけが「本当に無い」**。`Path.exists()` は `EACCES` も False に潰す）。
- **空リストは「カードが 1 枚も無い」の意味だけ**。走査に失敗したときは終端でないプレースホルダを 1 件返す
  （`all(status in TERMINAL)` が `[]` に True を返し、未完了の兄弟を残したまま mission が done になる）。
- task の識別子はファイル名。`id` 欄の食い違うカード・通常ファイルでないカードは `[破損]` として保留（例外にしない）。
- 依存欄の検証（`blocked_deps_problem()` / `released_deps_problem()`）は要素を 1 つも落とさない
  （`blocked_by: [null]` を「依存なし」にしない）。
- デーモン側 JSON 状態ストアは `lib_daemon_state.load_json_store(path, check=...)` が唯一の入口。壊れたエントリが
  1 つでもあればストア全体が `Unreadable`。
- 例外は `plan.sh` の `load_state()` 1 つだけ（`knowledge/empty-vs-unobservable.md` §4）。

## 書き込みの入口（`lib_state_store.py`。**plan.sh の queue への書き込みは全部ここを通る**（S3 / t012））

- queue への書き込みの唯一の入口（`knowledge/state-store.md` §3・§4.1）。`atomic_write_text`（tmp → fsync → replace → **親 dir fsync**・
  mode 引き継ぎ）/ `atomic_remove` / `transaction()`（`queue/.lock`・取得後に読み直す・入れ子は即 `NestedTransaction`）/
  `Txn.recover()`（R-1〜R-4。**正本は書かない**。plan.sh はまだ呼ばない = S4）/ `diagnose()`（書かない）/
  監査ログ `queue/audit/transitions-YYYYMMDD.jsonl`（1 トランザクション = 1 行。Result・理由・本文は出さない）。
- **plan.sh は `with_lock()` の中の `save_task` / `save_mission` / `save_state` / `publish_assignment` / `retire_assignment` /
  `classify_assignment` だけで queue を書く**（`_txn()` = lib の `Txn` への委譲）。`with_lock` の外で呼ぶと `RuntimeError`。
  plan.sh に直列化（`dump_yaml` 等）・原子的書き込み・assignment の判定のコピーを戻さない
  （`tests/test_state_store_serialization_matches_plan_sh.py` / `tests/test_state_store_callers.py`）。
  lib は**普通に import** する（`_load_scripts_module` は `sys.modules` に載せないので dataclass を持つ lib は読めない）。
- 書けない・読めないは**例外**（`StoreWriteError` / `StoreReadError` / `LockBusy`）。`None` / `False` / 成功に潰さない。
  plan.sh の `with_lock` が終了コードに写す（`LockBusy` → 4、それ以外 → 1）。assignment 撤去の失敗だけは今までどおり warn して続行。
  監査ログだけは書けなくても遷移を止めず stderr に警告。本文（Result・理由）・env・token は出さない。
- 障害注入は `FAULT_HOOK`（モジュール変数。env スイッチは付けない）。呼び出し元は plan.sh だけ
  （`tests/test_state_store_callers.py` の許可表。dispatcher / hooks / verifier-dispatcher は S5 まで import しない）。
- 戻し方: PR revert → `scripts/sync-main-checkout.sh`。card・mission・state・assignment は 1 バイトも書き換えていない
  （`knowledge/state-store.md` §4.1）。

## 依存（`lib_dep_rules.py`）

- 「依存が満たされた」の唯一の定義（`card_dependencies()`）。pull・task-graph・status・dispatcher がここだけを読む。
  コピーしない。**`failed` の依存は「保留（HELD）」**で、進める出口は Director の `plan.sh release-dep <id> --mission <slug>` だけ。
- 共有規則に env 停止スイッチを付けない（dispatcher と plan.sh で答えが割れる）。

## status の語彙（`lib_task_status.py`）

- task の status の語彙・終端 / 手放した / 判断待ちの集合・コマンドごとの「受け付ける元の status」
  （`ACCEPTS_FROM`）の**唯一の定義**。データだけ（I/O・import なし）。plan.sh / dispatcher.sh / lint_plan.py /
  taskvia-sync.sh / verifier-dispatcher.sh / lib_dep_rules.py は import して使う。status を 2 つ以上並べた
  リテラルを外に書かない（`tests/test_task_status_single_definition.py` が AST で落とす。足りない集合は
  `lib_task_status` に足す）。
- 拒否は `plan.sh` の `refuse_transition()` 1 か所（exit 2・何も書かない）。S1 は**現状を写す**（狭めるのは 01c）。
  `cancelled` は語彙に無い（書き手が無かった）。手書きされても知らない status = 拒否 / 待つ / 塞ぐ側に倒れる。
  設計・挙動が変わる箇所・戻し方: `knowledge/state-store.md` §1。

## mux（`lib_mux.py`）

- `registry/mux/<name>.json`（spawn 記録）は **kill の認可の唯一の証拠**。消してよいのは mux が
  「その id は無い」と明確に答えた（`error.code == "pane_not_found"`）ときだけ。herdr は失敗を全部
  `{"error"}` で返し server 不達も同じ形なので、それ以外は「観測できなかった」で記録を残す。
- 記録を書く `write_pane_record()` と消す `drop_pane_record(expect=<判定した記録>)` は
  `registry/mux/.records.lock` の同じ区間で直列化する。問い合わせは名前でなく**記録の id**を、
  記録の server の世代と同じ接続の上で流す（検証と破壊が別の接続なら別の server でありうる）。
- デーモンの再起動は `lib_daemon_watch.py restart`。`lib_mux.py kill` / `spawn` を素で叩かない。
- `keys <name> <key>...` は名前付きキー（`up` / `down` / `left` / `right` / `enter` / `escape` / `tab`）だけ。
  `send` はテキストなので選択ダイアログのカーソルは動かない。使う前に `capture` で位置を見る。
- `Mux.spawn()` の `env=` 引数は廃止（env は起動コマンドに `export` として埋める）。

## 通知・記録（dispatcher / watchdog / kai-review）

- 状態ベースの通知（needs_director / failed+handoff / review 拒否 / `[timeout]`）は、状態が変わるまで **1 回だけ**。
  `registry/daemons/notified-state.json`（台帳。fingerprint = 通知内容を決める入力）を `notify_state_once()` が見る。
  台帳が読めない・壊れているときは**再送側**に倒す。書き手は dispatcher と watchdog の 2 人で、書き換えはすべて
  `told_lock()`（`lib_daemon_state.py`）の中。`kind=timeout` は 24 時間 prune されない。
- codex-review の差分 300KB 超の拒否は `registry/daemons/review-refusals/<mission>__<task>.json` に記録し
  （`lib_review_refusal.py` が唯一の定義）、dispatcher は記録がある間 spawn しない。記録が読めない・
  欄の値が不正なら保留。
- Rule 5 (idle-with-task) は、`run_in_background` の shell / Monitor が生きている Worker には送らない。
  「ペインの裏で何かが走っているか」は `lib_pane_process.classify_process_tree()` の 1 か所
  （watchdog の idle 判定と共有）。判定根拠は comm ではなく**祖先の cmdline に Bash tool /
  Monitor の shell snapshot wrapper が現れるか**（t074。本番の `npm exec ...` は process.title
  書き換え + `sh -c "..."` を挟むため comm では MCP を job と誤読していた）。
  **観測できないときは通知する側**、watchdog は殺さない側（判定ごとに向きが違う）。
  job が連続して見え続けている時間が `BACKGROUND_JOB_MAX_SECONDS`（既定 30分）を超えたら
  通知を再開する（起動元だけでは job の中身が進んでいるかは分からない — Ren の `pgrep -f`
  自己一致ループの実例）。それでも黙り続ける裏で止まった job は watchdog の max が拾う。
  この上限タイマー (`<name>.job-since.json`) は「無い」(正常) と「読めない/書けない」
  (異常) を区別し、後者は上限判定を信用せず通常判定に流す（t082。握り潰すと安全弁自体が
  黙る側に壊れる）。cmdline が読めないノードも「消滅」以外は `unknown` に倒す（t082 P1。
  「マーカー無し」に潰すと本物の job が見えなくなり watchdog が誤って terminate しうる）。
  （`knowledge/watchdog-idle-judgment.md` §7-9）。
- **利用枠切れ** (`⚠ Usage limit reached · continuing automatically at 6pm`) は idle でも max でもない
  (C2 / t005)。同定は `lib_usage_limit.detect()` の 1 か所 (dispatcher と watchdog が共有): 行頭の `⚠` +
  直下に入力欄の枠、で**位置と構造に束縛**する (文言の部分一致にしない)。Rule 5 は出さず Director に 1 回・
  リセット後に Worker へ再開を促す 1 回・なお続けば Director へ再通知 1 回。watchdog は免除中 idle で終了せず、
  免除して観測した時間を max から除く。**読めない・同定できない → 利用枠切れではない**、免除は必ず上限つき
  (`excuse_deadline()`)。台帳の slug は `_daemon` (mission slug だと `prune_told()` が捨てる)。
  （`knowledge/watchdog-idle-judgment.md` §10）
- idle Worker の退役 (dispatcher): 残っている task が無い、または skill は合うが TARGET_DIR が合わず取れない
  task しか無い (`takeable_pending` が空) Worker は no-task と同じく退役させる。TARGET_DIR の記録が
  無い / 読めない Worker だけは合わないと確定できないので待機（C3 / t009。`knowledge/assignment-routing.md`）。
- `sync-main-checkout.sh` は restart した daemon の新しい世代の heartbeat を上限つきで待ってから status を出す。
  上限までに記録されなければ失敗として積む（`lib_daemon_watch.py wait-heartbeat`）。
- 台帳・拒否記録は**消してよい**（無い = 再通知 / 拒否されていない）。通知が届かない・review が動かないときの
  手当てはそのファイルを消すこと（dispatcher の再起動は不要。`knowledge/notify-once.md`「戻し方」）。

## plan.sh

- `fail` は `--head <sha>`（実在する commit）必須、免除は `--no-head "<理由>"`。`handoff_path` は**絶対パスのみ**。
- `done` は `--pr <N>` か `--no-pr "<理由>"` のどちらかが要る場合がある（後続の codex-review に pr_number が無いとき）。
- task の成果物は `deliverable: pr|file|none`（`add` / `update --deliverable`）。`done` は `deliverable: pr` に `--pr` / `--no-pr` を求める。
  lint は `hooks/lib_skill_perms.py` の `check_permission()` を task の skills で直接呼び、Write/Edit/(`pr` なら) `git push` が
  実際に拒否されないかで突き合わせる（`lint_plan.py` にスキル名を書かない。`can_produce_deliverable` は宣言のみで判定には使わない — t088）。
  必須化は `init` が mission.yaml に書く `deliverable_required: true` の mission だけ（`knowledge/assignment-routing.md` §6）。
- `init --inactive` は state.yaml を変えない（観察用の使い捨て mission 用。以降は `--mission` 必須）。
  `add` / `update --blocked-by` は循環を exit 2 で拒否して何も書かない — 循環の定義は
  `lib_dep_rules.find_dependency_cycle()` 1 か所（lint_plan.py と共有。コピーしない）。lint は `deliverable: pr` の
  下流に skills `review` の task が無ければ WARN（`knowledge/plan-input-guards.md`）。
- Result / 理由 / notes は `--result-file <path|->` / `--notes-file <path|->` で渡す（`done` / `needs-director` / `verify-result`）。二重引用符の位置引数は本文中のバッククォート・`$(...)` を**シェルが plan.sh 起動前に実行する**。`-` は呼び出し元の stdin（bash が fd 3 に退避）。併用・読めない・空・非 UTF-8 は exit 2 で何も書かない。`fail` は本文が無い。`knowledge/plan-sh-result-file.md`。
- 引数は厳格（未知の option は exit 2 で何も書かない）。`pull` だけ使い方の誤りが exit 1（exit 2 は「タスクなし」）。
- queue を書き換えるサブコマンドの後、`registry/task-graph/tasks.json` を再生成する（`CREWVIA_TASK_GRAPH=0` で停止）。

## 起動（`start.sh` / `lib_trust.py`）

- mux モードの start.sh は、claude を起動する**前**に `lib_trust.py check <cwd>` で trust を検査する。未信頼（10）・確認不能（11）・
  検査自体の異常終了は**すべて止める**（exit 1。「読めない」を「信頼済み」にしない）。`~/.claude.json` は書き換えない。
  拒否は端末と `logs/start-sh/refusals.log` の両方に出す（`_log_refusal`）。副作用より前に置く。
- kickoff 前後の最後の網（`lib_trust.py dialog`）が、`❯`（ダイアログの選択カーソルでもある）を入力行と誤認する穴を塞ぐ。
  ダイアログの文言の定義は `lib_trust.py` の 1 箇所。戻し方は `knowledge/file-map.md`「lib_trust.py」。
- `CLAUDE_CONFIG_DIR` は precheck (ambient env) と spawn 先 (`ENV_EXPORTS`) で必ず同じ値にする。設定されて
  いれば伝播し、未設定なら spawn 先で明示的に `unset`（mux server 側に残る古い値を消す。t051 P1）。
- `capture()` は「読めなかった」と「画面が本当に空」を区別できず同じ `""` を返す。空画面を「ダイアログ
  なし」に倒さない — kickoff 送信直前・送信後は `_require_no_trust_dialog`（画面の検査が成功するまで
  有限回再試行、それでも駄目なら拒否）を使う。プロンプト待ちループ側の `_trust_dialog_check` は緩い網
  （観測できない間は上位のポーリングに委ねる）で、`CREWVIA_BENCH_MODE=1` はここも対象外にする（t051 P2 / P2x2）。
- `CLAUDE_CONFIG_DIR` / `HOME` は precheck の**前**に一度だけ絶対パスへ解決する（相対のまま渡すと、
  precheck (start.sh 自身の cwd 基準) と WORK_DIR へ `cd` してから起動する claude とで基準が変わり、
  「同じ文字列」でも「同じ解決済みの対象」にならない。t070 P2）。解決できない値はそのまま precheck に
  委ねる（存在しない dir は lib_trust.py が untrusted として止める。安全側）。
- LAUNCH_CMD へ値を埋め込むときは必ず `_shq` を通す。自前の `'$var'` 埋め込みは、
  AGENT_NAME・TARGET_DIR・WORK_DIR のような外部由来の値に `'` が含まれるだけで壊れ、`'; cmd; #` の
  ような値ならペインでコマンドが追加実行される（t070 P1 シェルインジェクション）。ENV_EXPORTS・
  `--model` / `--settings` / `--permission-mode` の CLI 引数・`cd`・advisory メッセージまで、
  埋め込み箇所は全部 `_shq` を通す（族 D）。文字列の形を見るテストは評価時の挙動を保証しない —
  `tests/start-sh-trust-precheck.bats` は実際に LAUNCH_CMD をスタブ claude で評価するテストを持つ。
  `_shq` は bash 組み込みの `printf '%q'` を使わない（t089 / PR#237 4巡目 P2-2）: LAUNCH_CMD を
  実際に評価するのは pane の**設定済みシェル**であり、それが bash である保証はない。`%q` は改行や
  非 ASCII を `$'...'`（ANSI-C quoting、bash 専用）で出力するが、dash はそれを構文エラーにする。
  代わりに、値をシングルクォートで囲み中の `'` を `'\''` に置き換える POSIX 互換の方式にした —
  bash / dash / ash / ksh / zsh のどれでも同じ意味になる（常にクォート付きになる点が旧実装との
  観測できる違い — `--permission-mode auto` は `--permission-mode 'auto'` になる）。
  また LAUNCH_CMD 内の `cd $(_shq "$WORK_DIR")` は `claude...` の前を `;` ではなく `&&` でつなぐ
  （族 A）: `cd` が失敗しても `;` は後続を実行してしまい、**別のディレクトリで Claude が起動する**。

## worktree_gc.py

- 既定は dry-run。`--apply` は remove と判定したものを **削除せず隔離する**（`git worktree move` +
  `git worktree lock`。`git worktree remove` / `git branch -d` / `rm -rf` はどれも呼ばない — 実際の削除は
  このツールの外、人間が手で行う。`tests/test_worktree_gc.py::TestNoForcefulOperations` が AST で固定）。
  隔離は `--restore` で完全に戻せる。remove 判定は全条件を満たしたものだけで、読めない・git が失敗・
  プロセス表を取れない・index フラグ（assume-unchanged/skip-worktree）が付いている、は keep
  （`knowledge/worktree-gc.md`）。隔離する直前に判定をやり直す。
