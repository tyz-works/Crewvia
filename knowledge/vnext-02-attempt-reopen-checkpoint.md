# vNext 02 — Attempt 履歴 (P1)・reopen (P2)・checkpoint (P3) の実装設計

作成日: 2026-10-14。状態: 設計（未実装）。mission `20261014-vnext-02-attempt-reopen-checkpoint` t001。
元の提案: `knowledge/vnext-task-attempt-context.md`（以下「元文書」。§12 の P1・P2・P3 を、今の main に載る形に落とす）。

- 根拠のコードは **main `7f2638c`**（#300、2026-10-14）の行番号。`scripts/plan.sh:N` のように書く。確かめていないことは「未確認」。
- ユーザー決定（2026-10-14）: P1 + P2 + P3 を進める。P0 の残り・P4〜P6（budget / Curator / opt-in 運用）は対象外。
- **実装の PR は、未 merge の #291 → #293 → #292（E5 PR-2。2026-10-15 20:33 JST 以降に merge 予定）が入った後の main に載せる**。#298（`add` / `update` の deliverable lint）も別に未 merge。各 PR がどの未 merge PR と同じ箇所を触るかは §6 に表で示す（`gh pr diff` で確かめた）。
- この文書は文書だけの PR。コードは変えていない。

## 0. 要約

| 段 | 決めたこと |
|---|---|
| P1 Attempt 履歴 | 試行（Execution）は 01c で済んでいる。足りないのは (a) 試行ごとの Result・終わり方・head が card で上書きされて残らないこと、(b) 「Codex の指摘の修正」「QA の差し戻し」を新 task でなく同じ task の次の試行として開く手段。**`queue/missions/<m>/history/tNNN/` に、遷移ごとに 1 ファイルを足す台帳**を作り（上書きしない）、`plan.sh retry` を足す。総試行の上限は `max_attempts`（既定 5〜6。§7 の判断 2）。語の衝突を避けるため、reopen の回数は `generation` と呼ばず **`reopen_count`** にする |
| P2 reopen | `plan.sh reopen` を足す（done / verified / skipped → pending）。**依存先が pending のままなら、依存の規則（`lib_dep_rules.py`）が何も変わらなくても自動で待つ**。cascade は原子的ではなく、**下流 → 最後に I の順に書いて、途中で止まったら同じコマンドの再実行で完了させる（roll forward。§4.3.1）**。足りないのは進行中・検証待ち・検証中・完了済みの下流で、これは `--cascade` が**試行を閉じる**ことで無効化する（元文書 §15-1 の答え）。mission を active に戻す修正は小さいので**単独の先頭 PR**。archive の restore は**やらない**（需要の記録が無い） |
| P3 checkpoint | 退役の前に worktree の dirty diff（と、ペインが生きていれば capture）を `registry/checkpoints/` に保存し、次の試行の `pull` が `previous_attempt` として渡す。**保存の失敗は退役を止めない**。PreCompact が動かない原因は `AGENT_NAME` / `CREWVIA_AGENT_NAME` の食い違いと task の特定で、`plan.sh snapshot --self` で直す。handoff の path に mission を含める |
| PR | 8 本（A〜H）+ 任意 2 本。どれも 300KB よりずっと小さい見込み。独立して先に出せるのは A（mission を active に戻す）と C（PreCompact） |

## 1. 現状の確認（7f2638c）

元文書 §2 の表は 2262728 の時点。そのうち、この設計が使う事実を 7f2638c で取り直した。

1. **試行は 1 枚の card に 7 欄で持つ**。`current_execution_id` / `execution_status` / `execution_end_code` / `execution_count` / `execution_reserved_at` / `execution_agent` / `task_slug`（`lib_execution.py:85-88`）。attempt 番号は `execution_count + 1` をロックの中で決める（`lib_task_controller.py:458`）。record（`executions/<id>.json`）は projection で、判断には使わない。
2. **Result は上書きされる**。`fail` は Result を `FAILED — head: … — handoff: …` で置き換える（`plan.sh:5041-5055`）。`done` も同じ。前の試行の Result は card に残らない。残るのは監査ログの 1 行（理由・本文は出さない）と、凍結された record（Result は持たない）だけ。
3. **`update --reset` は理由も世代も残さない**。`reset_task` が試行を `RESET_BY_DIRECTOR` で閉じ、遷移元を問わず pending にする（`lib_task_controller.py:703-720`）。同時に `handoff_path` / `fail_head` を消す（`plan.sh:6568-6572`）ので、前の試行の引き継ぎは card から消える。
4. **mission は done のまま戻らない**。`done` の D5 だけが `mission.status = 'done'` を書く（`plan.sh:4905-4910`）。`cmd_add` は mission の status を戻さず（`plan.sh:3012-3013` は `next_task_id` を進めて保存するだけ）、dispatcher は active な mission が全部 done なら idle の Worker を退役させる（`dispatcher.sh:2446-2476`）。mission の status は `drafting` / `ready` / `reviewing` / `in_progress` / `done` が書かれる（`plan.sh:4908`・`:5829`・`:5996`・`:6034`）。**done の前が何だったかは card に残らない**（戻す値は未確認。§3.1）。
5. **依存の判定は 1 か所**。`lib_dep_rules.py` の `card_dependencies()` / `unmet_dependencies()`。pending の下流は、上流が終端でなくなれば自動で unmet になる（`done_ids` を毎回 status から作るため。`plan.sh:1425`・`:2439`）。
6. **下流の PR 番号の伝播**: `propagate_pr_number` は未設定の `pr_number` にだけ書く（`plan.sh:4556-4600`）。違う値が既にあれば `done` を拒否する（`pr_propagation_conflicts` `:4603-`）。
7. **PreCompact が card に書けない原因は 2 つ**。(i) hook が読む `CREWVIA_TASK_ID`（`hooks/pre-compact.sh:22`）を `start.sh` の `ENV_EXPORTS`（`start.sh:786`）が渡さない。(ii) hook が agent 名を `CREWVIA_AGENT_NAME` から読む（`pre-compact.sh:24`）が、`start.sh` が渡すのは `AGENT_NAME`。(ii) は元文書に無かった発見。実機のログは未確認。
8. **退役**: watchdog の `terminate` は Worker に 1-2 行の報告を頼み 60 秒待つだけ（`watchdog.py:132`・`:963-975`）。worktree の未 commit の作業を保存する処理は無い。
9. **handoff の path** は `registry/handoffs/<agent>/<task_id>_HANDOFF.md`（`git-helpers.sh:83`）。mission を含まない。

## 2. 用語

- **試行 (Attempt)** = Execution。新しい ID の発行系は作らない（元文書 §4 と同じ）。
- **`reopen_count`**（新しい card の欄）= その task を done / verified / skipped から開け直した回数。元文書の `generation` に当たるが、**`generation` の語は使わない**: 既に `started_at` を「実行世代」と呼ぶ `now_generation`（`plan.sh:502`）と監査ログの `generation` 欄がある。`execution_count`（試行数）・`rework_count`（verify-result の差し戻し）・`reopen_count` の 3 つは別の数で、足し合わせない。
- **台帳 (history)** = 試行と遷移の追記専用の記録（§3.2）。

## 3. P1: Attempt 履歴

### 3.1 何を土台にして何を足すか

土台は 01c の 7 欄。**足す card の欄は `reopen_count`・上限を上書きする `max_attempts`（任意）・Result の帰属を持つ `result_execution_id`（§3.2）だけ**。試行ごとの内容は card に積まず、台帳に置く（card に積むと、Result 中の `## ` 見出しが末尾の節として持ち越される問題と、card 全体の書き戻しが節の編集でも全欄の書き込みになる問題を、どちらも踏む）。

### 3.2 台帳 `queue/missions/<m>/history/tNNN/`

