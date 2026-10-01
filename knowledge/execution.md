# Execution + Task Controller (vNext 01c) — 設計 (ADR)

vNext 01c の実装 PR (E1〜E4) が従う設計。**コードの根拠はすべて main `d887acf`** (01b の記録 #268 の merge commit) の行番号。

- 入力: 原案 `~/obsidian/proposals/crewvia/crewvia-vnext-mission-01-control-plane-foundation.md`
  (§3.3 / §7.2 / §7.4 / §8.4 EXEC-01〜05 / §8.5 CTRL-01〜05 / §8.6 COMPAT-01〜04 / §9 / §10.5・10.6 / §11 AC-03〜05 / §14)、
  改訂案 `~/obsidian/proposals/crewvia/20260930_crewvia-vnext-mission-01-revision.md`
  (§2-C / §3 の表「Execution ID」「孤児の回収」/ §4-R3 の 01c 行 / R4「EXEC / CTRL (01c)」/ §7)、
  01a / 01b の引き継ぎ: `knowledge/state-store.md` §0・§1.4・§2.1・§10.3、`knowledge/git-policy.md` §1・§10・§16.4
- ユーザー決定 (改訂案 §7): **R2** — 呼び出し元ゼロの lib は通常どおり merge、**呼び出し側を移す PR は merge 前にユーザーの承認を取る**
  (plan.sh は主 checkout から直接実行されるので merge = cutover)。env の停止スイッチで新旧を切り替えない (不変条件 5)。
  **新しい ID は既存の世代 (card の `started_at` + `.identity`) の置き換えとして入れる** (並行して別の仕組みを作らない)
- PR と段の対応: E1 = t004 (lib・呼び出し元ゼロ) / E2 = t008 (pull) / E3 = t012 (done・fail・needs-director・
  ready-for-verification・verify-result) / E4 = t016 (update --reset・retire・reap・退役 marker)、本番確認 = t020、記録 = t021。
  **E4 は E4a (t016: marker の producer を切り替える) と E4b (新 task: 世代の照合を外す) に分ける提案** (§7・§9.2 の 4)。
  以下で単に「E4」と書くときは両方を指す
- この文書の決定は 9 つ (§1〜§9)。どれも「選んだ案・捨てた案・理由」を書く。§10 が禁止事項・不変条件との確認表、
  §11 が E1〜E4 に渡す族ごとの掃除の対象一覧

## 0. このミッションでやらないこと

| 項目 | 扱い | 理由 |
|---|---|---|
| Execution status の `stale` / `abandoned` / `recovered` | 作らない (原案 EXEC-04 で後続) | 「動いていない」の判定は watchdog / dispatcher の観測の話で、Controller の遷移ではない |
| Worker の選択・dispatcher の割り当て規則 | 変えない | 原案 §7.2「担当しない」。dispatcher は書かないまま (原案 §14-6) |
| `target_dir` の task の branch / base | 触らない (01b と同じ) | git-policy.md §10 の 7 |
| Taskvia への execution の同期 | しない | COMPAT-04。Taskvia の契約 (`started_at` = `now_iso()` plan.sh:2387) は今のまま |
| `started_at` の形式 | 変えない | **watchdog が時刻として読む** (watchdog.py:282-286 `parse_iso_epoch(task_card.get("started_at"))` が idle 時計の起点)。不透明な ID に置き換えると idle 判定が壊れる。`started_at` は「今の試行の開始時刻」として残し、**世代としての照合だけ**を execution_id に移す (§2) |
| one-shot migration (進行中の card に ID を後付け) | しない | §7 |
| mission の status 語彙 | 範囲外 | 01a §0 と同じ |

---

## 1. Execution ID の置き場と正本 (EXEC-01 / 03 / 05)

### 1.1 実測: 今の「試行」の識別 (d887acf)

- 世代は card の `started_at` (`now_generation()` plan.sh:487-497。µs 精度の UTC、docstring で「不透明な識別子」と宣言)。
  pull が書く (plan.sh:3538-3541)。
- projection は `queue/assignments/<agent>` (本文 `<slug>:<tid>`) と `<agent>.identity` (JSON: mission / task / worker /
  started_at)。書くのは `Txn.publish_assignment` (lib_state_store.py:1054-1060) の 1 か所で、identity → 本体の順。
- 監査ログの `execution_id` は null 固定 (lib_state_store.py:1144)。今の試行は `generation` 欄 (= `started_at`) に出る。
- card のキーの並び `TASK_META_KEY_ORDER` (lib_state_store.py:498-504) は `worker` / `started_at` / `completed_at` が隣り合う。
  `dump_yaml` は**知らないキーを末尾に足して保持する** (lib_state_store.py:510-524)。`lint_plan.py` に未知キーの検査は無い
  (`grep -n "meta.keys\|not in TASK_META\|unexpected key" scripts/lint_plan.py` が 0 件)。
- `task_slug` は card の欄ではない。pull のたびに title から作る (`_slugify` plan.sh:3607-3612、使用 :3616)。
  branch は `task/{mission_slug}/{task_id}-{task_slug}` (lib_git_policy.py:44)。

### 1.2 決定: card の frontmatter が正本。record は projection

**正本 (authority) は task card 1 枚のまま。** card に 5 欄を足す (`started_at` の直後。`TASK_META_KEY_ORDER` に入れる):

| 欄 | 型 | 意味 | 書き手 |
|---|---|---|---|
| `current_execution_id` | `ex-<32 hex>` / なし | **最新の**試行の ID。新しい reserve まで残る (terminal になっても消さない) | reserve |
| `execution_status` | `reserved` / `running` / `completed` / `failed` / `released` / なし | その試行の status。task の `status` とは別の欄 (原案 EXEC-05「同じ field で表現しない」) | Controller の全操作 |
| `execution_end_code` | §4.4 の終了コード / なし | その試行を**終わらせた操作**の固定コード (`DONE` / `VERIFIED` / `WORKER_FAILED` / `NEEDS_DIRECTOR` / `VERIFICATION_REJECTED` / `RESET_BY_DIRECTOR` / `RETIRED` / `WORKSPACE_CREATE_FAILED`)。`execution_status` を terminal にする**同じ card の書き込み**で入れる。active の間と reserve の直後は空。§4.4 の再送判定はこの欄だけを読む (record・task の status を読まない) | terminal にする Controller の操作 |
| `execution_count` | 整数 / なし | 最新の試行の attempt 番号 (= この task で発行した試行の数)。1 から単調増加 (EXEC-02) | reserve |
| `task_slug` | 文字列 / なし | 最初の reserve で title から作って**固定**する。以後の reserve はこの値を使う (git-policy.md §10 の 1) | 最初の reserve |

- `execution_end_code` を card に置く理由 (Codex P1-1): §4.4 は同じ ID の再送を「同じ操作なら成功・違えば conflict」で分け、
  `failed` の中でも `WORKER_FAILED` と `RESET_BY_DIRECTOR` で答えが違う。終了理由を record にしか置かないと、card のコミット直後
  (record の前) に落ちたとき R-5 が再生成する record では理由が分からず、task の status も reset の後は Director が自由に変えられる
  (`update --status X`) ので復元の根拠にならない。終了理由を正本に置けば、どの crash の点でも再送の答えは card 1 枚で決まる。
- 欄の組の整合 (lint と `STATE_INVALID`。E1): `execution_status` が `reserved` / `running` なら `execution_end_code` は空、
  `completed` / `failed` / `released` なら空でなく、組み合わせが §4.4 の表にあるもの (`completed` は `DONE` / `VERIFIED` だけ等)。

- **active な試行** = `execution_status ∈ {reserved, running}` **かつ** task の status が assignment を持つ status
  (`ASSIGNMENT_HOLDING_STATUSES` lib_task_status.py:56-61)。両方を要求する理由は §9.3 (旧コードに戻した後に残る古い欄を
  active と読まないため)。1 task の active な試行は最大 1 件 (EXEC-05) — 欄が 1 組しか無いので構造的に 1 件。
- **ID の形式**: `ex-` + `uuid.uuid4().hex` (32 桁の小文字 16 進。UUID v4 の衝突耐性 = EXEC-01 の下限)。agent 名・task ID・時刻から作らない。
  生成器は Controller の引数で注入する (`id_factory`。既定は uuid4。テストは固定列を渡す)。発行後は変えない。
- **attempt**: reserve が card の `execution_count` を読んで +1 し、同じロックの中で card に書く。並行 reserve は `queue/.lock` で
  直列化されるので重複しない (EXEC-02。2 本目は task が pending でないので拒否される — 今の pull と同じ)。
- **`started_at` は残す**: reserve が今どおり `now_generation()` で書く。意味は「今の試行の開始時刻」(watchdog の idle 時計)。
  世代としての照合は §2 の順で execution_id に移し、E4 の後は照合に使わない。

**Execution record (EXEC-03) を作る。置き場所は `queue/missions/<slug>/executions/<execution_id>.json`。**

- **record は projection**。判断 (遷移の可否・照合) には一切使わない。読むのは `get_execution` (履歴の表示)・`store-check`・
  R-5 (§1.4) だけ。だから正本は 2 枚に分かれない。
- 識別子はファイル名 (不変条件 2 と同じ作法。改訂案 R4)。中身の `execution_id` 欄は照合用の写し。
- JSON にする (原案は YAML): 書くのも読むのも lib だけで手編集しない・`.identity` と同じ形式・crewvia の YAML パーサ
  (`parse_yaml` の厳格な部分集合) に新しい形を足さない。原案は「Path は実装上変更してよい」。
- mission の dir の中なので `plan.sh archive` (dir ごと rename) で一緒に退避される。`tasks/` の外なので `list_tasks` / R-3 の
  列挙には入らない (E1 で `executions/` を読む・数えるコードが無いことを確かめる。§11 の E1 行)。

**state-store.md §2.1 の再判断条件「正本が 2 枚以上に分かれる」には当たらない → ジャーナル方式は採らない。**

- 最新の試行の record は card から**完全に再生成できる** (§1.3 の表)。過去の試行の record は、card が次の試行に進んだ時点で
  凍結され、以後どのコマンドも書かない・読まない (判断に使わない) ので、欠けても状態の正しさは変わらない (履歴が欠けるだけ。
  store-check が報告する)。
- 書く順序は 01a §2.1 の規則にそのまま乗る: **card がコミット点、record は card の後** (始める側も終わらせる側も)。
  assignment / identity の projection も今の順 (始める側は card → identity → 本体、終わらせる側は card → 撤去)。
  record は card の直後・assignment の前に書く。

捨てた案:

| 案 | 捨てた理由 |
|---|---|
| A. record を正本にし、card には `current_execution_id` だけ置く (原案 EXEC-05 の字面) | 試行の status の正本が record、task の status の正本が card になり、1 つの操作 (done = 試行 completed + task done) が 2 枚の正本を書く。§2.1 の再判断条件に当たり、ジャーナル (PREPARED / COMMITTED) が要る。01a が避けた族 C (新しいガードが新しい失敗状態を作る) をそのまま持ち込む |
| B. record を作らず、履歴は監査ログだけ | 監査ログは正本でも回復の材料でもなく、行が欠けうる (state-store.md §4「crash で行が欠ける」)。AC-03「1 Task の Execution 履歴と attempt 番号を保持できる」を欠けうる置き場で満たすことになる。record は最新を card から再生成でき、過去の分は凍結されるので、監査ログより強い |
| C. 試行の履歴を card の frontmatter に積む (`executions: [...]`) | 原案 EXEC-03「Task file を履歴置き場にしない」。card の書き換えが試行ごとに太り、手で読む card が読みにくくなる |
| D. `execution_status` を持たず task の status から導く | in_progress だけでは reserved と running を区別できない (§6 の pull の冪等化に要る)。pending だけでは released と failed を区別できない |
| E. ID を `started_at` そのものにする (形式だけ変える) | watchdog が時刻として読んでいる (§0)。時刻は不透明な ID ではない (改訂案 §2-C)。同じ秒に 2 回の reset + 再 pull で衝突した過去がある (`now_generation` の docstring) |

### 1.3 record の中身と、card からの再生成

```json
{
  "schema_version": 1,
  "execution_id": "ex-7d847b94c5ea42d28c4bbbfe9af8c642",
  "mission": "20261001-vnext-01c-execution-controller",
  "task": "t004",
  "attempt": 1,
  "agent": "Hana",
  "status": "running",
  "reserved_at": "2026-10-01T10:00:00.123456Z",
  "running_at": "2026-10-01T10:00:03Z",
  "ended_at": null,
  "end_code": null,
  "git": {"branch": "task/…/t004-…", "base": "origin/main", "pr_base": "main",
          "worktree": "/abs/path", "head_at_start": "0123abcd…"}
}
```

record の欄は 2 種類に分ける。**R-5 が card に合わせるのは「追従する欄」だけ**で、「不変の欄」は record を作るときに 1 回だけ
書き、以後どの回復も上書きしない (Codex P1-2)。

| 欄 | 種類 | 再生成の出どころ (card) | 再生成できないとき |
|---|---|---|---|
| execution_id / attempt | 不変 | `current_execution_id` / `execution_count` | — (必ずある。どちらも次の reserve まで card で変わらない) |
| mission / task | 不変 | ファイル名 (不変条件 2) | — |
| agent | **不変** | 作るとき**だけ** `worker` | 作るときに worker が空 → null で作り `reported:execution_record_owner_unknown` (下の「いつ作られるか」で、通常運用では起きない) |
| reserved_at | **不変** | 作るとき**だけ** `started_at` (reserve が同じ値で書く) | 同上 |
| git.branch / base / pr_base / worktree | 不変 | `task_slug` + Resolver (`lib_git_policy`。決定的) | `task_slug` が無い legacy の card は対象外 (record を持たない) |
| status / end_code | **追従** | `execution_status` / `execution_end_code` | — (必ずある) |
| running_at / ended_at / git.head_at_start | 追従 (書いた操作が入れる) | **card に無い** (判断に使わない詳細) | R-5 が作る・合わせるときは既存の値を残し、無ければ null。R-5 の監査行に残す |

- **不変の欄を上書きしない理由**: reset・`verify-result fail`・retire は card の `worker` / `started_at` を null にし、Director の
  `update --worker B` は active な試行の worker を書き換える。どれも正しい操作で、正本が「今の持ち主は誰か」を言い直しただけ。
  record の `agent` / `reserved_at` は「**誰がいつ予約した試行か**」という履歴で、card の今の値とは意味が違う。card に合わせて
  上書きすると、正しく保存済みの record が次の回復で null に壊れる。
- **いつ作られるか (不変の欄が card から取れる時点)**: record は reserve が card の直後に書く。そこで落ちても、その card を名指しする
  次のロックの回復 (R-5) が作る。worker / started_at を null にする操作 (reset・retire・verify-result fail) と worker を書き換える
  `update --worker` は**どれも card を名指しする**コマンドで、回復 (`recover_before`) は本体より前に同じロックの中で走る。だから
  これらが worker を消す時点では record は必ず既にある (無ければ直前の R-5 が reserve 時の値のまま残っている card から作る)。
  null で作るのは「record を手で消した」「読めない record が後で消えた」場合だけで、それは報告にする (推測で埋めない)。
- **終了理由の本文は保存しない**: `end_code` (§4.4 の固定コード。card の `execution_end_code` の写し) だけ。理由の文は
  needs-director の `needs_director_reason` や fail の Result が今どおり card に持つ。record に自由文を入れない (原案 CTRL-04
  「credential や全 log を record へ保存しない」、01a の監査ログの規則と同じ)。
- `git.head_at_start` は pull が worktree を作った後 (ロックの外) に `git -C <worktree> rev-parse HEAD` で取り、start に渡す。
  W2 で前の試行の commit が残った worktree を再利用したとき、**どの commit から始めた試行か**を後から追えるようにする (§3)。

### 1.4 回復規則 R-5 (record の projection) と crash の表

01a の R-1〜R-4 (state-store.md §2.3) に 1 つ足す。同じ作法 (コマンドの前提検査より前・名指しの範囲だけ・拒否を足さない・
修復 1 件ごとに `op=recover` を即時追記・正本を書かない)。

| 規則 | 条件 (すべて) | 書くもの |
|---|---|---|
| **R-5** record を card に合わせる | 名指しの card に `current_execution_id = X` (形が正しい) / `executions/X.json` が ABSENT (ENOENT だけ) か、読めて**追従する欄** (`status` / `end_code`) が card と食い違う | ABSENT なら card から §1.3 の表で作った record。ある record は**追従する欄だけ**を書き換え、不変の欄 (`execution_id` / `attempt` / `mission` / `task` / `agent` / `reserved_at` / `git.*`) と card に無い欄は既存の値を残す |

- **`agent` / `reserved_at` を食い違いの判定に使わない** (Codex P1-2)。card の `worker` / `started_at` は reset・retire・
  `verify-result fail` の後は null、`update --worker` の後は別の名前になるのが正常で、record と食い違って当然。
- 不変の欄 `execution_id` / `attempt` が card と食い違う record (`X.json` の中の `execution_id` ≠ X 等) は手編集か壊れた書き込みなので、
  上書きせず `reported:execution_record_identity_mismatch` (読めない record と同じ扱い)。
- 読めない record (EACCES・壊れた JSON) は**消さない・上書きしない**。`reported:execution_record_unreadable` (01a の UNVERIFIABLE と同じ扱い)。
- card が `[破損]` なら何もしない (01a と同じ)。
- **過去の試行の record は R-5 の対象外** (card の `current_execution_id` が指すものだけ)。凍結の意味。
- R-1 の identity の再生成は `execution_id` 欄を card の `current_execution_id` から入れる (§2 の行 5)。

| 操作 | 落ちた点 | 次のロック取得時 | 根拠 |
|---|---|---|---|
| reserve | card (X, reserved) の後・record の前 | R-5 が X を作る。R-1 が identity (X) → 本体 | 正本が「A が X を予約した」と言っている |
| start | card (running) の後・record の前 | R-5 が status を running に | 同上 |
| complete / fail / release | card (terminal + `execution_end_code`) の後・record の前 | R-5 が status / end_code を合わせる (agent / reserved_at は既存の record のまま。card の worker が null でも触らない)。R-2 が枠を撤去 | 終わらせる側も正本が先。終了理由も card にあるので、この点で落ちても §4.4 の再送判定は変わらない |
| reset / retire / verify-result fail | card (worker・started_at を null) の後・record の前 | 同上。record は reserve の時点か、この操作の直前の回復で既にある (§1.3「いつ作られるか」) | 不変の欄は card から取り直さない |
| `update --worker B` (active な試行) | card の後 | R-5 は何もしない (追従する欄は変わっていない)。record の agent は予約した A のまま | record の agent は履歴 |
| どれでも | record の後・assignment の前 | 01a の R-1 / R-2 のまま | record は判断に使わないので、record が先に進んでいても結論は変わらない |
| reserve (前の試行が凍結される) | 新しい card の後 | R-5 は新しい X だけを見る。前の試行の record は reserve の**前**の回復で card に合わせ済み (reserve は回復の後に走る) | 前の試行の terminal 状態は reserve より前に card にあった |

### 1.5 監査ログの `execution_id`

- 本体の行: card を書いたトランザクションは、書いた後の card の `current_execution_id` を入れる (`generation` 欄が書いた後の
  `started_at` を入れるのと同じ: plan.sh:789-790)。legacy の card (欄なし) は null のまま。
- 回復の行: R-1 / R-5 は card の `current_execution_id`。
- 拒否の行 (§8): 呼び出し元が名乗った ID ではなく、**card の** ID を入れ、名乗った ID は入れない (照合に失敗した値を記録の正として残さない。
  形が正しければ `detail` に `presented=ex-…` を入れる)。
- 門 (`_append_audit` の `_safe_*`) に `_safe_execution_id` (`ex-[0-9a-f]{32}` の完全一致。外れたら null) を足す。

### 1.6 正本 (card) に無い値を判断・回復・照合に使っている箇所 (族の洗い出し)

state-store.md §10.3 の規則「card に無い値を projection にだけ置くと R-1 が復元できない」を、この設計の全体に当てる。
Codex P1-1 (終了理由が record にしか無い) と P1-2 (R-5 が reset 後の card で record の持ち主を上書きする) は同じ族。
record・identity・枠の本文・退役 marker・監査ログ・呼び出し元の名乗りに出てくる値を全部挙げ、判断・回復・照合に使うものを
「card に持つ」「card から再生成できる」「判断に使わない」のどれかにする。

| # | 値 (置き場) | 使う場所 | 使い方 | 扱い |
|---|---|---|---|---|
| 1 | 終了理由 (record `end_code`、旧案の `failure.code`) | §4.4 の再送判定 | 判断 | **card に持つ** (`execution_end_code`。§1.2)。record は写し。再送判定は card だけを読む |
| 2 | 予約した agent / 予約時刻 (record `agent` / `reserved_at`) | R-5 の食い違い判定 (旧案) | 回復 | **判断に使わない** (R-5 の比較から外す。§1.4)。作るときだけ card から取り、以後不変 (§1.3) |
| 3 | 同上 | §8 の監査の `actor` (旧案「record / card の worker」) | 記録 | **card から**: 本体の操作の前の card の `worker`。record は読まない (§8 を直した) |
| 4 | 同上 | §9.3 の `reported:execution_fields_stale` (card の `started_at` ≠ record の `reserved_at`) | 報告だけ | **判断に使わない**: 報告を出すだけで何も書かない。task が holding・試行が active・card の `started_at` が空でないときだけ比べる (reset の後の null で誤報しない)。直すのは Director の `update --reset` で、その可否は card が決める |
| 5 | 試行の status / attempt / ID (record) | R-5 の比較 | 回復 | card が正本 (`execution_status` / `execution_count` / `current_execution_id`)。record を card に合わせる向きだけ |
| 6 | running_at / ended_at / git.head_at_start (record) | 表示・`get_execution` | 表示 | **判断に使わない** (§1.3)。欠けても null |
| 7 | `execution_id` / `started_at` (identity) | `classify_assignment` (#4)・`assignment_execution_verdict` (#11)・R-1 (#5) | 照合 | **card から再生成**: R-1 が `current_execution_id` / `started_at` から作り直す (§1.4 末尾)。食い違えば card が勝つ |
| 8 | `<slug>:<tid>` (枠の本文 `queue/assignments/<agent>`) | dispatcher の busy・`is_orphan_target` | 判断 | **card から再生成** (R-1 / R-2。01a のまま) |
| 9 | `task_execution_id` / `task_started_at` (退役 marker) | `_guard` / `_cleanup_deferred` / `_settle_terminated` (#12・#13)・retire の照合 (#8) | 照合 | **名乗り (証拠) であって正本ではない**: request を書いたロックの中で card から写した「どの試行を終わらせるか」。照合の答えは card が出す (card の今の試行と一致したときだけ後始末する)。marker が欠ける・読めない・欄が無いときは**保留して Director** (今の `_cleanup_deferred` と同じ hold。推測で後始末しない)。card から再生成しない理由: 後始末の時点で card は後任の試行に進んでいるかもしれず、そのとき card から作ると後任を終わらせる |
| 10 | `CREWVIA_EXECUTION_ID` (`.crewvia-env`・env)・`--execution`・pull の JSON | §5.2 の照合 | 照合 | **名乗り**。card の `current_execution_id` と比べるだけで、card に書かない。欠けたら「名乗りなし」(§5.2) |
| 11 | 監査ログ (`caller_check` / `execution_id` / `refused:`) | E5 の merge 条件 (§9.2 の 1)・本番確認 | cutover の判断 (人) | **判断の唯一の根拠にしない**: 監査は行が欠けうる (state-store.md §4) ので、欠けると `unverified` が少なく数えられる向きに誤る。E5 の条件は監査の 0 件に加えて、**コードと手順から言えること** (worker.md・kai-review・skill が ID を渡す・生きている Worker のセッションが全部その変更の後に起動した。§9.4) を要る |
| 12 | `.crewvia-env` の `CREWVIA_TASK_SLUG` | worktree のパス | 表示・パス | card の `task_slug` (§1.2) から pull が毎回書く。判断に使わない |

- 表に無い値で判断・回復・照合に使うものを足す PR は、この表に行を足す (E1〜E4 の Result の族の表 §11 と同じ)。

---

## 2. 世代の置き換え (改訂案 §5「並行して別の仕組みを作らない」)

### 2.1 今 `started_at` を世代として照合・転写している箇所 (全部)

調べ方: `timeout 120 grep -rnE '<pat>' scripts/ hooks/ crewvia | grep -v scripts/test_` (§11.1 に件数)。「時刻として使う」箇所
(watchdog の idle 時計・Taskvia) は照合ではないので表の外に置き、§2.3 に別に書く。

| # | 場所 | 何をしている | R/W | 移す段 |
|---|---|---|---|---|
| 1 | plan.sh:487-497 `now_generation` | 世代を作る | 源 | 残す (時刻として。§0) |
| 2 | plan.sh:3538-3541 `cmd_pull._do` | card に `started_at` を書き、`started_holder` に持つ | W | E2: 同じ場所で reserve が `current_execution_id` / `execution_status` / `execution_count` / `task_slug` も書く |
| 3 | plan.sh:2006-2014 → lib_state_store.py:1054-1060 `publish_assignment` | identity に `started_at` を写す | W | E1: identity に `execution_id` 欄を足す (引数で渡す)。E2 から呼び出し側が渡す |
| 4 | lib_state_store.py:1082-1104 `Txn.classify_assignment` | identity の `started_at` と世代を比べ MINE / SUCCESSOR | R | E1: 引数 `execution_id` を足す (§2.2 の規則)。E4 で世代の比較を外す |
| 5 | lib_state_store.py:1359-1422 `_r1` (**R-1**) | card の `started_at` で identity を作り直す / 一致を確かめる (`generation_mismatch`) | R/W | E1: card に `current_execution_id` があれば identity にも入れ、照合も ID で。legacy の card は今のまま |
| 6 | lib_state_store.py:1312-1356 `_r2` (**R-2**) / :624-632 `is_orphan_target` | 世代を見ない (`retire_assignment(..., None)`) | — | 変えない (撤去するのは card が手放した枠だけ。世代が要らない) |
| 7 | plan.sh:3204-3241 `_pull_worktree_failed` (**G1 の CAS** :3226-3228) | status・worker・`started_at` がこの pull の予約のままか | R | E2: `current_execution_id == X` かつ `execution_status == reserved` の CAS に置き換え (git-policy.md §16.4 の 5) |
| 8 | plan.sh:6182-6338 `cmd_retire` (必須の `--started-at` :6243-6256、照合 :6299-6305、classify :6310) | worker・`started_at`・枠の世代 | R | E4: `--execution <id>` を足す。移行期は両方を受け付ける (§2.2) |
| 9 | lib_retirement.py:1025-1066 `_write_request` / :766-799 `build_request` / :808-828 `_carried_from_request` | 退役 marker に `task_started_at` を写す | R card → W marker | **E4a**: marker と progress に `task_execution_id` を足す (card の `current_execution_id` を同じロックの中で読む)。`task_started_at` は残す。E4b より先に入れる (§7) |
| 10 | lib_retirement.py:550-592 `read_task_started_at` | card の `started_at` 行を読む | R | E4: `read_task_execution_id` を足す (同じ読み口) |
| 11 | lib_retirement.py:605-660 `assignment_execution_verdict` | identity の `started_at` と marker の世代 (ロックなし) | R | E4: 両方に execution_id があればそれで比べる。無ければ今の世代 |
| 12 | lib_retirement.py:1917-1940 `_bound_generation` / :1494-1510 `_guard` / :1581-1610 `_recheck_unprovable` / :1944-1980 `_cleanup_deferred` | marker の世代を証拠にする | R | E4: 証拠は execution_id 優先。どちらも無ければ今どおり保留 → Director |
| 13 | lib_retirement.py:2037-2039 `_settle_terminated` | `plan.sh retire --started-at <gen>` を組み立てる | exec | E4: marker に ID があれば `--execution <id>` を渡す |
| 14 | plan.sh:789-790 / :805 `save_task` / `create_task` → lib:1117-1153 | 監査ログの `generation` | W audit | E1: `execution_id` も入れる (§1.5)。`generation` 欄は残す (読み手の互換) |
| 15 | plan.sh:5945-5953 `update --reset` / :6315-6321 retire reset | `started_at` を null にする | W | E4: 試行を release / fail に (§4)。`started_at` の null は今どおり。`current_execution_id` は残す |
| 16 | plan.sh:3014-3033 `_env_mission_for_task` | `CREWVIA_MISSION_SLUG` + `AGENT_NAME == worker` + status (mission の曖昧さの解決だけ) | R | E3: `CREWVIA_EXECUTION_ID` が card の `current_execution_id` と一致すれば、それを証拠に使う (agent 名の一致より強い)。無ければ今のまま |
| 17 | agents/director.md:1138-1143 / :1485-1497 / :1511 | Director が `sed` で card の `started_at` を読み `retire --started-at` を打つ | 文書 | E4: `--execution` を `plan.sh status` の表示から渡す形に |

`execution_id` が今コードに現れるのは #14 の 1 か所 (null 固定) だけ (`grep -rn execution_id scripts/ hooks/ | grep -v test_` = 1 件)。

### 2.2 移行期に両方を照合する規則

移行期 = E2 の merge から、§7 の条件 (E4a が両デーモンで動いている・legacy の進行中 = 0・旧形式 marker = 0) を満たして E4b が merge されるまで。規則は 1 つの関数
(`lib_task_controller` の `execution_matches`。E1) に置き、#4・#7・#8・#11 がそれを呼ぶ (コピーしない。原案 §14-7)。

| card | 名乗られた証拠 | 判定 |
|---|---|---|
| `current_execution_id = X` (E2 以降に予約された試行) | execution_id = X | **一致** |
| 同上 | execution_id = Y (≠ X) | **不一致** (`EXECUTION_NOT_CURRENT`)。`started_at` が一致していても不一致 (ID が優先) |
| 同上 | execution_id なし・`started_at` だけ (旧 marker・旧引数) | `started_at` が一致すれば**一致**、ただし監査行に `caller_check=legacy_generation` を残す (E4 の後は不一致に倒す) |
| `current_execution_id` なし (legacy の試行) | `started_at` | 今の世代の照合のまま |
| 同上 | execution_id | **不一致** (この card はその ID を発行していない) |

identity (projection) 側も同じ: identity に `execution_id` があれば ID で、無ければ `started_at` で比べる。
「片方にだけ ID がある」組 (card に X・identity に ID なし) は、identity が E2 前に公開されたもの (= その時点の card には X が無い) か
旧コードの publish なので、**`started_at` が一致すれば一致・ID の照合は R-1 の作り直しを待つ** (読めないに倒すと、cutover の瞬間に
全 Worker の枠が UNVERIFIABLE になり退役が保留される)。

### 2.3 照合ではない `started_at` の使い道 (移さない)

| 場所 | 使い方 | 01c での扱い |
|---|---|---|
| watchdog.py:282-286 (他 :204 / :263 / :739 / :1559) | idle 時計の起点 (時刻として parse) | 変えない。reserve が今どおり書く |
| plan.sh:2387 `taskvia_sync_pull` / :5687-5688 `_resync_one` | Taskvia への時刻 | 変えない |
| scripts/log_to_obsidian.sh:152 | 表示 | 変えない |
| 監査ログ `generation` 欄 | 表示 | 残す (E1 で `execution_id` 欄が並ぶ) |

### 2.4 移す順序

1. **E1** (呼び出し元ゼロ): `lib_task_controller.py` と、lib_state_store の拡張 (identity の `execution_id` 欄・`classify_assignment` の
   `execution_id` 引数・R-5・`_safe_execution_id`・record の書き込み)。**拡張は既定値で今とバイトが同じ**であること
   (`execution_id=None` なら identity に欄を出さない・R-5 は欄の無い card で何もしない)。01a S3 の互換 golden
   (`tests/fixtures/plan_sh_compat_s3.golden.json`) が変わらないことで確かめる。
2. **E2**: pull だけが ID を発行する。この時点で ID を照合するのは G1 の CAS (#7) と R-1 (#5) だけ。他のコマンドは ID を
   **読まずに**今の動作のまま (card の新しい欄は `dump_yaml` が保持する)。
3. **E3**: done / fail / needs-director / ready-for-verification / verify-result が ID を照合する (§5)。
4. **E4a**: update --reset / retire / reap / 退役 marker が ID で照合し、**marker と progress に ID を書き始める** (producer の切り替え)。
   世代の照合は残す。**E4b**: E4a が両デーモンで動いた後に §7 の条件を満たしてから**世代の照合を外す**
   (#4・#8・#11・#12 の `started_at` の分岐を消す。§2.2 の表の 3 行目と 4・5 行目が「不一致」と「試行なし」に変わる)。
   順序の理由は §7 (確認から restart までの間に旧形式の marker を作らせない) と §9.4。

---

## 3. attempt と branch / worktree (EXEC-02 と git-policy.md §16.4 の 1・2)

**決定: 1 task = 1 branch = 1 worktree を保つ。attempt は branch / path に入れない。新しい試行は W2 で同じ worktree を再利用する。**

- 原案 §14-11「Execution ごとの Task branch 作成」は禁止事項で、EXEC-01 も「Task branch は原則再利用」。branch pattern に
  `{attempt}` を足す案 (git-policy.md §16.4 の 1 の前者) は禁止事項に当たるので捨てる。
- worktree は branch に 1 対 1 で付く (git は 1 つの branch を 2 つの worktree に checkout できない。W5)。だから worktree も 1 つ。
- 「新しい attempt は新しい worktree」と W2「登録済み・branch 一致なら再利用」の衝突 (git-policy.md §16.4 の 2) は、
  **前者を採らない**ことで消える。W2 は「同じ task の再取得」を前提にしており、新しい試行は同じ task の再取得そのもの。
- 新しい試行を作る操作は **reserve (= pull) だけ**。どの経路も最後は pending → pull を通る:

| きっかけ | 前の試行 | task | 次の試行 |
|---|---|---|---|
| `update --status pending --reset` (Director) | reserved → released / running → failed (`RESET_BY_DIRECTOR`) | pending | 次の pull (誰でも) |
| `needs-director` → Director の `update --status pending --reset` | running → failed (`NEEDS_DIRECTOR`) | needs_director → pending | 同上 |
| `retire --outcome reset` (watchdog) | reserved → released / running → failed (`RETIRED`) | pending | 同上 |
| **`verify-result fail` (rework < max)** | running → failed (`VERIFICATION_REJECTED`) | **pending (worker・started_at を null、枠を撤去)** | 同上。`rework_count` は今どおり +1 |
| pull の途中で死んだ (N8) | reserved のまま | in_progress | **同じ試行を再開** (新しい試行にしない。§6) |
| worktree を作れない (G1) | reserved → failed (`WORKSPACE_CREATE_FAILED`) | needs_director | Director の reset の後 |

- **`verify-result fail` を pending に変える (挙動の変更。E3 の承認事項)**。今は worker・started_at を残したまま in_progress に戻す
  (plan.sh:5095-5106) ので、新しい試行にならず、Worker が持っている ID と card が一致したまま「同じ試行のやり直し」になる。
  - 捨てた案: in_progress のまま**同じ Worker に新しい試行を直接予約する**。(1) Worker のシェル・env・`.crewvia-env` は古い ID を
    持ったままなので、その Worker の次の done が `EXECUTION_NOT_CURRENT` で拒否される (新しい ID を Worker に届ける経路が無い —
    pull の JSON が唯一の経路)。(2) 「pull を通らない予約」という 2 つ目の reserve の入口ができ、二重実装になる (原案 §14-7)
  - 影響: verifier の差し戻し後は dispatcher が配り直す (同じ Worker とは限らない)。verifier.md:99「Director が Worker に差し戻し」の
    手作業が要らなくなる。verifier-dispatcher は本番で 0 プロセス (state-store.md §10.1) なので、今動いている運用への影響は無い
  - `rework_count >= max_rework` で needs_human_review に倒す分岐 (:5097-5103) と `verify-result needs_human_review` は試行を
    running のまま残す (今と同じく worker・枠も残る。人が pass / fail を決めるまでその試行の結果は未確定)
- **W2 の再利用で中身を確かめない方針 (git-policy.md §1.3) はそのまま**。前の試行の commit・未 commit の変更は残る。
  record の `git.head_at_start` (§1.3) で、各試行がどの commit から始めたかを残す。前の試行を捨てたいなら Director が reset の前に
  worktree を片付ける (今と同じ)。
- 残るリスク (今と同じ・01c では閉じない): reset した試行の Worker プロセスがまだ生きていると、新しい試行と同じ worktree で
  同時に動きうる。退役 (watchdog) を経ずに reset した場合の話で、Director の手順 (reset の前に retire) で避ける。
  E3 以後は古いプロセスの done / fail は `EXECUTION_NOT_CURRENT` で拒否されるので、**card を書き換えることはできない** (今は書ける)。
- **title を変えても branch は変わらない** (E2 から)。`task_slug` を最初の reserve で card に固定するため (git-policy.md §10 の 1)。
  legacy の card (欄なし) は次の reserve で今の title から作って固定する (今の動作と同じ値)。

---

## 4. Controller の API と遷移表 (CTRL-01〜05・EXEC-04)

### 4.1 置き場所と API

新モジュール **`scripts/lib_task_controller.py`** (E1)。lib_state_store の `Txn` (ロック保持中) を受け取り、遷移の前提検査・
card の execution 欄・record・assignment の projection を書く。**ロックは取らない** (取るのは plan.sh の `with_lock` 1 か所。
lib_state_store の入れ子禁止 `NestedTransaction` に合わせる)。回復 (`recover_before`) はコマンドの前に plan.sh が今どおり呼ぶ。

```python
reserve_task(txn, slug, tid, agent, *, now, id_factory=None) -> ExecutionContext   # pending → in_progress / — → reserved
start_execution(txn, slug, tid, execution_id, git_context) -> Execution            # reserved → running
complete_execution(txn, slug, tid, caller, *, to_status) -> Execution              # running → completed
fail_execution(txn, slug, tid, caller, failure_code, *, to_status) -> Execution    # reserved|running → failed
release_execution(txn, slug, tid, caller, reason_code) -> Execution                # reserved → released
mark_task(txn, slug, tid, caller, *, to_status) -> Execution | None                # 試行を変えない task の遷移 (ready-for-verification 等)
get_execution(queue_dir, slug, execution_id) -> Execution                          # 読み取り (record。ロック不要)
```

- `caller` = `Caller(execution_id=None|str, source='flag'|'env'|'none', agent=..., director_override=False)` (§5)。
- 引数に `(slug, tid)` を取る (原案は `execution_id` だけ)。理由: record は判断に使わない (§1.2) ので、ID から card を引く
  索引を作らない。card を名指しし、その card の `current_execution_id` と `caller.execution_id` を照合する
  (原案 CTRL-04「current active Execution 以外を操作して Task 状態を変更してはならない」はこの照合で満たす)。
- 返り値・例外: `ControllerError(code, message)`。`code` は §4.4 の固定コード。plan.sh はコードを exit code に写す (§4.4)。
- Controller が**持たない**もの (今の plan.sh に残す。COMPAT-01 の「残存 legacy path」):
  done の D0〜D5 (pr_number の伝播。state-store.md §2.2)・fail の証拠 (`_validate_fail_evidence`)・QA gate / `required_evidence`・
  依存の判定 (`lib_dep_rules` を呼ぶ。pull の候補選び)・mission の done・Taskvia・worktree。これらは task の内容の検査で、
  試行の遷移ではない。plan.sh は Controller の呼び出しの**前**にこれらを検査し、Controller は遷移と照合だけを行う
  (同じ規則を 2 か所に置かない: status の受け付けは Controller だけが見る。§4.3 末尾の構造ガード)。

### 4.2 Execution status と task status の対応

EXEC-04 の遷移 (reserved → running → completed / failed、reserved → failed / released) をそのまま使う。
**running → released は作らない** (原案の図に無い)。running の試行を結果なしで手放す操作 (reset・退役・needs-director) は
`failed` + 固定コード (§4.4) で表す。表の `` `CODE` `` は card の `execution_end_code` に入る値 (§1.2)。

| 操作 (コマンド) | 試行 from → to | task from (§4.3 で狭めた後) → to | worker / 枠 |
|---|---|---|---|
| reserve (pull の 1 つ目のロック) | (なし or terminal) → reserved | pending → in_progress | worker = A・枠を公開 (identity に ID) |
| start (pull の 2 つ目のロック。**新設**) | reserved → running | in_progress → in_progress | 変えない |
| worktree を作れない (G1) | reserved → failed `WORKSPACE_CREATE_FAILED` | in_progress → needs_director | worker 残す・枠撤去 (今と同じ) |
| done | running → completed `DONE` | in_progress → done | 枠撤去 (今と同じ) |
| ready-for-verification | running (変えない) | in_progress → ready_for_verification | 今と同じ (枠は残る) |
| verifying | running (変えない) | ready_for_verification → verifying | 今と同じ |
| verify-result pass | running → completed `VERIFIED` | ready_for_verification / verifying / needs_human_review → verified | 今と同じ (枠は残り、R-2 が後で消す。state-store.md §7) |
| verify-result fail (< max) | running → failed `VERIFICATION_REJECTED` | ready_for_verification / verifying / needs_human_review → **pending** | **worker・started_at を null・枠撤去** (§3) |
| verify-result fail (≥ max) / needs_human_review | running (変えない) | ready_for_verification / verifying → needs_human_review | 今と同じ |
| fail (Worker) | running → failed `WORKER_FAILED` | in_progress → failed | 枠撤去 |
| needs-director | running → failed `NEEDS_DIRECTOR` | in_progress → needs_director | worker 残す・枠撤去 |
| update --reset | reserved → released `RESET_BY_DIRECTOR` / running → failed `RESET_BY_DIRECTOR` / それ以外は変えない | any → pending (+ `--status` の後書きは今どおり) | worker・started_at を null・枠撤去 (今と同じ) |
| retire --outcome reset | reserved → released `RETIRED` / running → failed `RETIRED` | in_progress → pending | 同上 |
| retire --outcome needs-director | 同上 | in_progress → needs_director | worker 残す・枠撤去 |
| update --status X (--reset なし。Director) | **変えない** | any → X | 今と同じ (枠は触らない) |
| reap-orphan-assignment | 変えない | 変えない | 枠撤去 (projection だけ) |

- 試行の無い task への操作 (legacy の card・Director の `update --status in_progress --reset` で開いた card・needs_director の後等) は
  **task の遷移だけ** (`mark_task`)。試行の欄は触らない (§5.3 の「試行なし」の経路)。
- `update --status X` で active な試行が残ったまま task が holding でない status になった card (例: running のまま `blocked`) は、
  §1.2 の定義で「active でない」。試行の欄は書き換えない (推測で terminal にしない。原案 §14-16)。store-check が
  `reported:execution_active_on_non_holding_status` を出す。次の reserve は pending からしか通らないので、Director が `--reset` する
  (そのとき reset が試行を failed `RESET_BY_DIRECTOR` にする)。

### 4.3 今の task 遷移のうち狭めるもの (state-store.md §1.4 / lib_task_status.py:82-91 を 1 行ずつ)

狭めるのは E3 (done / fail / verify-result / ready-for-verification / needs-director) と E2 (pull)。理由の列の「出口」は、
狭めた後もその状態から抜けるコマンドがあることの確認 (01a §2.5 の「出口を減らさない」の継続)。

| コマンド | from | 判定 | 理由 / 出口 |
|---|---|---|---|
| pull | pending | 残す | |
| pull | in_progress (自分の reserved の試行) | **足す** (再開) | §6。今は `--task` で exit 1 (plan.sh:3362-3366) |
| needs-director | in_progress | 残す | |
| done | in_progress | 残す | 試行があれば照合 (§5)、無ければ試行なしの経路 |
| done | pending | **狭める** | 一度も予約されていない task を「完了」にできる。Director が実行する task は今の運用どおり `update --status in_progress --reset` で開いてから done する (state-store.md §10.3 の 9。t019 で実績)。やらない task は `update --status skipped` (plan.sh:928 の案内) |
| done | blocked | **狭める** | Director が止めた task を Worker が完了させられる。出口: Director の `update --status in_progress` → done、または `update --status done` |
| done | ready_for_verification / verifying / needs_human_review | **狭める** | 検証を迂回する。出口: `verify-result pass` (needs_human_review からも通る) |
| done | verification_failed | **狭める** | 書き手が `update --status` だけ (state-store.md §1.1)。出口: `update --status` |
| fail | in_progress | 残す | |
| fail | needs_director | **残す** | Director が判断待ちの task を諦める出口 (今の運用)。試行は needs-director で既に failed なので試行なしの経路 |
| fail | pending / blocked | **狭める** | 走っていない task に失敗の証拠 (`--head`) を付けることになる。出口: `update --status skipped` / `failed` |
| fail | ready_for_verification / verifying / needs_human_review | **狭める** | 検証待ちの結論は verifier が出す。出口: `verify-result fail` / `needs_human_review` |
| fail | verification_failed | **狭める** | 同上 (書き手なし) |
| ready-for-verification | in_progress | 残す | 呼び出し元は今 0 (worker.md:653 の予告だけ) |
| verifying | ready_for_verification | 残す | |
| verify-result | ready_for_verification / verifying / needs_human_review | 残す | |
| verify-result | pending / in_progress / blocked / needs_director / verification_failed | **狭める** | 検証に出ていない task への判定。特に pending から needs_human_review を作れる (state-store.md §2.5 の例)。出口: in_progress は done / ready-for-verification、他は `update --status` |
| retire | in_progress | 残す | |
| update --status / --reset / --worker | any | **残す** | Director の手動操作 (plan.sh:5878-5880 の docstring)。狭めると今動いている回復手順 (`update --status` → done) が読めなくなる (state-store.md §1.4)。試行の扱いは §4.2 |
| reap-orphan-assignment | `is_orphan_target` | 残す | projection だけ |
| release-dep | pending | 残す | 遷移ではない |

- 狭めた拒否は今と同じ `REFUSED_TRANSITION` (exit 2・何も書かない。plan.sh:475-480)。pull は除く (§4.4)。
- **表は `lib_task_status.ACCEPTS_FROM` を書き換える** (データの置き場は 1 か所のまま)。Controller がそれを読む。
  E3 で plan.sh の各コマンドの `_TASK_STATUS.accepts(...)` / `refuse_transition(...)` の呼び出し (今 17 件。§11.1) を、移した
  コマンドから消す。**構造ガード**: plan.sh の中で `accepts(` を呼んでよいのは Controller に移していないコマンドだけ
  (allowlist を (関数, 件数) で持つ。memory `write-guard-allowlist-key-is-function-and-count`)。E4 の後は 0 件を目標にする。

### 4.4 冪等と domain error (CTRL-04 / CTRL-05)

**冪等**: 名乗った ID が card の `current_execution_id` と一致し、その試行が既に terminal のとき、答えは **card の
`execution_end_code` だけ**で決める (record・task の status は読まない。§1.2 / §1.6 の 1):

| card の `execution_end_code` (status) | 来た操作 (同じ ID) | 結果 |
|---|---|---|
| `DONE` (completed) | done | **成功 (exit 0)・何も書かない**・stdout に `already completed (idempotent)`。今は「already done」exit 2 なので、**ID を名乗った呼び出しだけ**挙動が変わる |
| `VERIFIED` (completed) | verify-result pass | 成功・何も書かない |
| `WORKER_FAILED` (failed) | fail | 成功・何も書かない |
| `NEEDS_DIRECTOR` (failed) | needs-director | 成功・何も書かない |
| `VERIFICATION_REJECTED` (failed) | verify-result fail | 成功・何も書かない (verifier の再送) |
| `DONE` / `VERIFIED` | 上の行以外 (fail / needs-director / done を `VERIFIED` に等) | **conflict** (`EXECUTION_ALREADY_TERMINAL`・exit 3・何も書かない) |
| `WORKER_FAILED` / `NEEDS_DIRECTOR` / `VERIFICATION_REJECTED` | 上の行以外 | conflict |
| `RESET_BY_DIRECTOR` / `RETIRED` / `WORKSPACE_CREATE_FAILED` (failed / released) | done / fail / needs-director / verify-result | conflict (`EXECUTION_NOT_CURRENT` と同じ扱い: その試行は持ち主以外の操作で終わり、もう誰の作業でもない) |

- **終了コードの書き手** (どれも terminal にする card の書き込みと同じ 1 回): done → `DONE`、verify-result pass → `VERIFIED`、fail → `WORKER_FAILED`、
  needs-director → `NEEDS_DIRECTOR`、verify-result fail (< max) → `VERIFICATION_REJECTED`、update --reset → `RESET_BY_DIRECTOR`、
  retire → `RETIRED`、G1 → `WORKSPACE_CREATE_FAILED`。
- **reset の後に Director が task の status を変えても答えは変わらない**: `update --status X` (--reset なし) は試行の欄を触らない (§4.2)
  ので `execution_end_code` は残る。次の reserve が新しい ID を発行した後は、古い ID の再送は `EXECUTION_NOT_CURRENT`。
- crash の点 (§1.4): 終了コードは terminal の status と同じ card の書き込みに入るので、「status は terminal・理由は不明」の card は
  できない。record が遅れていても答えは同じ。
- 「同じ操作」は操作の種類だけで判定する。引数の違い (done の `--pr` 違い) は今の D0 の検査 (state-store.md §2.2) が拒否する。
- ID を名乗らない呼び出しは今と同じ (done の二重は exit 2)。

**domain error の固定コードと exit code** (exit 2 は pull では idle の意味。memory `pull-exit-2-is-idle-usage-errors-must-be-1`):

| コード (CTRL-05) | 意味 | pull の exit | 他のコマンドの exit | 何か書くか |
|---|---|---|---|---|
| (成功) | | 0 | 0 | |
| `NO_TASK` | 取れる task が無い (idle) | **2** | — | 書かない (§8 の行も出さない) |
| `TASK_NOT_FOUND` | card が無い | 1 | 1 | 書かない |
| `STATE_INVALID` | card が読めない・欄の形が違う | 1 | 1 | 書かない |
| `TASK_NOT_ELIGIBLE` | status・依存・target が合わない | 1 (今の `--task` と同じ) / target・busy は 3 (今と同じ) | — | 書かない |
| `TASK_ALREADY_RESERVED` | 他者が予約・実行中 | 1 (今の「already in_progress」:3362) | — | 書かない |
| `INVALID_TRANSITION` | §4.3 の表が拒否 | (使わない) | **2** (`REFUSED_TRANSITION`。今と同じ) | 書かない |
| `EXECUTION_NOT_FOUND` | 名乗った ID の形が違う・card が試行を持たない | 3 | 3 | 書かない |
| `EXECUTION_NOT_CURRENT` | 名乗った ID が card の今の試行でない | 3 | 3 | 書かない |
| `EXECUTION_ALREADY_TERMINAL` | 違う terminal 結果への変更 | 3 | 3 | 書かない |
| `LOCK_FAILED` / (lock busy) | ロック | 1 / 4 | 1 / 4 (今と同じ) | 書かない |
| `GIT_POLICY_INVALID` / `WORKSPACE_CREATE_FAILED` | G1 の出口 | 1 (今と同じ。card は needs_director) | — | card を書く (G1) |
| `TRANSACTION_RECOVERY_REQUIRED` | **使わない** | — | — | 01a の回復は拒否を足さない (state-store.md §2.5)。表に無い食い違いは報告して本体は続ける |

- 「何も書かない」は**コマンド本体**の約束 (01a と同じ)。回復の行と §8 の拒否の行は残る。
- 機械が読む経路 (「文字列 message の解析を caller 契約にしない」): **exit code が第一の契約** (kai-review.sh・lib_retirement は今も
  rc で分岐している: lib_retirement.py:2042-2071)。加えて stderr の**最後の行**に `[plan.sh] error_code=<CODE>` を固定形式で出す。
  文言は変えてよいが、この 1 行の形は変えない (テストで固定する)。

---

## 5. 呼び出し元の照合 (AC-04「agent 名だけで完了・失敗・release しない」)

### 5.1 誰が何を打つか (d887acf の実測) と、ID をどこから得るか

| コマンド | 打つ者 (根拠) | 今の照合 | 01c で ID を得る場所 |
|---|---|---|---|
| pull | Worker (worker.md:232-237・start.sh:948)・kai-review.sh (:226-237、**JSON を捨てる** :234)・dispatcher は文面で指示するだけ (dispatcher.sh:2616-2620) | 名前の形・role・退役予約・busy (plan.sh:3255-3298 / :3395-3403) | **発行する側**。JSON に `execution_id` と `attempt` を足す (E2)。`.crewvia-env` に `CREWVIA_EXECUTION_ID` を足す (**任意**。git-policy.md §16.4 の 4) |
| done | Worker (worker.md:538-564 他)・kai-review.sh (:712)・review / QA / plan-review の skill (Worker として自分の task)・**Director が Worker の代わりに** (director.md:205-207) | **なし** (plan.sh:4489-4497 は枠の撤去に AGENT_NAME を使うだけ) | `--execution` > env `CREWVIA_EXECUTION_ID`。kai-review.sh は pull の JSON から取って渡す (E3)。Director は `plan.sh status` の表示から渡す (§5.3) |
| fail | Worker (worker.md:370-374・:779-812) | なし (:4649-4657) | 同上 |
| needs-director | Worker・kai-review.sh (`call_needs_director` :165、17 か所)・skill | なし (:4051) | 同上 |
| ready-for-verification | 呼び出し元 0 (worker.md:653 の予告だけ) | なし | 同上 |
| verifying | verifier-dispatcher.sh (:258-260、`AGENT_NAME=verifier-dispatcher`) | なし | verifier-dispatcher が card の `current_execution_id` を読み、verifier への指示文に `--execution` を入れる |
| verify-result | verifier (verifier.md:59-67)。指示は verifier-dispatcher.sh:403-408 | なし | 上の指示文の `--execution`。**verifier は試行の持ち主ではない** (検証する側) が、照合するのは「どの試行を判定するか」で、持ち主かどうかではない |
| update (--reset) | Director のみ (director.md:300 他)。デーモンは文面で勧めるだけ | なし (手作業用。plan.sh:5878-5880) | 不要 (§5.3) |
| retire | watchdog (lib_retirement.py:2037-2039)・Director (director.md:1485-1497) | worker・`started_at`・枠の世代 (plan.sh:6294-6313) | 退役 marker の `task_execution_id` (E4)。Director は `plan.sh status` から |
| reap-orphan-assignment | dispatcher (Kai-codex のみ。dispatcher.sh:777) | `is_orphan_target`・classify (plan.sh:6421-6432) | 不要 (card が手放した枠だけを消す。試行を変えない) |

`.crewvia-env` の今の中身は 3 行 (`CREWVIA_MISSION_SLUG` / `CREWVIA_TASK_ID` / `CREWVIA_TASK_SLUG`。plan.sh:3653-3663)。
pull の JSON には今 `started_at` も無く、Worker は自分の世代を知らない (照合が無い理由の 1 つ)。

### 5.2 照合の規則 (E3 から。done / fail / needs-director / ready-for-verification / verify-result 共通)

ID の出どころは **`--execution <id>` (明示) > env `CREWVIA_EXECUTION_ID`** の順。どちらも無ければ「名乗りなし」。

| card の今の試行 | 名乗り | 判定 | 監査行の `caller_check` |
|---|---|---|---|
| active (X) | X | 通す | `verified` |
| active (X) | Y (≠ X) | **拒否** `EXECUTION_NOT_CURRENT` (exit 3)。env 由来なら、どこから来た値か (env) と直し方 (`unset CREWVIA_EXECUTION_ID` か `--execution` を渡す) を拒否文に書く | 拒否の行 (§8) |
| active (X) | なし | **E3 では通す** (warn を stderr に 1 行) | `unverified` |
| terminal (X) | X | §4.4 の冪等 / conflict | `verified` |
| 試行なし (legacy の card・Director が開いた card・needs_director の後) | なし | 通す (task の遷移だけ。§4.3 の表で受け付ける from に限る) | `no_execution` (legacy の in_progress は `legacy_generation`) |
| 試行なし | X | 拒否 `EXECUTION_NOT_FOUND` (exit 3) | 拒否の行 |

- **agent 名は照合の根拠にしない** (AC-04)。名前は監査の `actor` と、枠 (assignment) の場所を決めるのにだけ使う。
- **名乗りなしを E3 で拒否しない理由**: `.crewvia-env` を必須にしない (git-policy.md §16.4 の 4)。env の無い shell は今ある:
  Claude Code の Bash ツールは呼び出しごとに env が消える (worker.md の手順は `source .crewvia-env` を毎回しない)・
  `target_dir` の task は worktree も `.crewvia-env` も無い・E3 の前に起動した Worker のプロンプトは古い worker.md。
  ここで拒否すると、cutover の瞬間に動いている全 Worker の done が止まる (出口を消す)。
- **名乗りなしを最終的に拒否する段 (E5) を足すことを提案する** (§9.2)。条件は観察: t020 の後、監査ログで `caller_check=unverified` が
  crewvia 本体の task で 0 件になり (worker.md / kai-review / skill が ID を渡すようになった)、target_dir の task の ID の受け渡し
  (pull の JSON から shell 変数へ) が worker.md に入ったこと。それまで AC-04 は「**違う ID では終わらせられない**」までで、
  「名乗らなければ終わらせられる」穴は E5 まで残る。**この残りを §10 の確認表に明記する**。
- 捨てた案: 名乗りが無いとき、`queue/assignments/<AGENT_NAME>.identity` から ID を補う。agent 名から ID を引くのは agent 名だけの照合と
  同じ (名前は使い回される)。
- 捨てた案: cwd の worktree の `.crewvia-env` を plan.sh が読んで補う。別の Worker が同じ task を再 pull すると `.crewvia-env` は
  新しい試行の ID に書き直される (W2 で毎回書く) ので、**古い試行のプロセスが新しい試行の ID を名乗れる**。

### 5.3 Director の操作を壊さない明示の経路

| Director の操作 (今の手順) | 01c の後 |
|---|---|
| Worker の完了報告を受けて `plan.sh done <tid>` (director.md:205-207) | **試行が active なら `--execution <id>` を付ける** (E3 で `plan.sh status --mission` に `ex-…` と attempt を表示する)。付けなければ E3 では `unverified` で通り、E5 の後は拒否。ID を渡す意味は「この試行について判断した」の明示で、Director が見た後に Worker が reset → 再 pull していれば `EXECUTION_NOT_CURRENT` で止まる (退役の `--started-at` が防いでいるのと同じ事故) |
| cutover の review task を `update --status in_progress --reset` で開いて `done` (t019) | 変えない。開いた card には試行が無い (`--reset` が worker を消し、`execution_status` は前の試行の terminal のまま) ので「試行なし」の経路で通る。state-store.md §10.3 の 9 の `reported:in_progress_without_worker` は今どおり出る (dedup は §8 の backlog) |
| `update --status pending --reset` (needs_director の差し戻し) | 変えない。試行は needs-director で既に failed |
| `fail <tid>` で needs_director の task を諦める | 変えない (試行なしの経路) |
| `retire <tid> --agent A --started-at <sed で読んだ値>` (director.md:1485-1497) | E4 で `--execution <id>` (`plan.sh status` から) に書き換える。移行期は `--started-at` も通る (§2.2) |
| `reap-orphan-assignment` | 変えない |
| `update --status <任意>` (手動の回復) | 変えない。試行の欄は触らない (§4.2) |

- Director 専用のフラグ (`--as-director` 等) は**作らない**。理由: Director かどうかを plan.sh は確かめられない (AGENT_NAME は Director の
  セッションにも Worker のセッションにも export される: start.sh:292 / :786)。確かめられない役割で照合を外す経路を作ると、
  それが agent 名だけの照合になる。Director に必要なのは「ID を見て名指しする」経路だけで、それは Worker と同じ `--execution`。

### 5.4 デーモン・スクリプト

- **kai-review.sh** (E3): pull の JSON を捨てず (`>/dev/null` :234 をやめる)、`execution_id` を取り出して done (:712) と
  `call_needs_director` (:165) に `--execution` で渡す。JSON が読めない・欄が無い (E2 の前の plan.sh) ときは今どおり名乗らない。
- **watchdog / lib_retirement** (E4): §2.1 の #9〜#13。marker の `task_execution_id` は、request を書くとき (`_write_request`) に
  `task_started_at` と同じロックの中で card から読む。旧 marker (欄なし) は `--started-at` の経路 (§2.2)。書き始めるのは E4a、
  旧 marker の読み口を外すのは E4b (§7)。`_carried_from_request` (progress への写し) も E4a で `task_execution_id` を運ぶ。
- **dispatcher** (E4): reap は変えない。`AGENT_NAME=dispatcher` を subprocess の env に入れる (§8)。
- **verifier-dispatcher** (E3): `verifying` を打つときに card の ID を読み、指示文に入れる。本番 0 プロセスなので restart は不要
  だが、起動している環境があれば restart (state-store.md §7 の S5 行と同じ)。

---

## 6. pull の冪等化 (N8。git-policy.md §16.4 の 3)

**決定: pull を reserve (1 つ目のロック) と start (2 つ目のロック) に分け、`reserved` の試行は同じ Worker の再 pull が
同じ task・同じ試行のまま再開する。`running` の試行は再開しない。**

```text
ロック 1: recover → 候補選び → reserve (card: in_progress / X / reserved / attempt / task_slug、record、identity(X)、枠)
ロック外: Taskvia → worktree (W0〜W7) → git rev-parse HEAD → .crewvia-env (X を含む)
ロック 2: recover → start (CAS: current_execution_id == X かつ execution_status == reserved → running、record)
          失敗 (worktree / env): CAS が同じなら fail_execution(X, WORKSPACE_CREATE_FAILED) + needs_director (G1 の出口)
stdout:   JSON (execution_id / attempt を含む)
```

- **不変条件: `reserved` ⇔ JSON はまだ誰にも渡っていない**。start は JSON を出す**前**にコミットするので、reserved の試行では
  誰も作業を始めていない。だから同じ Worker 名の再 pull に渡しても、2 つのプロセスが同じ作業をすることは無い
  (名前は使い回されるが、reserved の試行には「前任の作業」が存在しない)。
- **`running` を再開しない理由**: running なら JSON が渡った可能性がある (start のコミット後・JSON の出力前に死んだ場合だけ渡っていない)。
  同じ名前の別プロセス (退役されていない前任) が作業中かもしれず、渡すと同じ worktree で 2 プロセスが動く。出口は今と同じ
  Director の `update --reset` (または watchdog の退役)。start のコミットから JSON の出力までは同じプロセスの数行なので、ここで
  死ぬ窓は reserve〜start (subprocess・network を含む) よりずっと小さい。
- **再開の入口**:
  - `pull --task X` (dispatcher の指示): card X が in_progress・`worker == agent`・`execution_status == reserved` なら再開。
    それ以外の in_progress は今どおり `TASK_ALREADY_RESERVED` (exit 1。plan.sh:3362-3366)。
  - `pull` (--task なし): 候補選びの**前**に、`worker == agent` かつ reserved の card を探す (agent_busy_elsewhere :3079-3085 と同じ
    `mission_search_order` の範囲)。1 枚なら再開。2 枚以上は拒否 (exit 3。`agent_busy_elsewhere` と同じ文面の型)。
    0 枚なら今どおり候補選び。
  - `agent` が空の pull (`--agent` も `AGENT_NAME` も無い) は再開しない (持ち主を名指しできない。今の pull も worker 空で予約し
    枠を作らない)。
- 再開は**新しい試行を作らない** (attempt を増やさない)。ロック外の段 (Taskvia・worktree W2・`.crewvia-env` の書き直し) を最初から
  やり直し、start を打つ。Taskvia の再送は今の再 pull と同じ (冪等ではないが壊さない)。
- **G1 (needs_director の出口) との関係**: G1 の CAS (#7) を ID の CAS に置き換える (git-policy.md §16.4 の 5)。worktree を作れない
  ときは reserved → failed で、再開はされない (needs_director は pull の候補にならない: git-policy.md §1.2 の B)。
  「pending に戻すと同じ Worker に配り直され続ける」(git-policy.md §1.2 の A を捨てた理由) は、reserved の再開でも起きない:
  再開は「同じ Worker が自分で打った再 pull」だけで、dispatcher は in_progress を配らない。
- **01a S4 の回復との関係**: pull の窓で落ちた card (in_progress・枠なし) は今どおり R-1 が枠を作る (identity に X)。再開はその後に
  card を読むので、回復の結果を前提にできる。01a backlog 4 (「その Worker か card に次の plan.sh が触れるまで残る」) は、
  **その Worker の再 pull が触れる**ので、Worker が生きていれば閉じる。Worker が死んでいれば今どおり (dispatcher の Rule 5 →
  Director の reset)。
- **task_slug を card に固定する** (git-policy.md §10 の 1): reserve が書き、再開と start はそれを使う。再開の間に title が変わっても
  branch / worktree は変わらない (W2 で同じ worktree を見つける)。

捨てた案:

| 案 | 捨てた理由 |
|---|---|
| 再 pull で新しい試行を作る (reserved を released にしてから reserve し直す) | 試行の番号が「pull の途中で死んだ回数」で増え、attempt が作業のやり直しの回数でなくなる。release と reserve を 1 つのコマンドで行う 2 つ目の reserve の入口ができる |
| start を作らず、reserve 1 回で running にする (今の形) | reserved と running を区別できず、「JSON が渡ったか」を正本から言えない。N8 の再開を安全にできない (running の再開は上の理由で危険) |
| 同じ Worker の再 pull は running でも同じ試行を返す | 上の「running を再開しない理由」 |

---

## 7. 進行中の task の扱い (原案 §9.3)

**決定: (a) — one-shot migration はしない。legacy の試行は自然に終わるのを待ち、世代の照合を外す段 (E4) だけが「legacy の
進行中 = 0」を merge の条件にする。**

- **E2 / E3 は 0 を待たない**。新しいコードは legacy の card (`current_execution_id` が無い) を §2.2 の表の 4・5 行目と §5.2 の
  `legacy_generation` / `no_execution` で読み、今の動作 (照合なし) で扱う。ID を後付けしないので、進行中の card の正本を
  推測で書き換えない (原案 §9.1「進行中 Task を自動推測で書き換えない」)。
- **E4 を 2 つの PR に分ける (Codex P2)**: 旧形式の marker を作るのは常駐のデーモン (dispatcher の `_retirement.request`
  dispatcher.sh:1293 → `_write_request` lib_retirement.py:1025-1066。watchdog は request を progress に写す `_carried_from_request`
  :808-828) で、どちらも主 checkout を ff しても **restart するまで旧コードのまま**作り続ける。「0 件を確かめてから照合を外す」を
  1 つの PR でやると、確認 → merge → sync → restart の間に旧形式の marker ができ、新コードはそれを ID で照合できず後始末が保留になる。
  だから **作る側を先に切り替えて、旧形式が増えなくなってから数える**:
  - **E4a** (t016): 退役 marker と progress に `task_execution_id` を**書き始める** (producer の切り替え)・読む側は新旧の両方を受け付ける
    (§2.2 の表のまま。`started_at` の照合は残す)・retire の `--execution`・`update --reset` / retire が試行を release / fail・デーモンの
    `AGENT_NAME`。旧形式の marker が来ても今どおり `started_at` で照合するので、確認の 0 件を merge 条件にしない。
  - **E4b** (新 task。Director が card を足す。§9.2 の 4): **世代の照合と旧形式 marker の読み口を外す** (§2.4 の 4)。merge 条件は下の 0 件。
- **E4b の merge 条件** (Director が merge の直前に確かめ、0 でなければ待つ): (0) **E4a が両デーモンで動いている**ことの証明 —
  主 checkout の HEAD が E4a 以降・dispatcher と watchdog の起動時刻がその sync より後・新しい marker に `task_execution_id` が出ている
  (memory `prove-which-code-version-a-spawned-task-ran` の 3 点。`DAEMON_RESTART_FILES` lib_daemon_watch.py:240-258 は両デーモンとも
  `lib_retirement.py` を含むので sync-main-checkout.sh が両方を restart する) (1) `plan.sh store-check` が全 active mission で
  `reported:legacy_execution` (in_progress / ready_for_verification / verifying / needs_human_review で `current_execution_id` が無い card。
  E1 で足す報告) を 0 件 (2) `registry/retirements/*.json` と `*.progress.json` で `task_execution_id` の無い marker が 0 件。
  (0) の後は旧形式の marker を作るプロセスが残っていないので、(2) の数は**減るだけ**で、確認から restart までの間に増えない
  (E4a のコードが card に ID の無い task を退役させるときだけ欄が空になるが、それは (1) の legacy card で、0 件なら作られない。§9.4)。
  待てない card は Director が `update --reset` で legacy の試行を手放す — これは今ある手作業で、migration ではない
  (task は数時間で終わる)。
- 捨てた案: producer の切り替えを E3 に入れる。E3 の承認の対象 (§4.3 の狭め・verify-result fail) と無関係なデーモンの restart を E3 に
  持ち込み、E3 の rollback がデーモンの restart を要るようになる。E4a は元々 lib_retirement を変える段なので、そこに置く。
- 捨てた案: E4 を 1 つの PR のまま、確認の前にデーモンを止める。止めている間は退役も割り当ても止まり、止める・戻す手順が cutover に
  増える (原案 §14-3・4 の「手順で安全を作る」側)。producer を先に切り替えれば止めずに済む。
- E4b の後、legacy の card は「試行なし」として読む (§5.2)。in_progress の legacy card が後から現れる経路は `update --status in_progress`
  (Director) だけで、これは「試行なし」の正当な状態 (§5.3)。
- **legacy の card を読む規則** (原案 §9.1): 欄が無い = 試行なし。`execution_status` だけ・`current_execution_id` だけ等の**片方だけ**ある
  card は手編集か壊れた書き込みなので `STATE_INVALID` (lint も FAIL にする。E1)。

捨てた案: (b) Human approval 下の one-shot migration。

- 進行中の card に ID を付けるには「どの試行が生きているか」を決める必要があり、その根拠は card の `started_at` と枠の identity だけ。
  それは今の世代の照合そのもので、ID を付けても新しい証拠は増えない (同じ情報の言い換え)。
- 書き込み先が正本 (card) で、承認・手順・戻し方 (migration の逆) を別に用意することになる (原案 §14-5・§9.4「irreversible migration を
  含めない」)。
- crewvia の task は数時間で終わり、待つ費用が小さい。

---

## 8. 監査ログの穴 (state-store.md §10.3 の 1・git-policy.md §16.3 の 13)

**決定: 照合を入れるこの機会に「照合の拒否」の行を足し、`execution_id` と `caller_check` を埋める。idle と使い方の誤りは行にしない。**

| 穴 | 01c の扱い | 段 |
|---|---|---|
| `execution_id` が null 固定 | 埋める (§1.5) | E1 (lib) / E2〜E4 (呼び出し側) |
| 拒否 (exit 1/2/3) の行が無い・`result` は常に `ok` | **照合と遷移の拒否だけ**行にする: `EXECUTION_NOT_CURRENT` / `EXECUTION_NOT_FOUND` / `EXECUTION_ALREADY_TERMINAL` / `INVALID_TRANSITION` / `TASK_ALREADY_RESERVED`。`result=refused:<CODE>`。回復の行と同じ経路 (ロックの中で**即時**追記。本体の `die` を待たない。state-store.md §2.3 末尾) | E1 (lib の `Txn.refuse(code, ...)`) / E2・E3・E4 |
| 同上 (行にしないもの) | `NO_TASK` (pull の idle。Worker が数十秒ごとに打つ — 1 日数千行の雑音)・使い方の誤り (引数)・`TASK_NOT_FOUND`・ロック (取れていないので順序を保証できない)・`StoreError` | — |
| `actor` が `unknown` | (1) 照合に通った操作は `actor` = 本体の操作の**前の card の `worker`** (record は読まない。§1.6 の 3) (2) デーモンは subprocess の env に `AGENT_NAME` を入れる: lib_retirement の retire は `watchdog`、dispatcher の reap は `dispatcher` (verifier-dispatcher.sh:258 と同じ前例) (3) それ以外は今どおり `AGENT_NAME` か `unknown`。**`audit_actor` の docstring (plan.sh:1876-1879)「Director と デーモンは見分けられない」は事実と違う** (Director のセッションにも AGENT_NAME がある。start.sh:292 / :786) ので直す | E1 (lib) / E3・E4 |
| 照合の結果が残らない | 本体の行に `caller_check` (`verified` / `unverified` / `legacy_generation` / `no_execution`) を足す。E5 の判断材料 (§5.2) | E1 / E3 |
| `_SAFE_RESULT_RE` (lib_state_store.py:117) が `repaired:R-[1-4]` と `reported:` だけ | `repaired:R-5` と `refused:[A-Z_]+` を足す | E1 |
| state-store.md §10.3 の 9 (Director が開いた card への `in_progress_without_worker` が 1 呼び出し 1 行) | **01c では閉じない** (backlog)。「Director が開いた card」を判定する根拠が plan.sh に無い (§5.3 の理由と同じ)。dedup は報告の仕組みの変更で、照合とは別 | — |

- 拒否の行を足しても「exit 3 は 1 バイトも書かない」の約束は変えない (state-store.md §2.3 末尾と同じく、約束はコマンド本体の書き込みについて)。
  E1 の docstring と exit code の表 (§4.4) に書く。
- 行に入れない値: 名乗られた ID は `_safe_execution_id` を通ったときだけ `detail` に (§1.5)。理由の自由文・env は入れない (01a §4)。

---

## 9. cutover と rollback (R2)

### 9.1 段ごとの表

| PR | 本番で変わること | merge 前 | merge 後に Director が本番で確かめること | 戻し方 |
|---|---|---|---|---|
| **E1** (t004) | **なし** (呼び出し元ゼロ)。lib_state_store の拡張は既定値で今とバイトが同じ | 通常 merge (t007) | `grep -rn lib_task_controller scripts hooks` が lib 自身とテストだけ / 01a S3 の互換 golden が変わっていない (CI) | revert |
| **E2** (t008) | pull が ID を発行 (card に 5 欄 (`execution_end_code` は空)・`executions/` に record・identity に `execution_id`・`.crewvia-env` に `CREWVIA_EXECUTION_ID`・JSON に `execution_id` / `attempt`) / pull が 2 つ目のロックを取る (start) / **同じ Worker の再 pull が reserved の試行を再開** / G1 の CAS が ID に / `task_slug` を card に固定 (title を変えても branch が変わらない) / 監査行の `execution_id` | **ユーザー承認** (t011) | 実 Worker の pull 1 回で: card に 5 欄・record が 1 つ・identity に ID・`.crewvia-env` が 4 行・監査行に ID が出る / 既存の進行中 card (legacy) の done が今どおり通る / dispatcher の busy / idle が変わらない (枠の本文は同じ) / `store-check` の差分が `legacy_execution` の報告だけ | revert → `scripts/sync-main-checkout.sh`。**新しい欄・record・identity の欄は残ってよい**: 旧コードは欄を `dump_yaml` で保持し (読まない)、`executions/` を読まず、identity の `started_at` だけを見る (§9.3) |
| **E3** (t012) | done / fail / needs-director / ready-for-verification / verify-result が ID を照合 (違う ID は exit 3)・**§4.3 の狭め** (done が pending / blocked / 検証待ちから通らない等)・`verify-result fail` が pending (新しい試行へ)・ID を名乗った再送が冪等 (exit 0)・拒否の行・`caller_check`・kai-review.sh が ID を渡す・worker.md / skill / director.md が `--execution` を使う・`plan.sh status` に ID 表示 | **ユーザー承認** (t015)。**狭めの表 (§4.3) と verify-result fail の変更を承認の対象として明示する** | 実 Worker の done が `caller_check=verified` / 旧プロンプトの Worker の done が `unverified` で通る / Kai の review が done / needs-director まで通る / Director の cutover review task の `update --status in_progress --reset` → done が通る / 監査に `refused:` が出たら 1 件ずつ妥当か | revert → sync。card の新しい欄は E2 と同じく残ってよい。**狭めを戻すと再び pending から done が通る** (戻すのは制限を外す方向なので壊れない) |
| **E4a** (t016) | **退役 marker / progress に `task_execution_id` を書き始める** (producer の切り替え)・retire が `--execution` を受け付け・`update --reset` / retire が試行を release / fail (`execution_end_code`)・reap の照合が ID 優先・デーモンの `AGENT_NAME`。**世代の照合は残す** (新旧の marker を両方読む) | **ユーザー承認** (t019)。0 件の確認は**要らない** (旧形式も読める) | **dispatcher と watchdog の両方が restart された**こと (起動時刻 > sync。§7 の (0)) / 新しい marker・progress に `task_execution_id` / 退役の全経路で新しい試行を殺さない (QA t017 の観察を本番で 1 件) / 監査の retire 行の actor が `watchdog` | revert → sync。**両デーモンとも常駐なので restart が要る** (どちらも `lib_retirement` を import する: `DAEMON_RESTART_FILES` lib_daemon_watch.py:240-258。memory `merged-daemon-code-is-inert-until-restart`)。新しい marker の `task_execution_id` は旧コードが読まない (余分な欄) |
| **E4b** (新 task) | **世代の照合を外す** (§2.4 の 4: #4・#8・#11・#12 の `started_at` の分岐と、旧形式 marker の読み口) | **§7 の (0)〜(2)** + **ユーザー承認** | 退役が ID だけで通る / `caller_check=legacy_generation` が出ない / 保留 (Director への hold) が増えていない | revert → sync (両デーモン restart)。E4a のコードに戻るので新旧の marker を両方読める |

共通 (01a / 01b と同じ):
- merge 後に主 checkout を ff するまで本番は旧コード (memory `main-checkout-lags-after-pr-merge`)。確認は `git -C <主 checkout> log -1` から。
- 新しい版が動いたことは 3 点で証明する (memory `prove-which-code-version-a-spawned-task-ran`): 主 checkout の HEAD・プロセスの起動時刻・新しい版にしか書けない行 (E2 なら監査行の `execution_id`)。
- env の停止スイッチは付けない (不変条件 5)。戻しは常に revert。

### 9.2 分割の提案 (Director が card を組み替える)

1. **E5 を足す (提案)**: 「名乗りなしの done / fail / needs-director / verify-result を拒否する」(§5.2)。t020 の本番確認の後。
   条件は監査ログの `caller_check=unverified` が crewvia 本体の task で 0 件、target_dir の task の ID の受け渡しが worker.md に
   入っていること。これが入るまで AC-04 は部分的 (§10)。E3 に入れない理由は §5.2 (env を必須にしない・cutover の瞬間に
   動いている Worker の done を止めない)。
2. **E2 の中で pull の start (2 つ目のロック) を必ず先に書く**: 冪等化 (N8) と G1 の CAS の置き換えは start の上に乗る。
   start を後回しにすると、E2 の途中の commit で G1 の CAS が ID を見られない。
3. E3 の verify-result fail の変更 (§3) は**承認で外せるように** commit を分ける (ユーザーが「今の in_progress のまま」を選んだら
   その commit だけを落とせる。落とすと verify-result fail は新しい試行にならず、§2.2 の照合の上では「同じ試行」のまま
   rework する —— その場合 Worker の ID は変わらないので照合とは矛盾しない)。
4. **E4 を E4a (t016) と E4b (新 task) に分ける** (§7。Codex P2)。E4b は E4a の本番確認 (両デーモンの restart の証明) の後に着手し、
   QA は E4a に対して t017、E4b に対しては旧形式 marker が 0 件の隔離 queue と、E4a 時代の (ID 入り) marker が残った隔離 queue の
   両方で退役の全経路を通す。承認 task も E4a・E4b で 1 つずつ。

### 9.4 確認 → sync → restart の間に旧コードが作るもの (各段)

merge 前に何かを確かめる段でも確かめない段でも、merge から主 checkout の ff、デーモンの restart、Worker のセッションの入れ替わりまでの間は
**旧コードが動き続けて旧い形のものを作る**。Codex P2 はその族 (確認した後に古い形が増える)。旧コードの持ち主ごとに切り替わる時点が違う:

| 旧コードの持ち主 | 新コードに切り替わる時点 |
|---|---|
| `plan.sh` (呼び出しごとの bash + python) | 主 checkout の ff の後の**次の呼び出し**。ff の瞬間に走っている呼び出しは最後まで旧コード |
| `kai-review.sh` / `verifier-dispatcher.sh` (呼び出しごと) | 同上 (走っている bash は ff 前の inode を読み続ける) |
| dispatcher / watchdog (常駐 python。`lib_retirement` を import) | **restart の後** (sync-main-checkout.sh が `files_digest` の変化で restart。それまでは古い) |
| Worker / Director のプロンプト (worker.md / director.md / skill) | **そのセッションが起動し直した後**。生きているセッションは数時間古い手順のまま |
| hooks | 呼び出しごと (plan.sh と同じ) |

各段で、間に旧コードが作るものと新コードの読み方:

| 段 | merge 前の確認 | 間に旧コードが作るもの | 新コードの読み方 | 根拠 |
|---|---|---|---|---|
| **E2** | なし | 旧 pull: ID の無い in_progress の card (legacy)・identity に `execution_id` 無し・`.crewvia-env` 3 行 / 旧デーモン: `task_started_at` だけの marker (E2 では marker は変わらない) | legacy card は §2.2 の 4・5 行目と §5.2 の `legacy_generation`。identity は `started_at` で比べる (§2.2 末尾)。`.crewvia-env` は名乗りなし。marker は今のまま | E2 は 0 件を条件にしない (§7) |
| **E3** | なし (承認だけ) | 旧 plan.sh / 旧プロンプトの Worker・Director・kai-review: ID を名乗らない done / fail / needs-director・旧 verify-result fail が running の試行を in_progress のまま残す (worker も残る) | 名乗りなしは `unverified` で通す (§5.2)。旧 verify-result fail の card は「running の試行・in_progress」で、新コードの done (同じ試行の ID) がそのまま通る — 旧来の「同じ試行のやり直し」として読める (§9.2 の 3 と同じ形) | E3 は名乗りなしを拒否しない (§5.2) |
| **E4a** | なし (承認だけ) | 旧 dispatcher (restart 前): `task_started_at` だけの request / 旧 watchdog (restart 前): 新 request から progress を写すとき `task_execution_id` を落とす | E4a の読み口は新旧の両方を受け付ける (§2.2 の 3 行目: `started_at` が一致すれば一致・`legacy_generation`) ので、片方のデーモンだけ先に restart された間の組み合わせ (新 request + 旧 progress 等) も今どおり照合できる | E4a は世代の照合を外さない (§7) |
| **E4b** | §7 の (0)〜(2) | **E4a のデーモン** (restart 前): ID の入った marker。ID が空になるのは (i) 退役させる task の card が legacy (ID なし) のとき (ii) card が読めないとき (`UNKNOWN_STARTED_AT` と同じ) | (i) は (1) が 0 件で、E2 の後は legacy card を作る経路が無い (pull が必ず ID を発行する。Director の `update --status in_progress` の card は worker が空で枠を持たないので、その task に向けた退役は起きない) ので作られない。(ii) は今も E4b の後も保留 → Director (`_cleanup_deferred`) で、旧形式だから保留になるのではない。**旧形式 (`task_started_at` だけ) の marker を作るプロセスは (0) の時点で残っていない** | §7 の (0) |
| **E5** (提案) | §9.2 の 1 の条件 | 旧プロンプトのまま生きている Worker / Director のセッション: 名乗りなしの done / fail | E5 は名乗りなしを拒否するので、**読み替えでは救えない**。だから条件に「生きている Worker のセッションが全部、ID を渡す worker.md の版の後に起動した」(プロセスの起動時刻 > E3 の sync) と「target_dir の task の受け渡しが worker.md にある」を入れ、監査の 0 件 (§1.6 の 11: 欠けうる) だけに頼らない | §5.2 |

- rollback (§9.3) の向き (新コードが作ったものを旧コードが読む) は §9.3 の表。この表は roll forward の向き。

### 9.3 rollback で残るものを旧コードが読めること (原案 §9.4)

| 残るもの | 旧コード (d887acf) の読み方 | 根拠 |
|---|---|---|
| card の `current_execution_id` / `execution_status` / `execution_end_code` / `execution_count` / `task_slug` | 読まない。書き直すときは末尾に保持する (並びは変わる) | lib_state_store.py:510-524 `dump_yaml` |
| 同上 (lint) | 未知キーの検査が無いので通る | §1.1 の grep |
| `queue/missions/<slug>/executions/*.json` | 読まない。archive は dir ごと動かす | `list_tasks` は `tasks/` だけを読む (E1 で確かめる) |
| identity の `execution_id` 欄 | 読まない (`started_at` だけを比べる) | lib_state_store.py:1097-1104 / lib_retirement.py:649-659 |
| `.crewvia-env` の 4 行目 | 次の pull が 3 行で書き直す | plan.sh:3653-3663 |
| 監査行の `execution_id` / `caller_check` / `refused:` | 読み手がいない | state-store.md §10.2 の S3 行と同じ |
| 退役 marker の `task_execution_id` | 読まない | lib_retirement の読みは `task_started_at` だけ |

**戻した後にもう一度進める (roll forward) とき**: 旧コードが動いている間に、旧コードは `execution_status` を更新しない。
旧コードの pull は `started_at` と worker を書き直すが `current_execution_id` / `execution_status` は前の値のまま残るので、
「in_progress・active に見える古い試行」の card ができうる。新しいコードはこれを §2.2 の表で読むと `started_at` が record の
`reserved_at` と食い違う。**再び進める前に Director が `store-check` を走らせ**、E1 で足す `reported:execution_fields_stale`
(task が holding・試行が active・card の `started_at` が空でない card で、`started_at` ≠ `current_execution_id` の record の
`reserved_at`。record は判断に使わず、報告にだけ使う。§1.6 の 4) が 0 件であることを
確かめる。0 でなければその card を `update --reset` する (試行の欄は reset が terminal にする)。
pending / 終端の card に残った active な `execution_status` は §1.2 の定義で active でない (task が holding でない) ので、
次の reserve は通常どおり上書きし、前の試行の record を `failed` / `end_code=ABANDONED_OUTSIDE_CONTROLLER` で凍結して
`reported:stale_execution_status` を残す (正本の task status が「誰も持っていない」と言っているので推測ではない)。

---

## 10. 禁止事項・不変条件・01a / 01b の規則との整合 (確認表)

| 原案 §14 / 不変条件 / 規則 | この設計 |
|---|---|
| §14-1 main checkout での実装 | 全 PR は worktree。本番確認 (t020) は通常運用の観察と `store-check` だけ |
| §14-2 稼働中 queue へのテスト書き込み | テストは `CREWVIA_QUEUE` を付け替えた隔離 queue。record も監査ログも隔離先に書かれる (queue の下) |
| §14-3・4 hot reload / 自動 cutover | 段ごとの merge + ユーザー承認 + sync-main-checkout (§9.1) |
| §14-5 未承認の state migration | migration なし (§7)。legacy の card は待つ |
| §14-6 Dispatcher に遷移 authority | dispatcher は書かない。reap は projection だけ (今と同じ)。`AGENT_NAME` を env に入れるのは監査の actor のため |
| §14-7 plan.sh と Controller への同一規則の二重実装 | status の受け付けは `lib_task_status.ACCEPTS_FROM` (データ) + Controller (判定) だけ。plan.sh の `accepts(` 呼び出しは移したコマンドから消し、構造ガードで固定 (§4.3)。照合の規則は `execution_matches` 1 か所 (§2.2)。reserve の入口は pull だけ (§3・§6) |
| §14-8 Worker 等による state の直接更新 | 変えない (01a S5 の構造ガード)。record の書き込みも lib_state_store の Txn を通す (E1 で構造ガードの対象に入る) |
| **§14-9 Agent 名 / Task ID だけで完了・失敗・Recovery** | **E3 で「違う ID では終わらせられない」まで。名乗りなしは E5 まで `unverified` で通る** (§5.2)。Recovery (R-1〜R-5) は card を正本にし、agent 名から ID を引かない。退役 (E4) は marker の ID で照合 |
| §14-10・11 Agent 単位 / Execution ごとの branch | どちらも作らない (§3) |
| §14-12 Git 状態から Task 状態を推測 | しない。`head_at_start` は記録だけで判断に使わない |
| §14-13 worktree 作成失敗時の main checkout fallback | G1 の出口のまま (ID の CAS に置き換えるだけ) |
| §14-14 per-file replace を multi-file transaction と呼ぶ | 呼ばない。01a の「正本 1 枚 + 書く順序 + projection の再生成」に record を projection として足すだけ (§1.2) |
| §14-15 lock 内の network / LLM | reserve と start の間 (Taskvia・worktree・git) はロックの外。start は 2 つ目のロック (§6) |
| §14-16 silent 修復 | R-5 は監査に `repaired:R-5`。読めない record は報告だけ。active でない古い欄の凍結は `reported:` を残す (§9.3) |
| §14-17 unknown field / 本文の削除 | `dump_yaml` が保持。新しい欄も旧コードが保持 (§9.3) |
| §14-18・19 新 DB / Message Bus | なし (JSON ファイル) |
| §14-21 credential / 個人情報の保存 | record に自由文を入れない (`end_code` だけ。§1.3)。監査の門に `_safe_execution_id` |
| 不変条件 1 (queue を開くコードは lib_task_cards) | record の読みも `lib_task_cards` の読み口 (`read_regular_text_or_unreadable`) を通す。読めない = `Unreadable` |
| 不変条件 2 (識別子はファイル名) | record の識別子もファイル名 (`<execution_id>.json`)。task は今どおり `tNNN.md` |
| 不変条件 3 (依存は lib_dep_rules) | Controller は依存を判定しない。pull の候補選びが今どおり lib_dep_rules を呼ぶ |
| 不変条件 4 (デーモン再起動は lib_daemon_watch) | E4 の restart は sync-main-checkout.sh 経由 |
| 不変条件 5 (env 停止スイッチなし) | 付けない。`CREWVIA_EXECUTION_ID` は**値** (名乗り) であってスイッチではない。無くても動き、有れば照合する |
| 不変条件 6 (handoff_path は絶対パス) | 触らない。record の `git.worktree` も絶対パス |
| 不変条件 7 (台帳は消してよい) | registry/daemons は触らない |
| 01a: 回復は拒否を足さない | R-5 も同じ。拒否は Controller の照合と §4.3 の狭めだけで、どれも出口がある (§4.3 の表の理由列) |
| 01a: 正本が先・projection が後 | record・identity・枠のすべてで守る (§1.2) |
| 01b: 1 task = 1 branch | 守る (§3) |
| 01b: `.crewvia-env` の変数は必須にしない | 守る (§5.2) |
| 01b: G1 の CAS を置き換える | E2 (§6) |
| memory `pull-exit-2-is-idle-usage-errors-must-be-1` | pull の exit 2 は `NO_TASK` だけ (§4.4) |

---

## 11. E1〜E4 に渡す「族ごとの掃除」の対象一覧

各実装 task は、自分の PR の差分だけでなく、下の grep の**全件**を表にして「直した / 直さない (理由)」を Result に書く
(改訂案 R6)。件数は d887acf。`scripts/test_*` を除いた本体と、テストを別に数える。

### 11.1 grep と件数 (本体 = `scripts/ hooks/ crewvia` から `scripts/test_` を除く)

| パターン | 本体の件数 | 本体のファイル | テストの件数 |
|---|---|---|---|
| `started_at` | 81 | lib_retirement.py, log_to_obsidian.sh, watchdog.py, lib_daemon_watch.{sh,py} (別の意味), lib_state_store.py, CLAUDE.md, plan.sh | 186 |
| `\.identity` | 24 | lib_retirement.py, dispatcher.sh, watchdog.py, lib_state_store.py, lib_daemon_watch.py, CLAUDE.md, plan.sh | 57 |
| `classify_assignment` | 15 | lib_state_store.py, CLAUDE.md, plan.sh | 10 |
| `started-at` | 11 | lib_retirement.py, plan.sh | 18 |
| `task_started_at` | 13 | lib_retirement.py | 16 |
| `execution_id` | 1 | lib_state_store.py | 4 |
| `assignment_execution_verdict` | 6 | lib_retirement.py, dispatcher.sh (コメント) | 3 |
| `_bound_generation` | 6 | lib_retirement.py | 0 |
| `retire_assignment\(` | 10 | lib_state_store.py, plan.sh | 11 |
| `recover_before\(` | 16 | CLAUDE.md, plan.sh | 2 |
| `publish_assignment\(` | 5 | lib_state_store.py, plan.sh | — |
| `_TASK_STATUS\.accepts\(\|refuse_transition\(` | 17 | CLAUDE.md, plan.sh | — |
| `ACCEPTS_FROM` | 7 | lib_task_status.py, CLAUDE.md, plan.sh | — |
| `audit_actor\|\.actor *=` | 4 | lib_state_store.py, plan.sh | — |
| `crewvia-env` | 6 | start.sh, CLAUDE.md, plan.sh, hooks/pre-tool-use.sh | — |
| `now_generation` | 3 | plan.sh, worktree_gc.py (コメント) | — |

コマンド: `timeout 120 grep -rnE '<pat>' scripts/ hooks/ crewvia | grep -v 'scripts/test_' | wc -l`。
テストの件数は `timeout 120 grep -rnE '<pat>' tests/ scripts/test_*.sh | wc -l`。
**`generation` 単体は使わない** (218 件の大半が lib_mux / lib_daemon_watch の別の意味の世代)。

文書 (エージェントへの指示): `grep -rnE 'started[-_]at|plan\.sh (done|fail|needs-director|verify-result|retire)' agents/ skills/ .claude/skills/`
— 呼び出し箇所は §5.1 の表 (worker.md・director.md・verifier.md・skills/crewvia-qa・skills/crewvia-plan-review)。

### 11.2 段ごとの対象

| 段 | 対象 (§2.1 の # と行) | 確かめること |
|---|---|---|
| **E1** | lib_state_store: #3 (`publish_assignment` に `execution_id`)・#4 (`classify_assignment`)・#5 (R-1)・R-5 の新設・#14 (監査の `execution_id` / `caller_check` / `refused:`)・`_SAFE_RESULT_RE` (:117)・`TASK_META_KEY_ORDER` (:498-504)・`diagnose` (store-check: `legacy_execution` / `execution_fields_stale` / `execution_active_on_non_holding_status` / `execution_record_unreadable`)。lib_task_status: `ACCEPTS_FROM` の狭め (**データだけ**。E1 では plan.sh がまだ読むので、狭めた表は別名で置き、E3 で差し替える)。新 `lib_task_controller.py`。lint_plan: 片方だけの欄・`execution_status` と `execution_end_code` の組が §1.2 の整合に外れるものを FAIL。R-5 は追従する欄 (status / end_code) だけを合わせ、不変の欄を上書きしない (§1.3・§1.4) | 既定値で 01a S3 の互換 golden がバイト一致 / `executions/` を読む・数えるコードが他に無い (`grep -rn "tasks/" scripts/*.py scripts/plan.sh` で列挙を洗う) / 構造ガード (queue 書き込みは lib を通る) に record の書き込みが入る / 監査の門のテストに `_safe_execution_id` |
| **E2** | plan.sh: #2 (:3538-3549)・#7 (G1 の CAS :3204-3241・:3281・:3674)・`.crewvia-env` (:3653-3663)・JSON (:3552-3561・:3676-3679)・`--task` の in_progress 分岐 (:3360-3369)・候補選びの前の再開 (:3316-3331 の回復の後)・`_slugify` (:3607-3616) と `task_slug` の固定。hooks/pre-tool-use.sh:205-207 のコメント。agents/worker.md:297-307 (`CREWVIA_EXECUTION_ID` の export)。kai-review.sh:226-237 (JSON を捨てない準備) | 既存の pull の互換 (COMPAT-01): skill / priority / blocked_by / target_dir / Taskvia disabled / git offline (W6) / 監査行 / 並行 pull で予約 1 件 / pull を reserve〜start の各点で殺して再 pull |
| **E3** | plan.sh: cmd_done (:4331-4340・:4489-4497)・cmd_fail (:4601-4602・:4649-4657)・transition_to_needs_director (:3979-4008)・cmd_needs_director (:4047-4051)・cmd_ready_for_verification (:4934-4938)・cmd_verifying (:4988-4991)・cmd_verify_result (:5076-5108)・#16 `_env_mission_for_task` (:3014-3033)・`audit_actor` (:1876-1879)・`accepts(` / `refuse_transition(` の 17 件。kai-review.sh (:165・:712・:234)。verifier-dispatcher.sh (:258-260・:403-408)。文書: worker.md (done / fail / needs-director の手順)・director.md:205-207・verifier.md:59-67・skills/crewvia-qa/SKILL.md:179-238・skills/crewvia-plan-review/SKILL.md・`plan.sh status` の表示 | §4.3 の全行 (狭めた拒否と、狭めない行が今どおり通る)・§5.2 の全行・§4.4 の冪等と conflict・Director の手順 (§5.3) が全部通る・kai-review の 17 か所の needs-director |
| **E4** | plan.sh: cmd_update --reset (:5945-5953・:6053-6064)・cmd_retire (:6182-6338)・cmd_reap_orphan_assignment (:6341-6439)。lib_retirement: #9〜#13 (:550-592・:605-660・:766-828・:1025-1066・:1494-1610・:1917-1980・:2037-2071)。watchdog.py の `DAEMON_RESTART_FILES` / `files_digest` (state-store.md §10.3 の 10)。dispatcher.sh:777-786 (`AGENT_NAME`)。director.md:1138-1143・:1485-1497・:1511。ここまでが **E4a** (producer の切り替えを含む。世代の照合は残す)。**E4b**: 世代の照合を外す: #4・#8・#11・#12 と旧形式 marker の読み口 | 退役の全経路で新しい試行を殺さない (同名の後任が再 pull した後の退役・旧 marker・reserved の試行の退役)・§7 の 0 件の確認手順・`assignment_execution_verdict` の 4 つの答え (SAME / OTHER / ABSENT / UNREADABLE) が ID でも同じ向きに倒れる |

---

## 12. テスト観点 (QA task への申し送り)

- **E1 (t005)**: 並行 reserve (実 flock・独立プロセス) で active が 1 件・attempt の重複なし / reserve・start・terminal の各点で crash 注入 →
  次のロック取得で R-5 が §1.4 の表どおり / 遷移表の全行 (原案 §10.5 の 14 項目) / 同じ terminal の再送が冪等・違う terminal が conflict /
  ID 生成器の注入 / 呼び出し元ゼロ (`grep`) / terminal の card の後・record の前で落とし、同じ ID の再送の答えが §4.4 の表どおり
  (`WORKER_FAILED` と `RESET_BY_DIRECTOR` で違う。reset の後に `update --status X` しても同じ) / 正しく保存済みの record がある card を
  reset・`update --worker B` した後の回復で、record の agent / reserved_at が変わらない / 欠陥版 (R-5 を消す・照合を `started_at` だけにする・R-5 に agent の比較を戻す・再送判定を record から読む) で赤
- **E2 (t009)**: COMPAT-01 / 原案 §10.6 の全項目を固定 fixture で前後比較 / 並行 pull / pull を reserve の後・worktree の後・env の後・
  start の後で殺し、同じ Worker の再 pull が同じ ID を返す (reserved) / 返さない (running) / 進行中の legacy card の done・retire が今どおり /
  Kai の pull (JSON を捨てる今の kai-review でも壊れない) / G1 の W4 で `failed WORKSPACE_CREATE_FAILED` + needs_director
- **E3 (t013)**: §5.2 の全行を全コマンドで / §4.3 の狭めた行と残した行 / Director の手順 (§5.3) / verify-result fail → pending → 再 pull で attempt 2 /
  旧 worker.md の Worker (名乗りなし) が `unverified` で通る / 監査の `refused:` 行が `die` の後も残る / 欠陥版 (照合を外す) で赤
- **E4a / E4b (t017 と E4b の QA)**: E4a は新 request + 旧 progress / 旧 request + 新 progress の組 (片方のデーモンだけ restart された間。§9.4) で
  照合が今どおり。E4b は §9.2 の 4 の 2 つの隔離 queue。共通: 退役の全経路 (lib_retirement の `_guard` / `_recheck_unprovable` / `_settle_terminated`) で、同名の後任の新しい試行を殺さない /
  旧形式 marker (`task_execution_id` なし) / reserved の試行の退役が released / dispatcher + watchdog の実走 (隔離 mux。memory
  `qa-isolation-via-mux-backend-swap`) / §7 の 0 件の確認手順を隔離 queue で再現

---

## 13. 未決・backlog (01c の完了を止めない)

1. **E5 (名乗りなしの拒否)** — §9.2 の 1。入れるまで §14-9 は部分的。
2. `in_progress_without_worker` の報告の dedup (state-store.md §10.3 の 9) — §8。
3. 退役されていない前任のプロセスが、reset 後の新しい試行と同じ worktree で動くリスク — §3。E3 以後、前任は card を書き換えられない。
4. Taskvia に試行を出すか — §0。
5. 試行の `stale` / `abandoned` / `recovered` — §0 (原案の後続)。reserved のまま放置された試行 (Worker が再 pull しない) は今どおり
   Rule 5 → Director の reset。
6. record の掃除 (archive 以外で消す経路) — 作らない。1 試行 ~600 B で、量が問題になったら archive と同じ手動運用。
