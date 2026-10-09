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
  `Txn.recover()`（R-1〜R-4。**正本は書かない**。plan.sh が `recover_before()` で呼ぶ = S4 / t016。下の「回復」）/ `diagnose()`（書かない）/
  監査ログ `queue/audit/transitions-YYYYMMDD.jsonl`（1 トランザクション = 1 行。Result・理由・本文は出さない）。
- **plan.sh は `with_lock()` の中の `save_task` / `save_mission` / `save_state` / `publish_assignment` / `retire_assignment` /
  `classify_assignment` だけで queue を書く**（`_txn()` = lib の `Txn` への委譲）。`with_lock` の外で呼ぶと `RuntimeError`。
  plan.sh に直列化（`dump_yaml` 等）・原子的書き込み・assignment の判定のコピーを戻さない
  （`tests/test_state_store_serialization_matches_plan_sh.py` / `tests/test_state_store_callers.py`）。
  lib は**普通に import** する（`_load_scripts_module` は `sys.modules` に載せないので dataclass を持つ lib は読めない）。
- queue の下のディレクトリは `os.makedirs` ではなく `ensure_dir()`（作成 + 作った dir の親を fsync）で作る。新規ファイルの mode は
  `0666 & ~umask`（旧 `open(.., 'w')` と同じ）、既存ファイルの置き換えは元の mode を保つ（`knowledge/state-store.md` §4.2）。
- 書けない・読めないは**例外**（`StoreWriteError` / `StoreReadError` / `LockBusy`）。`None` / `False` / 成功に潰さない。
  plan.sh の `with_lock` が終了コードに写す（`LockBusy` → 4、それ以外 → 1）。assignment 撤去の失敗だけは今までどおり warn して続行。
  監査ログだけは書けなくても遷移を止めず stderr に警告。本文（Result・理由）・env・token は出さない。
- 障害注入は `FAULT_HOOK`（モジュール変数。env スイッチは付けない）。呼び出し元は plan.sh・`lib_registry.py`（workers.yaml の
  原子的書き込み）・`taskvia-sync.sh`（map の `locked_update_json`）だけ（`tests/test_state_store_callers.py` の許可表）。
  dispatcher / hooks / verifier-dispatcher は import しない —— **card を書きたいなら plan.sh の subcommand を呼ぶ**
  （S5 / t020: verifier-dispatcher は `plan.sh verifying`、hooks/pre-compact.sh は `plan.sh snapshot`。どちらもロックの中で
  card を読み直す。ロックの外で card を丸ごと読んで書き戻すと、その間に done が進めた status を巻き戻す）。
- **ロック外の書き込み**の入口も lib: 1 ファイルの原子的な書き込みは `atomic_write_text`（`.crewvia-env`・workers.yaml）、
  専用ロックが要る小さな共有 JSON は `locked_update_json`（taskvia map。キャッシュだけ `on_unreadable='reset'`）、
  mission dir の rename は `durable_rename`（元と先の**両方**の親 dir を fsync。`shutil.move` は通さない）。
- **構造ガード**: lib を通らない queue / registry への書き込みが増えたら CI が赤
  （`tests/test_queue_writes_go_through_the_store.py`。表は `(ファイル, 関数) → (件数, 理由)`）。書き込みを足す・消すときは
  表の件数を直す。通せない理由があるなら理由つきで足す（`knowledge/state-store.md` §6.1）。
- `parse_opts()` は queue の骨組み（`missions/` `archive/`）を作らない。作るのは `with_lock()` の中（ロックを取った後）。
- 戻し方: PR revert → `scripts/sync-main-checkout.sh`。card・mission・state・assignment は 1 バイトも書き換えていない
  （`knowledge/state-store.md` §4.1）。

## 回復（card = 正本・assignment / `.identity` = projection。S4 / t016）

- **ロックを取った直後・前提検査より前**に plan.sh の `recover_before(cards, agents, add_missions, archive_slugs)` が
  `Txn.recover()` を呼び、card が言っていることに projection を合わせる（R-1 枠を作る / R-2 孤児の枠を消す / R-3 `next_task_id` /
  R-4 退避済みを `active_missions` から外す）。**正本（status / worker / started_at）は書かない**。範囲は名指しの card・その worker の枠・
  **逆引き**（この card を指す枠）・呼び出し元の枠だけ（queue 全体は走査しない）。呼ぶコマンドは
  `knowledge/state-store.md` §2.7 の表。`reap-orphan-assignment` は自身が R-2 なので呼ばない。