- **1 遷移 = 1 ファイル**。名前は `<seq4>-<kind>-<execution_id>.md`（`seq4` はその task の台帳の通し番号。ゼロ埋め）。**既存のファイルを上書きも追記もしない**（競合しないので、1 本の追記ファイルより壊れにくい）。
- `kind`: `end`（試行が終わった）/ `retry` / `reopen` / `reset` / `retire` の 5 つ。
- 中身は frontmatter + 本文。frontmatter: `execution_id` / `attempt` / `end_code`（`end` のとき）/ `ended_at` / `head`（分かれば）/ `pr_number` / `worker` / `actor` / `reason`（`retry` / `reopen` / `reset` のとき必須）/ `checkpoint_dir`（§5。あれば）。本文: `end` のときだけ、その試行が書いた Result 全文。
- **書く場所は Controller の `_finish`（`lib_task_controller.py:564-`）の 1 か所**。報告の 6 コマンド・retire・reset・G1 の fail は全部ここを通る（元文書の表の「試行を持つ報告と予約は、すべて Controller を通る」。`update --status` だけは通らない — §3.6）。**台帳に入れる Result は `_finish` の `body_out` の `## Result` 節から取る**。`_finish` の引数 `body`（`lib_task_controller.py:564`）は `_load` が更新前に読んだ card 本文（`:647`・`:676`）で、今回の報告は入っていない。報告した Result は plan.sh が組んだ本文（done: `plan.sh:4878`、fail: `:5051`、needs-director: `:3550`・`:6909`）として `body=` で渡り、`_finish` では `new_body` になる。`body_out = body if new_body is None else new_body`（`:579`）が `txn.write_card` に渡る本文そのもの（`:599`・`:624`）なので、台帳はこの `body_out` を、同じ呼び出しの `write_card` の直前に書く。`new_body` が無い操作（retire・reset）の `body_out` は旧本文だが、**旧本文の Result が今の試行のものとは限らない**（試行 1 が done → reopen → 試行 2 を pull → 報告前に reset、の旧 Result は試行 1 のもの。`reserve_task` / `start_execution` は本文を保持する）。
  - **帰属は card の欄 `result_execution_id` で持つ**（足す card の欄は `reopen_count` / `max_attempts` に加えてこの 1 つ。§3.1）。Result を書く操作（`new_body` がある `_finish`）が、その試行の `execution_id` をこの欄に入れる（本文と同じ書き込み）。
  - **台帳の `end` に Result 全文を入れるのは `result_execution_id` == 閉じる試行の `execution_id` のときだけ**。違う・欄が無いときは本文を空にし、frontmatter に `result: none`（試行は Result を書かずに終わった）を書く。旧 Result は、それを書いた試行の `end`（その試行が閉じたときに台帳へ写してある）にだけ残り、別の試行の履歴には現れない。欄が無い既存の card（この設計より前に done になったもの）は帰属が分からないので `result: unattributed` として、旧本文の Result を入れずに `reopen` / `retry` の理由だけを残す。
  - **消さずに欄で束縛する理由**: reopen / retry の時点で本文の Result を消す案は、本文を書き換える card 書き込みが 1 回増える（節だけの編集でも card 全体の書き戻しになる問題を踏む）うえ、消す前に台帳へ写し終えたことの確認（台帳 → card の順の延長）が要る。欄で束縛すれば本文は触らず、`pull` が渡す `previous_attempt` も同じ欄で「前の試行の Result」と明示できる。
- **書き込みは `lib_state_store` の `Txn` に新しい 1 メソッド（`write_history`）を足して通す**。`tests/test_queue_writes_go_through_the_store.py` の許可表に 1 行足す。plan.sh や Controller から直に `open()` しない。
- **順序: 台帳 → card**（派生値は正本より前。card がコミット点）。card の書き込みが失敗しても、台帳に残るのは「まだ起きていない遷移」の 1 ファイル（孤児）。孤児の台帳は害が無く、回復（R-1〜R-5）の対象にしない。`store-check` が「card に対応する遷移が無い台帳」を**報告だけ**する。
- **再送の冪等（孤児の台帳があるとき）**。名前には `seq4`、中身には `ended_at` が入るので、再送で採番や時刻を作り直すと別ファイルが増える・内容が食い違う。次の規則で**既存イベントを見つけて再利用**する（全部ロックの中。ロックが直列化するので採番は競合しない）:
  1. **イベントの同定キー = (`execution_id`, `kind`, `reopen_count`)**。`reopen_count` はその操作を始めた時点の card の値（card がコミット点なので、card が更新されていない再送では同じ値）。ファイル名の `<kind>-<execution_id>` で候補を絞り（`history/tNNN/*-<kind>-<execution_id>.md`）、frontmatter の `reopen_count` で確定する。`retry` / `reopen` / `reset` は新しい試行を作らず `execution_id` が前の試行のものなので、`reopen_count` を足して「同じ ID の 2 回目の reopen」と区別する。
  2. **見つかったら `seq4` と `ended_at` をそのファイルから読んで再利用**し、続く card と record の書き込み（`_write_record(..., ended_at=)` は `lib_task_controller.py:625`）にも**この `ended_at`** を渡す（再送のときの `now` は使わない）。台帳は書き直さない。
  3. **見つからなければ** `seq4` = 台帳の最大 + 1、`ended_at` = 今。
  4. **再送で比べる欄**: `execution_id` / `attempt` / `end_code` / `head` / `pr_number` / `worker` / `actor` / `reason` / `checkpoint_dir` と本文（Result 全文）。**除くのは `seq4`（名前）と `ended_at` だけ**。全部同じなら台帳は何も書かず card へ進む。1 つでも違えば exit 3（`HISTORY_MISMATCH`、card は書かない。違う欄の名前だけを出し、値は出さない）。つまり**孤児ができた後は、同じ報告を送り直すことで先へ進む**。Result を直して送り直したいときは、同じ報告で card を先に通してから、次の試行・`reopen` で扱う（孤児を差し替える手段は作らない。§8）。
  5. 同定キーに合う候補が 2 つ以上あるのは台帳の破損（採番が競合しないので通常は無い）。`HISTORY_AMBIGUOUS` で止め、`store-check` が報告する。
- **台帳が書けなかったとき**: 遷移を止める（exit 1、何も書かない）。履歴を失ったまま進めるのは、この設計の目的に反する。ただし retire だけは、`plan.sh retire` が `RETIRED` で閉じる出口なので、台帳が書けなくても閉じる（警告を出し、`reason` に「台帳なし」を card ではなく監査ログの `result` に残す）。**この例外の根拠は「退役が止まると誰も窓を閉じない」（`daemon-authority.md` §5）**。
- 容量: Result は今も card に全文が載っているので、台帳は card と同じ量が試行の数だけ増える。1 task あたりの上限（`max_attempts` 5〜6）× Result の大きさ。mission を archive すると `history/` ごと動く（`_move_mission_dir`。`plan.sh:6093-6126`）。
- 読み口: `plan.sh history <id> [--mission <slug>]`（読み取り専用・ロックなし。台帳が読めないファイルを含むときは、そのファイルを飛ばさず `Unreadable` 相当で止まる — 空に潰さない）。`pull` の JSON に `previous_attempt`（§3.5）を足す。

### 3.3 retry（`plan.sh retry <id> --reason "<理由>" [--mission <slug>]`）

「同じ成果の、未達の作業をもう一度」。**新しい task を足さない**。

- 遷移元: `failed` / `needs_director` / `needs_human_review` / `verification_failed`（今の `ACCEPTS_FROM` の語彙。`lib_task_status.py`）。遷移先: `pending`。
- 試行が active（reserved / running）なら拒否（exit 3）。先に `update --close-execution` か `reset`。
- 保つもの: `pr_number`・`task_slug`（= branch 名。`lib_task_controller.py:460`）・`execution_count`・`rework_count`。消すもの: `worker` / `started_at` / `completed_at`。**`handoff_path` と `fail_head` は消さず、台帳の `end` に写してから card の欄を消す**（今の `update --reset` は写さずに消す。§1-3）。次の `pull` が `previous_attempt` で渡す。
- 理由は必須（空・制御文字は拒否）。理由は台帳にだけ書き、監査ログには出さない（監査ログは理由の本文を持たない方針。`lib_state_store.py:1231-1268`）。
- 上限: `execution_count >= max_attempts` なら exit 3（`ATTEMPT_CAP`）。上書きは `--over-cap "<理由>"` で、台帳の `retry` に残る。**上限の検査は retry / reopen の入口だけ**に置き、`reserve_task` には置かない — 既に `execution_count` が 6 の card がある（元文書 §14.1。20261011-small-backlog… の t005）ので、reserve に置くと今の card が pull できなくなる。
- 実装は Controller に `reopen_task(kind='retry')` を 1 つ足す。**`update --reset` の代わりに retry を使う運用**に変えるが、`--reset` は消さない（互換。§6 の PR-H で director.md に「理由が要る差し戻しは retry」と書く）。

**PR-E（t031）で実装して決めたこと**（上の本文との差・追加）:

- **拒否は 3 つの新しいコード（どれも exit 3・何も書かない・`refused:` の監査行 1 本）**: `ATTEMPT_ACTIVE`（試行が reserved / running。DETACHED の active も同じ）・`RETRY_NOT_ALLOWED`（遷移元でない status、または試行の記録が無い legacy の card — 台帳に写す試行が無いので `update --reset` に誘導する）・`ATTEMPT_CAP`。判定の順は 試行の記録 → active → status → 上限。`INVALID_TRANSITION`（exit 2）は使わない（「それ以外は exit 3」の指示）。
- **`needs_human_review` の試行は running のまま**（`verify-result needs_human_review` は試行を変えない。`mark_task`）なので、**その状態の card は active として拒否される**。つまり retry が通る `needs_human_review` は、先に試行を閉じた（`update --close-execution` 等で terminal になった）ものだけ。遷移元の 4 つは語彙として `ACCEPTS_FROM['retry']` に持つが、実運用で最も通るのは failed / needs_director。試行を retry が自分で閉じる（RESET と同じ扱いにする）案は、新しい終了コードと枠の撤去が要るので PR-E では採らなかった。
- **証拠の写し**: PR-D の `end` の台帳は `head`（= `fail_head`）と `reason`（handoff の有無）を持つが、**`handoff_path` の値は持たない**。そこで retry の台帳に `handoff_path` と `head` を写してから card の `handoff_path` / `fail_head` / `fail_head_waiver` を消す（`update --reset` が消す 3 欄と同じ）。台帳の frontmatter に任意の欄 `handoff_path` / `over_cap` を足した（再送で比べる欄にも入る）。**handoff のファイルは退避しない**（`update --reset` は `.stale-` に改名するが、retry の `previous_attempt.handoff_path` が指すファイルなので置いておく。同じ固定パスに次の Worker が書き直す）。
- **台帳の retry は `end_code` に終わった試行の `execution_end_code` を写す**（`WORKER_FAILED` 等）。次の `pull` の `previous_attempt` が原因を出せる。`attempt` は終わった試行の `execution_count`。
- **`previous_attempt`**: 同じ試行の台帳が結果（`end` / `reset` / `retire`）と retry の両方を持つとき、`history_path` は**結果の台帳**（Result 全文がある方）を指し、retry の台帳から `handoff_path`（無ければ `head` も）と **`retry_reason`**（Director の理由）を補う。結果の台帳が無い（PR-D より前に終わった）試行は retry の台帳を指す。
- **理由の検査**: 空・空白だけ・制御文字（改行・タブを含む）・501 文字以上は拒否（`--reason` / `--over-cap` とも。ロックの前に検査して `UsageExit`）。長さの上限は台帳の frontmatter に 1 行で入れるための実装上の値。
- **再送の冪等**: 同定キー (execution_id, `retry`, reopen_count)。台帳だけ書けて card が書けなかった孤児のあと同じ retry を送ると、新しいファイルは増えない。理由が違えば `HISTORY_MISMATCH`（出口は同じ理由で送り直す）。**限界（未解決）**: retry が成功した後、pull せずに `update --status failed` で手で戻して再び retry すると、同じ試行・同じ reopen_count の `retry` を「再送」と見なす（同じ中身なら何も書かず通り、理由が違えば `HISTORY_MISMATCH`）。通常の運用（retry → pull → 新しい試行）では起きない。PR-F の `reopen` が `reopen_count` を足すので、同じ試行を何度も開け直す運用は reopen の側で解く。
- **mission の再活性化**: retry の後に `_reactivate_done_mission` を呼ぶ（`update --reset` と同じ。done の mission の task が pending に戻るので）。
- **worktree の作り直し（§3.4 の QA 項目）は未確認**。retry は worktree・branch に触れない。`worktree_gc` が隔離した worktree を pull が作り直せるかは QA で確かめる。

