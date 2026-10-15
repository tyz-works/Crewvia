# Crewvia vNext — Issue / Task / Attempt と Context Curator 詳細設計

作成日: 2026-10-11。状態: レビュー用提案（未実装）。MUST は必須、SHOULD は理由付きで変更可能。

> **この文書の成り立ち (2026-10-11、mission 20261011-vnext-task-attempt-design-rebase t001)**
> 元の文書は、ユーザーが別マシンで書いた提案です（base 003f2b5 = #126、2026-09-02）。それを今の main (2262728) に合わせて、次のように直しました。
>
> - §1・§2 は今の main に合わせて書き直した。元の §1 にあった調査環境の段落は、§1 末尾の引用に歴史的記録として残した。
> - §3〜§13 の本文は書き換えていない。今の実装と矛盾・重複する記述には、各節の末尾に「【現行との差 (2026-10-11 確認)】」を足した。
> - 末尾に §14「段階の取捨の材料」を新しく足した。取捨は決めていない（Director とユーザーが決める）。
>
> 根拠はファイル:行（2262728 の時点）か PR 番号で示した。確かめられなかったものは「未確認」と書いた。モデル名・価格などの外部事実は、元の文書どおり「未確認」のまま。
>
> E5 PR-2 (#291) と、その上に積んだ #293 / #292 は未 merge で、merge は 2026-10-15 20:33 JST 以降の予定。これらで変わる箇所には「#291 merge 後に変わる」と書いた。

## 1. Goal・範囲・根拠

Taskを実現すべき成果の単位として維持し、修正・中断・検証差し戻しをAttempt履歴に積む。Workerセッション終了後も、次の担当者が必要な根拠を予算内で再取得できることをGoalとする。Directorが計画と判断を担い、Dispatcherが通知、Workerが作業、Verifierが成果検証を担う構造を維持する。

**前提 (2026-10-11 書き直し)**: 本書は Mission 01（vNext 01a State Store / 01b Git Policy / 01c Execution + Task Controller）の上に載る追加設計である。Mission 01 は完了済みで、01c は 2026-10-03 に閉じた。

| 段 | 入ったもの | PR | 正本の文書 |
|---|---|---|---|
| 01a | State Store（`queue/.lock` と原子的な書き込み）・監査ログ・回復 R-1〜R-5・`store-check` | #255 (ADR) / #257 / #258 / #261 / #262 | `knowledge/state-store.md` |
| 01b | Git Policy（`lib_git_policy.py`・mission.yaml の `git:` 欄・`plan.sh pr-base`） | #263 (ADR) / #265 / #266 / #267 / #268 | `knowledge/git-policy.md` |
| 01c | Execution ID・Task Controller（`lib_task_controller.py`）・報告の `--execution` 照合・遷移の狭め・`retire --execution`・世代照合の撤去 | #269 (ADR) / #270–#276 | `knowledge/execution.md` §1–§19 |
| E5 PR-1 | 報告が Execution ID を名乗る | #286 (設計) / #287 | `knowledge/execution.md` §20 |
| E5 PR-2 | 名乗りのない報告を拒否する (`EXECUTION_REQUIRED`) | #291（未 merge）。上に積んだ #293（env `CREWVIA_EXECUTION_ID` を名乗りに使わない）/ #292（E5 の backlog） | — |

元の文書が参照する `crewvia-vnext-mission-01-control-plane-foundation.md` は、このリポジトリに無い。ユーザーの Obsidian の `proposals/crewvia/` にある提案で、改訂案は `20260930_crewvia-vnext-mission-01-revision.md` である。

実装の正本は上の表の knowledge 文書で、元の文書の「Phase 0–2」はこの 01a / 01b / 01c に当たる。元の文書の「Mission 01 が完了済みとは扱わない」は撤回する。§2 の差分表と §12 の P0 は、Mission 01 が済んでいる前提で読み直す。

既存仕様の Phase 0–2（= 01a〜01c）へ Curator や Recovery を混ぜない方針は維持する。ただし 01a は独自の回復（R-1〜R-5）をすでに持っている（§6 の注記）。

**調査対象と base**:

- 調査したのは、このリポジトリの `origin/main` = `2262728`（#296、2026-10-11）。base の CI は緑。
- 元の文書の base `003f2b5`（#126、2026-09-02）から 161 commit 後にあたる（`git log --oneline 003f2b5..2262728 | wc -l`）。
- 判断はコード（`scripts/` `hooks/` `agents/`）を読んで行った。文書や dispatch 表の一覧だけでは判断していない。
- 未 merge の PR は `gh pr view` で中身を確かめただけで、コードの事実としては扱っていない。

参照会話は構想の材料であり仕様・モデル仕様の証拠ではない。Haiku系は候補名に留める。特定のHaiku 5.5の存在、モデルID、context window、発売日、価格は本設計では未確認。採用前に公式情報と利用可能な実環境で確認する。

Non-Goals: 実装・本番切替・既存queueの移行実行、SQLiteへのSoT移転、常設の新しい意思決定ロール、無制限の履歴注入、全Taskの強制Worker再起動、GitHub Issueと内部Issueの同一視、自動merge。

> 元の文書の調査環境（歴史的記録。書き直し前の §1 第 3 段落）: 調査対象は `/Users/tyz/workspace/crewvia`。local main HEADは `53e05e02ccf1b83cb68eede140617061ae19ded9`、保存済みorigin/mainは `003f2b51220ece06d40b1be0de4d2ac8d2b9f3ad`、localは25 commits behindでdirty。ネットワークfetchは行っていないため最新remoteとは断定しない。本書を置く独立cloneのbaseは後者。元checkoutの未コミットAGENTS.mdも読んだが、実装済み機能の証拠とは区別する。（2262728 のリポジトリ直下に AGENTS.md は無い。）

## 2. 現行との差分（2262728 で確認。2026-10-11 書き直し）

分類は次の 5 つ。

| 分類 | 意味 |
|---|---|
| **済み** | 狙いが今の実装で満たされている |
| **一部済み** | 狙いの一部だけが満たされている |
| **未着手** | 該当する実装が無い |
| **元の文書の誤認** | 元の文書の「現状」欄が今のコードと違う。base の時点で正しかったかは確かめていない |
| **異なる決定** | 元の文書の前提と違う方針を、Mission 01 が明示的に採っている |

### 2.1 元の文書の表の各行

| 対象 | 元の文書の「現状」→ 2262728 の現状（根拠） | 分類 | 元の文書の追加設計 |
|---|---|---|---|
| `scripts/plan.sh` 全体 | 試行を持つ報告と予約は、すべて `lib_task_controller.py` を通る（pull reserve `plan.sh:3913` / start `:3486`、needs-director `:4389`、done `:4731`・`:4882`、fail `:5057`、verify-result `:5544-5587`、`update --reset` `:6512`、retire `:6913`）。例外は `update --status` で、Controller を通らず語彙の検査だけで直接書く（`plan.sh:6430-6433`）。Issue・revision・reopen の世代は無い（`scripts/` を grep して `idempotency_key` / `expected_revision` / `lease_epoch` / task の `revision` は 0 件） | 一部済み（Controller は #270 / #271 / #273。Issue・世代は未着手） | Controllerに遷移集約、Issue・Attempt・世代追加 |
| 同 `cmd_update` | `--reset` は any → pending。進行中の試行を `RESET_BY_DIRECTOR` で閉じる（reserved なら released、running なら failed。`lib_task_controller.py:703-720`）。`completed_at` / `handoff_path` / `fail_head` を消し（`plan.sh:6512-6513`・`:6564-6571`）、監査行を 1 行書く（`queue/audit/transitions-YYYYMMDD.jsonl`、`lib_state_store.py:695`）。理由・revision・下流への影響は記録も検査もしない（`--reason` は無い。`plan.sh:6398-6410` の受け付けるオプション）。Result 本文・`rework_count`・`execution_count` は消さないので、前回の結果は card 本文に残る | 一部済み（試行を閉じるのは #273 / #274、監査行は #258。理由・revision・依存の影響は未着手） | 理由・revision・依存影響付きreopen、履歴保全 |
| 同 `cmd_verify_result` | fail では `in_progress` ではなく `pending` に戻す。試行を `VERIFICATION_REJECTED` で閉じ、`worker` / `started_at` を消す（`plan.sh:5576-5580`）。次の pull で attempt +1 の新しい試行になる。`rework_count >= max_rework`（既定 3）なら `needs_human_review` にし、試行は running のまま（`plan.sh:5581-5590`）。docstring の「fail → in_progress」（`plan.sh:5483`）は古い記述。**ただし Verifier は運用されていない**。`worker.md:690-691` に「Verifier 機能（M-QA-4）が導入されるまでは `done` を使い続けてよい」とあり、`verifier-dispatcher.sh` は `start.sh` / `./crewvia` から起動されない。queue の全 card（archive を含む）で `ready_for_verification` / `verifying` は 0 件 | 元の文書の誤認（in_progress）。「同 Task に新 Attempt」は済み（予約ではなく次の pull で作る）。経路は使われていない | Attemptを閉じ、新Attemptを同Taskへ予約 |
| `agents/director.md` Handoff 再計画 | 今も継続 task を新しく足す。「継続タスクを plan.sh add で追加する（元タスクとは独立した新タスク）」（`director.md:1399-1425`。通知文も `:1352`「plan.sh add で継続タスクを追加してください」）。Codex 指摘の修正も、実際の運用では巡ごとに新しい task を足している（§14.1） | 未着手 | 同成果ならretry、成果変更時のみ新Task |
| `scripts/dispatcher.sh` | busy の判定は「assignment ファイルがある」か「needs_director の card を持つ」（`dispatcher.sh:2588-2590`）。依存の判定は複製せず、`lib_dep_rules` を import する（`dispatcher.sh:195`・`:947-960`）。終端は `TERMINAL_STATUSES`（`dispatcher.sh:674` → `lib_task_status.py:42` = done / verified / skipped）で、元の文書の「terminal は done/skipped」とは違う。failed の依存は保留（HELD）になり、出口は `plan.sh release-dep`（`lib_dep_rules.py:83-95`） | 済み（依存判定は 1 か所。導入した PR は未確認）。元の文書の誤認（terminal） | 互換adapter継続、依存判定はControllerに統一 |
| `scripts/plan.sh` 終端 | 終端の定義は `lib_task_status.py:42` の 1 か所だけで、plan.sh は参照するだけ（`plan.sh:431`）。pull の `done_ids`（`:3704`）と mission の完了判定（`:4906`）も同じ集合を使う。verified の扱いは dispatcher と食い違わない | 済み（01a S1 の範囲。行単位の PR は未確認） | verifiedの扱いの不一致を共通predicateで解消 |
| `agents/worker.md`, `scripts/start.sh` | knowledge の注入は起動時の 1 回だけ。skill ごとの `knowledge/<skill>.md` を連結し、`--append-system-prompt` で渡す（`start.sh:553-601`）。Worker は 1 セッションで pull ループを回す（`worker.md:3`・`:112-134`）。task ごとの配布物（Execution Package）は無い | 未着手 | セッション非依存のExecution Package、隔離方針は段階導入 |
| `hooks/pre-compact.sh` | 「先頭の Task」ではない。task id が複数の mission に当たると `resolve_ambiguous_mission` に進み、確かめられなければ拒否する（`plan.sh:5365-5378`・`:3069-3089`）。書き込みは `plan.sh snapshot` 経由（`pre-compact.sh:52-54`）。保存するのは trigger / agent / task_id / timestamp だけで、作業内容は含まない（`pre-compact.sh:28-43`）。**本番では card への書き込みが動いていないと推定する**: hook が読む `CREWVIA_TASK_ID`（`pre-compact.sh:22`）を `start.sh` の `ENV_EXPORTS`（`start.sh:786`）が渡していないため、`queue/pre-compact-fallback.log` 行きになる（`:59-64`）。実機のログは未確認。execution_id とは結び付いていない | 元の文書の誤認（先頭一致）。一部済み（誤った mission に書くことは防ぐ）。実質は動いていない | mission/task/executionで特定、根拠付きcheckpoint |
| `scripts/git-helpers.sh` | 判断は `lib_git_policy.py` に移り、git-helpers は観測と副作用だけを持つ（`git-helpers.sh:10-13`）。<br>・branch: `task/{mission_slug}/{task_id}-{task_slug}`（`lib_git_policy.py:44`）<br>・base: origin → local へフォールバック（`lib_git_policy.py:477-487`）<br>・PR base: `plan.sh pr-base` から取り、決まらなければ push しない（main に倒さない。`git-helpers.sh:247-250`）<br>・試行ごとの記録: `git{branch, base, pr_base, worktree, head_at_start}` を execution record に残す（`lib_execution.py:327-355`）<br>・handoff の path: `registry/handoffs/<agent>/<task_id>_HANDOFF.md` で mission を含まないため、別 mission の同じ `tNNN` と衝突しうる（`git-helpers.sh:83`）<br>・commit と検証の証拠の対応付けは無い | 一部済み（Policy は #263–#268、試行の git 記録は #271。commit / 証拠の対応と handoff の mission 衝突は未着手） | Policy維持、Attemptとcommit/evidenceを対応付け |
| `agents/verifier.md`, `docs/qa-operations.md` | 独立 Verifier と作者との分離（`qa-operations.md:111-113`）は文書にある。light / standard / strict のモードは、保存と引き上げまで実装済み（`plan.sh:5613-5620` の順序と `upgrade_mode`、`:5658-5690` の `_apply_risk_flag_upgrades` が card の `verification.mode` を書く。`cmd_review` が verdict と同じトランザクションで呼ぶ `:6006-6007`。下げる経路は無い）。ただし、モードで手順を変える実行者は無い（`agents/verifier.md:14` は standard の手順だけ、`verifier-dispatcher.sh` はモードを読まない）。`verify-result` は判定を、その時点の `current_execution_id` を付けて追記する（`plan.sh:5520-5527`）ので、試行には結び付く。commit との束縛は未確認。運用はされていない（上の `cmd_verify_result` の行）。実際の検証は QA task（`qa_checkpoints`、`director.md:271-303`）と Codex review（`director.md:1066-1089`）が担う | 一部済み（試行との束縛）。Verifier の経路は使われていない | commit・Task世代に束縛した検証、context gate追加 |
| `docs/benchmarks/20260503_context-strategy.md` | 変わっていない。小タスク 5 件で、毎回再起動する C は +65% の時間・3.03M tokens。採ったのは A（1 セッション連続）+ PreCompact で、C は大タスク向けの位置づけ。その PreCompact の保存は、上の行のとおり実質動いていない | 変化なし | 永続化と起動戦略を分離、再起動の一律採用を避ける |

### 2.2 元の文書の表に無いが関係する既存機能

| 機能 | 2262728 の現状（根拠） | 分類 | 本書との関係 |
|---|---|---|---|
| Execution ID / 試行 | ID の形は `ex-<32hex>`（`lib_execution.py:34`）。<br>・正本: card の frontmatter の 7 欄 `current_execution_id` / `execution_status` / `execution_end_code` / `execution_count` / `execution_reserved_at` / `execution_agent` / `task_slug`（`lib_execution.py:85-88`）<br>・record（`queue/missions/<m>/executions/<id>.json`）: projection で、判断には使わない（`knowledge/execution.md:99`）<br>・attempt: `execution_count + 1` をロック内で決める（`lib_task_controller.py:458`）<br>・active な試行: 欄が 1 組なので構造的に 1 件（`execution.md:79`）<br>・status: reserved / running / completed / failed / released（`lib_execution.py:37`・`:43-49`）<br>・過去の試行の record は凍結し、card に履歴は積まない（`execution.md:109-111`・`:122`） | 済み（#269–#276） | §4「Execution Record と Attempt は同一実体」は既存方針と一致する。ただし status の語彙・ファイル形式・正本の置き場は違う（§4・§5 の注記） |
| 古い報告の拒否 | 名乗りがあって一致しなければ `EXECUTION_NOT_CURRENT` / `EXECUTION_NOT_FOUND` で exit 3（`lib_task_controller.py:343-373`）。**名乗りが無く試行が ACTIVE なら、警告だけで通る**（`:366-368`） | 一部済み。**#291 merge 後に変わる**（`EXECUTION_REQUIRED` で拒否） | §6「旧 Worker からの done/fail を拒否」 |
| 再送の冪等 | 同じ ID・同じ中身の再送は冪等。同じ ID で中身が違えば exit 3（`plan.sh:4742-4748`、`lib_execution.py:74-81`）。`idempotency_key` / `expected_revision` / `lease_epoch` は無く、execution_id が鍵の役をする | 一部済み | §6 の mutating API の検査 |
| ロック | `queue/.lock` は queue 全体で 1 本（`lib_state_store.py:723-753`）。task ごとの準備ロックは `executions/<tid>.prepare.lock`（`:1882-1941`）。ロックの直後に回復が走る（`plan.sh:2213` の `recover_before` → `Txn.recover()` `lib_state_store.py:1276`） | 済み（#257 / #261 / #271） | §6 の Global Control Lock |
| 複数ファイルの更新 | ジャーナル方式は明示的に採っていない（「ジャーナル方式 (選択肢 1) を採らない理由」`knowledge/state-store.md:212-216`。01c でも再判断の条件に当たらない `knowledge/execution.md:107`）。代わりに次の 3 つで扱う。<br>(1) card を正本でコミット点とし、assignment / `.identity` / record を projection にする（`state-store.md:198-210`）<br>(2) ファイルごとに原子的に書き、書く順序を card → record → assignment に固定する<br>(3) 回復 R-1〜R-5（`lib_state_store.py:1417-1732`） | 異なる決定 | §6 の multi-file journal と真正面から重なる（§6 の注記） |
| 回復を誰が起こすか | 回復を起こすのは plan.sh のコマンドだけ（12 か所。`state-store.md:590`）。dispatcher / watchdog は回復も `store-check` も呼ばず（grep 0 件）、起動時に回復を待つ仕組みも無い（backlog: `state-store.md:1128`）。State Store には quarantine が無い。名前の似た仕組みは別物で、worktree の隔離（`worktree_gc.py` の `--list-quarantine` / `cmd_restore`）と、退役中の Worker の隔離（`lib_retirement.py`）がある | 未着手 | §6「recovery が終わるまで Dispatcher を開始しない」 |
| 監査ログ | `queue/audit/transitions-YYYYMMDD.jsonl`。1 トランザクションで 1 行、O_APPEND + fsync（`lib_state_store.py:1231-1268`）。欄に `execution_id` / `from_status` / `to_status` / `result` を含むが、理由の本文は無い | 済み（#258） | §4「状態変更は監査 event」 |
| outbox | 無い。execution を Taskvia に同期しない（`knowledge/execution.md:27`）。Taskvia へは `taskvia-sync.sh` が queue を読んで送る | 未着手 | §6・§12 の outbox |
| 遷移の狭め | `ACCEPTS_FROM` で、pull は pending、done は in_progress、fail は in_progress / needs_director、verify-result は検証待ちの 3 つだけを受け付ける（`lib_task_status.py:97-102`）。`update --status` には効かない | 済み（#273）。`update` は対象外 | §12「旧 update --reset/--status を Controller に委譲」 |
| `retire --execution` | watchdog の terminate から retirement marker を経て `plan.sh retire --execution` を呼ぶ（`lib_retirement.py:2055-2057`）。in_progress・worker・試行の 3 つが一致したときだけ、試行を `RETIRED` で閉じて pending に戻す（`plan.sh:6741-6926`）。kill の前に checkpoint は取らない。Worker に「現在の状況を 1-2 行で記載して終了」と送り（`watchdog.py:132`）、60 秒待つだけ（`watchdog.py:126`） | 済み（#274） | §6 の hard kill / graceful |
| pull の冪等・再開 | 自分の reserved（start 前）は再開し、running は再開しない（`plan.sh:3398-3409`）。worktree を作れなければ試行を `WORKSPACE_CREATE_FAILED` で閉じ、needs_director にする（`plan.sh:3520-3569`） | 済み（#271） | §6 の Git 準備の saga |
| mission の done / archive | 全 task が終端なら mission を自動で done にする（`plan.sh:4904-4910`）。`add` は mission の status を戻さない（`cmd_add` `plan.sh:2893-`。mission の status を書くのは `:4908` / `:5604` / `:5829` / `:5996` / `:5999` / `:6033` だけ）。dispatcher は全 mission が done なら idle Worker を退役させる（`dispatcher.sh:2446-2476`）。`archive` は status を検査せずに移し（`plan.sh:6093-6126`）、restore は無い | 未着手 | §5 の reopen・mission の再活性化・archive からの restore |
| needs_director と段階上げ | `needs-director` は試行を `NEEDS_DIRECTOR` で閉じ、枠を撤去する（`plan.sh:4389-4390`）。dispatcher が Director に通知し（`dispatcher.sh:2907-2955`）、段階 1 は 5 分で再通知、段階 2 は 10 分で Telegram に送る（`dispatcher.sh:3014`、`lib_escalation.py:93-117`）。Director は `update --status pending --reset` で差し戻す（`director.md:309-310`） | 済み（#280 / #281 / #283 / #284） | §8 R3「Director へ照会」、§5 の needs_human_review の受け皿 |
| failed の依存の保留 | failed は依存を満たさず HELD になり、出口は `release-dep` だけ（`lib_dep_rules.py:83-95`） | 済み | §5 の reopen の下流の扱いと関係する |

ベンチマーク内のモデル価格・TPM・大型Taskへの推測は現在の公式仕様として流用しない。集計cache tokenはcontext occupancyではない。既存reset・再作業を「再オープンなし」と誤認しない一方、監査付き再オープン／Attempt分離は別機能である。（2026-10-11 補記: 試行の分離そのものは 01c で入った。足りないのは、監査付き・理由付き・下流への影響付きの reopen と、終わった task の世代の管理である。）

## 3. 責務とAuthority

| 主体 | 責務・権限 |
|---|---|
| Director | Issue triage、成果/AC定義、Task分解、再オープン承認、予算変更、依存影響判断 |
| Task Controller | lock下の予約・開始・終了・reopen、世代/lease確認、適格性と依存判定 |
| State Store | validation、atomic replace、multi-file journal/recovery、監査、outbox |
| Dispatcher | 予約可能Taskの通知。状態を独自推測して書き換えない |
| Context Manager | source選択、token計測、retrieval、package版管理、context gate。決定論的制御 |
| Context Curator | 証拠から抽出・分類・要約の候補を返す。状態/Git/承認を変更しない |
| Worker | package確認、コード変更、追加検索要求、checkpointと結果提出 |
| Verifier / Reviewer | AC・diff・機械check・根拠検証。作者と同一sessionを禁止 |

CuratorはContext Managerの交換可能な処理アダプタであり新Directorではない。取得文書・会話・ログの命令を実行権限へ昇格しない。正式な制約はDirector承認済みTask/Contractからのみ取り込む。

> **【現行との差 (2026-10-11 確認)】**
>
> - **Task Controller はもうある**（`scripts/lib_task_controller.py`）。
>   - ロックは自分では取らず、呼び出し側（plan.sh の `with_lock`）が `queue/.lock` を持つ（`lib_task_controller.py:20`）。
>   - 予約・開始・終了・reset は Controller に集約済み（§2.1）。
>   - reopen・lease・世代の確認は無い。適格性と依存の判定も Controller には無く、`lib_dep_rules.py`（依存）と `lib_task_status.py`（語彙・終端・`ACCEPTS_FROM`）が担う。
> - **State Store**: validation・atomic replace・回復・監査は済み。multi-file journal と outbox は採っていないか、まだ無い（§2.2「複数ファイルの更新」「outbox」）。
> - **書き手の範囲**: 誰が何を書くかの一覧は `knowledge/state-store.md` §5（`:881-`）にある。dispatcher は retirement marker（`dispatcher.sh:1335-` `retire_worker`）と通知の台帳を書く。dispatcher が card を書くかどうかは、本調査では確かめていない（未確認）。
> - **Verifier / Reviewer**:
>   - Verifier は運用されていない（§2.1）。作者と別 session の検証は、今は Codex review（Kai。`kai-review.sh`、`director.md:1066-1089`）と QA task が担う。
>   - 「作者と同一 session を禁止」を機械的に検査するのは、Verifier Dispatcher だけ（`qa-operations.md:111-113`）。
> - **Context Manager / Context Curator**: 該当する実装は無い。

## 4. データモデル・互換性

Issueは問題・要求・観測、Taskは成果とAC、Attemptは一度の実行。Issue:Taskは多対多、Task:Attemptは1対多。同じWorker名でもsession IDは異なる。

Mission 01のExecution RecordとAttemptは **同一実体** とする。新たなAttempt IDの発行系を増やさず、`execution_id`を主キー、`attempt_no`をTask内単調増加表示番号とする。Worker一回のleaseが一Attempt。検証はそのAttemptに付属し、検証差し戻し後の実装修正は新Attempt。

提案layout（SoTは既存ファイルストアを拡張）:

```text
queue/missions/<mission>/
  tasks/tNNN.md                    # existing; typed frontmatterを追加
  issues/iNNN.yaml
  executions/<execution_id>.yaml   # Mission 01と整合; Attempt record
  context/tasks/tNNN/<revision>.json
  context/packages/<execution_id>/<package_id>.json
  artifacts/<execution_id>/        # checkpoint, redacted logs, evidence
queue/assignments/<worker>          # existing mission:task projection
queue/assignment-leases/<worker>.json
queue/transactions/<tx_id>/
queue/outbox/<event_id>.json
```

Task必須追加: `schema_version`, `revision`, `generation`（reopenごと+1）, `issue_ids`, `active_execution_id`, `latest_execution_id`, `latest_verified_execution_id`, `git_policy_ref`, `context_policy_ref`。既存id/title/description/skills/priority/blocked_by/verification/target_dirは維持する。

Issue: `id`, `mission`, `kind`(bug/request/question/risk), `status`(open/triaged/resolved/closed), `statement`, `evidence_refs`, `task_refs`, `resolution`, `revision`。同じ症状の重複候補は提示のみ、Directorが統合。外部IssueはURLと外部IDを持つ任意linkで、双方向同期は後続。

Attempt: `execution_id`, `task_ref`, `task_generation`, `attempt_no`, `reason`, `parent_execution_id`, `status`, `worker_name`, `session_id`, `lease_epoch`, timestamps, `package_id`, `git`, `checkpoints`, `result_ref`, `verification_refs`, `usage`, `failure_class`。予約時発行し、一Taskにつきactive Attempt最大1。開始後のidentity/base/package参照は不変。状態変更は監査eventを追加し、過去のterminal結果は上書きしない。

ArtifactRef: `artifact_id`, `relative_path`, `sha256`, `source_revision`, `classification`, `created_at`。RepoSourceRefはrepo identity / commit / path / line span / content hash。許可rootからのpath traversalとsymlink escapeを拒否。秘密値は永続化・モデル送信前にredactし、原ログを無条件にGit追跡しない。

> **【現行との差 (2026-10-11 確認)】**
>
> - **「Execution Record と Attempt は同一実体」**: 01c の方針と一致する。attempt_no も、`execution_count` としてすでに task 内で単調に増える（`lib_task_controller.py:458`）。ただし次の 3 点が違う。
>   1. **正本の置き場所**: 正本は card の frontmatter で、record（`executions/<id>.json`。YAML ではない）は projection にすぎない（`knowledge/execution.md` §1.2 `:49-`・`:99`）。本書の layout で `executions/<execution_id>.yaml` を正本にするなら、01c が選択肢を比べて下した決定（`execution.md:122` 付近の表）を覆すことになる。
>   2. **Task の追加欄**:
>      - `active_execution_id` は、今の `current_execution_id` に当たる。
>      - reset しても試行の欄は消さない（`lib_task_controller.py:703-720`）。このため、終わった試行の ID も `current_execution_id` に残る。
>      - `latest_execution_id` を別の欄にする必要があるかは、§5 の状態遷移を決めるときに判断する。
>      - 次の欄は無い: `revision` / `issue_ids` / `latest_verified_execution_id` / `git_policy_ref` / `context_policy_ref`。
>      - Git Policy は mission.yaml の `git:` 欄にあり、mission 単位で決まる（`knowledge/git-policy.md` §2 `:232-`）。task 単位の参照は無い。
>   3. **`generation` という語は、すでに別の意味で使われている**:
>      - `started_at` を「実行世代」と呼んでいる（`plan.sh:502` `now_generation`、監査ログの `generation` 欄 `lib_state_store.py:1231-1253`）。
>      - 照合に使うのは E4b でやめた（`execution.md` §18）。ただし `started_at` は watchdog の idle 時計として今も使われている。
>      - reopen の世代に同じ名前を付けると混同する。別名にするか、既存の語を改めるかを決める必要がある。
> - **layout**:
>   - ある: `queue/assignments/<worker>`（projection）。
>   - 無い: `assignment-leases/`・`transactions/`・`outbox/`・`issues/`・`context/`・`artifacts/`。
> - **「過去の terminal 結果は上書きしない」**:
>   - 監査ログと record の凍結（`execution.md:109-111`）は、すでに満たしている。
>   - ただし card の Result 本文は報告のたびに上書きされる（例: fail は `FAILED — head: … — handoff: …` で上書き。`plan.sh:5051-5055`）。過去の試行の結果の本文は card に残らない。

## 5. 状態遷移とTaskを増やす基準

Taskは既存statusを利用する。Attemptのstatusはreserved → preparing → running → submitted → succeeded/rejected、途中からfailed/interrupted/cancelled。lease期限切れは失敗原因であって成果完了ではない。

| 操作 | 前提 | 同一transactionの結果 |
|---|---|---|
| reserve | pending、依存充足、activeなし | Attempt reserved、新lease、Task in_progress、assignment projection |
| prepare/start | lease一致、Git準備済み、context gate pass | preparing→running、package固定 |
| submit | running、revision/lease一致 | submitted、Task ready_for_verification、Worker lease解放、Verifier用lease別管理 |
| verify pass | submitted、commit/package/generation一致、AC充足 | succeeded、Task verified、latest_verified更新 |
| verify fail | submitted、証拠一致 | rejected、回数加算、Task pendingまたはneeds_human_review |
| execution fail | active lease一致 | failed/interrupted、active解放、Task failed、retry判断待ち |
| retry | failedまたは差し戻しpending、上限内 | 同Taskをpendingへ、次reserveで新Attempt |
| reopen | done/verified/skipped/failed、activeなし、理由あり | generation+1、pending、完了時刻clear、旧結果保存、mission再活性化 |

retryは同generationの未達成作業、reopenは確定済み結果の再評価。`verified`は成果検証済みでGit merge済みを意味しない。Issueは対応Taskすべての必要AC確認後にDirectorがresolvedにする。単なるWorker終了をIssue解決にしない。

同じACの中断・再試行・差し戻し・回帰修正は原則同Task。独立成果、別repo、異なる承認境界、ACの大幅変更なら新Taskを作り`supersedes`/`related`を記録する。自動類似度だけでTaskを統合しない。

reopen APIは `task_ref, expected_revision, reason, issue_refs, actor, idempotency_key`を要求する。受付時に依存Taskの推移閉包を返す。pending下流はblocked、running下流はcancel_requested+lease fence、既にverified下流はinvalidation eventとneeds_human_reviewに移す。running下流は終了確認まで再割当しない。下流を無言で再オープンしない。Controllerのeligible predicateでgenerationと検証有効性を検査する。cycleは事前拒否。

archive済みmissionは明示restoreを同transactionで実行し元Task IDを維持。保存が失われた場合はreopenを拒否し復旧を要求する。Taskvia ID/mapも維持する。

> **【現行との差 (2026-10-11 確認)】**
>
> - **Attempt の status の語彙が違う**:
>   - 実装にあるのは reserved / running / completed / failed / released の 5 つ（`lib_execution.py:37`、遷移は `:43-49`）。
>   - 本書の preparing / submitted / succeeded / rejected / interrupted / cancelled は無い。
>   - 終わり方の区別は status ではなく終了コードで持つ。DONE / VERIFIED / WORKER_FAILED / NEEDS_DIRECTOR / VERIFICATION_REJECTED / RESET_BY_DIRECTOR / RETIRED / WORKSPACE_CREATE_FAILED / ABANDONED_OUTSIDE_CONTROLLER がある（`lib_execution.py:63-68`）。
>   - stale / abandoned / recovered は 01c が後続に送ったもの（`execution.md` §0 `:20-`）。
> - **すでに今の挙動と一致している操作**:
>   - reserve: Task を in_progress にする（`lib_task_controller.py:457`）。
>   - verify fail: Task を pending に戻す（§2.1）。
>   - execution fail: `plan.sh fail` が試行を `WORKER_FAILED` で閉じ、Task を failed にする（`plan.sh:5057`）。下流は HELD になる（`lib_dep_rules.py:83-95`）。本書の「retry 判断待ち」に当たり、retry の手段は `update --reset`（理由を記録しない）。
> - **submit**: `ready-for-verification` の経路はある（`plan.sh:5327`）が、使われていない（§2.1）。
> - **reopen**:
>   - 専用のコマンドは無い。近いのは `update --status <x> [--reset]` で、done / verified の task も `--reset` で pending に戻せる（遷移元は問わない。`lib_task_controller.py:709`）。ただし世代・理由・下流への影響は扱わない。
>   - mission を再び active にする手段も無い。done の mission に task を足しても done のまま（`cmd_add` `plan.sh:2893-`、§2.2）で、dispatcher はその mission を done とみなして Worker を退役させる（`dispatcher.sh:2446-2476`）。
> - **archive 済み mission の restore**: 無い（plan.sh のサブコマンド表 `plan.sh:7035-7059`）。Taskvia の ID / map を維持できるかは未確認。

> この節の reopen について、元の設計そのものの未解決課題を §15-1 に書いた（下流が検証待ち・検証中のときに無効化されない）。

## 6. 永続化・競合・障害回復

Mission 01のGlobal Control Lock→reload→precondition→schema validation→同filesystem temp+fsync+replace→directory fsyncを継承する。Task/Attempt/assignment更新には単一ファイルatomicだけでは不足する。

transaction intentと対象before/after hashをdurableに保存し、prepared marker後に各replace、commit markerを保存する。起動時に未完transactionをlock下でroll-forward、hash不一致はquarantineしてdispatch停止。projectionはcommitted SoTから再生成できる。recoveryが終わるまでDispatcherを開始しない。LLM呼出し・Git・Taskvia通信中はlockを保持しない。

Git準備はsaga: reserved → Git操作(intent ID付き) → preparing → context作成 → running。失敗はAttemptをfailedにし所有leaseだけ解放。既存branch/worktreeはidentityと状態を確認して再利用、他leaseの資源を削除しない。途中成果はquarantineし、dirty worktreeを自動reset/cleanしない。

全mutating APIはexecution_id + lease_epoch + expected_revision + idempotency_keyを検査。同じkey/同じpayloadは前結果、異なるpayloadはconflict。旧Workerからのdone/failや古いVerifier結果は拒否。名前だけでassignmentを消さない。外部同期はcommitted outboxをretryしevent_idで重複排除、Taskvia障害でローカル完了を巻き戻さない。

checkpointを作業開始後、意味のある変更後、submit前、PreCompact、graceful終了で保存。内容は目的・実施済み・残作業・失敗した手段・変更files・Git HEAD/dirty diff artifact・確認commands/results・注意制約・未解決Issue。保存失敗は見えるeventとし、成功扱いにしない。

hard killでも前checkpointとGit観測から`incomplete`な記録を作れること。会話の完全保存を再開必須条件にしない。gracefulは既存30秒枠を守り、小型モデル呼出しを待たず決定論的checkpointを優先。既存Handoff pathは移行期adapterから新Artifactへ参照を張り、同名worker/task上書きを避ける。

> **【現行との差 (2026-10-11 確認)】**
>
> - **multi-file journal は 01a が比べたうえで退けた方式**。対象は intent・before/after hash・prepared/commit marker・roll-forward・quarantine で、根拠は `knowledge/state-store.md:212-216` と、01c の再判断 `knowledge/execution.md:107`。
>   - 今の正しさは次の 3 つで成り立っている（§2.2「複数ファイルの更新」）: card が唯一の正本 / 書く順序を固定 / 回復 R-1〜R-5 が projection を正本に合わせる。
>   - Issue・context・artifact を足しても、Task の正本が card 1 枚のままなら同じ方式で足りる可能性がある。
>   - journal を入れるなら、state-store.md §2.1 の再判断条件「正本が 2 枚以上に分かれる」に当たることを先に示す必要がある（execution.md:107 が同じ判定をしている）。
> - **「recovery が終わるまで Dispatcher を開始しない」**:
>   - 今の回復は、plan.sh がロックを取った直後に、触る card・worker の範囲だけで走る。全体は走査しない（`state-store.md` §2.5 `:377-`）。
>   - dispatcher は回復を起こさず、待ちもしない（backlog `state-store.md:1128`）。
> - **「LLM 呼出し・Git・Taskvia 通信中は lock を保持しない」**: 済み。
>   - pull は worktree をロックの外で作る（`plan.sh:3995-4047`）。
>   - その間は、準備ロックと start の compare-and-set で守る（`plan.sh:3380-3395`・`:3468-3496`）。
> - **Git 準備の saga**:
>   - 近い形はすでにある。reserve → ロックの外で worktree を作る → CAS で start、の順で、失敗すると `WORKSPACE_CREATE_FAILED` で needs_director になる（`plan.sh:3520-3569`）。
>   - 違う点: 既存の worktree は W2 で中身を確かめずに再利用する（`knowledge/git-policy.md:125`・`:132`）。本書の「identity と状態を確認して再利用」とは違う。
> - **mutating API の検査**:
>   - あるもの: execution_id の照合と再送の冪等（§2.2）。
>   - 無いもの: lease_epoch / expected_revision / idempotency_key。
>   - 名乗りの無い報告は #291 が merge されるまで通る。
>   - 「名前だけで assignment を消さない」: `retire` は試行の ID で束縛している（`plan.sh:6741-6926`）。一方 `fail` は、card の worker の枠に加えて `AGENT_NAME` の枠も撤去する（`plan.sh:5070` `_retire_caller_slot`）。これが名前で消す経路に当たるかは、評価していない（未確認）。
> - **outbox**: 無い（`execution.md:27`）。
> - **checkpoint**: 決定論的な checkpoint は無い。
>   - watchdog は 1-2 行の状況報告を求め（`watchdog.py:132`）、60 秒待つだけ（`watchdog.py:126`）。猶予は 60 秒で、本書の「既存 30 秒枠」とは違う。
>   - Handoff は Worker が手で書く（`worker.md:786-866`）。
>   - PreCompact は実質動いていない（§2.1）。
>   - Handoff の path は mission を含まない（`git-helpers.sh:83`）。本書の「同名 worker/task の上書きを避ける」には、今も対応していない。

## 7. 3層ContextとExecution Package

L3 Evidence Storeはredactedログ、過去Attempt、decision/contract、Git evidenceを保持。L2 Task Working Contextは承認済みTask、最新Git、Issue、重要decision、最新checkpointの構造化snapshot。L1 Execution Packageは今回の目的に限定したimmutable配布物。L2/L1は派生物であり元根拠の代わりにならない。

Package必須項目:

```yaml
schema_version: 1
package_id: pkg-unique
execution_id: exec-unique
task_ref: mission/t001
task_generation: 2
task_revision: 17
source_manifest_hash: sha256-value
repo_commit: full-sha
policy_version: context-v1
required:
  goal: source-ref
  acceptance_criteria: [source-ref]
  constraints: [source-ref]
  current_state: [source-ref]
  open_findings: [source-ref]
  verification_commands: [source-ref]
selected_claims: []     # each: text, source_refs, uncertainty
omitted: []             # reason + source_ref; 無言の脱落禁止
retrieval_handles: []
budget: {}             # limit/estimated/measured/reserved
curator_provenance: {}  # adapter/model/prompt version/usage
validation: {}          # gates + diagnostics
```

必須制約・ACは原文の機械抽出で保存し、Curatorが短縮・弱めることを禁止。現状を履歴要約から断定せずrepo/Taskの現revisionで検査する。未確認のclaimは未確認として残す。Workerは開始時generation/HEAD/package hashを照合し不一致なら再構築を要求する。

> **【現行との差 (2026-10-11 確認)】** L1〜L3 と Execution Package に当たる実装は無い。今 Worker が受け取るのは次の 3 つだけ。
>
> - 起動時に注入される skill の knowledge（`start.sh:553-601`）
> - pull の JSON（`plan.sh:3453-` `_pull_result`）。handoff_path は含まない
> - card の本文

## 8. トークン予算・段階的検索

モデルwindowとeffective contextを別設定にする。`W`は実際のWorker能力、`S`はsystem/tool、`O`はoutput reserve、`H`は作業中検索/対話reserve、`M`は余裕、`E`は実験で定める有効入力上限。Package上限 `P = min(config.package_limit, E, W-S-O-H-M)`。負値または必須情報がPを超える場合は作業開始を拒否する。モデル能力不明なら承認済み保守profileを要求し、最大windowを推測しない。

初期実験値（モデルの仕様・最適値ではない）: Package 12,000 tokens、mandatory 6,000 / repo evidence 3,000 / decisions+checkpoint 2,000 / optional 1,000。未使用枠はManagerが融通できるがmandatoryを落とさない。Curator一呼出しinput 16,000/output 3,000、最大2回。追加検索は1回2,000、最大3回・合計6,000、総wall clock 60秒。上限はTask profileで明示変更、変更者と理由を監査。

1. R0: Task/AC/制約、最新Issue、最新checkpoint、HEAD・diff摘要をIDで直接取得。
2. R1: repo内symbol/pathと関連Contract/decisionをmetadata+lexical検索。候補最大20、選択最大8。
3. R2: 過去Attemptをfailure class/Issue/pathで絞り、必要spanだけ取得。必要なら後続でsemantic検索adapterを追加。
4. R3: 解決しない矛盾や不足をDirectorへ照会。全履歴を自動投入しない。

関連性→正規根拠→revision一致→鮮度で並べ、同hash重複排除。新しい要約が古い承認済みcontractを上書きしない。検索要求は `execution_id, question, desired_source_types, max_tokens`、応答は根拠span・hash・token count・不足理由。cache keyはTask generation/revision、repo SHA+dirty diff hash、source manifest、policy/prompt/adapter版。源変更でinvalidateする。

> **【未解決 (2026-10-12)】** この式は、継続セッションに既にある会話履歴の占有量を引いていない。§15-2 を参照。

各モデル固有tokenizerを優先し、不可なら保守的上界+余裕を使用しestimatedと表示。runtimeが追加注入するsystem/toolを含め計測。usageのinput/output/cache read/writeとcontext占有を混同しない。試行・Task・Missionで累積呼出し/時間/usageを予算化する。価格profile未確認なら金額計算はdisabled、価格確認日と通貨を必須にする。

> **【現行との差 (2026-10-11 確認)】** 該当する実装は無い。モデルの選び分けは `config/crewvia.yaml` の `model_per_skill`（skill ごとに決める。`config/crewvia.yaml:38`）だけで、token 予算という概念は無い。

## 9. モデル交換可能なContext Curator

Provider非依存interface:

```text
curate(request) -> CuratorResult
request: schema_version, purpose, evidence_spans, immutable_constraints,
         budget, required_fields, request_id
result: claims[{text, source_refs, uncertainty}], selected_refs,
        contradictions, missing_fields, usage, adapter_version, model_id
```

Provider adapterはauth・model ID・token計測・structured output・timeouts/rate limitsを吸収する。Haiku系候補、別小型モデル、deterministic/no-LLM adapterを同schemaで接続する。model routingはquality profileで行い、特定名称をcodeに固定しない。

Curatorへのアクセスは選択済みredacted evidenceのみ。shell/network/Git write/queue write権限は与えない。出力は未信頼JSONとしてvalidateし、存在しないsource ref、根拠不一致、必須制約の欠落、矛盾未報告を拒否。self-confidenceを検証通過条件にしない。

timeout/invalid JSON/429/サービス障害は最大1回retry（合計2回枠内）、その後deterministic extractive packageにfallback。mandatoryが入らないならneeds_human_review。高能力モデルへの昇格は明示policy内で一回だけ、予算を越える自動cascadeは禁止。Curator失敗はWorker実装Attemptのrework_countに加算しない。

config案: `context.enabled`, `curator.adapter`, `curator.model_id`(unset可), `capability_profile`, `budget_profile`, `prompt_version`, `timeout`, `fallback: extractive`, `session_isolation: legacy|fresh_context|fresh_process`。キー名は実装時の既存config schemaに合わせる。defaultはlegacy+Curator disabled、永続化は独立に導入できる。

> **【現行との差 (2026-10-11 確認)】** 該当する実装は無い。外部モデルを呼ぶ仕組みとしては Codex reviewer（`kai-review.sh`）が既にある。ただし役割は review で、結果を `done` / `needs-director` で返す点が Curator とは違う。

## 10. 検証ゲート

| Gate | 時点・判定者 | 必須条件・失敗時 |
|---|---|---|
| G0 Plan | Director/Plan Reviewer | 成果・AC・依存・リスク・予算・Git policyが確定。曖昧ならhuman review |
| G1 Context | Manager、重要Taskは独立review | schema/根拠/必須項目/予算/hash一致。失敗ならfallbackまたは停止 |
| G2 Start | Controller+Worker | lease/generation/HEAD/cwd/target repo一致、準備成功。staleなら再構築 |
| G3 Result | 既存checks+Verifier | diff scope、AC、test、秘密漏出、対象commit一致。failは新Attempt |
| G4 Integration | Reviewer+Git Policy | 必要review/CI、PR head一致、依存generation有効。mergeは別承認境界 |
| G5 Cutover | Human Gate | isolated rehearsal、backup/recovery、rollback確認。本設計では実行しない |

light/standard/strictは既存の順序を保持しrisk_flagsで下げない。新鮮なpackageの確認は全mode共通。検証証拠はexecution_id / Task generation / submitted commit / dirty diff hash（あれば）/ check versionに束縛する。検証後のcode変更で証拠を無効化し再submitする。skipは依存充足を既存互換として扱うが、strict TaskはDirectorによる理由と影響承認なしにskip不可。

Curator品質試験はconstraint recall、citation precision、contradiction detection、stale claim率、Worker AC達成率、費用/時間を測る。固定fixtureで必須制約脱落0・架空根拠0、矛盾fixture全件検出をリリース条件とする。shadowで既存方式と同じTask群を比較し、少なくとも小/長期/中断/再オープン/多mission/敵対ログを含める。単発5件の結果で全Task方針を決めない。

> **【現行との差 (2026-10-11 確認)】** 今ある仕組みとの対応:
>
> - **G0**: plan review がある（`agents/plan_reviewer.md`、skill `crewvia-plan-review`）。
> - **G1**: 無い。
> - **G2**: 近いものとして、pull の CAS（`plan.sh:3380-3395`）と worktree の準備（§2.2「pull の冪等・再開」）がある。
> - **G3**: Verifier は動いていない（§2.1）。実際には QA task（`qa_checkpoints` / `required_evidence`。`director.md:271-303`）が担っている。
> - **G4**: Codex review（`director.md:1066-1089`）と CI が担う。merge は Director がユーザーの承認を得て行う運用。
> - **light / standard / strict**（`qa-operations.md:123`）: 「実装済みの部分」と「運用されていない部分」を分けて書く。
>   - **モードの保存と引き上げは実装済み**: 順序は `plan.sh:5613` の `_MODE_ORDER` で決まる。`upgrade_mode`（`:5616-5620`）は 2 つのうち厳しい方を返す。`_apply_risk_flag_upgrades`（`:5658-5690`）は、plan review の Risk Flags を見て card の `verification.mode` を引き上げる。`cmd_review` はこれを verdict と同じトランザクションの中で呼ぶ（`:6006-6007`）。モードを下げる経路は無い。本文の「risk_flags で下げない」は、今のコードでも成り立っている。
>   - **モードに従って検証を変える運用は無い**: Verifier は動いていない（§2.1）。`agents/verifier.md:14` には standard の手順しかなく、`verifier-dispatcher.sh` はモードを読まない。
>   - **読む側の形の食い違い**: `taskvia-verification-sync.sh:49-53` は card の行頭の `mode:` を grep している。一方、上の関数は入れ子の `verification:` の下に `mode:` を書く（実際の card は `verification:` の次行に字下げ付きの `mode: strict`）。このため、この grep は書かれたモードを拾わず、既定の standard になる可能性がある。実機では確かめていない。

## 11. Git連携

Mission 01のPolicyを唯一の決定点とする。default branch `task/<mission>/<task-id>-<task-slug>`、base origin/main（なければlocal main）、PR base main、既存worktree pathを維持する。予約時にrepo identity・resolved base SHA・branch・worktree・policy版をAttemptに保存する。Task title変更でbranchを再生成せず、初回branch refをTaskに保持。

未merge Taskのretryは同branchを再利用、先行Attemptのheadをstart SHAに記録する。Attemptごとにbranchを無条件に増やさない。並行leaseは禁止。dirty成果はpatch artifactとHEADを両方保存しsecret scan、無許可のstash/reset/force pushは禁止。

merge済みTaskのreopenは旧PR/branchを書き換えず、同Taskに新generation branch（例 `task/<mission>/<id>-<slug>-r2`）と新PRを作る。新baseはPolicyで再解決し旧merge SHAをhistoryに残す。branchが削除済みでもTask/Attemptの記録は残る。未merge reopenでbase更新が必要ならrebase/merge前に衝突と証拠無効化を明示する。

Commit footer案は`Crewvia-Task: mission/t001`、`Crewvia-Execution: exec-id`、`Crewvia-Generation: 2`。footerは追跡補助で権限判定のSoTではない。PR descriptionにIssue/Task/Attempt・AC・検証結果を表示し、既存PRを更新する。

`target_dir`は既存direct/main契約を保持。追加のWorktree化を暗黙に行わず、repo realpath/remote identity/branch/dirty状態を検査し記録する。fresh Workerでも同じ直接作業rootを使用する。production/self-hosting testではこの互換動作を利用してmainを書き換えず、必ずtemp target repoを使用する。

> **【現行との差 (2026-10-11 確認)】**
>
> - **今と一致する点**:
>   - 既定の branch・base（origin、無ければ local）・PR base main（`lib_git_policy.py:43-45`・`:477-487`）。ただし PR base が決まらないときは main に倒さず止める（`git-helpers.sh:247-250`）。
>   - 「初回の branch ref を Task に保持」は済み。`task_slug` を最初の reserve で card に固定する（`lib_task_controller.py:460`）ので、title を変えても branch は変わらない。
>   - 未 merge の task の retry は同じ branch を再利用し、開始時の SHA を記録する。これも済み（`execution.md:343`・`:371`）。
> - **予約時に保存するもの**:
>   - ある: record の `git{branch, base, pr_base, worktree, head_at_start}`（`lib_execution.py:327-355`）。
>   - `base` が解決済みの SHA か ref 名かは未確認。
>   - 無い: repo identity・policy 版。
> - **merge 済み task の reopen で `-r2` branch を作ることは、今の Policy ではできない**:
>   - pattern の置換子は `{mission_slug}` `{task_id}` `{task_slug}` だけで、試行や世代ごとの branch は書けない（`knowledge/git-policy.md:252`・`:257-259`）。
>   - さらに、再 pull は登録済みの worktree を中身を確かめずに再利用する（W2。`git-policy.md:125`・`:132`）。そのままでは merge 済みの古い branch と worktree に戻ってしまう。
>   - reopen を入れるなら、Git Policy の改訂（世代の置換子など）が要る。§11 冒頭の「Mission 01 の Policy を唯一の決定点とする」とぶつかる箇所である。
> - **commit footer**: 無い。
> - **target_dir の task**:
>   - worktree を作らない（`plan.sh:3995`）。PR base は既定の main（`plan.sh:3225-3228`）。
>   - 「既存の direct/main 契約を保持」は今と一致する。

## 12. 移行と段階的実装計画

| 段階 | 依存・変更箇所 | 成果・Acceptance Criteria |
|---|---|---|
| P0 基盤 | Mission 01 Phase 0–2、plan/git/State Store | lock/journal/recovery/Execution ID、既存CLI/assignment契約維持、crashと競合試験pass |
| P1 Issue/Attempt | P0、Controller/schema/plan adapter | 旧Task読込、同Taskの2 Attempt、idempotencyとstale report拒否、task増殖しない |
| P2 reopen | P1、Director/依存判定/Taskvia outbox | verified再開・archive復元・下流無効化・cycle拒否・旧result保持 |
| P3 永続Context | P1、Worker/PreCompact/Handoff/Artifact | 同IDが別missionでも誤保存0、hard killからfresh sessionで復元、秘密redaction |
| P4 budget/retrieval | P3、Manager/tokenizer/config | mandatory超過で停止、bounded retrieval、source変更でcache無効化、no-LLMで利用可 |
| P5 Curator shadow | P4、provider adapter/eval | 二adapterの同schema、timeout/invalid/架空根拠のfallback、品質基準を満たす |
| P6 opt-in運用 | P2/P5、Dispatcher/Verifier/session policy | isolated end-to-end、旧と新missionの共存、モデル停止でも安全、rollback rehearsal |

Directorは各段階をschema/API、実装、QA、独立Reviewに分解し、同じstate/dispatcherファイルへの同時変更を避ける。P3はP2と独立して進められるが完了ゲートは個別。reserve-first通知モデル・integration branch・Watchdog全体再設計は別Missionで、本追加設計に必須でなければ延期する。

旧schemaの読込はadapterがdefaultを補う。過去の実行履歴を会話から捏造しない。必要なTaskだけ`legacy_import`の出典付きsnapshotを作り、過去Attempt数はunknownとする。`rework_count`は既存検証差し戻し数として保持しattempt_noと区別。legacy missionのdone bypassは互換mode限定、新modeではdoneをsubmitとして扱いpass前に依存を解除しない。

旧`update --reset/--status`も新modeではControllerへ委譲し、不正遷移を拒否する。`queue/assignments/<worker>`の内容はmission:taskを維持、sidecar leaseとの更新を同transactionに含める。新機能未対応のDispatcherと新mode missionを混在させない。Taskvia表示は既存statusへprojection、Attempt一覧は後続の任意UI。

rollbackは新mode受付停止→active leaseのcheckpoint/終了→journal recovery→outbox drain→backup検証。新schema recordsを保全しlegacy projectionを別runtimeで生成する。旧binaryを同じ新queueに直接戻さない。phase flagをoffにするだけで安全と扱わない。

> **【現行との差 (2026-10-11 確認)】**
>
> - **P0 の大半は Mission 01 で済んでいる**（§1 の表）。残りは次の 6 つ。
>   - (a) journal: 採らないと決めている（§6 の注記）。
>   - (b) 回復を dispatcher が起こす・待つ: 未着手（`state-store.md:1128`）。
>   - (c) lease_epoch / expected_revision / idempotency_key: 未着手。今は execution_id で代わりをしている。
>   - (d) 名乗りの無い報告の拒否: #291（merge 待ち）。
>   - (e) `update --status` を Controller に委ねる: 未着手。
>   - (f) outbox: 未着手。
> - **「rework_count を attempt_no と区別」**: すでに別の欄（`rework_count` と `execution_count`）。
> - **legacy**: `execution_count` の欄が無い古い card がある。01b の 29 枚には 1 枚も無い（§14.1 の集計で確認）。
> - **「新 mode では done を submit として扱う」**: 今は Verifier を運用していない（§2.1）。この状態で done を submit にすると、すべての task の完了が Verifier 待ちになる。先に Verifier を動かす必要がある。

## 13. QA・禁止事項・レビュー論点

試験はunique temporary CREWVIA_REPO_ROOT/QUEUE、local bare remote、stub Taskvia/providerで行う。実queue/registry、本番Taskvia、remote refs、元main checkoutにwriteしない。基盤コードが変更された場合のみ必要な既存suiteも回す。

必須scenario:

- 2 Worker同時reserveで1件だけ成功。旧lease doneで新assignmentを削除できない。
- transactionの全replace境界でkillして、復旧後Task/Attempt/assignmentが一貫する。
- prepareでGit失敗、Curator timeout、context超過を注入しlease/resource漏れを防ぐ。
- verify pass後にHEADが変わり古いpassを拒否。WorkerとVerifierの同一sessionを拒否。
- 同Taskを中断→retry→差し戻し→passし、Task数1・Attempt履歴全件保存。
- upstream reopenがpending/running/verified下流とarchive済みmissionへ正しく波及する。
- HandoffとPreCompactをfresh Workerが読み、別missionのt001を混同しない。
- 証拠内の「制約を無視せよ」、架空source、旧contract、path traversal、secretを拒否。
- tokenizer不在、model adapter停止、Taskvia長期停止でも安全にfallback/再同期できる。
- target_dir direct、branch reuse、merge後reopen、deleted branch、dirty conflictを隔離repoで確認。

レビューで確定すべき事項: 初期budget profile、retry/max_rework/総Attempt数の別上限、Issue resolved authority、下流invalidation UX、ログ保持期間と権限、shadow標本数と費用上限。初期運用案は総Attempt最大5、検証差し戻しは既存max_rework=3維持、raw redacted logs 30日・根拠manifest/decision/verificationはmission寿命中保持（採用前に承認）。未解決Issueや結果の根拠はGCしない。

完了条件は各段階のAC・障害試験・独立Reviewと変更説明が揃うこと。品質未検証のCuratorやfresh processをdefaultにしない。設計の承認と本番cutoverの承認は別であり、本書作成ではruntimeを変更しない。

> **【現行との差 (2026-10-11 確認)】**
>
> - **テストの隔離**: 規則はすでにある（`tests/CLAUDE.md`。`CREWVIA_MUX_TEST_ISOLATION` 等）。
> - **「2 Worker 同時 reserve で 1 件だけ成功」**: 同じ予約を並行で pull するケースは、01c で扱った（`knowledge/execution.md` §6.1 `:673-`）。
> - **「旧 lease の done で新しい assignment を削除できない」**:
>   - 名乗りがあれば `EXECUTION_NOT_CURRENT` で拒否される。
>   - 名乗りが無い報告は、#291 が merge されるまで通る。

## 14. 段階の取捨の材料 (2026-10-11 追加)

ここでは取捨を決めない（Director とユーザーが決める）。今の運用で実際に起きた困りごとと、各段階に残っている作業量の目安だけを並べる。

### 14.1 今の運用で起きた困りごと

**(a) 修正のたびに新しい task を足す運用**

直近 12 mission の task を分類した結果を下に示す。対象は `queue/archive` と `queue/missions` のうち名前が `2026` で始まるディレクトリで、名前順の末尾 12 件。この mission と `*overwritten*` は除いた。結果は 12 件ともすべて archive にあった。

分類の規則（card の frontmatter の title と skills だけで判定）:

| 分類 | 条件 |
|---|---|
| Rcodex | skills に `codex-review` があり、title に「再突合」を含む |
| Rreview | skills に `review` があり、title が `再レビュー\|再々レビュー\|再確認\|差分レビュー` に一致する |
| Rqa | skills に `qa` があり、title が `QA 追加\|QA 再実行\|QA 追補\|再 QA\|QA: PR #` に一致する |
| Fcodex | 上のどれでもなく、title が `fix \d+ 巡目\|Codex (P\d\|\d+ ?巡目)` に一致する（Codex 指摘の修正） |
| Fother | 上のどれでもなく、title に「追加」を含む |

| mission | 総数 | Fcodex | Fother | Rcodex | Rqa | Rreview | execution_count>1 |
|---|---|---|---|---|---|---|---|
| 20261001-vnext-01b-git-policy | 29 | 7 | 0 | 0 | 0 | 0 | 0 |
| 20261001-vnext-01c-execution-controller | 36 | 10 | 0 | 0 | 0 | 0 | 7 |
| 20261003-t020-obs | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| 20261004-watchdog-hard-idle-unknown | 18 | 4 | 1 | 4 | 1 | 0 | 1 |
| 20261005-director-escalation-telegram | 31 | 6 | 4 | 6 | 0 | 3 | 3 |
| 20261006-kai-review-full-findings | 7 | 0 | 2 | 0 | 0 | 1 | 1 |
| 20261008-e5-reject-unnamed-reports | 13 | 3 | 2 | 0 | 2 | 0 | 3 |
| 20261008-pull-reject-empty-agent | 5 | 1 | 0 | 0 | 0 | 0 | 3 |
| 20261009-e5-env-and-backlog | 11 | 1 | 1 | 0 | 1 | 0 | 3 |
| 20261009-e5-pr2-reject-unnamed | 7 | 2 | 0 | 0 | 1 | 0 | 1 |
| 20261009-telegram-escalation-p3 | 16 | 3 | 0 | 0 | 2 | 0 | 4 |
| 20261011-small-backlog-agent-flag-and-suppression-hole | 24 | 8 | 2 | 0 | 4 | 0 | 4 |
| **合計** | **199** | **45** | **12** | **10** | **11** | **4** | **30** |

- Codex 指摘の修正 task（Fcodex）は 45 / 199 件。再突合の codex-review task（Rcodex）を足すと 55 件。
  - mission の card には「53 件」とあったが、数え方が違う。上の規則で数えると 45〜55 件になる。
  - Fcodex の規則は title だけを見るので、description でしか Codex 由来と分からない task は Fother に入る（例: 1011 の t016・t017）。そのため Fcodex は下限寄りの数字である。
- レビューの後に足した task（F と R の合計）は 82 / 199（41%）。
- 同じ card を開け直した痕跡（`execution_count` > 1）は 30 枚。最大は 20261011-small-backlog-… の t005（codex-review）の 6 回。
  - 01b には `execution_count` の欄自体が無い（01c より前の mission）。
- 再現: 上の規則をそのまま書いたスクリプトで、2026-10-11 に同じ数を得た。

**(b) done になった mission に task を足しても done のまま**

- 2026-10-11 に、設計 PR #295 の後で実装 task を足す前に mission が done になった。dispatcher が「全ミッション完了」で idle Worker を退役させた。対処は mission.yaml を手で戻すこと（Director の運用記録による。リポジトリ内に痕跡は確認していない）。
- 原因は 2 つ: `cmd_add` が mission の status を戻さないこと（`plan.sh:2893-`）と、全 mission が done のときの退役（`dispatcher.sh:2446-2476`）。

**(c) Worker が無言で止まり、未 commit の作業を引き継いだ**

- 2026-10-04 Luna t016、2026-10-05 Ren t020、2026-10-11 Luna t019 の 3 回起きた。いずれも hard_idle で回収された（Director の運用記録による）。
- 2026-10-11 の t019 では、tab が消えた Worker の未 commit の作業を Director が patch に保存し、次の Worker に `git apply` させた（`queue/archive/20261011-small-backlog-agent-flag-and-suppression-hole/tasks/t019.md:46-48`、patch は同じディレクトリの `t019-wip.patch`）。
- 2026-10-05 の t020 は worktree のまま引き継いだ（`queue/archive/20261005-director-escalation-telegram/tasks/t020.md:38-41`）。

**(d) PreCompact の保存が実質動いていない**

コードからの推定で、実機のログは未確認（§2.1）。

**(e) Handoff の path が mission を含まない**

衝突しうる（`git-helpers.sh:83`）。実際に衝突した事例は見つけていない（未確認）。

**(f) 回復を誰も起こさない窓**

pull の途中で落ちた card は、次に plan.sh が触るまで食い違いのまま残る（`state-store.md:1128`、01a の QA F2）。本番で起きた事例は見つけていない（未確認）。

**(g) 名乗りの無い報告**

古い Worker の報告が、新しい試行に対して通りうる。E5 の動機で、#291 で塞ぐ（merge 待ち）。

**(h) Director の判断待ち**

監査ログ（`queue/audit/transitions-*.jsonl`、2026-09-30 以降）の集計は次のとおり。

| 集計の対象 | 件数 |
|---|---|
| 上の 12 mission での `op=needs-director`（`result=ok`） | 80 件（全 mission では 89 件） |
| 上の 12 mission での `update` による needs_director → pending / in_progress | 56 件 |

差し戻しには理由も世代も残らない（§2.1 `cmd_update`）。

### 14.2 段階ごとの残り

作業量の目安は「小 = 1〜2 PR / 中 = 3〜5 PR / 大 = 6 PR 以上 + 運用規則の変更」。実際の PR 数ではなく、目安である。

| 段階 | 今の状態 | 残っている作業の目安 | 対応する困りごと (§14.1) | 注意 |
|---|---|---|---|---|
| P0 基盤 | 大半は済み（Mission 01） | 小〜中: #291 系の merge、回復を dispatcher が起こす・待つ、`update --status` を Controller に委ねる、outbox | (f)(g) | journal は 01a が退けた方式（§6 の注記）。入れるなら再判断条件を示す |
| P1 Issue/Attempt | 試行（Execution）は済み。Issue・revision・Attempt の語彙の拡張・Director の運用規則は未着手 | 大: schema と Controller の拡張、plan adapter、Director の「修正は同じ task の retry」規則、Codex 指摘 1 巡ごとの扱い | (a)(h) | `generation` の語の衝突（§4 の注記）。過去の試行の Result 本文が残らない（§4 の注記） |
| P2 reopen | 無い（`update --reset` が近い） | 中〜大: 世代、下流の無効化、mission の再活性化、archive からの restore、Git Policy の世代の置換子 | (a)(b) | (b) だけなら「add で mission を戻す」小さな単独修正でも直せる |
| P3 永続 Context | Handoff（手書き）と PreCompact（実質動いていない）だけ | 中: 決定論的な checkpoint・dirty diff の artifact・handoff path に mission を含める・PreCompact に task を届ける | (c)(d)(e) | P2 と独立に進められる（本書 §12 と同じ） |
| P4 budget/retrieval | 無い | 大: tokenizer・予算・段階的検索・cache の無効化 | 対応する事故の記録は、本調査では見つけていない（未確認） | モデルの外部事実は未確認のまま |
| P5 Curator shadow | 無い | 大: provider adapter・評価 fixture・shadow 比較の費用 | 同上（未確認） | 費用の上限はレビューで決める（§13） |
| P6 opt-in 運用 | 無い | 大: isolated end-to-end・新旧の共存・rollback の rehearsal | — | Verifier を運用していないこと（§2.1）が、done = submit の前提を崩す |

## 15. レビューで見つかった元の設計の未解決課題 (2026-10-12 追加)

方針: レビュー（Codex など）で元の設計（§3〜§13 の本文）の穴が見つかったら、本文は書き換えずにこの節へ 1 項目ずつ積む。解決は段階の取捨の後の設計 task で行い、この文書のレビューの往復は続けない。

### 15-1. reopen の下流の無効化に、検証待ち・検証中の下流が含まれていない (PR #297 Codex P2、t002)

**漏れている状態**
- §5 の reopen は、下流の扱いを次の 3 つしか定義していない。
  - pending の下流: blocked にする。
  - running の下流: cancel_requested にし、lease を fence する。
  - verified の下流: invalidation event を出し、needs_human_review にする。
- submit の後の下流は、この 3 つのどれにも当たらない。このとき下流は「Attempt が submitted、Task が ready_for_verification / verifying、Worker lease は解放済み」の状態にある。
- そのため、この間に上流を reopen すると、下流は無効化の対象にも fence の対象にもならない。

**起きる害: 古い上流に基づく結果が承認されうる**
- §5 の verify pass の条件は「submitted、commit/package/generation 一致、AC 充足」である。
- 比べているのは下流自身の commit / package / generation で、上流の reopen ではどれも変わらない。
- このため、reopen 前の上流を前提にした下流の結果を Verifier が pass できてしまう。

**直し方の方向（Codex の提案。まだ決めていない）**
- submitted の Attempt（Task が ready_for_verification / verifying）について、上流の reopen 時に Verifier の lease を無効化する規則を定める。
- 検証を確定する時点（verify pass / fail の受付）で、依存先の世代と検証の有効性を確かめ直す。§5 の「Controller の eligible predicate で generation と検証有効性を検査する」を、検証の確定にも適用する。

**今の実装との関係**
- reopen も Verifier の運用も今は無い（§2.1、§5 の注記）。
- このため、今の運用でこの害が起きることはない。P2（reopen）を設計するときに解く。

### 15-2. §8 のトークン予算が、継続セッションの既存履歴を計上していない (PR #297 Codex P2、t002)

**漏れているもの**
- §8 の `P = min(config.package_limit, E, W-S-O-H-M)` は、`S`（system/tool）、`O`、`H`、`M` を引くが、Worker のセッションに既に積まれている会話履歴（過去の会話・ツール結果・前の Package）の占有量を引く項が無い。
- §9 の `session_isolation` は既定が `legacy` で、同じセッションを使い続ける。このため、継続セッションでは 2 件目以降の Task の開始時点で、履歴がすでに window の一部を占めている。

**起きる害**
- Package が `P` に収まって §8 の検査を通っても、既存履歴と合わせた実際の入力が window を超えうる。
- 超えた場合は、予期しない自動圧縮（Package の mandatory 部分が要約に巻き込まれうる）か、入力の拒否が起きる。どちらも §8 の「mandatory を落とさない」を守れない。

**直し方の方向（Codex の提案。まだ決めていない）**
- 既存履歴の占有量 `X` を `P` の式から差し引く（`W-S-O-H-M-X`）。
- `X` を計測できないとき、または差し引いた残りが必要量に足りないときの動作（作業開始の拒否・圧縮・新規セッション（`fresh_context` / `fresh_process`）への切り替え）と、その切り替え条件を定義する。

**今の運用との関係**
- 今の Worker は同じセッションで続けて Task を pull する。`agents/worker.md:117-133` のループは、done の後に待機して Dispatcher の次の assign で `plan pull` に戻り、終了するのは「タスクなし、shutdown」を受けたときだけである。
- ただし今は Package も §8 の予算も無い（§8 の注記）。このため、今の運用では「予算を通ったのに超える」害は起きない。Context 層（P1 以降）を入れるときに解く。