- **回復は拒否を 1 つも足さない**。表に無い食い違い（`reported:<コード>`）は stderr と監査ログに 1 行出して**書かずに**本体へ進む。
  回復自体の書き込みが失敗しても本体は止めず警告する（止めると出口が消える）。修復・報告の `op=recover` の行は lib が**その場で**
  監査ログに追記する（本体が後で `die` しても残る）。env の停止スイッチは付けない。
- R-2 で消してよい status の集合は `lib_state_store.is_orphan_target(status, worker)` の 1 定義（手放し済み ∪ needs_director ∪
  worker の無い pending）。`reap-orphan-assignment` も同じ関数。blocked / verification_failed 等を足さない（Director が作業中の card に
  `update --status` で付けても Worker は動いており、消すと動いている Worker を殺す経路になる）。
- R-1 は**所有の証拠**（`worker = A` の in_progress の card が missions/ **と archive/** の全体でこの 1 枚だけ）を読んだときだけ書く。
  `plan.sh archive` は status を検査しないので archive/ にも A の card が残りうる。読めない card があれば書かない。
- `plan.sh store-check [--mission <slug>]` は同じ判定を**書かずに**列挙する読み取り専用の入口（ロックなし。2 回連続で出たものだけが本物）。
- done は `D0 拒否 → D1 自分の card に pr_number → D2 依存先へ伝播 → D3 done → D4 枠撤去 → D5 mission done` の順（派生値は正本より前）。
  D0 の 2 つの拒否は exit 3・何も書かない・出口をメッセージに出す。順序を戻すと `tests/test_projection_recovery_on_lock.py` が赤。

## 依存（`lib_dep_rules.py`）

- 「依存が満たされた」の唯一の定義（`card_dependencies()`）。pull・task-graph・status・dispatcher がここだけを読む。
  コピーしない。**`failed` の依存は「保留（HELD）」**で、進める出口は Director の `plan.sh release-dep <id> --mission <slug>` だけ。
- 共有規則に env 停止スイッチを付けない（dispatcher と plan.sh で答えが割れる）。

## Execution + Task Controller（`lib_execution.py` / `lib_task_controller.py`。01c E1 / t004 で作成。**呼び出し元は plan.sh だけ**: pull（E2 / t008）・done / fail / needs-director / ready-for-verification / verifying / verify-result と `update --close-execution`（E3 / t012））

- `lib_execution.py` = card 1 枚から決まること（`ex-<32 hex>` の形・試行の欄 `EXECUTION_FIELDS`・`attempt_view`（照合・冪等・R-1・
  reserve の手順 0・store-check が呼ぶ**唯一の読み方**）・record の形と card への追従）。`lib_task_controller.py` = 遷移（reserve /
  start / complete / fail / release / reset / mark / abandon_detached / get）。書き込みは全部 `lib_state_store.Txn` の中で、
  card → record → assignment（identity → 本体）の順。**`lib_state_store` が import してよいのは `lib_execution` だけ**（controller を
  import すると循環する）。
- **domain error は `ControllerError.code`（固定コード）が契約**。文字列の解析を契約にしない。メッセージ・監査行・record には
  コード・位置・識別子だけを出し、card の本文・他の欄・Result・入力の行を出さない（secret を仕込んだテストで固定）。
- 呼び出し元は E4（reset / retire）で増える。増やすのは cutover なので、`tests/test_task_controller_has_no_callers_yet.py` の `ALLOWED_MENTIONS` と
  `PLAN_SH_ALLOWED_OPERATIONS`（plan.sh が呼んでよい Controller の操作）を意図して広げる（ユーザー承認が要る PR）。設計は
  `knowledge/execution.md`（§14 が E1・§15 が E2・§16 が E3 の実績と戻し方）。
- **pull（E2）の形**: ロック 1（recover → 再開の判定 → 候補選び → `reserve_task`）→ **準備ロック**（`lib_state_store.acquire_prepare_lock`。task ごと・
  LOCK_NB・取れなければ exit 1 で何も書かない）→ ロック外（Taskvia・worktree・`.crewvia-env`）→ ロック 2（`start_execution`、失敗なら
  `fail_execution(WORKSPACE_CREATE_FAILED)`）→ JSON。**start は JSON を出す前**にコミットする（`reserved` ⇔ JSON はまだ誰にも渡っていない。だから
  同じ Worker の再 pull は `reserved` を新しい試行にせず再開できる。`running` は再開しない）。ロックの順序は 準備ロック → `queue/.lock` → 小さな共有ファイル。
- **start と G1 の CAS は `_pull_cas_ok`（新しい欄 `current_execution_id == X ∧ execution_status == reserved` と、今までの欄 in_progress ∧ worker ∧
  `started_at` の AND）**。新しい欄だけにしない: 旧形式の書き手（rollback 中の旧コード・Director の手編集）は execution の欄を更新しない
  ので、解放済みの予約が `X / reserved` のまま残る。reset の後に Director が card を開き直した形は、今までの欄だけが拒否する。
  **E4b でも外さない**（旧書き手が本番に残っていないことは証明できない。`knowledge/execution.md` §18.3）。
- `.crewvia-env` は 3 行（`CREWVIA_MISSION_SLUG` / `CREWVIA_TASK_ID` / `CREWVIA_TASK_SLUG`）。試行の ID は**書かない**（E5: 再 pull で上書きされる古い値を source して名乗る事故の元）。名乗りは pull の JSON の `execution_id` / `attempt` を `--execution` で渡す。
  `task_slug` は最初の reserve で card に固定（title を変えても branch は変わらない。式は `lib_execution.slugify_title` の 1 か所）。
  Controller の domain error は `[plan.sh] error_code=<CODE>` を stderr の**最後の行**に出す（exit 2 は使わない = idle）。
- 戻し方: PR revert → `scripts/sync-main-checkout.sh`。新しい欄・record・identity の欄・`.crewvia-env` の 4 行目は残ってよい（旧コードは読まない）。
  reserved のまま残った試行は Director が `update --reset`（`knowledge/execution.md` §15.4）。
- **報告の 6 コマンド（E3）**: `done` / `fail` / `needs-director` / `ready-for-verification` / `verifying` / `verify-result` は名乗り（`--execution <id>` **だけ**。env `CREWVIA_EXECUTION_ID` は読まない = E5 / `knowledge/execution.md` §20.11。非空なら無視した旨を stderr に 1 行。`_execution_caller` がロックの前に決める）を Controller が card の今の試行と照合する。違えば exit 3（`EXECUTION_NOT_CURRENT` / `NOT_FOUND` /
  `ALREADY_TERMINAL`）・遷移の拒否は exit 2（表は `lib_task_status.ACCEPTS_FROM` の 1 か所。done は in_progress だけ・fail は in_progress / needs_director・verify-result は検証待ちだけ）・
  いずれも何も書かず、stderr の**最後の行**は `[plan.sh] error_code=<CODE>`。**空の `--execution ""` は exit 1**（名乗りなしに倒さない）。**E5 PR-2 から、名乗りなしの 6 コマンドは、card が active（reserved / running）の試行を持つとき `EXECUTION_REQUIRED`（exit 3・何も書かない・監査は `refused:EXECUTION_REQUIRED` の 1 行）で拒否される**（`_authorize(require_name=…)`。Director の done / fail / needs-director も同じ）。試行なし・DETACHED・TERMINAL の名乗りなしと、`update` / `retire` / `pull` / G1 の fail は今までどおり通る（Director が `update --reset` で開いた card への done の出口）。照合の拒否 4 つ（`EXECUTION_REQUIRED` / `NOT_CURRENT` / `NOT_FOUND` / `ALREADY_TERMINAL`）は `plan.sh` の `_controller_die` が `UsageExit` で出す = **task-graph を再生成しない**。PR-1 の警告（`UNNAMED_WARNING`）は ACTIVE の名乗りなしが拒否になって出る場面が無くなったので撤去した。pull は stderr に `[plan.sh] 報告には --execution ex-… を付ける (…)` を 1 行足す（stdout の JSON は同じ）。
  ID を名乗った同じ操作の再送は、**中身（done の `--pr` / `--no-pr` / Result・fail の head / handoff・needs-director の reason）が card と同じときだけ**成功（exit 0・何も書かない）。
  違えば exit 3・何も書かない・値は出さない（`_resend_conflict`。`knowledge/execution.md` §16.10。IDEMPOTENT の分岐を足したら比較も足す）。**done / fail は Controller の `dry_run=True` で検査してから**派生値（pr_number の伝播）・証拠の検証に進む。
  `verify-result fail`（< max）は試行を `VERIFICATION_REJECTED` で終え task を pending に戻す（次の pull が新しい試行）。
- **plan.sh から Controller を呼ぶときは `_load_task_for_report` を通す**: 読めない card・語彙に無い status は今までどおり exit 2（Controller は `STATE_INVALID` exit 1 にするので、plan.sh の寛容な読み口で今の答えを保つ）。
  `_controller_die(command, e)` が `ControllerError` を終わり方に写す（固定コード + 識別子だけ）。枠の撤去は Controller が **card の worker の枠**を外し、`_retire_caller_slot` が `AGENT_NAME` の枠の後始末を残す
  （`with_lock` のコールバックの中だけで呼ぶ。`transition_to_needs_director` のような LOCKED_HELPERS からは呼ばない）。
- `plan.sh update <id> --close-execution` は Director 用の「閉じる手段」: DETACHED で active な試行（E2 の間に終わった task に残った running 等）を task に触れず `ABANDONED_OUTSIDE_CONTROLLER` で閉じる。
- 戻し方（E3）: PR revert → sync。**revert 先でも `--execution` が通る互換（commit `e3-execution-flag-compat`・別 PR）を先に merge しておく**（起動済みの Worker・走っている kai-review.sh が付け続ける）。`knowledge/execution.md` §16.4。
- **退役は試行の ID だけで束縛する（E4a で ID を書き始め、E4b で世代を外した）**: `plan.sh retire` は `--execution <ex-…>` 必須（世代 `--started-at` は未知のオプション・空の `--execution ""` は exit 1）。退役 marker / progress の `task_execution_id`（`lib_retirement.read_task_execution_id` が ACTIVE / TERMINAL の試行だけ束縛・legacy / DETACHED は None）が後始末の唯一の証拠で、**ID が無い・`ex-<32hex>` でない・旧形式（`task_started_at` だけ）の marker は kill の前なら `phase=unprovable`、後なら `_cleanup_deferred` で Director へ保留**（世代で後始末しない・queue は何も書かない）。枠の照合（`classify_assignment` / `assignment_execution_verdict`）は identity の `execution_id` だけ: ID の無い identity は「この試行のもの」と言えない（撤去しない）。`identity.started_at` は projection として書き続けるが**比べない**。世代の読み口を戻さない構造ガード: `tests/test_execution_e4b_no_generation_readers.py`・赤の実証 `python3 tests/red_proof_e4b_generation.py`（`knowledge/execution.md` §18）。
## Git Policy（`lib_git_policy.py`。01b G2 / t008 で作成、G3 / t012 で呼び出し元を移した）

- task の branch・worktree path・base・PR base を決める唯一の場所（`knowledge/git-policy.md` §2・§3）。**判断だけ**で
  subprocess を持たない（観測 = `git show-ref` / `git worktree list` と副作用 = fetch / worktree add は呼び出し元）。
- mission.yaml の `git:` は**無いときだけ**既定値。あって読めない・未知の mode / 欄・型違い・空・制御文字・
  既定値以外の `worktree_root`・`parse_yaml` が結果に反映しなかった行（4 字下げ・flow 形式・空行やコメントの後ろ・重複キー。
  形は列挙せず「その行を除いて読み直しても結果が同じ行」を `_unparsed_line` が探す）は `GitPolicyError` で停止する。**`Unreadable` を既定値に倒さない**。`git:` を足したら `policy_from_text` を通すこと
  （lint も G3 でこれを呼ぶ。読み口を複製しない）。
- `task_branch_pattern` は置換子の直後に区切り（末尾か `/`。`{task_id}` だけ `-` も可）を必須にする（別の task が同じ branch になる pattern を通さない）。
- branch 名の規則は git より**狭い**（英数字で始まる成分・`.` / `.lock` で終わらない・先頭成分が `refs` / `origin` でない等）。
  規則を変えたら `tests/test_git_policy_resolver.py` の部分集合の検査（本物の `git check-ref-format --branch`）が通ること。
- **G3（t012）で呼び出し元になった**: `git-helpers.sh`（CLI `lib_git_policy.py resolve-task --lines` / `pr-base --lines`。
  `crewvia_create_worktree` / `_remove_worktree` / `_create_pr` が branch・path・base・PR base を**ここから**得る。式のコピーを持たない）・
  `plan.sh`（`_GIT_POLICY`。pull の失敗の分類 **P1** = Resolver の拒否と `plan.sh pr-base`）・`kai-review.sh`（diff base）・
  `worktree_gc.py`（片付けの根 `DEFAULT_WORKTREE_ROOT`）・`lint_plan.py`（`check_git_policy`）。呼び出し元の集合は
  `tests/test_git_policy_resolver.py::test_git_policy_callers_are_exactly_the_cutover_set` の `ALLOWED_CALLERS`
  （増やすのは cutover = ユーザー承認の PR だけ）。env の停止スイッチは付けない。
- **`plan.sh pr-base`** はエージェントが PR base を取る唯一の入口（読み取り専用・ロックなし。`.crewvia-env` には PR base を**出さない**。
  `knowledge/git-policy.md` §5）。引数なし = 自分の assignment の task（card が in_progress・worker が自分）／`--mission --task` =
  他人の task（所有者を見ない）／`--diff-ref` = QA の diff の ref。決められなければ **exit 1・stdout 空**（`main` に倒さない。
  使い方の誤りも exit 1 — exit 2 は idle の意味）。`target_dir` の task は mission の `git:` を見ず `DEFAULT_PR_BASE`。
  退避済み（archive）の mission は見ない（exit 1）。
- **「指定されたか」は presence で見る**（G3 fix 3）: `pr-base` は `--mission` / `--task` の**どちらかが指定されたら**明示の形に
  入り、両方必須・値は `check_*` で検証（`--mission "" --task ""` は exit 1）。assignment への fallback は**どちらも無いときだけ**。
  `if opts.get(...)`・`[[ -n "$x" ]]` で既定値に倒すと、他の PR から取り出した空の識別子が自分の task の base を成功で返す。
  この PR の掃除の結果: `pr-base` の `--mission/--task` だけが該当（修正済み）。`--diff-ref` は bool で空値を取らない・
  `AGENT_NAME` 空は拒否・`target_dir` の空は「無し」という card の既存の意味・git-helpers.sh の `${CREWVIA_QUEUE:-}` は
  plan.sh と同じ env の既定規則（引数ではない）・`crewvia_create_pr` / `crewvia_create_worktree` の必須引数は `-z` で拒否。
- **外から来る名前はパスにする前に検査**（G3 fix 2）: mission slug は `check_mission_slug`・task id は `check_task_id`
  （どちらも `lib_git_policy`）。`plan.sh pr-base`（assignment 由来の値も）・Resolver の CLI・`load_git_policy` が同じ関数を通る。
  **利用者に見えるエラーは `format_policy_error` 1 か所**（`[code] 欄: 詳細 (場所)`）。lint の PyYAML 例外は
  `lint_plan._yaml_error_location`（型名 + 行・列だけ。`str(e)` は出さない）。`e.detail` を自前で並べると
  `tests/test_git_policy_untrusted_names_and_error_text.py` が赤。
- **mission.yaml の無関係な字下げミス 1 行でも pull は止まる**（Resolver が `parse_yaml` の読み飛ばしを fail closed で拒否する）。
  出口: pull は exit 1・JSON なし・card を `needs_director`（理由に `(P1)`・行番号・ファイルの場所・直し方。**行の中身は出さない**）。
  事前には `plan.sh lint --mission <slug>` が同じ検査で FAIL にする。直したら `plan.sh update <id> --status pending --reset`。
- **Resolver の外に branch / base / worktree root のリテラル（`origin/main`・`--base main`・`.claude/worktrees` …）を書かない**:
  `tests/test_git_decisions_go_through_policy.py` が (ファイル, 関数) の allowlist で赤にする（寄せない箇所は理由つき。
  文書は agents/*.md・skills/*/SKILL.md の fenced code block だけ）。**task id は `tNNN` の形**（Resolver はそれ以外を拒否する）。

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
- **hard_idle が unknown で止まらない・止まっても Director に届く** (t003 / §11): zombie (state=Z) は木から外す。読めない (EACCES)
  ノードでも BFS を続け、job があれば `executing`。見送り (unknown / executing / awaiting_human / mux_pid_unavailable) が
  `daemons.hard_idle_suppressed_notify_seconds` (既定 600・**env なし**) 続いたら `SuppressedIdleNotifier` が Director に (episode の中で理由ごとに 1 回。§11-13)
  (fp は Execution ID・解けたら台帳キーを消す)。`WARN:` 行は理由が変わった時 + 10 cycle ごと。
  （`knowledge/watchdog-idle-judgment.md` §11-9）