### 3.4 pr_number・branch・worktree

- `pr_number`: 残す。同じ PR に追加 commit を積む運用と一致する。`done --pr <N>` で別の番号を出すと既存の拒否（`plan.sh:4757-4761`）に当たる — retry で新しい PR を作りたいときは Director が `update --pr-number` で明示的に変える（今のまま）。
- branch: `task/{mission_slug}/{task_id}-{task_slug}`（`lib_git_policy.py:44`）。`task_slug` は最初の reserve で card に固定されるので retry でも変わらない。同じ branch を再利用し、開始時の SHA を新しい試行の record に残す（01c で済み。`execution.md:343`・`:371`）。
- worktree: 登録済みの worktree を中身を確かめず再利用する（W2。`git-policy.md:125`・`:132`）。別の Worker が前の Worker の未 commit の作業の上で始まる。これは retry では**望ましい**（作業の継続）が、`worktree_gc.py` が完了済み task の worktree を隔離している場合は worktree が無い。**retry / reopen の後に pull が worktree を作り直せるか（branch が残っていれば作れるか）は未確認** — PR-E の QA 項目にする。
  - **PR-E QA（t033）の結果**: `worktree_gc` で隔離済みの task は、retry / reopen の後の pull が W5 で `needs_director` になり**試行を 1 つ消費する**。隔離された worktree を作り直さない。**retry / reopen の前に `worktree_gc --restore` で戻す**（plan.sh は worktree を見ないので、reopen の出力には出ない）。

### 3.5 下流（codex-review / qa の task）との関係

今の運用は、修正のたびに fix task と再確認の task を新しく足す（元文書 §14.1: レビュー後に足した task が 82 / 199）。retry / reopen を入れた後の運用:

1. 実装 task I（deliverable: pr）の下流に review R と QA Q が `blocked_by: [I]` で付いている。
2. R が指摘（P1/P2）を出す → Director は **I を `reopen --cascade reviews --reason "Codex 1 巡目 P2: …"`**（I が done の場合。§4）。I は pending、R と Q も done なら pending に戻る。
3. **I が pending の間、R と Q は依存で待つ**（§1-5）。「QA の再確認が後の fix 回より先に pull される」問題（memory: qa-recheck-pulled-before-later-fix-tasks）は、blocked_by を後から足せないことが原因だった。今回は blocked_by が元から付いたままなので起きない。
4. I が再び done → R・Q が pull できる。R の `pr_number` は既に設定済みで、I が同じ PR 番号で done する限り衝突しない（§1-6）。

`pull` の JSON に足す `previous_attempt`: `{execution_id, attempt, end_code, ended_at, head, history_path, checkpoint_dir}`（無ければ欄ごと省く）。R・Q の Worker は自分の台帳（前回の指摘・前回の FAIL）を `history_path` から読める。

### 3.6 `update --status` は通さない

`update --status` は Controller を通らず語彙の検査だけで書く（`plan.sh:6430-6433`）。このため台帳が書かれない。**P1 では塞がず、`update --status` を台帳が書かれない経路として文書に明記する**（元文書 §12 の P0(e) `update --status` の Controller 化は P0 の残りで対象外）。台帳は「Controller を通った遷移の記録」であり、`store-check` が card の status と台帳の最後の遷移が食い違う task を**報告だけ**する（直さない）。

### 3.7 総試行数の上限

`max_attempts` の既定は **6**（§7 の判断 2。元文書 §13 の案は 5 だったが、実測で `execution_count` の最大が 6 なので、5 にすると今の運用で `--over-cap` を常用させる）。`config/crewvia.yaml` に `attempts.max_total`、card に `max_attempts`（任意）。差し戻し（verify-result）の上限は今の `max_rework`（既定 3。`plan.sh:528`）のまま、**別の数として並べる**。

**PR-E（t031）で実装した**: 検査は `plan.sh retry` の入口（Controller の `reopen_task`）だけ。`execution_count >= 上限` で `ATTEMPT_CAP`（exit 3・何も書かない・監査ログに `refused:ATTEMPT_CAP` の 1 行）、`--over-cap "<理由>"` で通し、理由を台帳の retry の `over_cap` に残す（上限に達していないときの `--over-cap` は記録しない — 越えていない）。上限の読み順は card の `max_attempts` → `config/crewvia.yaml` の `attempts.max_total` → 6。**card の `max_attempts` が正の整数でなければ `STATE_INVALID`（既定値に倒さない — 上限を黙って変えることになる）**、config の値が不正なときは警告して既定値 6（上限は安全弁で、config の壊れで retry を止めない）。

## 4. P2: reopen

### 4.1 `plan.sh reopen <id> --reason "<理由>" [--mission <slug>] [--cascade none|reviews|all] [--branch reuse]`

「確定した結果の再評価」。遷移元: `done` / `verified` / `skipped`。遷移先: `pending`。`failed` 等は retry（§3.3）。

1. 検査（全部ロックの中。何も書く前に）: 遷移元の status ・試行が active でない（active なら exit 3）・`reopen_count` と `execution_count` の上限・mission が archive されていない（されていれば exit 3 で「新 mission に新 task を」と案内。§4.5）。
2. `reopen_count += 1`、`completed_at` を消す、`worker` / `started_at` を消す。`pr_number` と `task_slug` は残す。旧 Result は台帳の `end`（既に書いてあるはず）を指す `reopen` を足す。
3. mission が done なら戻す（§4.2）。
4. 下流の処理（§4.3）。
5. 書く順は §4.3 の **roll forward の順**（下流 → 最後に I）。task ごとに台帳 `reopen`（理由つき）→ card の順（§3.2）。

**同じ reopen の再送**: I の card が最後に書かれるので、**I が pending なら reopen は完了している**（exit 3 `NOT_REOPENABLE`）。I がまだ done / verified / skipped なら**途中で止まった reopen**で、同じコマンドを再実行すれば続きから完了する（§4.3 の roll forward。I 自身の台帳は §3.2 の同定キーで再利用する）。

### 4.2 mission を active に戻す（独立した先頭 PR）

- `cmd_add` と `reopen` / `retry` と `update --status pending|in_progress`（done の mission にある task を未完了に戻す経路）で、mission の `status` が `done` なら戻す。**戻す値は `in_progress`**（`plan.sh:6034` が mission の起動で書く値）。`done` より前の値は残っていないので、`ready` / `reviewing` には戻せない。`completed_at` は消す。**戻す値の選び方は PR-A で、dispatcher と lint が `in_progress` を想定どおり扱うことを確かめてから確定する（未確認）**。
- 戻すのは `plan.sh` だけ（dispatcher は mission を書かない）。dispatcher は次のサイクルで mission が done でないことを読む（`dispatcher.sh:2446-2476`）。すでに退役した idle の Worker は戻らない — 退役済みの Worker を呼び戻す手段は別の問題で、ここでは扱わない（Director が `./crewvia` で起動し直す。運用記録の通り）。
- 元文書 §15 の困りごと (b) は、この PR だけで直る。

### 4.3 下流の無効化（元文書 §15-1 の答え）

下流 = `blocked_by` の推移閉包で I に依存する task。閉包の列挙は **`lib_dep_rules.py` に `downstream_closure(graph, tid)` を足して 1 か所**にする（plan.sh にコピーしない。不変条件 3。訪問済みの集合で止まるので、循環があっても終わる — ただし `add` / `update --blocked-by` が循環を拒否している（`find_dependency_cycle`）ので、通常は無い）。