- idle Worker の退役 (dispatcher): 残っている task が無い、または skill は合うが TARGET_DIR が合わず取れない
  task しか無い (`takeable_pending` が空) Worker は no-task と同じく退役させる。TARGET_DIR の記録が
  無い / 読めない Worker だけは合わないと確定できないので待機（C3 / t009。`knowledge/assignment-routing.md`）。
- `sync-main-checkout.sh` は restart した daemon の新しい世代の heartbeat を上限つきで待ってから status を出す。
  上限までに記録されなければ失敗として積む（`lib_daemon_watch.py wait-heartbeat`）。
- 台帳・拒否記録は**消してよい**（無い = 再通知 / 拒否されていない）。通知が届かない・review が動かないときの
  手当てはそのファイルを消すこと（dispatcher の再起動は不要。`knowledge/notify-once.md`「戻し方」）。

## Telegram（`lib_telegram.py` / `ask_user.sh`。PR-A / 設計 `knowledge/director-escalation-telegram.md`）

- **token は argv・ログ・例外文・状態ファイルに出さない。** Bot API は `urllib` をプロセス内で呼び、失敗は例外でなく固定コードの値
  （`ApiResult.error`）。`Credentials` は `repr` に値を出さない。dispatcher の python は起動直後に `_CREWVIA_TG_RESOLVED_*` を `os.environ` から
  外し、poll のサブプロセスにだけ env で渡す（`lib_telegram.run_cycle`）。**`env VAR=… cmd` は使わない**（env の argv に載る。bash の前置代入を使う）。
- **認証情報の解決は `resolve_credentials()` の 1 か所**（config の `telegram.credentials.source` が `op` / `file` のどちらか 1 つ。`CREWVIA_TG_*` の env は読まない。
  `ask` は運搬用の変数を読まない・`carried=True` の動詞だけが読む）。`file` は 0600（group / other のビットが 0・所有者が自分・通常ファイル・symlink 不可）で
  なければ**中身を開かない**。dispatcher.sh は起動時に 1 回だけ取り出し（失敗の間は 600 秒間隔でだけ取り直す）、シェル変数に持つ（`export` しない）。
- **未設定なら 1 バイトも書かない。** 例外は受信側の状態ファイル（`telegram-receiver.json`）が既にあるときの `enabled: false` への書き換えだけ。
  設定されているのに取り出せない（`credential_command_failed` 等）ときは `enabled: false` のファイルを作る（`ask` が断る・Director に 1 通）。
- 状態ファイルは `lib_daemon_state.load_json_store` の入口で読み、`told_lock` + `write_told_atomic` で書く。形の検証は `telegram_*_problem()`（書き手も同じ関数）。
  **壊れていたら**転送しない・offset を進めない・`ask` は断る（観測できなかったことを「答え無し」「有効」に倒さない）。台帳を消せば復旧。