| 下流の状態 | 何が起きるか | 仕組み |
|---|---|---|
| pending / blocked | 何もしない。I が終端でなくなるので**自動で待つ** | `lib_dep_rules` は変えない |
| reserved / running（in_progress） | `--cascade` が無ければ **reopen を拒否**（exit 3、該当 task を列挙）。あれば、その試行を `RESET_BY_DIRECTOR` で閉じて pending に戻す（`reset_task` と同じ）。Worker は次の報告で `EXECUTION_NOT_CURRENT` / `ALREADY_TERMINAL` を受ける | 既存の Controller の操作 |
| **ready_for_verification / verifying（元文書 §15-1）** | in_progress と同じ。`mark_task` はこれらの状態でも試行を running のまま持つ（`lib_task_controller.py:730-`）ので、試行を閉じれば**検証の結果は `verify-result` の名乗りの照合で拒否される**。「上流が変わったのに下流の検証が pass する」害は、試行を閉じることで塞ぐ | 同上。verify pass 側にも「依存先の `reopen_count` の和が予約時と同じか」を見る検査を足すかは、Verifier を運用していないので**足さない**（Verifier を動かす段で解く。§8） |
| done / verified（完了済み） | `--cascade reviews` は skills に `codex-review` / `review` / `qa` を含む task だけ、`all` は全部、`none` は何もしない。対象は `reopen_count += 1` で pending に戻す。**対象外の完了済み下流は、拒否も無効化もせず、reopen の出力に「古い結果に基づく下流」として列挙する**（Director が判断する） | `reopen_task` を下流の各 task に適用 |

- 既定の `--cascade` は `reviews`（§7 の判断 5）。
- **経路上の中間 task も無効化の対象に含める（閉包）**。`lib_dep_rules` の「依存が満たされた」は**直接の依存だけ**を見る（`unmet_dependencies`）。I → A（非レビュー）→ R（review）が全部 done のとき、R だけ戻すと R は A の done を根拠にすぐ pull でき、I の修正前の結果をもう一度確定してしまう。したがって対象 R の集合に対し、**I から R への依存の経路上にある task（I の下流かつ R の上流）をすべて対象に足す**。足された中間 task は、skills に関わらず done / verified なら `reopen_count += 1` で pending に戻す（A は I が再完了するまで pending のまま pull されず、R は A の再完了まで待つ）。`downstream_closure` の隣に `path_closure(graph, root, targets)`（root の下流集合 ∩ targets の上流集合）を `lib_dep_rules.py` に足して 1 か所にする。書く順は §4.3.1 のまま（I から遠い順）。
- **経路上の中間 task の status ごとの扱い（PR-F fix 2・t046。Codex 2 巡目 P1）**。「中間」= 経路上で、別の経路上の task（または選ばれた下流）の依存になっている task。`failed` は依存の判定で「保留」だが、`released_deps` で解除された下流は I の再完了を待たずに pull できる（I(done) → A(failed) → R(pending, `released_deps=[A]`)）。A は戻す手段が無い（failed は reopen の遷移元でない）ので、**戻せない中間は拒否する**。

  | 経路上の中間の status | 扱い | 理由 |
  |---|---|---|
  | done / verified | `reopen_count += 1` で pending に戻す | 上の閉包 |
  | in_progress / ready_for_verification / verifying / needs_human_review | 試行を `RESET_BY_DIRECTOR` で閉じて pending（`--cascade none` は `CASCADE_ACTIVE_DOWNSTREAM`） | §4.3 の表 |
  | pending / needs_director / blocked / verification_failed | そのまま | 未完了なので、それに依存する下流は I の再完了より前に pull できない |
  | **skipped** | **拒否** `CASCADE_SKIPPED_ON_PATH` | 終端で、戻せない（PR-F・t036） |
  | **failed**（他の task の依存になっているもの） | **拒否** `CASCADE_FAILED_ON_PATH`（exit 3・何も書かない・経路と task を出す。Director が `retry` 等で片付けてから reopen） | 保留のまま残り、解除済みの下流が走れる。解除されていなくても、後から `release-dep` された瞬間に同じ害になるので区別しない |
  | failed（経路の**端**。依存元を持たない） | そのまま | 走る下流が無い |
  | 語彙の外（手書きの `cancelled` 等） | 何も書かず `STATE_INVALID`（exit 1。store が card を読めない）。lib にも `CASCADE_FAILED_ON_PATH` の保険がある | 知らない status を「通す」側に倒さない |

  `--cascade none` は何も戻さないので上の拒否は `none` には掛けない。代わりに、**経路上の failed の中間を `released_deps` で解除している下流**も `stale_runnable`（出力の「再完了を待たずに走りうる」）に出す。
- **`released_deps` の扱い**: 解除の根拠が消えるのは I だけでなく、**pending に戻した task 全部**（I と cascade で戻した下流）。下流の card の `released_deps` から、「戻した task で、かつその下流の依存であるもの」を全部消す（消した task は出力に出る）。中間 A が戻った後で再び failed になっても、R の古い解除で自動では通らない。
- **経路上の中間 task を対象外にする指定は拒否する**。将来 `--keep <id>` のような除外を足しても、`<id>` が対象 R への経路上にあり done / verified なら exit 3（該当する経路を出す）。除外できるのは経路上にない完了済み下流だけで、それは今まで通り「古い結果に基づく下流」として列挙する。
- **`lib_dep_rules` の依存判定を推移的にする案は採らない**: 判定は dispatcher・`plan.sh pull`・lint が共有する唯一の規則で（不変条件 3）、変えると reopen と無関係な既存の全 task の割り当てが変わり影響が大きい。cascade の側で経路を閉じれば、規則を変えずに同じ保護が得られる。
- 下流の `released_deps` に I の id があれば消す（`released_deps` は failed の依存を解除する欄。I が戻ったら解除の根拠が無い。`plan.sh:6536` と同じ作法）。
#### 4.3.1 intent に決定を書いて最後まで実行する（途中で止まっても推測しない。t059 で書き換え）

**原子的にはしない**。`Txn` は書き込みをステージせず、呼ばれた順に 1 ファイルずつ atomic に書き（`lib_state_store.py` の `_write` / `write_card`）、rollback も無い。書く順がそのまま crash モデルで、`knowledge/state-store.md:679` もこれを採った理由を述べている。したがって「1 つの Txn」が保証するのは **1 回のロック + 1 つの監査 txn_id** であって、全部書くか全部書かないかではない。途中の I/O エラー・プロセス停止は起きうるものとして、次のように設計する。

**この節の履歴（前の方式を撤回した理由）**: 初版（PR #308 の t036〜t054）は「journal は使わない」と決め、再実行が各 card・台帳の印（`root` / `root_reopen_count`）から**済んだ所を推測し直す**方式だった。Codex は 5 巡続けて P1 を出し、2・3・5 巡目と 5 巡目の P2 はどれも「止まっている間に下流が動く（pull されて done になる・legacy の実行中 B がいる）と、推測が取りこぼす」系だった。推測は『止まった時点の状態』を再構成しようとするが、止まっている間に状態は動く。01a が journal を退けた理由（`state-store.md:212-219`: 回復に要る情報は既に正本にある）は、**この場面には当てはまらなかった**: 要る情報は『reopen の時点でどの task をどう戻すと Director が決めたか』で、それは正本のどこにも無い。**決定を最初に 1 か所へ書き、それを最後まで実行する**。

1. **検査を全部先に**（§4.1-1 と、下流の列挙・拒否の判定。何も書く前）。拒否されれば何も書かれない。targets は `_reopen_targets` の 1 関数。
2. **intent を書く（= 決定のコミット点）**: `queue/missions/<m>/reopen-intents/<root>-<n>.json`（n = 新しい root の `reopen_count`。1 つの reopen に 1 ファイル・上書きしない = `Txn.write_reopen_intent` が `AlreadyExists`）。中身: `version` / `actor`（台帳に書く actor。回復が別のコマンドでも同じ actor で書く = 台帳の同定キーの再利用が一致する）/ `root` / `root_reopen_count` / `reason` / `cascade` / `over_cap` / `root_capped` / `created_at` / `closure`（`released_deps` の掃除の範囲）/ `tasks`（**戻す task の一覧**。各 `id`・`op`（`reopen` = done / verified から戻す・`reset` = 実行中の試行を閉じる）・`capped`、**I から遠い順**）。書き込みは `lib_state_store` の `Txn.write_reopen_intent`（`tests/test_queue_writes_go_through_the_store.py` の対象外 = lib の中）。
3. **intent に従って実行する**（`lib_task_controller.execute_reopen_intent`。最初の実行も回復も同じ道）: mission の再活性化 → 一覧の順に 台帳 `reopen` / `reset`（理由つき）→ card → `released_deps` の掃除 → **I の card を最後** → **intent を消す（= 完了）**。完了の印は『intent が無い』。`completed` に書き換える方式は採らない（消す方が『残っている = 未完』が 1 つの事実になり、store-check・回復・テストが同じ判定を読める）。
4. **回復は intent を見る**: plan.sh の queue を書くコマンド（`recover_before` を呼ぶもの全部。**pull を含む**）は、ロックを取った直後（projection の回復の後・前提検査より前）に、範囲の mission の未完の intent を**最後まで実行してから**本体に進む（`_complete_reopen_intents`）。**一覧を作り直さない**。一覧の各 task は『その操作がもう済んでいるか』を **card の今の status** で確かめて冪等に行う: done / verified → 戻す（`reopen_count + 1`）、実行中 → 試行を閉じて pending、それ以外（pending 等）→ 済んでいる。台帳だけ書けて card が書けなかった孤児は台帳の同定キー（execution_id, kind, reopen_count）で再利用する。止まっている間に下流が動いた場合（pull されて done になった・古い結果で走っている）も、一覧にある限り戻す（`reopen_count` は『完了のたびに 1 回』で増える）。
   - これは「**回復は正本を書かない**」（`scripts/CLAUDE.md` の回復の節）の**意図した例外**。理由: intent は Director の決定済みの操作の続きで、状態から推測した修復ではない（一覧は決定の時点で固定されている）。
   - dispatcher は queue を読むだけで、割り当ての後に Worker が pull する。**pull 側の回復が先に走る**ので、止まった cascade の窓（I は done のまま下流が pending）で古い結果の下流が pull されることは無い。dispatcher は変えない。pull の入口で窓を作らないテスト: `test_a_pull_that_arrives_before_the_recovery_recovers_first_and_pulls_nothing_stale`。
   - **完了できない intent**（読めない・壊れている・名前の形でない・一覧の card が読めない・試行の記録が無い）は固定コード `REOPEN_INTENT_INCOMPLETE`（exit 1）。**その mission の pull だけ止める**（名指しなら exit 1・何も書かない。名指しでなければその mission を候補から外す）。他の mission と、読み取り専用のコマンド、他の書き込みコマンドは止めない（止めると出口まで塞ぐ）。壊れた intent は消さない・直さない。出口: card を直して次の queue を書くコマンドか、同じ引数の `plan.sh reopen` を再実行する。
   - **止める遷移の表**（t061。Codex: pull の 1 回目のロックだけ検査し、予約 → ロックを放して worktree の準備 → その間に reopen が intent を書いて途中で失敗 → 2 回目のロックでも回復が失敗、のとき `start_execution` に進んで下流が始まった）。ロックを取って task を進める全部の遷移を、**「task を走らせる（古い土台の上で作業を始めさせる）か」**で分けた:

     | 遷移 | 完了できない intent がある mission で | 理由 |
     |---|---|---|
     | pull の 1 回目のロック（候補選び・予約） | 止める | 下の 2 行と同じ（t059） |
     | pull の 2 回目のロック（start: reserved → running） | **止める**（t061。予約は書き換えない・JSON を出さない・`REOPEN_INTENT_INCOMPLETE`） | 予約と start の間にロックを放す窓がある。start は『この task を走らせる』唯一の点 |
     | pull の worktree 失敗の出口（`needs_director` に倒す） | 通す | 予約を閉じるだけで task を進めない。止めると予約が恒久に残る |
     | done / fail / needs-director / ready-for-verification / verifying / verify-result | 通す | Worker の作業を失わせない。報告は下流を走らせない（走らせる pull が止まっている）。intent は card の今の status で冪等なので、あとで done の下流も一覧にあれば戻る |
     | retry / update --status・--reset / release-dep / add | 通す | Director の出口を塞がない。task を走らせるのは pull だけ |
     | retire | 通す | 窓を閉じる側。止めると誰も閉じない |
     | reopen（別の root） | 止める（exit 1。Controller も重ねて始めない） | intent の上に重ねない |
     | 退避（その mission） | **止める**（t061） | 半端な cascade（I は done のまま下流が pending）を退避先に固めない。reopen は退避済みを扱わず出口が無くなる |

     つまり『止める』は **task を走らせる点（start）・新しい決定（reopen）・出口を無くす点（退避）** の 3 つだけで、それ以外は通す。判定は `_INTENT_BLOCKED`（`recover_before` が毎回作り直す = 前のロックの失敗を引きずらない）を、止める遷移が自分のロックの中で読む。
5. **同じ reopen の再実行**: 未完の intent があればそれを完了して成功を報告する（`再実行で完了`）。`--reason` / `--cascade` / `--over-cap` が intent と違えば `HISTORY_MISMATCH`（exit 3・何も書かない）。他の root の未完の intent は先に完了する。Controller を直に呼んでも、未完の intent がある mission では新しい reopen を始めない（`REOPEN_INTENT_INCOMPLETE`）。
6. **途中状態の見つけ方**（`store-check`。**報告だけ・直さない**）: 未完の intent があれば `reopen-intent-incomplete  mission=<m>  task=<root>  (intent=<名前>)`。直すのは次に queue を書く plan.sh コマンド（pull を含む）か、同じ `plan.sh reopen` の再実行。tmp の残骸（置換前に落ちた書き込み）は intent ではない（数えない）。
7. **前の方式の推測ロジックは消した**: `_cascade_marker`・`reopen_is_complete`・`already` の台帳の印からの導出・台帳の印と `--reason` の食い違いによる `HISTORY_MISMATCH`・`cascade-incomplete`（`lib_history.cascade_incomplete`）。台帳の `root` / `root_reopen_count` の 2 欄は**監査用に残す**（どの reopen で戻したかが読める）。
8. **途中で止まったときの状態は、古い結果が通る側には倒れない**（初版から変わらない性質）。書く順（下流 → 最後に I）で作られる中間状態は『I が done のまま・下流の一部が pending』だけで、危険な逆（下流が done / verified のまま上流が pending）はどの時点でも作られない。この窓は intent の実行が閉じ、窓が開いている間に pull が来ても pull 自身が先に閉じる。

### 4.4 Git（merge 済みの task の reopen）

`-r2` のような世代つきの branch は、今の Git Policy ではできない（置換子は `{mission_slug}` `{task_id}` `{task_slug}` だけ。`git-policy.md:252`・`:257-259`）。**P2 の v1 は、PR が merge 済みの task の reopen を扱わない**: `reopen --branch reuse`（既定）は未 merge の PR の task 用で、同じ branch・同じ PR を使う。merge 済みか否かを plan.sh は調べない（`gh` をロックの中で呼ばない）ので、**Director が確認して `--branch reuse` を明示する**（省略は拒否）。merge 済みの task の修正は、今までどおり新 task（元の task を `related` に書く）。Policy に世代の置換子を足す改訂は任意の PR-I（§6）。

### 4.5 archive の restore はやらない

archive 済み mission の task の reopen は拒否する（exit 3、「新 mission に新 task を」）。理由: (1) 元文書 §14.1 の困りごと (a)〜(h) に、archive 済み mission を開け直したい事例が無い。(2) restore は mission dir を戻し、`state.yaml` の `active_missions` と `default_mission` を直し、Taskvia の archive 同期（`taskvia_sync_archive` `plan.sh:2456-`）を逆向きに行う必要があるが、Taskvia 側で archive 済みの mission が復元できるかは**未確認**。需要が出たら別 mission で設計する。

### 4.6 循環

reopen は依存の辺を足さないので、循環を作らない。下流の列挙だけが辺をたどる（§4.3、訪問済みの集合）。`add` / `update --blocked-by` の循環の拒否（`find_dependency_cycle`）は今のまま。

### 4.7 実装で決めたこと（PR-F・t036）

- **`reopen_count` の上限は設けない**。reopen した task が再び reopen できるのは再び done になった後で、そのたびに pull が試行を 1 つ発行する。したがって reopen の回数は `execution_count` の上限（`max_attempts`。入口で検査・`--over-cap` も retry と同じ形）で抑えられる。別の数を足すと、2 つの上限の食い違いが新しい拒否の理由になる。
- **上限は戻す下流にも適用する**（`ATTEMPT_CAP`・`--over-cap` は全員に効く）。入口だけだと、cascade で戻された下流が上限を黙って超えて pull される。
- **実行中の下流は選ばれたかに関係なく閉じる**（cascade が none 以外のとき）。I が変わるので、動いている作業は古い土台の上にある。対象の status は in_progress / ready_for_verification / verifying / needs_human_review（`ASSIGNMENT_HOLDING_STATUSES`）。`--cascade none` は `CASCADE_ACTIVE_DOWNSTREAM`（exit 3・該当 task を列挙）。
- **roll forward は intent**（t059。§4.3.1）: 旧版の『台帳の印から済んだ所を推測する』は撤回した。targets は選ばれた下流と実行中の下流だけ（印のある task を数える分岐は無い）。
- **台帳の `reopen_count` は増やす前の値**（同定キー (execution_id, kind, reopen_count) が再送で変わらない）。`store-check` の `reopen-intent-incomplete` は未完の intent を報告だけする（旧 `cascade-incomplete` は撤回）。
- 台帳の食い違い（孤児の台帳と違う中身）は、書く前に `_write_reopen(dry_run=True)` で I と戻す下流について見つける。実行中の下流の `reset` の台帳だけは書く側でしか見つからない（食い違えば exit 3 で、すでに書いた分は同じ引数の再実行で続きから完了する）。
- `released_deps` から I を消すのは、I に直接依存する下流の card（`blocked_by` に I がある task）。pending のままの下流も対象（解除の根拠が消えるため）。消した task は出力に出る。
- `--branch reuse` は必須で他の値は拒否。mission は `_reactivate_done_mission`（PR-A）で戻す。archive 済み mission は exit 3（`MISSION_ARCHIVED`。`--mission` を省略しても archive 側を探して同じ答え）。
- 対象の skills の置き場: `lib_dep_rules.CASCADE_REVIEW_SKILLS`（codex-review / review / qa）。

## 5. P3: checkpoint

困りごと (c) は「Worker が無言で止まり、未 commit の作業が失われかけた」（2026-10-04・10-05・10-11 の 3 回。元文書 §14.1）。