- 判定は純粋関数（`classify_update` / `sweep_questions` / `receiver_verdict` / `parse_callback_data` / `apply_answer`）。callback_data は **`fullmatch`**
  （`$` は末尾の改行を許す）。受け付ける条件は全部 AND（chat・from・nonce・message_id・index・open かつ期限内）。先に確定した 1 つだけが有効（CAS）。
- **受信は後始末に締め出されない**（§2-3d）: `poll_once` は 受信 → 記録 → 後始末 の順で、後始末（answerCallbackQuery・ボタンを消す・諦めの通知）は `_Budget` の残りだけを使い、件数にも上限がある。poll の経路の `api_call` は必ず `timeout=` を取る（構造テストが赤にする）。確定した失敗（400/403/404）・回数・時間で `unbutton` は外れる。
- 受信側の心拍は定数 30 秒・stale は 90 秒（`poll_interval` と独立）。`ask` は stale / disabled / unknown / 別の bot（`bot_id`・`chat_hash` の不一致）で断る。
- テスト: `tests/test_telegram_pure.py`（純粋関数・認証情報）/ `tests/test_telegram_fake_bot_api.py`（偽の Bot API サーバー `tests/telegram_fake_api.py`・
  token の漏れを全 cmdline / 出力 / 状態ファイルで grep）/ `tests/test_telegram_dispatcher_glue.py`（dispatcher.sh の差し込みと bash 側の取り出し）。
  本物の Bot API は叩かない。`api_base` は lib の引数（`--api-base`）で、env のテスト用スイッチは本番コードに無い。

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