### 5.1 誰が書き、誰が読み、どこに置き、いつ消すか

| | 内容 |
|---|---|
| **書く** | 退役の実行者（watchdog 側の `lib_retirement.py`）。`plan.sh retire --execution` を呼ぶ**前**に、`lib_checkpoint.py`（新しい lib。**worktree を読むだけ**で何も変えない）を呼ぶ。queue には書かない（queue への書き込みは Txn だけ） |
| **読む** | `pull`（次の試行に `previous_attempt.checkpoint_dir` を渡す）・Worker（`worker.md` に「`checkpoint_dir` があれば先に読め」を足す。PR-H）・Director（needs_director 通知に path を載せる） |
| **置く** | `registry/checkpoints/<mission>/<tid>/<execution_id>/`（gitignored。handoff と同じ registry 側。mode 0700 / 0600）。**queue の外**に置く理由: patch は大きくなりうる・秘密を含みうる・queue は Git に載せない運用でも mission ごと動く |
| **消す** | `scripts/checkpoint_gc.py`（`worktree_gc.py` と同じ作法: 既定は dry-run、`--apply` でも**削除でなく隔離**）。対象は「task が終端で、保存から 30 日を過ぎたもの」。未解決の task（終端でない）のは消さない。期間は §7 の判断 7 |

### 5.2 内容

- `meta.json`: `execution_id` / `mission` / `task` / `taken_at` / `reason`（`timeout` / `hard_idle` / `window_gone` …）/ `branch` / `head` / `dirty`（bool）/ 各ファイルの `truncated` と元のサイズ。
- `dirty.patch`: `git diff HEAD --no-ext-diff --no-textconv`（**`--binary` は付けない**。`--no-textconv` は必須: 読み取りの git が textconv を起動する。memory: git-blame-runs-textconv-by-default）。上限 1MB で切り、切ったら `truncated: true`。テキストの差分だけが入り、バイナリは `Binary files … differ` の 1 行になる。
- **バイナリ差分は保存しない**。`--binary` の出力は圧縮・符号化されていて、§5.3 の文字列 redaction では中の秘密を検出できず、redaction が正常終了しても復元可能な秘密が残るため。代わりに `binary-files.txt` に変更されたバイナリ（`git diff HEAD --numstat` で `-` と出るもの）の**ファイル名・サイズ・blob sha（`git hash-object`）だけ**を記録する。**バイナリの内容はこの checkpoint から復元できない**（`meta.json` に `binary_files: <件数>` と `binary_restorable: false` を書き、Worker 向けの読み方にも明記する）。必要なら blob sha を手がかりに worktree が残っていれば取り戻す。
- `untracked.tar` ではなく **`untracked/` に個別ファイル**（1 ファイル 64KB 以下・合計 2MB 以下。超えた分は一覧だけ）。シンボリックリンクは辿らない。**バイナリ（先頭 8KB に NUL を含む）の untracked ファイルも内容は保存せず**、`binary-files.txt` に名前・サイズ・blob sha だけ載せる。
- `status.txt`: `git status --porcelain=v1 -z` の結果。
- `pane.txt`: ペインが**まだ生きているとき**だけ、最後の 200 行（`lib_mux.py capture`）。window が消えた場合は無い（それでも他は取れる — worktree の path は record の `git.worktree`（`lib_execution.py:327-355`）から得る。ペインの有無に依存しない）。

### 5.3 秘密

- **ファイル名で除外**: `.env*` / `*.pem` / `*.key` / `*.p12` / `*.pfx` / `*_rsa` / `*_dsa` / `*_ed25519` / `*credentials*` / `*.secret*`（利用者の security.md の一覧と同じ）。パッチの該当ファイルの hunk は落とし、`meta.json` に除外した件数だけ書く。
- **内容の redaction**（patch・untracked・pane の全部。バイナリは保存しないので対象外）: `op://…`・`-----BEGIN … PRIVATE KEY-----` から END まで・`(ghp|gho|ghs|github_pat)_…`・`sk-…`・`AKIA…`・`xox[abprs]-…`・`Bearer <長い文字列>`・`(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)\s*[=:]\s*\S+` の値を `[REDACTED:<n>]` にする。置換の件数を `meta.json` に書く。**redaction が走らなかった（例外）ときはそのファイルを保存しない**（生のまま残さない側に倒す）。
- 正規表現は `timeout 120` を要する検索ではないが、**バックトラックしない形**（量指定子を入れ子にしない）で書き、テストで長い 1 行（1MB）を通す。

### 5.4 失敗しても退役を止めない

`lib_retirement` の段階に `checkpoint` を足す。**期限 20 秒（`daemons.checkpoint_seconds`）で打ち切り、失敗・期限切れは dir を作らず（途中までの dir は消す）、ログと Director への一回きりの通知に残して、退役を続ける**。理由: 退役が止まると誰も窓を閉じない（`daemon-authority.md` §5）。保存が失敗したことは**成功扱いにせず**、`plan.sh retire` に `--checkpoint none` を渡して台帳の `retire` に「checkpoint なし」と書く。判断 6 で確認する。

### 5.5 `retire` / 台帳への接続

`plan.sh retire --execution <id> --checkpoint <dir|none>` を足す（任意の option。無ければ今まで通り）。台帳の `retire` の `checkpoint_dir` に入る。**dir の存在は plan.sh が検査する**（`lib_checkpoint.py verify <dir>` が `meta.json` の `execution_id` と照合 — 別の試行の dir を指せない）。

### 5.6 PreCompact を task に届ける（困りごと (d)）

- 原因 (i): task は pull のあとに決まるが、`ENV_EXPORTS` は起動時の 1 回（`start.sh:786`）。(ii): `CREWVIA_AGENT_NAME` は誰も設定しない（§1-7）。
- 直し方: hook は `plan.sh snapshot --self --section-file -` を呼ぶ。`--self` は `AGENT_NAME`（`start.sh` が渡す）の assignment（`queue/assignments/<agent>` = `mission:task`）から mission と task を決め、`classify_assignment` の規則で「card が in_progress・worker が自分」を確かめる。決められなければ書かずに拒否（今の `resolve_ambiguous_mission` と同じ。別 mission の同じ `tNNN` に書かない）。節の見出しに `execution_id` を入れる。
- `CREWVIA_TASK_ID` を `ENV_EXPORTS` に足す案は採らない: task は 1 セッションの中で変わる（Worker は pull ループを回す。`worker.md:112-134`）。
- 書く内容は今と同じ（trigger / agent / task / timestamp）。**作業内容は保存しない**（P3 の checkpoint は退役の経路。PreCompact は compact 後に「どの task か」を思い出すためのもの）。

### 5.7 handoff の path に mission を含める（困りごと (e)）

`crewvia_handoff_path <agent> <mission> <task_id>` → `registry/handoffs/<agent>/<mission>/<task_id>_HANDOFF.md`（`git-helpers.sh:83`）。**古い path も読む**（`plan.sh fail` の `handoff_path` は絶対パスのみ・存在を検査する、不変条件 6。新旧どちらでも通る）。呼び出し側の `agents/worker.md:410`・`:811` と、読み手の dispatcher の通知（`handoff_path` を card から読む）を揃える。実際に衝突した事例は見つけていない（未確認）ので、緊急ではない — 先頭ではなく PR-B。

## 6. PR の分割と順序

300KB を超える PR は `kai-review.sh` が fail-closed で拒否する（memory: pr-size-breaks-review-machinery）。どの PR も差分は数十〜百数十 KB の見込み（実装 + テスト）。下の「触る箇所」は `gh pr diff` で確かめた未 merge PR との重なり。

| PR | 範囲 | 依存 | 触る箇所（未 merge PR との重なり） | rollback | 本番確認 |
|---|---|---|---|---|---|
| **A** mission の再活性化（P2 の小物。**単独で先に出せる**） | `cmd_add` と `update --status`（done の mission の task を未完了に）で mission を `in_progress` に戻す。`completed_at` を消す。lint / dispatcher が `in_progress` を想定どおり扱うことの確認 | なし | `plan.sh` の `cmd_add`（#298 が `:3005` 付近を触る — #298 の後に出すか、衝突を手で解く） | PR revert。mission.yaml は手で `done` に戻せる | 使い捨て mission で done → add → status が戻り、dispatcher が「全ミッション完了」を出さない |
| **C** PreCompact（P3 (d)。**単独で先に出せる**） | `hooks/pre-compact.sh` / `plan.sh snapshot --self` | なし | `cmd_snapshot`（#291/#293/#292/#298 のどれも触らない） | PR revert。fallback ログに戻る | 実 Worker の PreCompact で card の節が出る（`/compact` を打つ）。fallback ログ `queue/pre-compact-fallback.log` が増えない |
| **B** handoff の path（P3 (e)） | `git-helpers.sh` / `worker.md` / テスト | #291 の merge（`worker.md` を #291・#293 が触る） | 同左 | revert（新旧どちらも読めるので、書き手を戻すだけ） | 新しい path で fail → dispatcher の通知に中身が載る |
| **D** 台帳（P1 (a)） | `Txn.write_history`・`_finish` の呼び出し・`plan.sh history`・`pull` の `previous_attempt`・store-check の報告 | #291・#293 の merge（`lib_task_controller.py` を触る） | `lib_task_controller.py:_finish`（#291/#293 が `:570-614` 付近を触る） | revert。台帳は残ってよい（旧コードは読まない） | 1 task を done → fail（別 task）で `history/` に 1 ファイルずつ増える。card と台帳の Result が同じ |
| **E** retry と総試行数の上限（P1 (b)(d)） | Controller `reopen_task(kind='retry')`・`plan.sh retry`・`max_attempts` | D | `plan.sh`・`lib_task_controller.py` | revert。`update --reset` は残してあるので運用は戻れる | 使い捨て mission で fail → retry → pull で `previous_attempt` が出る・上限で exit 3 |
| **F** reopen と下流の無効化（P2） | `plan.sh reopen`・`--cascade`・`lib_dep_rules.downstream_closure`・mission の再活性化（A と同じ関数） | D・E | 同上 + `lib_dep_rules.py` | revert | 使い捨て mission で I → R → Q を作り、I を reopen して R・Q が pending に戻り、I の再 done まで pull されない |
| **G** checkpoint（P3） | `lib_checkpoint.py`・`lib_retirement.py` の段階・`plan.sh retire --checkpoint`・`checkpoint_gc.py` | D | `lib_retirement.py`（未 merge PR は触らない） | revert（`--checkpoint` は任意 option なので、戻しても retire は動く） | 観察用の手書き marker（memory: observe-retirement-with-handwritten-marker）で、dirty な worktree の patch が出る・秘密がマスクされる |
| **H** 運用規則の文書 | `agents/director.md`（「修正は新 task でなく retry / reopen」）・`worker.md`（`previous_attempt` / `checkpoint_dir` を読む）・`scripts/CLAUDE.md` | E・F・G の本番確認 | `agents/*.md`（#291 が触る）。**運用の変更なのでユーザーの承認が要る** | revert | 次の mission で Director が retry を使い、新 task の数が減る |
| **I**（任意）Git Policy の世代置換子 | merge 済みの task の reopen | F | `lib_git_policy.py`・git-helpers | revert | — |
| **J**（任意）Verifier の依存先の検査 | verify pass が依存先の `reopen_count` を見る | Verifier を運用する段 | — | — | — |

順序: **A と C を先頭**（独立・小さい・今の困りごとを直す）→ #291 系の merge 後に B・D → E → F・G（並行可。同じファイルを触るので順番に merge）→ H。各 PR の merge と cutover はユーザーの承認（plan.sh は主 checkout から直接実行されるので merge = cutover。memory: main-checkout-lags-after-pr-merge）。

## 7. ユーザーに決めてもらう点

1. **着手の順序**: A（mission の再活性化）と C（PreCompact）を #291 系の merge を待たずに先に出す。**推奨: はい**（どちらも独立で小さく、困りごと (b)(d) をすぐ直す）。
2. **総試行数の上限**: 5（元文書の案）か 6 か。実測で `execution_count` の最大は 6（t005）。**推奨: 6**（今の運用で `--over-cap` を常用させない）。差し戻しの `max_rework` 3 は変えない。
3. **merge 済みの task の reopen**: v1 は扱わず、新 task で続ける。Git Policy に世代の置換子を足す（PR-I）か。**推奨: 扱わない**（PR-I は需要が出てから）。
4. **archive の restore**: やらない。**推奨: やらない**（需要の記録が無い。Taskvia の復元は未確認）。
5. **`--cascade` の既定**: `reviews`（codex-review / review / qa の完了済み下流だけ自動で開け直し、他は列挙のみ）か `all` か `none` か。**推奨: `reviews`**（検証系は古い head の結果が無意味になる。他の下流は Director が判断する）。
6. **checkpoint の保存失敗**: 退役を止めない（期限 20 秒で打ち切り、Director に 1 回通知）。**推奨: 止めない**（止めると誰も窓を閉じない）。
7. **checkpoint の保持期間**: 30 日（終端の task のみ。元文書 §13 の案と同じ）。**推奨: 30 日**。
8. **Director の運用規則の変更**: 「Codex の指摘の修正は新 task でなく retry / reopen」にする（PR-H）。**推奨: 承認する**（レビュー後に足した task は 41%）。ただし PR-E・F の本番確認の後。
9. **語**: `generation` を使わず `reopen_count` にする。**推奨: そのまま**。

## 8. 未解決の課題（この文書のレビューで積む場所）

Codex の往復は 3 巡で打ち切った (PR #301 の t002)。以下は実装 PR の網羅テストで解く。残った P2 以下はここに積む（元文書 §15 と同じ方針）。

- **cascade の経路閉包で `skipped` の中間 task が漏れる**（Codex 3 巡目 P2。実装 PR F）。
  - **PR-F（t036）で解いた（拒否）**: 無効化の経路上に `skipped` の task があれば `plan.sh reopen` は exit 3（`CASCADE_SKIPPED_ON_PATH`・何も書かない・`reopen_count` も増えない・`t001 -> t002` の形で経路を出す）。「経路」= `dep.path_closure(graph, I, targets)` で、`targets` は `--cascade` が選ぶ task（reviews = skills に codex-review / review / qa。all = 全部）のうち done / verified / **skipped** / 実行中 / 前の実行で戻した task。**選ばれた skipped の task 自身（末端）も経路に含む**。`--cascade none` は targets が空なので拒否しない。出口は、skipped の task を先に `plan.sh reopen` で戻す（skipped は reopen の遷移元）か、`--cascade` を見直すこと。網羅テスト: `tests/test_reopen_cascade_plan_sh.py`（途中・末端・複数経路の片方・経路外・3 値）。以下は解く前の記述。
  - 漏れている状態: `lib_task_status.py` の `TERMINAL_STATUSES` に `skipped` がある。I → A(`skipped`) → R(`done`) の形で I を reopen すると、A が終端として扱われ経路が切れ、R が無効化されない。R は I の再完了より前に pull できてしまう。
  - 起きる害: 古い I の結果に依存した R（review / qa 等）が、I の再完了を待たずに走り、無意味な結果を `done` にする。
  - 直し方の方向（未決）: (a) `skipped` も経路上なら無効化の対象にする。(b) 経路上に `skipped` の task がある reopen を拒否する。Director の推奨は (b) 拒否（`skipped` は Director が意図して飛ばした task なので、勝手に開けない）。
  - 解く PR: F。網羅テストに入れるケース: I → A(`skipped`) → R(`done`)、`skipped` が経路の途中・末端・複数本の経路の片方にある形、`--cascade none|reviews|all` それぞれ、拒否の場合は何も書かれない（`reopen_count` も増えない）こと。
- **PR-F の Codex 2 巡目（PR #308・t046）で直した 2 点**（P1×2）:
  - **経路上の failed の中間（P1）**: I(done) → A(failed) → R(pending, `released_deps=[A]`) で reopen が通り、A は failed のまま・R の解除も残るので、R が I の再完了を待たずに走れた。経路上の（他の task の依存になっている）failed は `CASCADE_FAILED_ON_PATH` で拒否（§4.3 の表に status ごとの行を足した）。`released_deps` は I だけでなく pending に戻した task 全部の解除を消す。`--cascade none` では出力の「再完了を待たずに走りうる」に出す。網羅テスト: 中間 A の status（語彙の全部 + 語彙の外）× R が A を解除しているか × cascade 3 値で「reopen が成功したのに I が pending の間に R を pull できる」組み合わせが残らない（`test_no_combination_leaves_r_pullable_before_the_root_completes_again`）。陽性対照（I が done のままなら本当に pull できる）つき。
  - **下流から先に pending にする書き込みの途中で止まる（P1）**: **§4.3.1-4 の判断を保つ**（途中の状態は reopen 前の状態で結果を確定し直すだけ・無駄な作業が増えるだけ）。台帳の印を pull / reserve / `lib_dep_rules` に読ませる案は採らない。収束のテストを足した: kill の後・再実行の**前**に R を pull して done / 実行中にしてから同じ reopen を再実行し、R が再び pending になり、I → A → R の順でしか完了できないこと。`plan.sh reopen` の出力に再実行の案内を足した。
  - **残るリスク**: 止まってから再実行までの間に R が古い結果で走る（無駄な作業と、R が done になる間の `reopen_count` の +1）。Director が気づくのは reopen の出力（途中で死ぬと成功時の出力は届かないので、書き始める前の stderr にも出している）と `store-check` の `cascade-incomplete`。dispatcher が自動で再実行・待機する案は範囲外（P0 の残り: 回復を dispatcher が起こす）。
- **PR-F の Codex 1 巡目（PR #308・t042）で直した 3 点**（P1×1・P2×2）:
  - **pending の review が経路の終点に数えられていなかった**（P1）: I(done) → A(done, code) → R(pending, review) の既定の reopen で A が done のまま残り、R が A の古い結果で I の再完了より前に pull できた。経路閉包の targets は **選ばれた下流を status を問わず**含める（pending / blocked / 実行中 / done / verified / skipped）。`--cascade none` は戻さないが、出力に「I の再完了を待たず、戻していない古い中間の結果で走りうる pending の下流」（`ReopenResult.stale_runnable`）を警告として列挙する（none 以外でも、選ばれなかった pending の下流に同じ形があれば出す）。網羅テスト: 中間 A（done / verified / pending / 実行中）× R（pending / blocked / 実行中 / done / verified）× cascade（reviews / all）で、成功した reopen の後に R を実際に `pull` して通らないこと。
  - **上限の検査が `done_set` だけで `reset_set` を見ていなかった**（P2）: 上限に達した実行中の下流が `--over-cap` なしで戻り、再試行されていた。reopen が pending に戻す全部の集合（I・done_set の中間と末端・reset_set）の検査は `_over_cap()` の 1 関数を通る。拒否は ATTEMPT_CAP（exit 3・何も書かない）。`--over-cap` の理由は reset した下流の台帳（`reset` の `over_cap`）にも残る。
  - **mission の再活性化が I の card の後ろだった**（P2）: 間で止まると再実行が `NOT_REOPENABLE` で止まり、mission が done のまま dispatcher に Worker を退役させた。再活性化は**検査を全部通った後・最初の書き込み**（`reopen_task(before_write=)`）に移した。加えて、旧い順序で止まった残り（I が reopen 済みで mission が done）の出口として、`plan.sh reopen` は `reopen_is_complete`（I が pending で、今の試行に `reopen_count - 1` の reopen の台帳がある）のときだけ、`NOT_REOPENABLE` の前に mission を戻して exit 0 で終わる。reopen を書き終えた形でないときの拒否は mission を書き換えない。
- **PR-F の Codex 4 巡目（PR #308・t054）で直した 2 点**（P1×2）。1〜3 巡で「経路の終点」を status ごとの足し算で書いていた族の仕上げ:
  - **経路の終点（targets）の定義を 1 か所にした**（P1）: `lib_task_controller._reopen_targets` = 「cascade が選ぶ下流（status を問わない）」∪「実行中の下流」∪「前の実行で戻した下流（台帳の印）」。
    経路の閉包・skipped / failed の拒否・戻す集合（done_set / reset_set）・stale_runnable・released_deps の掃除は全部ここから導く。実行中の B(code) は reset されるが選ばれていないので、
    I(done) → A(done) → B(in_progress) で A が done のまま残り B がすぐ pull し直せた。前の実行で戻した下流（reset 後に止まった B）も、再実行で数えないと同じ穴になる。
    総当たり: `tests/test_reopen_targets_matrix.py`（グラフ 3 種 × 各ノードの状態 14 通り（語彙の全部 + 一度も走っていない pending）× skills × cascade 3 値。期待する集合はグラフから独立に導く。
    拒否なら queue が 1 バイトも変わらない・成功なら経路上に終端が残らず pending は pull できない・書き込みの全点で kill → 再実行でも同じ）。
  - **壊れた card があれば reopen を拒否する**（P1）: `list_tasks()` は読めない・壊れた card を `blocked_by=[]` の孤立 node に置き換えるので、I → A → R の A が壊れていると I から A・R への辺が消え、R を戻さずに成功した。
    `CASCADE_UNREADABLE_CARDS`（exit 3・何も書かない・壊れた card の id だけ。plan.sh が `[破損]` の pseudo-task を数えて Controller に渡す）。出口は card を直して同じコマンドを打つこと。
    **族の洗い出し**: graph を作って経路を辿るのは `cmd_reopen` だけ。`retry` は task 1 枚（graph を作らない）、`store-check` の cascade-incomplete は台帳と card の組から導き graph を使わない
    （`tests/test_reopen_cascade_plan_sh.py::test_retry_and_store_check_do_not_depend_on_the_whole_mission_graph`）。
- **Result の帰属を `new_body` の有無で判定すると、古い Result が現在の試行に再帰属される**（Codex 3 巡目 P2。実装 PR D）。
  - **PR-D（t019）で解いた**: `result_replaced` を Controller の明示の引数にし（plan.sh の done / fail だけが渡す）、`verify-result` は渡さない。needs-director は `## Result` 節を置き換えない
    （理由は frontmatter と `## Needs-Director 詳細`）ので渡さない — 設計の「done / fail / needs-director の --result-file」のうち、実際に Result 節を組み直すのは done / fail だけ。
    網羅テスト: `tests/test_history_ledger_controller.py` / `tests/test_history_ledger_plan_sh.py`。以下は解く前の記述。
  - 漏れている状態: `verify-result` は既存の本文に Verification 節を足した `body_new` を渡す。帰属を「`new_body` があるか」で決めると、Result 節を置き換えていないこの呼び出しでも、既存の（古い）Result が現在の試行のものとして台帳に書かれる。
  - 起きる害: 台帳の Result と試行（`execution_id`）の対応が崩れ、前の試行の Result が今の試行の結果として `previous_attempt` や監査に出る。
  - 直し方の方向（未決）: 帰属を更新するのは Result 節を実際に置き換えた呼び出し（`done` / `fail` / `needs-director` の `--result-file`）だけ、と明示の引数で指定する（`new_body` の有無から推測しない）。
  - 解く PR: D。網羅テストに入れるケース: `verify-result` だけを呼んだ後の台帳の帰属が変わらないこと、`done` / `fail` / `needs-director` の `--result-file` ありとなし、Result 節を置き換えず本文の別の節だけを足す呼び出し、同じ報告の再送。
- **別の遷移で閉じた試行の孤児が store-check に報告されない**（Codex 3 巡目 P2。PR-D の `lib_history.py:411-417`。解く PR は E か F、または store-check の単独 PR）。
  - **PR-E（t031）で解いた**: `lib_history.orphan_reason()`。結果の台帳（`end` / `reset` / `retire`）は、その試行の `execution_status` / `end_code`（今の試行は card、過去の試行は execution record）と突き合わせる。試行がまだ active なら孤児（`<kind>`）、終わっていても **`end_code` が台帳と違えば孤児**（`<kind>:closed_by=<END_CODE>`）。同じ `end_code` で閉じていれば（閉じた kind が done でも）孤児ではない。過去の試行（pull で次の試行に進んだ後）も record で判定するので、reset の後に次の Worker が pull しても報告は残る。record が読めないときは推測で孤児にしない。`retry` の台帳は、今の試行のもので card がまだ retry の遷移元 status のままなら孤児。報告は 1 台帳ファイル 1 件で、読み取りだけ。網羅テスト: `tests/test_retry_plan_sh.py`（done 孤児 × reset / retire / `--close-execution` / 何も閉じない / done で追いついた、2 回報告しない、書かない）。
    **残る限界**: done の孤児と同じキー (X, `end`, reopen) の別の終わり方（`fail` / `needs-director`）は、再送の比較で `HISTORY_MISMATCH` になり card を書けない（孤児を差し替える手段が無い — 下の QA 項目 (3)）。この PR の範囲は「報告する」まで。以下は解く前の記述。
  - 漏れている状態: 孤児（台帳に DONE があるのに card が追いついていない試行）の検出は、current execution が active の間しか走らない。`done` が台帳だけ書いて card の前に落ち、その後 `reset` / `retire` が同じ試行を別の kind で閉じると、台帳には DONE が残るのに終端の試行は検出の対象外になり、store-check が何も報告しない。
  - 起きる害: 台帳に「起きていない完了」が残り、`previous_attempt` や監査がそれを読む。診断（孤児の報告）は出ない。card と record は正しいので、実行時の動作は壊れず、見えない食い違いだけが残る。
  - 直し方の方向（未決）: active でない（終端の）試行でも、台帳とコミット済みの遷移を突き合わせ、遷移の kind と台帳の DONE が食い違うものを孤児として報告する。
  - 網羅テストに入れるケース: `done` が台帳だけ書いて落ちた後に `reset` で閉じる / `retire` で閉じる / 何も閉じない（active のまま）、それぞれで store-check が孤児を報告するか。閉じた kind が `done` のときは報告しないこと（偽陽性なし）。同じ試行を 2 回報告しないこと。報告は読み取りだけで台帳・card・record を書かないこと。
- 実装の PR（D・F）の QA で確かめること: (1) cascade を各書き込みの後で kill して再実行し、`cascade-incomplete` が出て再実行で消えること・`reopen_count` が二重に増えないこと。(2) 孤児の台帳を作ってから同じ報告を送り直すと、新しいファイルが増えず `ended_at` が台帳と card・record で一致すること。(3) 孤児の台帳のあとに違う Result で送ると `HISTORY_MISMATCH` で card が書かれないこと（この拒否のあとの出口が「同じ報告を送り直す」だけでよいかは、運用で足りなければ孤児の差し替えを別に設計する）。
- 範囲外として残したこと: Verifier の運用と、その verify pass が依存先の変更を見る検査（PR-J）。`update --status` の Controller 化（P0 の残り）。回復を dispatcher が起こす・待つ（P0 の残り）。
- 確かめていないこと: mission の戻し先 `in_progress` が lint / dispatcher で想定どおりか（PR-A）。retry / reopen の後に `worktree_gc` で隔離済みの worktree を pull が作り直せるか（PR-E の QA）。reopen した codex-review task が、`registry/daemons/review-refusals/` の古い拒否記録で spawn されないことがあるか（記録は消してよい。`scripts/CLAUDE.md` の「通知・記録」）。`done` → `pending` を Taskvia が受け付けるか。

- **PR-F の方式変更（t059・ユーザー判断 2026-10-17 の提案 B）**: PR #308 の Codex 5 巡（t037〜t054）は『再実行が済んだ所を推測し直す』族を 5 回出した。推測をやめ、§4.3.1 の intent 方式にした。上の 2・3・5 巡目の『再実行までの間に下流が動く』『legacy の実行中 B』は、一覧が最初に固定されるので起きない（`tests/test_reopen_intent_recovery.py`）。消したテストと理由は t059 の Result。
