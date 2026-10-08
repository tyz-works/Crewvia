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
  §11 が E1〜E4 に渡す族ごとの掃除の対象一覧。§9.5 が往復 (新 → rollback → roll forward) × 各書き込み点の crash の表 (t024)

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

**正本 (authority) は task card 1 枚のまま。** card に 6 欄を足す (`started_at` の直後。`TASK_META_KEY_ORDER` に入れる):

| 欄 | 型 | 意味 | 書き手 |
|---|---|---|---|
| `current_execution_id` | `ex-<32 hex>` / なし | **最新の**試行の ID。新しい reserve まで残る (terminal になっても消さない) | reserve |
| `execution_status` | `reserved` / `running` / `completed` / `failed` / `released` / なし | その試行の status。task の `status` とは別の欄 (原案 EXEC-05「同じ field で表現しない」) | Controller の全操作 |
| `execution_end_code` | §4.4 の終了コード / なし | その試行を**終わらせた操作**の固定コード (`DONE` / `VERIFIED` / `WORKER_FAILED` / `NEEDS_DIRECTOR` / `VERIFICATION_REJECTED` / `RESET_BY_DIRECTOR` / `RETIRED` / `WORKSPACE_CREATE_FAILED` / `ABANDONED_OUTSIDE_CONTROLLER`)。`execution_status` を terminal にする**同じ card の書き込み**で入れる。active の間と reserve の直後は空。§4.4 の再送判定はこの欄だけを読む (record・task の status を読まない) | terminal にする Controller の操作 |
| `execution_count` | 整数 / なし | 最新の試行の attempt 番号 (= この task で発行した試行の数)。1 から単調増加 (EXEC-02) | reserve |
| `execution_reserved_at` | `started_at` と同じ形 / なし | その試行を予約したとき reserve が `started_at` に書いた値の**写し**。次の reserve まで変えない (reset・retire・`verify-result fail` が `started_at` を null にしても残す)。§1.2 の「試行の読み方」で、card の今の `started_at` がこの試行の予約のものか (= Controller の外で取り直されていないか) を **card 1 枚で**判定するのに使う (t024 / Codex P1) | reserve |
| `task_slug` | 文字列 / なし | 最初の reserve で title から作って**固定**する。以後の reserve はこの値を使う (git-policy.md §10 の 1) | 最初の reserve |

- `execution_end_code` を card に置く理由 (Codex P1-1): §4.4 は同じ ID の再送を「同じ操作なら成功・違えば conflict」で分け、
  `failed` の中でも `WORKER_FAILED` と `RESET_BY_DIRECTOR` で答えが違う。終了理由を record にしか置かないと、card のコミット直後
  (record の前) に落ちたとき R-5 が再生成する record では理由が分からず、task の status も reset の後は Director が自由に変えられる
  (`update --status X`) ので復元の根拠にならない。終了理由を正本に置けば、どの crash の点でも再送の答えは card 1 枚で決まる。
- 欄の組の整合 (lint と `STATE_INVALID`。E1): `execution_status` が `reserved` / `running` なら `execution_end_code` は空、
  `completed` / `failed` / `released` なら空でなく、組み合わせが §4.4 の表にあるもの (`completed` は `DONE` / `VERIFIED` だけ等)。

- **試行の読み方 (`attempt_view(meta)`。E1 の `lib_task_controller` に 1 か所。照合・冪等・R-1・reserve・reset・store-check が
  これだけを呼ぶ)**: card の欄だけから 4 つのどれかを返す。上から順に判定する。

  | 答え | 条件 | 意味 |
  |---|---|---|
  | `NONE` | `current_execution_id` が無い | 試行なし (legacy の card) |
  | `DETACHED` | (a) `started_at` が空でなく `execution_reserved_at` と違う、または (b) `execution_status ∈ {reserved, running}` で task の status が `ASSIGNMENT_HOLDING_STATUSES` (lib_task_status.py:56-61) に無い | 欄は前の試行 X のものだが、card は X の外で取り直された (a: 旧コードの pull) か、X を Controller の外で手放した (b: 旧コードの reset / done 等、Director の `update --status X`)。**card の今の持ち主は X の持ち主ではない** |
  | `ACTIVE` | `DETACHED` でなく `execution_status ∈ {reserved, running}` (⇒ task は holding) | X が今の試行 |
  | `TERMINAL` | `DETACHED` でなく `execution_status` が terminal | X は終わった。`started_at` は X の予約の値か null (reset・retire・`verify-result fail` が消した) |

  - **active な試行** = `ACTIVE`。1 task の active な試行は最大 1 件 (EXEC-05) — 欄が 1 組しか無いので構造的に 1 件。
  - (a) の根拠: 新コードで `started_at` を書くのは reserve (`execution_reserved_at` と同じ値を同じ書き込みで) だけで、他は null にする
    だけ (plan.sh:3540 / :5951 / :6319 を Controller に移した後も同じ)。だから「空でない `started_at` が `execution_reserved_at` と違う」は
    **Controller を通らない pull (旧コード) が card を取り直した**ことを card 1 枚で示す。µs の世代 (`now_generation`) が 2 回の pull で
    一致する確率は今の世代の照合と同じ前提で無視する。
  - (b) は「持ち主のいない active」。DETACHED は**保存しない** (毎回 card から計算する) ので、Director が `update --status blocked` →
    `update --status in_progress` で戻せば ACTIVE に戻る (§4.2 の「推測で terminal にしない」と同じ)。
  - DETACHED の読み方: 照合 (§2.2・§5.2) では「今の試行なし」として読み、X を名乗った呼び出しは `EXECUTION_NOT_CURRENT`
    (§4.4 の冪等は TERMINAL だけ)。R-1 は identity に `execution_id` を入れない。reserve と `update --reset` は DETACHED の active な欄を
    `failed` / `ABANDONED_OUTSIDE_CONTROLLER` で閉じる (§1.4 の手順 0)。store-check は holding の card の (a) を
    `reported:execution_detached`、(b) を `reported:execution_active_on_non_holding_status` (§4.2) で報告する。
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
| reserved_at | **不変** | card の `execution_reserved_at` (card でも次の reserve まで変わらない。t024 で card に置いた) | — (必ずある) |
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
- R-1 の identity の再生成は `execution_id` 欄を card の `current_execution_id` から入れる (§2 の行 5)。**入れるのは
  `attempt_view` が ACTIVE のときだけ**。DETACHED (a) の card (旧コードの pull が B に取り直させた) の枠は B のもので、X を入れると
  B の identity が古い試行を名乗る。DETACHED の card の identity は `started_at` だけで作る (legacy と同じ形。§2.2 末尾の規則で読める)。

| 操作 | 落ちた点 | 次のロック取得時 | 根拠 |
|---|---|---|---|
| reserve | card (X, reserved) の後・record の前 | R-5 が X を作る。R-1 が identity (X) → 本体 | 正本が「A が X を予約した」と言っている |
| start | card (running) の後・record の前 | R-5 が status を running に | 同上 |
| complete / fail / release | card (terminal + `execution_end_code`) の後・record の前 | R-5 が status / end_code を合わせる (agent / reserved_at は既存の record のまま。card の worker が null でも触らない)。R-2 が枠を撤去 | 終わらせる側も正本が先。終了理由も card にあるので、この点で落ちても §4.4 の再送判定は変わらない |
| reset / retire / verify-result fail | card (worker・started_at を null) の後・record の前 | 同上。record は reserve の時点か、この操作の直前の回復で既にある (§1.3「いつ作られるか」) | 不変の欄は card から取り直さない |
| `update --worker B` (active な試行) | card の後 | R-5 は何もしない (追従する欄は変わっていない)。record の agent は予約した A のまま | record の agent は履歴 |
| どれでも | record の後・assignment の前 | 01a の R-1 / R-2 のまま | record は判断に使わないので、record が先に進んでいても結論は変わらない |
| reserve の手順 0 (前の試行 X を閉じる) | card (X を `failed` / `ABANDONED_OUTSIDE_CONTROLLER`) の後・record X の前 | X はまだ card の `current_execution_id` なので R-5 が record X を合わせる。task は pending のままなので次の pull が reserve をやり直す (X は terminal なので手順 0 は飛ばす) | 前の試行の終端が正本 (card) に先に入る |
| 同上 | record X の後・新しい card の前 | 何もしない (record X は card と一致済み) | 同上 |
| reserve (前の試行が凍結される) | 新しい card (Y) の後 | R-5 は新しい Y だけを見る。前の試行 X の record は、Y を書く**前**に card 上で terminal になり、R-5 (reserve の前の回復) か手順 0 で card に合わせ済み | **X の終端は Y を書く前に必ず card にあった** (下の手順 0) |

**reserve の手順 0 (前の試行の終端を正本に残す。t024 / Codex P2-2)**。reserve は pending の card にしか通らない。その card の
`attempt_view` が DETACHED で `execution_status` が reserved / running (= X が active のまま、task は誰も持っていない: 旧コードの
reset・Director の `update --status pending` の後。pending なので (b) の条件を必ず満たす。旧コードの pull の後の `update --status pending`
では (a) も満たすが、扱いは同じ) なら、Y を発行する**前に、同じロックの中で**次の順に書く:

1. card: `execution_status = failed`・`execution_end_code = ABANDONED_OUTSIDE_CONTROLLER` (他の欄は変えない) — **コミット点**
2. record X: 追従する欄を card に合わせる (R-5 と同じ書き込み。無ければ card から作る)・監査に `reported:stale_execution_status`
3. 以後は通常の reserve (card に Y → record Y → identity → 枠)

- 旧案 (§9.3 の 1 巡目) は「Y の card を書いた後で X の record を凍結する」で、Y の card の直後に落ちると X は card の
  `current_execution_id` から外れ、R-5 (最新 ID だけ) が二度と X を扱わない。X の終端が card に一度も保存されないので回復できなかった。
  手順 0 は**終端を正本に先に書く**ので、どこで落ちても「X の終端は card にある or X は card の今の試行で R-5 の範囲」のどちらか。
- 捨てた案: card に `previous_execution_id` を持ち R-5 が 2 件を合わせる。欄が増え、旧コードの往復が 2 回続くと 3 件目が漏れる
  (同じ族の再発)。手順 0 は「current を進めるのは reserve だけ、進める前に前の current を card 上で terminal にする」という不変条件で、
  件数に依らない。
- これで **「card の current でない record は、card が current を進めた時点の terminal に合っている」** が全経路で成り立つ。
  例外は record を手で消した・読めない場合だけ (報告。§1.4 の R-5 と同じ)。store-check は current でない record が active のまま
  (card を手で編集した・lib を通らない書き込みがあった等。どの Controller の経路からも作られない) を `reported:execution_record_superseded_active` で報告する (書かない。履歴の欠けで、状態の
  判断には使わない)。
- 手順 0 は `update --reset` でも同じ: reset の対象が DETACHED で欄が reserved / running なら `RESET_BY_DIRECTOR` ではなく `ABANDONED_OUTSIDE_CONTROLLER`
  で閉じる (試行は reset の前に Controller の外で手放されていた。§4.2)。

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
| 4 | 予約時の世代 (旧案は record `reserved_at`) | `attempt_view` の DETACHED (a) (旧案の `reported:execution_fields_stale`) | 判断 | **card に持つ** (`execution_reserved_at`。t024)。旧案は record と比べる報告だけで、しかも試行が active の card しか見なかったので、X を completed にした後に旧コードが取り直した card (Codex P1) を見逃した。card の `started_at` と `execution_reserved_at` の比較は試行の status に依らず、照合・冪等の判断に使う (§1.2) |
| 5 | 試行の status / attempt / ID (record) | R-5 の比較 | 回復 | card が正本 (`execution_status` / `execution_count` / `current_execution_id`)。record を card に合わせる向きだけ |
| 6 | running_at / ended_at / git.head_at_start (record) | 表示・`get_execution` | 表示 | **判断に使わない** (§1.3)。欠けても null |
| 7 | `execution_id` / `started_at` (identity) | `classify_assignment` (#4)・`assignment_execution_verdict` (#11)・R-1 (#5) | 照合 | **card から再生成**: R-1 が `current_execution_id` / `started_at` から作り直す (§1.4 末尾)。食い違えば card が勝つ |
| 8 | `<slug>:<tid>` (枠の本文 `queue/assignments/<agent>`) | dispatcher の busy・`is_orphan_target` | 判断 | **card から再生成** (R-1 / R-2。01a のまま) |
| 9 | `task_execution_id` / `task_started_at` (退役 marker) | `_guard` / `_cleanup_deferred` / `_settle_terminated` (#12・#13)・retire の照合 (#8) | 照合 | **名乗り (証拠) であって正本ではない**: request を書いたロックの中で card から写した「どの試行を終わらせるか」。照合の答えは card が出す (card の今の試行と一致したときだけ後始末する)。marker が欠ける・読めない・欄が無いときは**保留して Director** (今の `_cleanup_deferred` と同じ hold。推測で後始末しない)。card から再生成しない理由: 後始末の時点で card は後任の試行に進んでいるかもしれず、そのとき card から作ると後任を終わらせる |
| 10 | `CREWVIA_EXECUTION_ID` (`.crewvia-env`・env)・`--execution`・pull の JSON | §5.2 の照合 | 照合 | **名乗り**。card の `current_execution_id` と比べるだけで、card に書かない。欠けたら「名乗りなし」(§5.2) |
| 11 | 監査ログ (`caller_check` / `execution_id` / `refused:`) | E5 の merge 条件 (§9.2 の 1)・本番確認 | cutover の判断 (人) | **判断の唯一の根拠にしない**: 監査は行が欠けうる (state-store.md §4) ので、欠けると `unverified` が少なく数えられる向きに誤る。E5 の条件は監査の 0 件に加えて、**コードと手順から言えること** (worker.md・kai-review・skill が ID を渡す・生きている Worker のセッションが全部その変更の後に起動した。§9.4) を要る |
| 12 | `.crewvia-env` の `CREWVIA_TASK_SLUG` | worktree のパス | 表示・パス | card の `task_slug` (§1.2) から pull が毎回書く。判断に使わない |
| 13 | 前の試行の終端 (旧案は reserve の後に record だけを凍結) | 前の試行の record・store-check | 回復 | **card に先に書く** (§1.4 の手順 0。t024 / Codex P2-2)。旧案は終端が card に一度も入らず、新しい ID を書いた直後に落ちると回復の範囲 (最新 ID) から外れた |
| 14 | 同じ予約の pull が進行中か (プロセスの中) | §6 の再開 | 判断 | **正本に置かない・推測しない**: 準備ロック (§6.1。kernel の flock) が持つ。card は「reserved か」だけを答え、「誰かが準備中か」は flock が答える。flock は持ち主のプロセスが死ねば kernel が外すので、古い値が残らない |

- 表に無い値で判断・回復・照合に使うものを足す PR は、この表に行を足す (E1〜E4 の Result の族の表 §11 と同じ)。

---

## 2. 世代の置き換え (改訂案 §5「並行して別の仕組みを作らない」)

### 2.1 今 `started_at` を世代として照合・転写している箇所 (全部)

調べ方: `timeout 120 grep -rnE '<pat>' scripts/ hooks/ crewvia | grep -v scripts/test_` (§11.1 に件数)。「時刻として使う」箇所
(watchdog の idle 時計・Taskvia) は照合ではないので表の外に置き、§2.3 に別に書く。

| # | 場所 | 何をしている | R/W | 移す段 |
|---|---|---|---|---|
| 1 | plan.sh:487-497 `now_generation` | 世代を作る | 源 | 残す (時刻として。§0) |
| 2 | plan.sh:3538-3541 `cmd_pull._do` | card に `started_at` を書き、`started_holder` に持つ | W | E2: 同じ場所で reserve が `current_execution_id` / `execution_status` / `execution_count` / `execution_reserved_at` (= 書いた `started_at`) / `task_slug` も書く |
| 3 | plan.sh:2006-2014 → lib_state_store.py:1054-1060 `publish_assignment` | identity に `started_at` を写す | W | E1: identity に `execution_id` 欄を足す (引数で渡す)。E2 から呼び出し側が渡す |
| 4 | lib_state_store.py:1082-1104 `Txn.classify_assignment` | identity の `started_at` と世代を比べ MINE / SUCCESSOR | R | E1: 引数 `execution_id` を足す (§2.2 の規則)。E4 で世代の比較を外す |
| 5 | lib_state_store.py:1359-1422 `_r1` (**R-1**) | card の `started_at` で identity を作り直す / 一致を確かめる (`generation_mismatch`) | R/W | E1: card に `current_execution_id` があれば identity にも入れ、照合も ID で。legacy の card は今のまま |
| 6 | lib_state_store.py:1312-1356 `_r2` (**R-2**) / :624-632 `is_orphan_target` | 世代を見ない (`retire_assignment(..., None)`) | — | 変えない (撤去するのは card が手放した枠だけ。世代が要らない) |
| 7 | plan.sh:3204-3241 `_pull_worktree_failed` (**G1 の CAS** :3226-3228) | status・worker・`started_at` がこの pull の予約のままか | R | E2: **置き換えではなく AND**。`current_execution_id == X` ∧ `execution_status == reserved` の**上に**、今までの項 (in_progress・worker 一致・`started_at` 一致) を残す (`_pull_cas_ok`。§15.2 の 2・§18.3。Codex 3 巡目 P1)。移行中は旧形式の書き手 (rollback 中の旧コード・Director の手編集) が status / worker / started_at だけを動かし execution の欄を更新しないので、新しい欄だけでは解放済みの予約に `X / reserved` が残ったまま CAS が通る |
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
| 18 | agents/director.md の Rule 5 の通知の対応表 (`[Rule 5] Worker {name} が blocked / idle-with-task です` の行の `(3) 回復不能なら kill + plan.sh retire`) と「kill した Worker の後始末は `plan.sh retire`」の節 | Rule 5 で Director が打つ `retire` の名指し (旧: `--started-at`) | 文書 | E4: 上の #17 と同じ形 (`--execution <plan.sh status の ex-…>`)。ID の無い旧形式の card は retire では手放せず `update --status pending --reset` (E4b) |
| 19 | scripts/plan.sh の冒頭の使い方 (`retire` の行 :56-62・`done` / `fail` の `--execution` の行 :29-32 ・:45) と `--help` の文言・:6731 / :4866 / :6981 のコメント | `--started-at` / 世代の名指しの説明 | 文書 | E3: `--execution` の行を足す (戻し先の互換のため読み捨てる注記つき)。E4b: `retire` の `--started-at` を外した旨に書き直す。**コメントの残り (`exit 1` と書いたが実際は使い方の誤りで exit 2・`generation=None` のまま) は §19.2 の backlog** |

`execution_id` が今コードに現れるのは #14 の 1 か所 (null 固定) だけ (`grep -rn execution_id scripts/ hooks/ | grep -v test_` = 1 件)。

### 2.2 移行期に両方を照合する規則

移行期 = E2 の merge から、§7 の条件 (E4a が両デーモンで動いている・legacy の進行中 = 0・旧形式 marker = 0) を満たして E4b が merge されるまで。規則は 1 つの関数
(`lib_task_controller` の `execution_matches`。E1) に置き、#4・#7・#8・#11 がそれを呼ぶ (コピーしない。原案 §14-7)。

| card | 名乗られた証拠 | 判定 |
|---|---|---|
| `current_execution_id = X` (E2 以降に予約された試行) で `attempt_view` が ACTIVE / TERMINAL | execution_id = X | **一致** |
| 同上 | execution_id = Y (≠ X) | **不一致** (`EXECUTION_NOT_CURRENT`)。`started_at` が一致していても不一致 (ID が優先) |
| 同上 | execution_id なし・`started_at` だけ (旧 marker・旧引数) | `started_at` が一致すれば**一致**、ただし監査行に `caller_check=legacy_generation` を残す (E4 の後は不一致に倒す) |
| `current_execution_id` なし (legacy の試行) | `started_at` | 今の世代の照合のまま |
| 同上 | execution_id | **不一致** (この card はその ID を発行していない) |
| `attempt_view` が DETACHED (欄は X だが card は X の外で取り直された・手放された。§1.2) | execution_id = X または Y | **不一致** (`EXECUTION_NOT_CURRENT`)。X はこの card が発行したが、今の持ち主の試行ではない |
| 同上 | `started_at` だけ | 「`current_execution_id` なし」の行と同じ (今の世代の照合)。旧コードの pull で取り直した持ち主は旧コードの世代を持っているので、それで照合する。監査行は `caller_check=detached_execution` |

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
| reserve (pull の 1 つ目のロック) | (なし or terminal) → reserved。前の試行が DETACHED で reserved / running なら先に手順 0 で failed `ABANDONED_OUTSIDE_CONTROLLER` (§1.4) | pending → in_progress | worker = A・枠を公開 (identity に ID) |
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
| update --reset | ACTIVE: reserved → released `RESET_BY_DIRECTOR` / running → failed `RESET_BY_DIRECTOR`。DETACHED で reserved / running: failed `ABANDONED_OUTSIDE_CONTROLLER` (§1.4 の手順 0 と同じ書き込み)。それ以外は変えない | any → pending (+ `--status` の後書きは今どおり) | worker・started_at を null・枠撤去 (今と同じ) |
| retire --outcome reset | reserved → released `RETIRED` / running → failed `RETIRED` | in_progress → pending | 同上 |
| retire --outcome needs-director | 同上 | in_progress → needs_director | worker 残す・枠撤去 |
| update --status X (--reset なし。Director) | **変えない** | any → X | 今と同じ (枠は触らない) |
| reap-orphan-assignment | 変えない | 変えない | 枠撤去 (projection だけ) |

- 試行の無い task への操作 (legacy の card・Director の `update --status in_progress --reset` で開いた card・needs_director の後等) は
  **task の遷移だけ** (`mark_task`)。試行の欄は触らない (§5.3 の「試行なし」の経路)。
- `update --status X` で active な試行が残ったまま task が holding でない status になった card (例: running のまま `blocked`) は、
  §1.2 の `attempt_view` で DETACHED (b) (active でない)。試行の欄は書き換えない (推測で terminal にしない。原案 §14-16。
  `update --status in_progress` で戻せば ACTIVE に戻る)。store-check が `reported:execution_active_on_non_holding_status` を出す。
  次の reserve は pending からしか通らないので、Director が `--reset` する (そのとき reset が試行を failed
  `ABANDONED_OUTSIDE_CONTROLLER` にする)。`--reset` なしで pending にした場合は次の reserve の手順 0 が同じく閉じる (§1.4)。

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

**冪等**: 名乗った ID が card の `current_execution_id` と一致し、`attempt_view` が **TERMINAL** のとき、答えは **card の
`execution_end_code` だけ**で決める (record・task の status は読まない。§1.2 / §1.6 の 1)。
**DETACHED のときは表を引かず `EXECUTION_NOT_CURRENT`** (t024 / Codex P1): X を completed にした後に旧コードが card を取り直した
(別の作業が in_progress) なら、X の done の再送を「成功」と答えると、呼び出し元は今の task が完了したと読む。新コードだけで同じことが
起きる経路 (reset → 再 pull) では新しい ID が発行され、X の再送は `EXECUTION_NOT_CURRENT` になる。DETACHED (a) はその再 pull が
Controller の外で起きた形なので、同じ答えに揃える。`started_at` が null の TERMINAL (reset・retire・`verify-result fail` の後) は
取り直されていないので、今までどおり表を引く:

| card の `execution_end_code` (status) | 来た操作 (同じ ID) | 結果 |
|---|---|---|
| `DONE` (completed) | done (**同じ中身**) | **成功 (exit 0)・何も書かない**・stdout に `already completed (idempotent)`。今は「already done」exit 2 なので、**ID を名乗った呼び出しだけ**挙動が変わる。中身 (`--pr` / `--no-pr` / Result) が違えば conflict (§16.10) |
| `VERIFIED` (completed) | verify-result pass | 成功・何も書かない |
| `WORKER_FAILED` (failed) | fail (**同じ中身**) | 成功・何も書かない (head / no-head / handoff が違えば conflict。§16.10) |
| `NEEDS_DIRECTOR` (failed) | needs-director (**同じ中身**) | 成功・何も書かない (reason が違えば conflict。§16.10) |
| `VERIFICATION_REJECTED` (failed) | verify-result fail | 成功・何も書かない (verifier の再送) |
| `RETIRED` (failed / released) | retire (`--execution` が同じ ID) | 成功・何も書かない (watchdog の `_settle_terminated` が card の書き込みの後に死んで打ち直す場合。E4a) |
| `DONE` / `VERIFIED` | 上の行以外 (fail / needs-director / done を `VERIFIED` に等) | **conflict** (`EXECUTION_ALREADY_TERMINAL`・exit 3・何も書かない) |
| `WORKER_FAILED` / `NEEDS_DIRECTOR` / `VERIFICATION_REJECTED` | 上の行以外 | conflict |
| `RESET_BY_DIRECTOR` / `RETIRED` / `WORKSPACE_CREATE_FAILED` / `ABANDONED_OUTSIDE_CONTROLLER` (failed / released) | done / fail / needs-director / verify-result | conflict (`EXECUTION_NOT_CURRENT` と同じ扱い: その試行は持ち主以外の操作で終わり、もう誰の作業でもない) |

- **終了コードの書き手** (どれも terminal にする card の書き込みと同じ 1 回): done → `DONE`、verify-result pass → `VERIFIED`、fail → `WORKER_FAILED`、
  needs-director → `NEEDS_DIRECTOR`、verify-result fail (< max) → `VERIFICATION_REJECTED`、update --reset → `RESET_BY_DIRECTOR`、
  retire → `RETIRED`、G1 → `WORKSPACE_CREATE_FAILED`、reserve の手順 0 と DETACHED への update --reset →
  `ABANDONED_OUTSIDE_CONTROLLER` (§1.4)。
- **reset の後に Director が task の status を変えても答えは変わらない**: `update --status X` (--reset なし) は試行の欄を触らない (§4.2)
  ので `execution_end_code` は残る。次の reserve が新しい ID を発行した後は、古い ID の再送は `EXECUTION_NOT_CURRENT`。
- crash の点 (§1.4): 終了コードは terminal の status と同じ card の書き込みに入るので、「status は terminal・理由は不明」の card は
  できない。record が遅れていても答えは同じ。
- 「同じ操作」はまず操作の種類で判定し (Controller の `IDEMPOTENT`)、**そのうえで要求の中身を card と比べる** (t034 / §16.10)。
  done は `--pr` / `--no-pr` / Result 本文、fail は head / no-head / handoff、needs-director は reason。**違えば conflict (exit 3・何も書かない・
  値は出さず項目名だけ)**、同じなら成功 (exit 0)。上の表の「成功」は**同じ中身の再送**の行で、違う中身の 2 回目は新しい記録にならず conflict。
  (E3 の初版は中身を見ずに exit 0 を返し、`done "first" --pr 5` → `done "CORRECTED" --pr 6` が成功に見えて card は最初のままだった)
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
| TERMINAL (X) | X | §4.4 の冪等 / conflict | `verified` |
| TERMINAL (X) (Director が開いた card・needs_director の後) | なし | 「試行なし」と同じ (task の遷移だけ。試行の欄は触らない) | `no_execution` |
| 試行なし (`NONE`: legacy の card) | なし | 通す (task の遷移だけ。§4.3 の表で受け付ける from に限る) | `no_execution` (legacy の in_progress は `legacy_generation`) |
| 試行なし (`NONE`) | X | 拒否 `EXECUTION_NOT_FOUND` (exit 3) | 拒否の行 |
| DETACHED (欄は X。§1.2) | X / Y | 拒否 `EXECUTION_NOT_CURRENT` (exit 3)。X を名乗っても冪等の表を引かない (§4.4) | 拒否の行 |
| DETACHED | なし | 「試行なし」と同じ (task の遷移だけ。X の欄は触らない — X を閉じるのは reserve / reset の手順 0 だけ) | `detached_execution` |

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
ロック 1: recover → 候補選び → reserve (手順 0 → card: in_progress / X / reserved / attempt / execution_reserved_at /
          task_slug、record、identity(X)、枠) または再開の判定 (書かない)
準備ロック: queue/missions/<slug>/executions/<tid>.prepare.lock を LOCK_EX|LOCK_NB で取る (§6.1)。取れなければ exit 1・何も書かない
          取れたら card を読み直し (ロックなしの読み)、X が reserved でなければ exit 1・何も書かない
ロック外: Taskvia → worktree (W0〜W7) → git rev-parse HEAD → .crewvia-env (X を含む)          [準備ロックを持ったまま]
ロック 2: recover → start (CAS: `_pull_cas_ok` = in_progress ∧ worker 一致 ∧ started_at 一致 ∧ current_execution_id == X ∧ execution_status == reserved
          → running、record。新しい欄だけの CAS ではない。理由は §2.1 #7・§15.2 の 2)
          失敗 (worktree / env): CAS が同じなら fail_execution(X, WORKSPACE_CREATE_FAILED) + needs_director (G1 の出口)
          CAS が外れたら何も書かず exit 1 (JSON を出さない)                                     [準備ロックを持ったまま]
準備ロックを外す
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
- **G1 (needs_director の出口) との関係**: G1 の CAS (#7) に ID の項を足す (git-policy.md §16.4 の 5。**今までの項は外さない** — AND。§15.2 の 2・§18.3)。worktree を作れない
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

### 6.1 同じ予約の並行 pull (t024 / Codex P2-1)

**問題**: 再開は「同じ agent・reserved」だけを見るので、最初の pull がまだロック外 (worktree の準備中) にいる間に 2 本目の pull
(同じ agent 名。dispatcher の指示の再送・Worker の打ち直し・退役されていない前任) が来ると、2 本とも同じ branch / path に
`git worktree add` する。片方が W4 / W5 で失敗し、その失敗側が成功側の start より先にロック 2 を取ると、CAS (X・reserved) は
一致するので試行を failed `WORKSPACE_CREATE_FAILED` にし、task は needs_director、成功側の start も CAS で外れる (両方失敗)。
start の CAS は「試行がまだ reserved か」しか言えず、「他の pull が準備中か」を言えない。

**決定: ロック外の準備 (Taskvia・worktree・`.crewvia-env`) とロック 2 を、task ごとの準備ロックで直列化する。待たない。**

- **準備ロック**: `queue/missions/<slug>/executions/<tid>.prepare.lock` を `flock(LOCK_EX | LOCK_NB)`。最初の pull も再開も、
  ロック 1 を外した後・Taskvia の前に取り、ロック 2 のコミット (start / G1 の失敗 / CAS 外れ) の後に外す。中身は空で、消さない
  (archive が dir ごと動かす)。取得・解放は lib_state_store の口を通す (不変条件 1・構造ガード §10 の §14-8)。
- **取れない = 同じ task の pull が準備中** → その pull は **exit 1・何も書かない・worktree と `.crewvia-env` に触れない・JSON を
  出さない** (`TASK_ALREADY_RESERVED`、文面は「同じ予約の pull が進行中」)。監査の行は出さない (ロックの外で決まる拒否。§8 の
  「ロック」と同じ理由)。待たない理由: 待つと先の pull の後に W2 で同じ worktree を再利用し、ロック 2 で CAS が外れるだけ
  (やることが無い)。
- **取れた後に card を読み直す** (ロックなしの読み。`lib_task_cards`): X が reserved でなければ (先の pull が start 済み・reset 済み)
  exit 1・何も書かない。この読みは「無駄な worktree 操作をしない」ための早期打ち切りで、正しさは CAS が持つ (読みと CAS の間に
  変わっても、CAS が外れて何も書かない)。
- **これで「失敗側が他の pull の進行中を見分ける」が構造になる**: G1 の失敗を書けるのは準備ロックを持つ pull だけで、持っている間は
  同じ task の他の pull は worktree に触れていない。だから W4 / W5 の失敗は「他の pull と競合した」ではなく、その pull 単独の失敗。
- **ロックの順序**: 準備ロックは queue/.lock の**外側**で、queue/.lock を握ったまま準備ロックを取らない (ロック 1 は準備ロックの前に
  外す)。lib_state_store.py:1609-1616 の「queue/.lock が最も外側」は S5 の小さな共有ファイルの規則で、準備ロックはそれより外の
  1 段として足す (E2 でその docstring に追記する)。LOCK_NB なので、仮に逆順で取るコードが入っても待ちの輪 (deadlock) にはならず
  exit 1 になる。
- **古いロックが残らない**: flock は持ち主が死ねば kernel が外す。準備中に殺された pull の後は、次の再 pull が取れて
  再開できる (N8 の経路のまま)。ファイルの有無ではなく flock で判断するので、残ったファイルは意味を持たない。
  **ただし flock は「開いたファイル記述」に付く**: 準備の subprocess (worktree を作る helper・`git rev-parse`) に記述子を渡して
  (`PrepareLock.fileno()` → `subprocess.run(pass_fds=...)`) あるので、**python だけが SIGKILL されても、子孫が記述子を持っている間は
  ロックが外れない** (t031 / Codex P2。渡していなかった間は、親だけの kill で子孫が排他の外で動き続け、再試行が別の helper を同時に走らせた)。
  子孫が終われば kernel が外す。親だけが死んで子孫が生きている間の再 pull は exit 1「同じ予約の pull が進行中」(`TASK_ALREADY_RESERVED`・何も書かない)。
  残る限界: helper の下で detach する子孫 (例: git の自動 gc のデーモン化) も記述子を持ち続けるので、その間は再 pull が待たされる
  (fail closed。外す手段は無く、子孫が終わるのを待つ)。
- **network をロックの中に入れない (§14-15)** に当たらない: §14-15 は queue/.lock の話で、準備ロックは同じ task の pull だけを
  止める (他の task・他のコマンドは止めない)。
- 捨てた案: pull ごとの nonce を card に書き、start / G1 の CAS を「最後に再開した pull だけ」にする。古い側は書けなくなるが、
  新しい側の `git worktree add` が古い側の作りかけと衝突して失敗すると、正しく作れた worktree があるのに needs_director になる。
  直列化しない限り、どちらの失敗が本物かを CAS では言えない。
- 捨てた案: 準備をロック 1 の中に入れる。Taskvia (network) と git を queue/.lock の中で走らせることになる (§14-15)。

**E2 のテスト項目** (独立プロセス・実 flock。§12 の E2 行に入れる):

1. 1 本目を worktree の直前 (準備ロック取得後) で止め、同じ agent の 2 本目を走らせる → 2 本目は exit 1・card / record / 監査の
   本体行 / worktree / `.crewvia-env` が 1 バイトも変わらない。1 本目を進めると start → running・JSON
2. 1 本目をロック 1 の後・準備ロックの前で止め、2 本目が準備ロックを取って最後まで進む → 1 本目は準備ロックか読み直しで exit 1、
   JSON を出さない・何も書かない
3. 準備ロックを持つ側に W4 / W5 を注入 → G1 が `failed WORKSPACE_CREATE_FAILED` + needs_director (他に準備中の pull が無いので正しい)。
   その間に来た 2 本目は exit 1
4. 準備ロックを持つ pull を SIGKILL → 次の再 pull が準備ロックを取れ、同じ X で再開 (attempt が増えない)
5. 欠陥版 (準備ロックを外す) で finding の経路 (2 本とも失敗・X が `WORKSPACE_CREATE_FAILED`) が再現して赤になる
   (memory `regression-test-must-prove-red`)
6. 別の task の pull は準備ロックで止まらない (task ごと)

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
  E1 で足す報告) と `reported:execution_detached` (holding の card で `attempt_view` が DETACHED (a): rollback 中に旧コードの pull が
  取り直した持ち主で、legacy と同じく `started_at` でしか照合できない。§1.2) を合わせて 0 件 (2) `registry/retirements/*.json` と `*.progress.json` で `task_execution_id` の無い marker が 0 件。
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
| 照合の結果が残らない | 本体の行に `caller_check` (`verified` / `unverified` / `legacy_generation` / `no_execution` / `detached_execution`) を足す。E5 の判断材料 (§5.2) | E1 / E3 |
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
| **E2** (t008) | pull が ID を発行 (card に 6 欄 (`execution_end_code` は空)・`executions/` に record・identity に `execution_id`・`.crewvia-env` に `CREWVIA_EXECUTION_ID`・JSON に `execution_id` / `attempt`) / pull が 2 つ目のロックを取る (start) / **同じ Worker の再 pull が reserved の試行を再開** / G1 の CAS が ID に / `task_slug` を card に固定 (title を変えても branch が変わらない) / 監査行の `execution_id` | **ユーザー承認** (t011) | 実 Worker の pull 1 回で: card に 6 欄 (`execution_reserved_at` = `started_at`)・record が 1 つ・identity に ID・`.crewvia-env` が 4 行・監査行に ID が出る / 既存の進行中 card (legacy) の done が今どおり通る / dispatcher の busy / idle が変わらない (枠の本文は同じ) / `store-check` の差分が `legacy_execution` の報告だけ | revert → `scripts/sync-main-checkout.sh`。**新しい欄・record・identity の欄は残ってよい**: 旧コードは欄を `dump_yaml` で保持し (読まない)、`executions/` を読まず、identity の `started_at` だけを見る (§9.3) |
| **E3** (t012) | done / fail / needs-director / ready-for-verification / verify-result が ID を照合 (違う ID は exit 3)・**§4.3 の狭め** (done が pending / blocked / 検証待ちから通らない等)・`verify-result fail` が pending (新しい試行へ)・ID を名乗った再送が冪等 (exit 0)・拒否の行・`caller_check`・kai-review.sh が ID を渡す・worker.md / skill / director.md が `--execution` を使う・`plan.sh status` に ID 表示 | **ユーザー承認** (t015)。**狭めの表 (§4.3) と verify-result fail の変更を承認の対象として明示する** | 実 Worker の done が `caller_check=verified` / 旧プロンプトの Worker の done が `unverified` で通る / Kai の review が done / needs-director まで通る / Director の cutover review task の `update --status in_progress --reset` → done が通る / 監査に `refused:` が出たら 1 件ずつ妥当か | revert → sync。card の新しい欄は E2 と同じく残ってよい。**狭めを戻すと再び pending から done が通る** (戻すのは制限を外す方向なので壊れない)。**revert 先でも `--execution` を受け付ける (読み捨てる) 互換を E3 の前に別 PR (#272) で入れた** — 起動済みの Worker・走っている kai-review.sh は revert の後も `--execution` を付けて報告し続けるので、戻し先が `unknown option` で拒否すると完了・失敗の報告が止まる。**#272 を E3 より先に merge すること** (§16.4) |
| **E4a** (t016) | **退役 marker / progress に `task_execution_id` を書き始める** (producer の切り替え)・retire が `--execution` を受け付け・`update --reset` / retire が試行を release / fail (`execution_end_code`)・reap の照合が ID 優先・デーモンの `AGENT_NAME`。**世代の照合は残す** (新旧の marker を両方読む) | **ユーザー承認** (t019)。0 件の確認は**要らない** (旧形式も読める) | **dispatcher と watchdog の両方が restart された**こと (起動時刻 > sync。§7 の (0)) / 新しい marker・progress に `task_execution_id` / 退役の全経路で新しい試行を殺さない (QA t017 の観察を本番で 1 件) / 監査の retire 行の actor が `watchdog` | revert → sync。**両デーモンとも常駐なので restart が要る** (どちらも `lib_retirement` を import する: `DAEMON_RESTART_FILES` lib_daemon_watch.py:240-258。memory `merged-daemon-code-is-inert-until-restart`)。新しい marker の `task_execution_id` は旧コードが読まない (余分な欄) |
| **E4b** (新 task) | **世代の照合を外す** (§2.4 の 4: #4・#8・#11・#12 の `started_at` の分岐と、旧形式 marker の読み口) | **§7 の (0)〜(2)** + **ユーザー承認** | 退役が ID だけで通る / `caller_check=legacy_generation` が出ない / 保留 (Director への hold) が増えていない | revert → sync (両デーモン restart)。E4a のコードに戻るので新旧の marker を両方読める |

共通 (01a / 01b と同じ):
- merge 後に主 checkout を ff するまで本番は旧コード (memory `main-checkout-lags-after-pr-merge`)。確認は `git -C <主 checkout> log -1` から。
- 新しい版が動いたことは 3 点で証明する (memory `prove-which-code-version-a-spawned-task-ran`): 主 checkout の HEAD・プロセスの起動時刻・新しい版にしか書けない行 (E2 なら監査行の `execution_id`)。
- env の停止スイッチは付けない (不変条件 5)。戻しは常に revert。

### 9.2 分割の提案 (Director が card を組み替える)

1. **E5 を足す (提案)**: 「名乗りなしの done / fail / needs-director / verify-result を拒否する」(§5.2)。t020 の本番確認の後。
   条件は監査ログの `caller_check=unverified` が crewvia 本体の task で 0 件、target_dir の task の ID の受け渡しが worker.md に
   入っていること。これが入るまで AC-04 は部分的 (§10)。**【決定 (Director 判断) E5 は 01c に入れず後続に送った**。条件はこの項のとおり
   (監査の `caller_check=unverified` が 0 件・target_dir の task の ID の受け渡し・生きているセッションが ID を渡す版の後に起動。§9.4 の E5 行)。実績は §19.3】E3 に入れない理由は §5.2 (env を必須にしない・cutover の瞬間に
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
| card の `current_execution_id` / `execution_status` / `execution_end_code` / `execution_count` / `execution_reserved_at` / `task_slug` | 読まない。書き直すときは末尾に保持する (並びは変わる) | lib_state_store.py:510-524 `dump_yaml` |
| 同上 (lint) | 未知キーの検査が無いので通る | §1.1 の grep |
| `queue/missions/<slug>/executions/*.json` | 読まない。archive は dir ごと動かす | `list_tasks` は `tasks/` だけを読む (E1 で確かめる) |
| identity の `execution_id` 欄 | 読まない (`started_at` だけを比べる) | lib_state_store.py:1097-1104 / lib_retirement.py:649-659 |
| `.crewvia-env` の 4 行目 | 次の pull が 3 行で書き直す | plan.sh:3653-3663 |
| 監査行の `execution_id` / `caller_check` / `refused:` | 読み手がいない | state-store.md §10.2 の S3 行と同じ |
| 退役 marker の `task_execution_id` | 読まない | lib_retirement の読みは `task_started_at` だけ |

**戻した後にもう一度進める (roll forward) とき** (t024 で書き直し。旧案の `reported:execution_fields_stale` は試行が active の
card しか見ず、X を completed にした後に旧コードが `update --reset` → pull した card — 別の作業が in_progress なのに
`X / completed / DONE` が残る — を見逃した。Codex P1):

- 旧コードは execution の 6 欄を書かず消さない (`dump_yaml` が保持)。変えるのは task の status / worker / `started_at` と projection だけ。
  旧コードが `started_at` を書くのは pull (新しい µs の値) と reset / retire (null) だけ (plan.sh:3540 / :5951 / :6319)。
- だから roll forward の後、新コードは**どの card も `attempt_view` (§1.2) で card 1 枚から読み分けられる**:
  旧コードの pull が取り直した card は試行の status に依らず (active でも completed でも) DETACHED (a)、旧コードが手放した active な
  試行は DETACHED (b)。DETACHED の X を名乗った再送は **`EXECUTION_NOT_CURRENT` (exit 3・何も書かない)** で、§4.4 の冪等の表を
  引かない。旧コードの持ち主 (名乗りなし) の操作は「試行なし」として task の遷移だけ (§5.2)。
- **roll forward の前に Director がやることは無い** (正しさは card から構造で決まり、手順に頼らない。原案 §14-3・4)。roll forward の
  後に `store-check` を走らせ、`reported:execution_detached` (holding の DETACHED (a)) と `execution_active_on_non_holding_status`
  (DETACHED (b)) を**確認として**見る。DETACHED (a) の card は旧コードの持ち主が終えるのを待つか、Director が `update --reset` で
  手放させる (reset は DETACHED の active な欄を `ABANDONED_OUTSIDE_CONTROLLER` で閉じ、terminal の欄はそのまま残す)。E4b の前には
  §7 の (1) で 0 件にする。
- 欄を退避する案 (roll forward の前に DETACHED の card の欄を `previous_*` に移す) は捨てた: roll forward の前に走るのは旧コード
  (§9.4 の切り替わりの時点) で、旧コードにはその処理が無い。roll forward の後に新コードで退避しても、`attempt_view` が同じ判定を
  欄を動かさずに出せるので、正本の書き換えが増えるだけ。
- pending の card に残った active な欄 (DETACHED (b)) は、次の reserve の手順 0 (§1.4) が Y を書く**前に** card 上で
  `failed` / `ABANDONED_OUTSIDE_CONTROLLER` にし、record X を合わせ、`reported:stale_execution_status` を残す (正本の task status が
  「誰も持っていない」と言っているので推測ではない)。

### 9.5 往復 (新 → rollback → roll forward) と各書き込み点の crash (t024 の族の掃除)

t023 と t024 で P1 が続けて「正本に無い値」「回復の範囲が最新 ID だけ」「rollback / roll forward の往復」から出た。往復と crash の
組み合わせを全部並べ、各行で**正本 (card) から判定・回復できる**ことを示す。表に書けない行は末尾に列挙する (実装 task の card へ)。

**前提 (表の各行が使う 3 つの事実)**:

- **F1** 旧コード (d887acf) は execution の 6 欄と record を書かない・読まない・消さない。変えるのは task の status / worker /
  `started_at` と identity / 枠。旧コードの `started_at` の書き手は pull (新しい値) と reset / retire (null) だけ (§9.3)。
- **F2** 新コードの判断は `attempt_view` (card だけ)。record は R-5 が「card の current」だけを card に合わせる。identity は R-1 が card
  から作り直す (ACTIVE のときだけ ID を入れる)。どちらも名指しの card に、コマンドの本体の前に走る。旧コードの回復 (R-1 / R-2) は
  R-5 を持たないので、rollback 中は record が遅れたまま残る — record は判断に使わないので結論は変わらない。
- **F3** current を進めるのは新コードの reserve だけで、進める前に前の current を card 上で terminal にし record を合わせる
  (§1.4 の手順 0)。だから「current でない record は terminal で card に合っている」が全経路で成り立つ。
  旧コードは current を進めない (F1) ので、往復を何回挟んでもこの不変条件は崩れない。

**旧コードが card にしうること** (F1 から全部): (i) 触らない (ii) holding のまま `started_at` を変えない操作
(旧 `verify-result fail` は in_progress・worker・`started_at` を残す・ready-for-verification・verifying) (iii) 非 holding にする
(旧 done / fail / needs-director / verify-result pass / retire needs-director / `update --status X`) (iv) reset / retire reset
(pending・`started_at` null) (v) (iv) の後の旧 pull (in_progress・新しい `started_at`)。roll forward 後の `attempt_view` は、
新コードが残した欄 (下の表の「card」列) と (i)〜(v) で次のとおり機械的に決まる:

| 欄 \ 旧コードの操作 | (i) | (ii) | (iii) | (iv) | (v) |
|---|---|---|---|---|---|
| ACTIVE の X | ACTIVE (同じ持ち主) | ACTIVE (旧来の「同じ試行のやり直し」。§9.4 の E3 行) | DETACHED (b) | DETACHED (b) | DETACHED (a) |
| TERMINAL の X | TERMINAL | — (holding でない task には旧コードも (ii) を打てない。Director が開いた card は TERMINAL のまま) | TERMINAL | TERMINAL (`started_at` null。新コードの reset の後と同じ) | **DETACHED (a)** (Codex P1 の形) |
| NONE | NONE | NONE | NONE | NONE | NONE |

**操作 × 書き込み点**:

| # | 操作: 落ちた点 | card (正本) に残るもの | 新コードのまま次のロック | rollback を挟んだ後 → roll forward 後の判定 |
|---|---|---|---|---|
| P0a | pull 手順 0: card (X `ABANDONED_OUTSIDE_CONTROLLER`) の後・record X の前 | pending・X terminal | R-5 が record X。次の pull が Y を発行 (X は terminal なので手順 0 なし) | (i)/(iv): TERMINAL(X)・R-5 が record X / (v): DETACHED (a)・R-5 が record X (X はまだ current)。X の再送は NOT_CURRENT |
| P0b | pull 手順 0: record X の後・card (Y) の前 | 同上 (record も一致) | 同上 | 同上 |
| P1 | reserve: card (Y reserved) の後・record Y の前 | in_progress・A・Y reserved・`execution_reserved_at` = `started_at` | R-5 が record Y (agent = A)・R-1 が identity (Y)・A の再 pull が再開 (§6) | (i): ACTIVE(Y)・旧 R-1 が作った ID なしの identity は §2.2 末尾で読み、次の R-1 が ID を入れる / (iv): DETACHED (b) → 次の reserve の手順 0 が Y を閉じる。record Y はそこで作られ、worker が null なので agent = null + `reported:execution_record_owner_unknown` (**履歴の欠け。表外 1**) / (v): DETACHED (a) |
| P2 | reserve: record Y の後・identity / 枠の前 | 同上 | R-1 / R-2 (01a のまま) | P1 と同じ (record Y は agent = A で既にある) |
| P3 | ロック外 (準備中) で死ぬ | 同上 | 準備ロックは kernel が外す。A の再 pull が再開 (§6.1) | (i): ACTIVE(Y) → 再開 (旧コードの間の A の再 pull は旧 `--task` で exit 1・何も書かない) / (iv)(v): P1 と同じ |
| P4 | start: card (running) の後・record の前 | in_progress・Y running | R-5 が status。再開しない (running)・Director の reset | (i): ACTIVE(Y)。持ち主は JSON を受け取っていない (同じプロセスの数行) ので Director の reset / (iv): DETACHED (b) → 手順 0 / (v): DETACHED (a) |
| P5 | start: record の後・JSON の前 | 同上 | 同上 | 同上 |
| P6 | G1: card (needs_director・Y `WORKSPACE_CREATE_FAILED`) の後・record / 枠撤去の前 | needs_director・Y terminal | R-5・R-2 | 旧 R-2 が枠を撤去・record は roll forward 後に名指しされたとき R-5 / TERMINAL(Y)。(iv)(v) は TERMINAL / DETACHED (a) |
| D1 | done: card (done・Y `DONE`) の後・record の前 | done・Y terminal・`started_at` は Y の値 | R-5・R-2。Y の done の再送は冪等 (exit 0) | (i)(iii): TERMINAL・再送は冪等 / (iv) (旧 Director の `update --status pending --reset`): TERMINAL・再送は冪等 (新コードの reset の後と同じ答え。§4.4) / **(v): DETACHED (a)・再送は NOT_CURRENT** (Codex P1 の行) |
| D2 | done: record の後・枠撤去の前 | 同上 | R-2 | 同上 |
| FL1 | fail / needs-director: card (Y `WORKER_FAILED` / `NEEDS_DIRECTOR`) の後・record の前 | failed / needs_director・Y terminal | R-5・R-2。同じ操作の再送は冪等 | D1 と同じ形 (`--reset` で pending → (v) で DETACHED (a)) |
| V1 | verify-result fail: card (pending・worker / `started_at` null・Y `VERIFICATION_REJECTED`) の後・record の前 | pending・Y terminal | R-5・R-2。verifier の再送は冪等。次の pull が Z (手順 0 なし) | (i): TERMINAL・再送は冪等 / (v): DETACHED (a)・verifier の再送は NOT_CURRENT、旧 pull の持ち主は「試行なし」 |
| U1 | update --reset: card (pending・null・Y `RESET_BY_DIRECTOR` / `ABANDONED_OUTSIDE_CONTROLLER`) の後・record の前 | pending・Y terminal | R-5・R-2 | V1 と同じ形 |
| T1 | retire: card (pending / needs_director・null・Y `RETIRED`) の後・record の前 (watchdog が progress を書く前) | 同上 | R-5・R-2。watchdog の `--execution Y` の打ち直しは冪等 (§4.4 の RETIRED 行) | (i): 旧 watchdog の `--started-at` の打ち直しは card の `started_at` (null) と合わず今と同じ拒否 → 今と同じ `_settle_terminated` の扱い (§9.4 の E4a 行。旧コードの挙動そのもの) / roll forward 後: TERMINAL・新 watchdog の打ち直しは冪等 |
| A1 | crash なし: 新コードの ACTIVE(Y) のまま rollback し、持ち主 A が旧 done / fail を打つ | done / failed・Y running のまま (旧コードは欄を触らない) | — | (iii): DETACHED (b)。Y の再送は NOT_CURRENT。task は正しく done / failed。**record Y と欄は running のまま残る (表外 2)** |
| A2 | crash なし: 新コードの ACTIVE(Y) のまま rollback し、旧 `verify-result fail` | in_progress・A・Y running | — | (ii): ACTIVE(Y)。A の新 done (Y) が通る (旧来の同じ試行のやり直し) |
| R1 | 往復を 2 回以上 | F1・F3 により欄は最後の新コードの書き込みのまま、`started_at` は最後の書き手の値 | — | 上の (i)〜(v) の表を最後の旧コードの操作で引くだけ (`attempt_view` は保存しない値なので、往復の回数に依らない) |
| R2 | TERMINAL の X に旧コードが (iv) → (v) → (iv) (取り直した B をさらに reset)、または roll forward 後に新コードが DETACHED (a) の card を `update --reset` | pending・`started_at` null・X terminal | — | TERMINAL(X)。X の再送は §4.4 の表 (DONE なら冪等) で、**今その card を持つ者はいない**ので「別の作業が in_progress なのに成功」(Codex P1 の害) にはならない。答えは新コードの「done の後に reset」と同じ (§4.4「reset の後に status を変えても答えは変わらない」)。B がいたことは card から消えている (表外 6) |

**表に書けない行 (実装 task の card に送る項目。Director が転記する)**:

1. **(E2 / E1) rollback 中の P1 + 旧 reset**: record Y の `agent` が null で作られる (旧コードは R-5 を持たず、record を作る前に
   worker が null になる)。状態の判断には影響しない (record は判断に使わない) が履歴が欠ける。`reported:execution_record_owner_unknown`
   で報告するだけにするか、card に `execution_agent` (予約した agent の写し。`execution_reserved_at` と同じ扱い) を足すかを E1 で決める。
2. **(E1 / E4a) A1: task が終端 (done / failed / verified / skipped) のまま DETACHED (b) の欄が残る**。手順 0 は reserve と reset でしか
   走らず、終端の task は再 pull されないので、record と欄は running のまま、`execution_active_on_non_holding_status` の報告が消えない。
   状態の判断には影響しない (照合は NOT_CURRENT、task の status は正しい)。Director の閉じる手段 (例: `update --close-execution`) を
   作るか、終端の task では報告しない (報告の条件を holding 以外の非終端だけにする) かを決める。
3. **(E1) 名指しされない card の record**: R-5 は名指しの card にしか走らないので、roll forward の後に一度も触られない card
   (旧コードが done にした等) の record は遅れたまま。store-check の `reported:execution_record_stale` (current の record が card と
   食い違う) で数えるだけにし、直すのは次に名指ししたコマンド。履歴だけの欠けで、F2 により判断は変わらない。
4. **(E4a) 旧 watchdog の打ち直し (T1 の (i))**: 旧コードの挙動のまま (今の rc の扱い) で、新しい答えは作らない。E4a の QA (t017) で
   「新 retire の後に旧 watchdog が `--started-at` で打ち直す」を 1 件通し、今と同じ拒否で後始末が壊れないことを確かめる。
5. **(E2) 準備ロックの導入前の版との並行**: 旧コードの pull と新コードの再開が同じ task で同時に走る形 (rollback / roll forward の
   瞬間) は、旧コードは準備ロックを取らないので §6.1 の直列化が効かない。旧コードの pull は pending からしか予約しない (in_progress は
   `--task` で exit 1) ので、新コードの reserved の試行の worktree に旧 pull が触れる経路は無いはずだが、E2 の QA で「reserved の card に
   旧コードの `pull --task` を打つ → exit 1・worktree に触れない」を 1 件確かめる。
6. **(判断の記録。card には送らない) R2: 間に B がいた事実は card から消える**。旧コードの reset が `started_at` を null にした時点で
   (F1)、旧コードが B に取り直させた痕跡は正本に残らない。新コードだけの経路 (reset → 再 pull → reset) なら current が Y に進むので
   X の再送は NOT_CURRENT になり、答えが割れる。割れても害が無いことで受け入れる: どちらも card を持つ者がいない (pending) ので
   書き込みは起きず、X の再送への「成功」は X についての事実 (X は DONE で終わった) で、今の task の完了を意味しない (task は pending と
   出ている)。痕跡を残すには旧コードの書き込みが要り、旧コードは変えられない。新コードの `update --reset` が DETACHED (a) を消す
   ときに印を残す案 (`execution_reserved_at` を空にして「封じた」と読む) は、旧コードの (iv) で同じ印が残らないので、答えを
   揃えられず、欄の組の整合 (片方だけの欄は `STATE_INVALID`) に例外を足すだけなので採らない。

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
| **E1** | lib_state_store: #3 (`publish_assignment` に `execution_id`)・#4 (`classify_assignment`)・#5 (R-1)・R-5 の新設・#14 (監査の `execution_id` / `caller_check` / `refused:`)・`_SAFE_RESULT_RE` (:117)・`TASK_META_KEY_ORDER` (:498-504)・`diagnose` (store-check: `legacy_execution` / `execution_detached` / `execution_active_on_non_holding_status` / `execution_record_unreadable` / `execution_record_stale` / `execution_record_superseded_active`)。`lib_task_controller.attempt_view` (§1.2。照合・冪等・R-1・reserve の手順 0・reset・store-check が呼ぶ唯一の読み方) と終了コード `ABANDONED_OUTSIDE_CONTROLLER`・card の `execution_reserved_at`。lib_task_status: `ACCEPTS_FROM` の狭め (**データだけ**。E1 では plan.sh がまだ読むので、狭めた表は別名で置き、E3 で差し替える)。新 `lib_task_controller.py`。lint_plan: 片方だけの欄・`execution_status` と `execution_end_code` の組が §1.2 の整合に外れるものを FAIL。R-5 は追従する欄 (status / end_code) だけを合わせ、不変の欄を上書きしない (§1.3・§1.4) | 既定値で 01a S3 の互換 golden がバイト一致 / `executions/` を読む・数えるコードが他に無い (`grep -rn "tasks/" scripts/*.py scripts/plan.sh` で列挙を洗う) / 構造ガード (queue 書き込みは lib を通る) に record の書き込みが入る / 監査の門のテストに `_safe_execution_id` |
| **E2** | plan.sh: #2 (:3538-3549)・#7 (G1 の CAS :3204-3241・:3281・:3674)・`.crewvia-env` (:3653-3663)・JSON (:3552-3561・:3676-3679)・`--task` の in_progress 分岐 (:3360-3369)・候補選びの前の再開 (:3316-3331 の回復の後)・`_slugify` (:3607-3616) と `task_slug` の固定・reserve の手順 0 (§1.4)・準備ロック (§6.1。lib_state_store.py:1609-1616 のロック順序の docstring に追記)。hooks/pre-tool-use.sh:205-207 のコメント。agents/worker.md:297-307 (`CREWVIA_EXECUTION_ID` の export)。kai-review.sh:226-237 (JSON を捨てない準備) | 既存の pull の互換 (COMPAT-01): skill / priority / blocked_by / target_dir / Taskvia disabled / git offline (W6) / 監査行 / 並行 pull で予約 1 件 / pull を reserve〜start の各点で殺して再 pull |
| **E3** | plan.sh: cmd_done (:4331-4340・:4489-4497)・cmd_fail (:4601-4602・:4649-4657)・transition_to_needs_director (:3979-4008)・cmd_needs_director (:4047-4051)・cmd_ready_for_verification (:4934-4938)・cmd_verifying (:4988-4991)・cmd_verify_result (:5076-5108)・#16 `_env_mission_for_task` (:3014-3033)・`audit_actor` (:1876-1879)・`accepts(` / `refuse_transition(` の 17 件。kai-review.sh (:165・:712・:234)。verifier-dispatcher.sh (:258-260・:403-408)。文書: worker.md (done / fail / needs-director の手順)・director.md:205-207・verifier.md:59-67・skills/crewvia-qa/SKILL.md:179-238・skills/crewvia-plan-review/SKILL.md・`plan.sh status` の表示 | §4.3 の全行 (狭めた拒否と、狭めない行が今どおり通る)・§5.2 の全行・§4.4 の冪等と conflict・Director の手順 (§5.3) が全部通る・kai-review の 17 か所の needs-director |
| **E4** | plan.sh: cmd_update --reset (:5945-5953・:6053-6064)・cmd_retire (:6182-6338)・cmd_reap_orphan_assignment (:6341-6439)。lib_retirement: #9〜#13 (:550-592・:605-660・:766-828・:1025-1066・:1494-1610・:1917-1980・:2037-2071)。watchdog.py の `DAEMON_RESTART_FILES` / `files_digest` (state-store.md §10.3 の 10)。dispatcher.sh:777-786 (`AGENT_NAME`)。director.md:1138-1143・:1485-1497・:1511。ここまでが **E4a** (producer の切り替えを含む。世代の照合は残す)。**E4b**: 世代の照合を外す: #4・#8・#11・#12 と旧形式 marker の読み口 | 退役の全経路で新しい試行を殺さない (同名の後任が再 pull した後の退役・旧 marker・reserved の試行の退役)・§7 の 0 件の確認手順・`assignment_execution_verdict` の 4 つの答え (SAME / OTHER / ABSENT / UNREADABLE) が ID でも同じ向きに倒れる |

---

## 12. テスト観点 (QA task への申し送り)

- **E1 (t005)**: 並行 reserve (実 flock・独立プロセス) で active が 1 件・attempt の重複なし / reserve・start・terminal の各点で crash 注入 →
  次のロック取得で R-5 が §1.4 の表どおり / 遷移表の全行 (原案 §10.5 の 14 項目) / 同じ terminal の再送が冪等・違う terminal が conflict /
  ID 生成器の注入 / 呼び出し元ゼロ (`grep`) / terminal の card の後・record の前で落とし、同じ ID の再送の答えが §4.4 の表どおり
  (`WORKER_FAILED` と `RESET_BY_DIRECTOR` で違う。reset の後に `update --status X` しても同じ) / 正しく保存済みの record がある card を
  reset・`update --worker B` した後の回復で、record の agent / reserved_at が変わらない / 欠陥版 (R-5 を消す・照合を `started_at` だけにする・R-5 に agent の比較を戻す・再送判定を record から読む) で赤 /
  **`attempt_view` の (i)〜(v) × 欄の表 (§9.5) の全セル** を、旧コード (d887acf) の plan.sh を実際に打って作った card で確かめる
  (旧コードの操作を手で模した fixture にしない) / X completed → 旧 `update --reset` → 旧 pull → 新コードの X の done の再送が
  `EXECUTION_NOT_CURRENT` (Codex P1。欠陥版「冪等を DETACHED でも引く」で赤) / 手順 0 の card の後・record X の後・card (Y) の後で
  crash 注入し、どの点でも store-check に `execution_record_superseded_active` が出ない (Codex P2-2。欠陥版「Y を書いてから X を凍結」で赤)
- **E2 (t009)**: COMPAT-01 / 原案 §10.6 の全項目を固定 fixture で前後比較 / 並行 pull / pull を reserve の後・worktree の後・env の後・
  start の後で殺し、同じ Worker の再 pull が同じ ID を返す (reserved) / 返さない (running) / 進行中の legacy card の done・retire が今どおり /
  Kai の pull (JSON を捨てる今の kai-review でも壊れない) / G1 の W4 で `failed WORKSPACE_CREATE_FAILED` + needs_director /
  **§6.1 の E2 のテスト項目 1〜6** (同じ予約の並行 pull) / §9.5 の P0a〜P6 の各点で crash 注入 → 新コードのまま・旧コードを挟んで
  (隔離 queue で d887acf の plan.sh を打つ) の両方で表どおり / §9.5 の表外 5 (旧 `pull --task` が reserved の card で exit 1)
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

---

## 14. E1 の実績と、§9.1「表に書けない行」への決定 (t004)

呼び出し元ゼロの lib を入れた。本番の挙動は変わらない (plan.sh・dispatcher・hooks は `lib_task_controller` を import しない。
`tests/test_task_controller_has_no_callers_yet.py` が名前の出現で固定し、E2 が最初の呼び出し元を足すときに許可表を意図して広げる)。

- 置き場: `scripts/lib_execution.py` (card 1 枚から決まること: ID の形・欄・`attempt_view`・照合・record の形)、
  `scripts/lib_task_controller.py` (reserve / start / complete / fail / release / reset / mark / abandon_detached / get。書き込みは
  すべて渡された `lib_state_store.Txn` の中)。書き込みの lib が controller を import すると循環するので、遷移から独立な部分は
  `lib_execution` に置いた。`lib_state_store` は `lib_execution` だけを import する。
- 検証: 単体 (原案 §10.5 の 14 項目 + 表) `tests/test_task_controller_unit.py` / 独立プロセスの並行 reserve
  (2〜4 プロセス × 20 回) `tests/test_task_controller_concurrency.py` / 全書き込み点で SIGKILL する crash 注入 (13 場面 × 点 × 20 回)
  `tests/test_task_controller_crash_injection.py`。

**決定 (§9.1 の「表に書けない行」の 1〜3)**:

1. **rollback 中の P1 の後に record Y の `agent` が null になる件 → card に `execution_agent` を足す** (予約した agent の写し。
   `execution_reserved_at` と同じ扱いで、次の reserve まで変わらない。`worker` は reset / retire / `update --worker` で変わるので使わない)。
   record の `agent` は card から再生成でき (R-5)、旧コードが worker を null にしても履歴が欠けない。agent なしの予約 (`agent=None`) と
   旧コードの跡で欄が無い card だけ record の `agent` が null になり、`reported:execution_record_owner_unknown` で報告する
   (数えるだけ。判断には使わない)。欄は任意で、無い card は不整合にしない (`fields_problem` は型だけ見る)。
2. **終端の task に DETACHED (b) の欄が残る件 → Director 用の閉じる手段を作る**。`abandon_detached_execution` (card の試行を
   `failed` / `ABANDONED_OUTSIDE_CONTROLLER` で閉じる。task の status には触れない。閉じるものが無ければ `INVALID_TRANSITION`)。
   報告は終端の task を別コード `reported:execution_active_on_finished_task` に分け、E4b の gate (0 件の確認) に混ぜない。holding 以外の
   非終端 (pending 等) は従来どおり `reported:execution_active_on_non_holding_status` (次の reserve の手順 0 か abandon が閉じる)。
   CLI の口 (`plan.sh update --close-execution` 等) は呼び出し側を移す PR (E3 / E4a) で足す。
3. **名指しされない card の record の遅れ → 数えるだけ**。回復 (apply) は書かず、`diagnose` が `reported:execution_record_stale` で
   数える。判断 (冪等の答え) は card から返り、record を使わない。`test_a_record_left_behind_for_an_unnamed_card_is_only_counted_…` が固定。

**戻し方 (E1)**: PR を revert し、`scripts/sync-main-checkout.sh` で主 checkout を ff する。呼び出し元ゼロなので本番の queue・registry・
デーモンには何も書かれておらず、データの戻しは要らない。lib_state_store の拡張は既定値 (`execution_id` を渡さない) で今とバイトが同じ
(01a S3 の互換 golden が変わっていないことで確かめる)。

### 14.2 Codex 1 巡目 (PR #270 / t029): 「書いてから失敗する」族と「崩れた record で落ちる」族

- **P2-2 / 族 1**: `reserve_task` は候補の ID の検証 (形・現在の試行との衝突・既存 record との衝突) を、手順 0 の `_abandon` より**前**に終える。
  `_finish` の `abandon_detached` 経路も、task の遷移の検査を `_abandon` より前に置いた (今の表では RESET に command が無く拒否されないが、
  表が変わっても「書いてから拒否」にならない)。拒否の**監査行**だけは書いてよい (§8)。それ以外 (card・record・枠・identity) は
  `tests/test_task_controller_malformed_inputs.py::test_every_refusal_writes_nothing_but_the_audit_row` が全拒否コード (27 場面) で sha256 の一致を見る。

  | 操作 | 最初の書き込み | それより前に終わる検証 |
  |---|---|---|
  | reserve | (手順 0 の) `_abandon` か card | agent・now・status・枠の持ち主・**候補 ID** |
  | start | card | git_context・ID の形・NOT_FOUND / NOT_CURRENT / terminal |
  | complete / fail / release / reset | `_abandon` か card | 引数・照合・meta_updates・試行の遷移・task の遷移 |
  | abandon_detached | card | DETACHED かつ active |
  | mark | card | 引数・照合・task の遷移・meta_updates |

- **P2-1 / 族 2**: record の形の検査は `lib_execution.record_shape_problem` の 1 か所 (必須: execution_id / mission / task / reserved_at (空でない文字列)・
  attempt (bool でない int)・status (既知の語)・git (dict)。任意: agent / end_code / running_at / ended_at は None か文字列)。
  `record_problem` (照合・`_write_record`・`_execution_of`) / `get_execution` / diagnose が同じ関数を通る。崩れた record は
  **card から導く** (`record_from_card`。判断に使わない) か、書き込みでは**上書きせず**報告に回す (`reported:execution_record_identity_mismatch`)。
  任意欄の**欠け**は健全 (`record.get`)。
- **P2-3**: diagnose の record 走査は、集合判定 (`status in ACTIVE_STATUSES`) の前に `record_shape_problem` を通し、崩れた record は
  `reported:execution_record_malformed` (detail は固定コード。値は出さない) にして次の record へ進む (01a S2 の `status: []` と同じ族)。

| 経路 | 欄が欠ける | 型が違う (`[]` / `{}` / int) | 空 | 結果 |
|---|---|---|---|---|
| start / complete / fail / 冪等の再送 (`_execution_of`) | card から導く | 同左 | 同左 | 例外なし・record は書き換えない |
| `_write_record` | 上書きしない | 同左 | 同左 | 報告だけ (R-5 が `identity_mismatch`) |
| `get_execution` | `STATE_INVALID` | 同左 | 同左 | 例外で落ちない |
| diagnose | `reported:execution_record_malformed` | 同左 | 同左 | 他の task の検査を続ける |
| recover (R-5) | 上書きしない・報告 | 同左 | 同左 | 例外なし |


---

## 15. E2 の実績 (t008): pull が Controller の最初の呼び出し元になった

**これは cutover** (本番の挙動が変わる。merge 前にユーザー承認 = t011)。`plan.sh pull` だけが `lib_task_controller` を呼ぶ
(`reserve_task` / `start_execution` / `fail_execution`。`tests/test_task_controller_has_no_callers_yet.py` が集合を固定)。
dispatcher の busy / idle 判定 (`queue/assignments/<agent>` の有無・本文 `<slug>:<tid>`) は変えていない (COMPAT-02)。

### 15.1 pull の形 (§6 のとおり。実装した順序)

```text
ロック 1: recover → (--task なしなら) 自分の予約済みの card を探す → 候補選び → reserve_task
          または 再開 (_resume_reserved: 枠を card に合わせるだけ。attempt は増やさない)
準備ロック: acquire_prepare_lock (LOCK_EX|LOCK_NB。取れなければ exit 1・何も書かない) → card を読み直し (_pull_cas_ok)
ロック外: Taskvia → worktree (W0〜W7) → .crewvia-env (4 行目に CREWVIA_EXECUTION_ID) → git rev-parse HEAD   [準備ロックを持ったまま]
ロック 2: recover → _pull_cas_ok → start_execution (git の文脈つき)。外れたら何も書かず exit 1 (JSON なし)
          worktree / env の失敗なら _pull_cas_ok → fail_execution(WORKSPACE_CREATE_FAILED) + needs_director (G1)
準備ロックを外す → JSON (今までの欄 + execution_id + attempt)
```

- **監査行**: reserve の行 (`op=pull`・`pending → in_progress`・`execution_id`) と start の行 (`in_progress → in_progress`・
  `caller_check=verified`) の 2 行 (今までは 1 行)。再開は枠を公開し直したときだけ 1 行 (何も書かないときは 0 行)。
- **exit code**: pull の domain error は `EXECUTION_NOT_FOUND` / `NOT_CURRENT` / `ALREADY_TERMINAL` だけ 3、他は 1。**2 にしない** (idle)。
  stderr の最後の行 `[plan.sh] error_code=<CODE>` (`TASK_ALREADY_RESERVED` / `STATE_INVALID` 等)。`--task` の既存の「already in_progress」文言も
  この行を付けた (文言は変えていない)。
- **`.crewvia-env`**: 今までの 3 行 + 4 行目 `export CREWVIA_EXECUTION_ID=ex-…` (**必須にしない**。読む側は無くても動く。名乗り = 照合の入力で、
  card には書かない)。`PR_BASE` は出さない (git-policy.md §5 のまま)。
- **record の `git`**: branch (Resolver) / pr_base (Resolver) / worktree / `head_at_start` (`git rev-parse HEAD`)。base は観測 (`git show-ref`) が
  要るので記録しない (判断に使わない欄。None)。`target_dir` の task は worktree が無いので git の文脈なし。
- **`task_slug`**: 最初の reserve が card に固定し、再開・以後の reserve はその値を使う。plan.sh の `_slugify` のコピーは消した
  (式の置き場は `lib_execution.slugify_title` の 1 か所。E1 の単体テストは「plan.sh に式が無い」ことを固定する)。
- **O2**: `lib_task_controller._check_generation` は世代の形に加えて**時刻として読めること** (watchdog の `parse_iso_epoch` と同じ読み方)
  を要求する。`'yesterday'` は世代の形に合うが idle 時計の起点にならない。pull は今までどおり `now_generation()` (µs 精度の UTC)。

### 15.2 決定

1. **O1 (E1 QA): 「reserve / 再開が自分の古い枠を上書きする」を採る。R-1 に「execution_id の食い違う identity を作り直す」は足さない。**
   - 状態: card は新しい Y・枠 (assignment + identity) は同じ task の前の試行 X のまま。R-1 は枠が**無い**ときだけ作り直すので
     `reported:generation_mismatch` が出続け、reserve の再送は `TASK_ALREADY_RESERVED` (E1 QA が 240/240 で再現)。
   - 採った規則: 再開 (`_resume_reserved`。同じ Worker の再 pull) が、**card の持ち主 (自分) の試行 Y で枠を公開し直す**
     (`classify_assignment` が ABSENT / SUCCESSOR のとき `publish_assignment(.., execution_id=Y)`)。別の task を指す枠・読めない枠は上書きしない
     (`agent_busy_elsewhere` と同じ exit 3)。**card が正本・枠は projection** なので、持ち主が card から公開し直すのは回復の向きと同じ。
   - 捨てた案 (R-1 の拡張): R-1 は名指しの card に対して**本体より前**に走る回復で、他の Worker の取り直しの後でも走る。食い違う identity を
     「card の試行で作り直す」と、別の Worker が後任として公開した枠を巻き戻しうる (R-1 は所有の証拠 = card の worker が自分、を読むが、
     reserved の窓では所有が移った直後と区別できない)。再開は**持ち主自身**が自分の再 pull で打つので、この曖昧さが無い。
   - 補足: plan.sh の pull は reserve の**前**に回復 (R-2) が走り、pending の card を指す孤児の古い枠を消す。だから O1 の状態は
     「reserve の card の後に古い枠が現れる」とき (rollback 中の旧書き手・Controller を直接呼ぶ側) にだけできる。テストはその状態を
     card の後ろの全書き込み点で作って再 pull の収束を見る。
2. **CAS は新しい欄と今までの欄の AND (Codex 3 巡目 P1)**: `_pull_cas_ok` = `in_progress` ∧ worker 一致 ∧ `started_at` 一致 ∧
   `current_execution_id == X` ∧ `execution_status == reserved`。旧形式の `update --reset` は status / worker / started_at だけを動かし
   execution の欄を更新しない (E4 まで)。`attempt_view` の DETACHED (b) が「status が holding でない」ケースを拾うので、reset だけなら
   Controller の照合が先に拒否するが、**reset の後に Director が `update --status in_progress` で card を開き直す**と status は holding・
   欄は X / reserved のまま ACTIVE に戻る — このときは AND の今までの欄 (worker 空・`started_at` null) だけが拒否する (赤の実証 E05)。
   start (`_pull_start`) と G1 (`_pull_worktree_failed`) が同じ関数を通る (コピーしない)。
3. **準備ロック (§6.1)**: `lib_state_store.acquire_prepare_lock(queue_dir, slug, tid)` (`PrepareLock`)。`queue/missions/<slug>/executions/<tid>.prepare.lock`
   の flock (LOCK_EX|LOCK_NB)。**ロックの順序**: 準備ロック → `queue/.lock` → S5 の小さな共有ファイルのロック。`queue/.lock` を握ったまま
   取ろうとすると `NestedTransaction` (入れ子の構造ガード)。`diagnose` は `executions/` の `ex-<32hex>.json` だけを record として数えるので、
   `.prepare.lock` は走査に出ない。
4. **再開の対象 (`_is_reserved_by`)**: in_progress ∧ worker == 自分 ∧ 試行が ACTIVE ∧ `execution_status == reserved`。**`running` は再開しない**
   (JSON が渡った可能性)・DETACHED は再開しない・試行の欄が壊れた card は対象にしない。`--task` なしの pull は候補選びの**前**に探し、
   2 枚以上は exit 3 (どれか決められない)。target_dir が違う card は再開しない (今の候補選びと同じ比較)。
5. **進行中の legacy card (欄なし)**: 書き換えない。`pull --task` は今どおり exit 1「already in_progress」・`done` は今どおり通る
   (`test_a_legacy_in_progress_card_is_not_rewritten_and_pull_still_refuses_it`)。
6. **kai-review.sh**: 変更なし。`pull --task <id> --agent Kai-codex --skills codex-review [--mission]` は同じ `cmd_pull` を通り、JSON を捨てても
   card・record・枠は揃う (`test_kai_review_style_pull_discarding_the_json_goes_through_the_same_path`)。dispatcher の二重着弾は今どおり exit 1。
   ID を `done` / `needs-director` に渡す準備 (JSON を捨てない) は E3。
7. **Controller が今の pull より厳しかった 2 点を、今の pull に合わせた (既存テストが全 pytest で見つけた)**:
   - **孤児の枠の上書き**: 今の pull は、別の task を指す枠が**孤児** (手放し済み・pending・無い mission を指す) なら上書きする
     (`agent_busy_elsewhere` が判定。`tests/test_assignment_routing.py`)。E1 の `reserve_task` は別の task を指す枠を一律 `TASK_NOT_ELIGIBLE` にしていた
     (孤児かどうかは**他の card の status** で決まり、Controller は他の card を読まない)。**`reserve_task(foreign_slot_checked=True)`**
     (既定 False。呼び出し側が孤児と確かめ済みのときだけ) を足し、pull は `agent_busy_elsewhere` を通した後なので True を渡す。
     読めない枠・生きている別の task を指す枠は、`agent_busy_elsewhere` が先に exit 3 で拒否する (今と同じ)。
   - **worker 名の範囲**: E1 は agent 名を `_safe_token` (英数字・`_.-`) に絞っていたが、今の pull は枠のファイル名として使える名前 (非 ASCII の
     名前も) を受け付ける。空白・制御文字 (card の frontmatter・identity・record を壊しうる) だけを拒否する規則に緩めた
     (`_check_agent_arg`。名前の中身は監査ログに出ない: `actor` は門が `unknown` にする)。名前の使い回しは変えない。
   - この型 (**新しい lib が今のコードより厳しい**) の掃除: reserve の拒否条件を 1 つずつ今の pull と突き合わせた。status (`ACCEPTS_FROM_NARROWED['pull']` と
     `ACCEPTS_FROM['pull']` は同じ `{pending}`)・agent 名 (上)・枠 (上)・`now` の形 (pull は常に時刻形)・試行の欄の整合 (欄を持つ card だけ。本番の
     queue / archive に `task_slug` / `execution_*` を持つ card は 0 枚 — `grep -rlE '^(task_slug|execution_[a-z_]+|current_execution_id):' queue/missions queue/archive`)・
     mission slug / task id の形 (card の列挙が既に `tNNN` と slug を要る)。

### 15.3 検証

- 互換性: `tests/test_plan_sh_compat_s3.py` を、E2 が足した 4 つ (pull の JSON の 2 欄・card の試行の欄・`executions/`・identity の `execution_id`) を
  **取り除いた**出力で、cutover 前の golden と比べる (task の選択・exit code・stderr・assignment の本文・card の他の欄・他の subcommand の出力は
  1 バイトも変わらない。取り除く物が実際にあることも別のテストで固定)。`.crewvia-env` は `test_git_policy_pull_and_pr_base_cutover.py` が「前の 3 行 + 4 行目」を固定。
- `tests/test_pull_execution_e2.py` (45 件): ID の置き場の一致・並行 pull (2 形 × 20 回)・準備ロック (§6.1 の E2 項目 1〜4・6)・
  reserve〜start の各点の SIGKILL (段階 4 点 × 再 pull 2 形 + lib の書き込み点の全点)・CAS (旧形式の reset / Director の開き直し / 後任の取り直し)・
  O1・O2・legacy・secret。`tests/test_pull_execution_e2_rollback.py`: 旧コード (`505d16b`) の `pull --task` が reserved の card で exit 1・worktree に触れない
  (§9.5 の表外 5。git の履歴が無い浅い clone では skip)。
- 赤の実証: `tests/red_proof_e2_pull.py` (E01 冪等化・E02a/b/c ID の発行・E03 二重予約の拒否・E04 準備ロック・E05 CAS・E06 O1・E07 O2・E08 start)。
  赤は「狙ったテスト名の FAILED」だけ。

### 15.4 戻し方 (E2)

PR を revert し、`scripts/sync-main-checkout.sh` で主 checkout を ff する。**新しい欄・record・identity の欄・`.crewvia-env` の 4 行目は残ってよい**
(§9.3): 旧コードは card の試行の欄を `dump_yaml` で保持して読まず、`executions/` を読まず、identity の `started_at` だけを比べ、
次の pull が `.crewvia-env` を 3 行で書き直す。**reserved のまま残った試行** (pull の途中で死んだ後に revert した場合) は、旧コードが
`pull --task` を「already in_progress」で拒否する (exit 1・worktree に触れない) ので、Director が `plan.sh update <id> --status pending --reset` で戻す
(今までの「worker が落ちた」と同じ手順)。revert の後にもう一度進める (roll forward) とき、旧コードが取り直した card は `attempt_view` が DETACHED として
読む (§9.3・§9.5)。env の停止スイッチは付けていない (不変条件 5)。

### 15.5 E3 以降に送るもの

- 名乗り (`CREWVIA_EXECUTION_ID` / `--execution`) を使う照合は E3。pull の JSON の `execution_id` を shell 変数に取り出す手順 (特に `target_dir` の task は
  `.crewvia-env` が無い) は worker.md に E3 で書く。今の worker.md は `.crewvia-env` の export と再開の手順だけ。
- `update --reset` / retire が試行を release / fail にするのは E4a。それまで reset は試行を閉じず、次の reserve の手順 0 が閉じる (テストで固定)。

### 15.6 Codex 1 巡目 (t031 / PR #271): 準備ロックを子孫にも持たせる

- **P2**: 準備ロックは python の flock で、記述子は worktree を作る subprocess に渡っていなかった。作成中に python だけが SIGKILL されると、bash / git の子孫は
  生き残るのに kernel がロックを外し、再試行は同じ予約を再開して**別の helper を同時に走らせる** (作りかけの worktree を見るか `WORKSPACE_CREATE_FAILED`)。
  t008 の crash テストはプロセスグループごと kill するのでこの形を見逃していた。
- **修正**: `PrepareLock.fileno()` を `pass_fds` で helper と `git rev-parse` に渡す (flock は開いたファイル記述に付くので、子孫が持っている間は外れない)。
  捨てた案: 「回復を許す前に子孫が止まっていることを確かめる」 — 子孫を同定する証拠 (pid・起動時刻) を別に持つことになり、pid の使い回しの問題を足す。
  記述子の継承は kernel が子孫の生死と排他を結ぶので、証拠が要らない。
- **族ごとの掃除 (pull の中で排他を取った後に起動する subprocess)**:

| subprocess | 排他 | (a) 親だけ kill で排他の外に出るか | (b) 再試行と同時に走るか | 処置 |
|---|---|---|---|---|
| `bash -c "source git-helpers.sh && crewvia_create_worktree …"` (plan.sh `cmd_pull`) | 準備ロック | **出ていた** | **走っていた** | 直した (`pass_fds`)。20 回反復のテスト |
| helper の下の git (`fetch` / `worktree add` / `show-ref` 等) | 準備ロック | helper の子孫なので記述子を継承 → 出ない | 再試行は exit 1 | 同じ修正で足りる (bash / git は継承した記述子を閉じない) |
| `git -C <worktree> rev-parse HEAD` (`_pull_git_context`) | 準備ロック | 出ていた (短時間・読み取りだけ) | 走りうるが読み取りだけで害なし | 一貫のため `pass_fds` を渡した |
| Taskvia (`urllib.request.urlopen`。`taskvia_sync_pull`) | 準備ロック | 子プロセスを起こさない (python の中) → 親が死ねば止まる | n/a | 不処置 |
| ロック 1 / 2 (`queue/.lock`) の中 | `queue/.lock` | subprocess を起こさない (grep `subprocess\.` で pull の経路は上の 2 つだけ) | n/a | 不処置 |
| task-graph の再生成 (`maybe_refresh_task_graph`) | 自前のロック | python の中・コマンドの最後 | n/a | 不処置 |
| kai-review.sh の pull | (同じ `cmd_pull`) | 同上 | 同上 | 同じ経路。変更なし |
| `_resolve_head_commit` の git (`done` / `fail` の経路。pull ではない) | なし | n/a | n/a | 範囲外 |

- テスト: `tests/test_pull_execution_e2_parent_kill.py` (helper stub が `$PPID` = python の pid を残し、**その pid だけ**を SIGKILL。子孫が生きている間に再 pull を
  打ち、helper が同時に 2 本走らない・exit 1・card 不変、子孫が終わった後は同じ試行を再開、を 20 回)。赤の実証 E09 (`pass_fds` を外す)。

---

## 16. E3 の実績 (t012): 報告の 6 コマンドが Controller を通り、呼び出し元を Execution ID で照合する

**これは cutover** (本番の挙動が変わる。PR #273。merge 前にユーザー承認 = t015)。承認の対象として明示するもの: (1) §4.3 の**狭め** (done が pending / blocked / 検証待ちから通らない等)・
(2) **`verify-result fail` が新しい試行になる** (task は pending・worker を手放し、次の pull が attempt + 1)・(3) 名乗った ID が違えば **exit 3** で拒否・
(4) 遷移・照合の拒否が**監査ログに `refused:` の行**を残す・(5) done / fail / needs-director が **card の worker の枠**を撤去する (`AGENT_NAME` が無くても)。
前提の互換 (`--execution` を受け付けて読み捨てる) は別 PR **#272** (E3 より先に merge する。§16.4。#272 の CI は最初 `test_plan_result_file` の fail の option の表で赤だった — option を足す PR は、その option 集合を固定する既存テストを全 pytest で洗うこと)。

### 16.1 何が変わったか (plan.sh の 6 コマンド + `update --close-execution`)

| コマンド | 呼ぶ Controller の操作 | 試行 | task |
|---|---|---|---|
| `done` | `complete_execution(to_status=done)`。**先に `dry_run=True`** (照合・遷移の検査だけ。派生値 D1 / D2 を書く前) | running → completed `DONE` | in_progress → done・枠撤去 (D4)・D5 は今のまま |
| `fail` | `fail_execution(WORKER_FAILED)`。先に dry_run (証拠の検証より前) | running → failed `WORKER_FAILED` | in_progress / needs_director → failed |
| `needs-director` | `fail_execution(NEEDS_DIRECTOR)` | running → failed `NEEDS_DIRECTOR` | in_progress → needs_director (worker は残す) |
| `ready-for-verification` / `verifying` | `mark_task` | 変えない (running のまま・枠は残る) | in_progress → ready_for_verification / → verifying (`verifier` 欄を同じ書き込みで) |
| `verify-result pass` | `complete_execution(to_status=verified)` | running → completed `VERIFIED` | 検証待ち → verified (枠は残す。R-2 が後で消す) |
| `verify-result fail` (< max) | `fail_execution(VERIFICATION_REJECTED)` | running → failed `VERIFICATION_REJECTED` | 検証待ち → **pending**・worker / started_at を null・枠撤去。次の pull が attempt + 1 |
| `verify-result fail` (≥ max) / `needs_human_review` | `mark_task` (`needs_human_review`) | 変えない (running) | → needs_human_review |
| `update <id> --close-execution` (新) | `abandon_detached_execution` | DETACHED で active な試行 → failed `ABANDONED_OUTSIDE_CONTROLLER` | **触れない** (§16.2 の 7) |

- **名乗りの出どころ**は `--execution <id>` (明示) > env `CREWVIA_EXECUTION_ID` (§5.2)。`_execution_caller` (plan.sh) が**ロックを取る前**に決める。agent 名は照合の根拠にしない。
- **ID を名乗った同じ操作の再送は成功** (exit 0・何も書かない・stdout に `already completed (idempotent)` 等)。D0〜D5・Taskvia・registry の bump も走らせない。違う結果への変更は conflict (exit 3)。
- 拒否の終わり方: exit code は `lib_execution.EXIT_CODES` (遷移の拒否 2・照合の 3 つ 3・読めない card 1)、stderr の**最後の行**は `[plan.sh] error_code=<CODE>` (固定形式。テストで固定)。
  文言は固定の文 + 識別子だけ (card の中身・名乗られた値は出さない。secret を仕込んだテストで固定)。
- **kai-review.sh** は pull の JSON を捨てず `execution_id` を取り出して done / needs-director に渡す (JSON が読めない・欄が無い・`--skip-pull` は名乗らない)。**親 shell から継いだ `CREWVIA_EXECUTION_ID` は捨てる** (別 task の ID を名乗って自分の報告が拒否されない)。
- **verifier-dispatcher.sh** は card の今の試行 (active のときだけ) を `verifying` に `--execution` で渡し、verifier への指示文にも入れる。壊れた値・terminal の試行は名乗りなし。
- **`plan.sh status --mission`** の進行中の行に `[ex-… attempt N]` を出す (Director が `--execution` に渡す値。terminal の試行・legacy は出さない)。
- 文書: worker.md (pull の JSON から `EXECUTION_ID` を取る・`${EXECUTION_ID:+--execution "$EXECUTION_ID"}`・拒否されたら打ち直さない・再送は安全)・director.md (done に `--execution`・狭めの出口)・verifier.md・crewvia-qa / crewvia-plan-review の skill。
  **E5 まで名乗りなしは通る** (§5.2)。AC-04 は「違う ID では終わらせられない」までで、「名乗らなければ終わらせられる」穴は E5 まで残る (§10)。

### 16.2 決定

1. **照合・遷移の検査と書き込みを分ける (`dry_run`)**: done は D1 / D2 (pr_number の伝播) をコミット点の**前**に書く。検査が書き込みの後ろにあると、拒否された done が依存先に番号を書いたまま終わる。
   Controller の `complete_execution` / `fail_execution` に `dry_run=True` を足し、**同じ `_finish` が検査だけを行って判定 (`PROCEED` / `IDEMPOTENT` / `TASK_ONLY`) を返す** (検査を 2 か所に書かない)。
   done / fail は plan.sh が先に dry_run → 証拠の検証 (fail)・D0〜D2 (done) → 本番の呼び出し。status の拒否が証拠の検証より先、という今の順序も保つ。赤の実証 R07。
2. **狭めは `lib_task_status.ACCEPTS_FROM` を書き換える** (表の置き場は 1 か所): E1 が別名で置いた `ACCEPTS_FROM_NARROWED` は消した。Controller と plan.sh (`pull` / `retire`) が同じ表を読む。
   plan.sh の `accepts(` の呼び出しは移したコマンドから消えた (残るのは pull / retire。構造ガード `tests/test_task_status_single_definition.py` の設計の写し `REFUSED` を狭めた表に直した)。
3. **読めない card・語彙に無い status の card は、今までどおり exit 2 の拒否** (`_load_task_for_report`): Controller は card を厳密に読み `STATE_INVALID` (exit 1) にするが、今までの plan.sh は寛容な読み口で
   「この status の task には使えません」(exit 2) だった。**新しい lib が今のコードより厳しくならない**ように、Controller の前に plan.sh の読み口で今の答えを保つ (memory `new-lib-stricter-than-legacy-path-breaks-compat`)。
   試行の欄が壊れた card (E2 以降の card だけ) は新しい条件で `STATE_INVALID` (exit 1・何も書かない)。
4. **枠の撤去は card の worker の枠** (`txn.retire_assignment(owner, …, None)`)。今までは `AGENT_NAME` の枠だった。Director が (AGENT_NAME が Worker でなくても・無くても) Worker の task を終わらせると、
   Worker の枠が同じトランザクションで外れる (以前は次の回復 R-2 まで残り、Worker が busy に見えた)。**他の task を指す枠・後任の枠は外さない** (classify が ASSIGN_MINE のときだけ)。
   `AGENT_NAME` の枠の後始末 (`_retire_caller_slot`) は Controller の後に残した (card の worker ではない AGENT_NAME が持つ、**この task を指す**枠。今までと同じ警告つき)。
   この違いを前提にしていたテスト 4 本 (「AGENT_NAME 無しの done は枠を撤去しない」で孤児の枠を作っていた) は、孤児の枠を手で書き直す形に直した (`reap-orphan-assignment` の互換 golden も同じ場面を両方の版で作る)。
5. **監査**: 遷移・照合の拒否は `refused:<CODE>` の行 (card の ID・名乗られた ID は `presented=` で形が正しいときだけ。本体の `die` の後でも残る)。本体の行に `execution_id` / `caller_check`。
   **`actor` は `AGENT_NAME` を置き換えない** (§8 の (1) を縮めた): `AGENT_NAME` が無い (`unknown`) 呼び出しのときだけ**操作の前の card の worker** で補う (`Txn.record(actor_hint=)`)。
   Director が Worker の代わりに done を打った行を Worker の行にしない。Controller が `save_task` を通さず `record()` で行を予約するので、`with_lock` の「card を書いたトランザクションは card の行だけ」の判定に `Txn.has_body_record` を足した
   (足さないと mission の done で task を持たない余分な行が 1 本出る)。
6. **`verify-result fail` が新しい試行**になる点 (§9.2 の 3 は「承認で外せるよう commit を分ける」と提案していた): Controller 側の規則は E1 で既にある (`_FAIL_RULES` の `VERIFICATION_REJECTED`) ので、**変更は plan.sh の `cmd_verify_result` の 1 分岐 + テスト 2 本 (`test_verify_fail_below_the_limit_starts_a_new_attempt` と `test_verify_fail_at_the_limit_…`) + 文書**に局在する。
   commit は**分けていない** (`cmd_verify_result` 全体が Controller 経由に書き換わり、1 分岐だけの commit を切り出すと他の分岐が旧形式の書き込みに戻る)。ユーザーが承認で「今の in_progress のまま」を選んだ場合は、Controller に
   「試行を変えずに task を in_progress に戻す」遷移 (今は無い。`mark_task` の対象外) を足して plan.sh の分岐を差し替える別 commit にする。
   rework_count の上限 (`max_rework`) に達したときと `needs_human_review` の verdict は試行を閉じない (`mark_task`)。
7. **E2〜E3 の間にできた card の扱い (必須条件)**: E2 の間は done / needs-director / fail が試行を閉じないので、終わった task に `running` の試行が残る
   (`store-check` の `reported:execution_active_on_finished_task` / `execution_active_on_non_holding_status`)。**E3 の後に新しく出ない** (テスト `test_after_e3_a_finished_task_leaves_no_active_attempt_behind`)。
   すでにできた分は **`plan.sh update <id> --close-execution [--mission <slug>]`** で閉じる (§14.1 の決定 2 が「CLI の口は E3 / E4a で足す」としていたもの): DETACHED で active な試行を、**task に触れず** (status / worker / started_at / 枠は 1 バイトも変えない)
   `failed` / `ABANDONED_OUTSIDE_CONTROLLER` にする (card → record)。持ち主のいる (ACTIVE の) 試行は閉じない・他の更新オプションと併用できない・閉じるものが無ければ exit 2。報告から外す案は採らなかった
   (rollback 中の旧コードの跡 §9.5 の表外 2 も同じ手段で閉じられ、報告を残したまま Director が 1 件ずつ消せる)。本番では merge → sync の後に `store-check` の件数を見て、件数ぶん打つ (Director の作業。件数は merge 後に確認)。
8. **今の運用が通ることをテストで固定** (§5.3): cutover review task の `update --status in_progress --reset` → `done` (試行なしの経路・`caller_check=no_execution`)・kai-review の pull → done / needs-director・
   verifier (持ち主でない) の verify-result・target_dir の task (pull の JSON の ID)・Director が `--execution` を付けた done・Director が見た後の reset → 再 pull は exit 3。
9. **dispatcher の文面を 1 か所直した**: `[review-refused]` の通知は「手動差分レビューの結果を `plan.sh done`」と案内していたが、その task は pending のままで、E3 の狭めで done が拒否される。
   「`update --status in_progress --reset` で開いてから done」に直した (dispatcher は常駐なので sync の restart で反映)。族の掃除 (§16.5 の F5) で見つけた。
10. **done が `reserved` の試行を拒否する (新しい条件)**: 今までは in_progress なら done が通った。`reserved` (pull が start に進む前に落ちた・Worker は JSON を受け取っていない) の試行を完了にはできない (`INVALID_TRANSITION`)。
    出口: 同じ Worker の再 pull が再開する (§6)・Director は `update --status in_progress --reset` で開いてから done (試行なしの経路)。

### 16.3 互換性 (COMPAT-01): 違いは表にしたものだけ

固定 fixture (`tests/plan_sh_compat_scenario.py` の 39 段) を今の plan.sh で走らせ、cutover 前 (a1f6957) の golden と比べる `tests/test_plan_sh_compat_s3.py`。E3 で出た差は 2 段の stderr だけ (と、孤児の枠の場面を両方の版で作るための scenario の 1 行 — 下の表の最終行) で、**表の `have` と完全一致するときだけ golden に戻して比べる**
(`E3_EXPECTED_DIFFERENCES`。表に無い違いはそのまま赤)。queue の全ファイルはバイト一致 (E2 の足した欄・record・identity を取り除いた比較。E3 の終了コードの欄も試行の欄なので同じ正規化に入る)。

| 場面 | 今まで | E3 の後 |
|---|---|---|
| 遷移の拒否 (`done` が done の task へ等) の stderr | 1 行 `task 't002': done は status='done' の task には使えません (受け付けるのは: <広い列挙>)` | 同じ書き出し + 狭めた列挙 (`in_progress`) + **最後の行に `[plan.sh] error_code=INVALID_TRANSITION`**。exit 2・何も書かない |
| `done` が pending / blocked / 検証待ち / verification_failed から | 通る (exit 0) | **exit 2** (§4.3) |
| `fail` が pending / blocked / 検証待ち / verification_failed から | 通る | **exit 2** |
| `verify-result` が pending / in_progress / blocked / needs_director / verification_failed から | 通る | **exit 2** |
| `verify-result fail` (< max) | task は in_progress・worker 残す・stdout `→ in_progress` | **task は pending・worker / started_at null・枠撤去**・stdout `→ pending`・試行 failed |
| ID を名乗らない active な試行への報告 | 通る | **同じ** (stdout・exit も同じ。監査の `caller_check=unverified`) |
| `--execution <id>` の付いた報告 | exit 2 (unknown option) | 今の試行なら通る・違えば **exit 3** (`EXECUTION_NOT_CURRENT`)・形が違えば exit 3 (`EXECUTION_NOT_FOUND`)・空は exit 1 |
| 同じ ID の `done` の再送 | exit 2 | **exit 0** (idempotent。ID を名乗った呼び出しだけ) |
| AGENT_NAME が Worker でない (無い) done / fail / needs-director | Worker の枠が残る (次の回復まで) | **card の worker の枠が外れる** (§16.2 の 4) |
| 監査ログ | 拒否は行なし・`execution_id` は pull だけ | 遷移・照合の拒否は `refused:` の行・6 コマンドの行に `execution_id` / `caller_check` |
| 試行が `reserved` の task への done | 通る | exit 2 (§16.2 の 10) |

### 16.4 戻し方 (E3) — **#272 を先に merge する**

PR を revert し、`scripts/sync-main-checkout.sh` で主 checkout を ff する (dispatcher は文面を 1 か所変えたので restart される)。**新しい card の欄・record・identity の欄は残ってよい** (§9.3)。狭めを戻すと再び pending から done が通る
(制限を外す方向なので壊れない)。revert したあとの注意は 3 つ:

- **起動済みの Worker のプロンプト・走っている kai-review.sh は `--execution` を付けて報告し続ける**。戻し先の plan.sh が `unknown option` で拒否すると完了・失敗の報告が止まる (設計レビュー t002 の P2)。
  採った対策: **`--execution` を受け付けて読み捨てる互換を E3 の前に別 PR (#272・commit `e3-execution-flag-compat`) で入れた**。E3 の revert はその commit を巻き戻さないので、戻し先でも `--execution` 付きの報告が通る
  (`tests/test_plan_sh_execution_flag_rollback.py`: 互換 commit の plan.sh を git の履歴から取り出して、6 コマンドが `--execution` 付きで通ることを確かめる。履歴に無い浅い clone は skip)。**#272 が E3 より先に merge されていることを承認の前に確認する**
  (E3 の PR は #272 の commit を含む。#272 を後に merge すると、E3 を revert したときに互換も一緒に消える)。もう 1 つの案 (rollback 手順に「残っている呼び出し元の切り替え」を含める) は採らなかった: 起動済みの全セッションと走っている bash を 1 つずつ止める手順は漏れる。
- **E3 の間に verify-result fail で pending に戻った task**: revert 後の旧コードは pending の card を普通に pull できる (worker は null)。同じ試行 (VERIFICATION_REJECTED で terminal) の欄は残り、次の pull (旧コード) は欄を触らない →
  roll forward の後 `attempt_view` が §9.5 の (iv) → (v) として DETACHED / TERMINAL を card から読み分ける。Director がやることは無い。
- **E3 の間に閉じた試行が増える**: 旧コードは試行を閉じない。revert 後に旧コードが done した task は `running` の試行が残る (E2 の間と同じ形)。
  **`update --close-execution` は E3 (#273) で足したので、revert すると消える** (t034 / Codex P3)。revert の間は閉じる手段が無く、
  roll forward (#273 を戻して入れ直す) の**後**にだけ使える。revert の間は `running` の残りを数えるだけにして (`store-check`)、閉じるのは roll forward の後に回す。

### 16.5 族ごとの掃除 (直した型と同じ型を、同じデータ・同じ判定を扱うコードと文書について)

**F1 「指定されたか」を値の真偽で判定する (01b G3 の型)** — `--execution` / `CREWVIA_EXECUTION_ID` を読む / 渡す箇所を `grep -rn -- "--execution\|CREWVIA_EXECUTION_ID" scripts hooks agents skills crewvia` で数えた (テスト・knowledge を除く: 57 行 / 10 ファイル。文書 4・plan.sh・kai-review.sh・verifier-dispatcher.sh・lib_task_controller.py・scripts/CLAUDE.md)。

| 箇所 | 判定 | 処置 |
|---|---|---|
| plan.sh `_execution_caller` (flag) | `'--execution' in opts` (presence)。空・空白だけは exit 1 | 直した。赤の実証 R02 |
| plan.sh `_execution_caller` (env) | `os.environ.get(...) is not None`。空は exit 1 | 直した。赤の実証 R03 |
| kai-review.sh `PULL_EXECUTION_ID` | `[[ -n ... ]]` — **自分で pull の JSON から取り出した値**で、空は「JSON に欄が無い」の意味 (明示の指定ではない) | 不処置 (名乗らない側に倒すのが正。E3 は名乗りなしを拒否しない) |
| kai-review.sh 継いだ env | `unset CREWVIA_EXECUTION_ID` | 直した (継いだ別 task の ID を名乗らない) |
| verifier-dispatcher `card_execution_id` | `is not None` / 形の検査。壊れた値は None | 不処置 (card の値で、明示の指定ではない) |
| worker.md `${EXECUTION_ID:+--execution …}` | ID が取れていないとき `--execution` ごと外す (空を渡さない) | 文書で明記 |
| `--mission ""` (6 コマンドの `opts.get('--mission')` の真偽) | 空は省略と同じに倒れ、task id で mission を探す (曖昧なら拒否) | **不処置**: 識別の名乗りではなく所在の指定で、`$TASK_MISSION` が消えた Worker (Bash ツールは呼び出しごとに env が消える) の報告が今これで通っている。拒否に変えると出口を消す |
| plan.sh の他の `opts.get(...)` の真偽 (数えた 21 か所) | 21 のうち 6 コマンドに関わるのは `--mission` だけ。残りは add / update / pull / status / archive / resync / pr-base の任意の option (空が意味を持つもの `--worker ''`・`--blocked-by ''` を含む) | E3 の範囲外 (01b G3 が `pr-base` を直した族の残り。backlog) |

**F2 新しい lib が今のコードより厳しい** — Controller の拒否条件を 1 つずつ今の plan.sh と突き合わせた (memory `new-lib-stricter-than-legacy-path-breaks-compat`):

| Controller の拒否 | 今まで | 処置 |
|---|---|---|
| card が読めない・語彙に無い status (`STATE_INVALID`) | exit 2 (`status='cancelled'` の task には使えません) | **今の答えを保った** (`_load_task_for_report`。§16.2 の 3)。単体テスト `test_an_unknown_status_is_refused` |
| 試行の欄が壊れた card (`fields_problem`) | 欄を知らない (読まない) | 新しい条件 (E2 以降の card だけ・手編集した card)。exit 1・何も書かない。不処置 (E1 の malformed テストが固定) |
| `meta_updates` / `meta_remove` の形 (禁止キー・JSON 化) | 無い | plan.sh が渡す欄 (`completed_at` / `pr_number` / `no_pr_waiver` / `needs_director_reason` / `rework_count` / `verifier` / fail の証拠欄) に禁止キーは無い。不処置 |
| 遷移の表 (§4.3) | 広い表 | 狭めた (表にした差。§16.3) |
| `reserved` の試行への done | 通る | 新しい条件 (§16.2 の 10)。出口あり |
| 名乗った ID の照合 | 無い | 新しい条件 (名乗った呼び出しだけ) |
| 試行の `running` を要求 (complete) | 要求しない | 同じ (reserved のみ新しく拒否) |

**F3 `AGENT_NAME` を根拠に枠を撤去する** — `grep -n "retire_assignment(" scripts/plan.sh` は 6 行 (定義 2・`_retire_caller_slot` 1・`update --reset` 1・`retire` 1・`reap-orphan-assignment` 1)。
done / fail / needs-director / verify-result fail は Controller (card の worker) に移し、`_retire_caller_slot` は AGENT_NAME の枠の後始末として残した。`update --reset` / `retire` は **E4** (対象外・今のまま)。reap は変えない。

**F4 報告コマンドを打つ呼び出し元 (ID を渡す箇所)** — `grep -rlE "plan(\.sh)? (done|needs-director|fail|ready-for-verification|verify-result|verifying)"` で 20 ファイル:
worker.md (41 行: 全部の例を直した)・director.md (16: done の例に `--execution` と出口を書いた)・crewvia-qa (7)・crewvia-plan-review (17)・verifier.md (6) は文書を直した。
worker-codex.md (15) は Kai-codex の説明で、実際の呼び出しは kai-review.sh (直した)。verifier-dispatcher.sh (4・直した)・kai-review.sh (9・直した)・benchmark-ctx.sh (2: ID を名乗らず通る。不処置)・start.sh (3: kickoff の文面は pull だけ・コメント)・
hooks (8: コマンド文字列の例・コメント)・lint_plan.py / lib_review_refusal.py / cleanup-target-dir.sh / scripts/CLAUDE.md / lib_task_status.py (各 1〜2: コメント)・README (5: 概要。不処置)・dispatcher.sh (10: コメント 9 + **[review-refused] の案内 1** — 直した。F5)。

**F5 狭めた遷移の消費者 (狭めた status から done / fail / verify-result を勧めている箇所)** — dispatcher.sh の `[review-refused]` 通知 (task は pending のまま「`plan.sh done`」と案内 → 拒否される。**直した**)。
director.md の手順 (`update --status in_progress --reset` → done は今のまま通る)・plan.sh の needs_director への done の拒否文 (出口を出す。今のまま)・`verifier-dispatcher` が `ready_for_verification` だけを拾う (`accepts('verifying')`) — 他に勧めている箇所は無かった。

**F6 status の集合・表のコピー** — `tests/test_task_status_single_definition.py` (AST) が通る。Controller の `_MARK_TARGETS` は command → 単一の status リテラルの dict (集合ではない)。表の置き場は `lib_task_status.ACCEPTS_FROM` の 1 か所のまま。

### 16.6 検証

- `tests/test_execution_e3_caller_table.py` (113 件): 呼び出し元ごとの表 (§5.2 の各行 × 報告コマンド)・空の明示指定・形の違う名乗り・再送・conflict・Director が開いた card・取り直された後・DETACHED・狭めた遷移 (31 の組)・
  検証の流れ・`verify-result fail` が新しい試行・`--close-execution`・監査 (actor・拒否の行)・secret・kai-review の形・target_dir の task。
  `tests/test_verifier_dispatcher_names_the_attempt.py` (13 件)・`scripts/test_kai_review.sh` (3 本足した: pull の JSON の ID で done / needs-director が `verified`・継いだ env を捨てる)・
  `tests/test_plan_sh_execution_flag_rollback.py` (3 件。#272 と同じ)。
- 赤の実証: `python3 tests/red_proof_e3_caller.py` (11 変異: 照合を外す / 空の `--execution` を名乗りなしに倒す / 空の env / 狭めを戻す / 冪等を外す / verify-result fail が worker を手放さない / dry_run を派生値の後に回す /
  拒否の行を残さない / actor を置き換える / verifier-dispatcher が `--execution` を渡さない / `--close-execution` が live な試行を閉じる)。**全て「狙ったテスト名の assert の失敗」で RED** (collection / import の失敗は数えない)。
- 既存テストの更新 (狭め・枠の撤去・拒否の行の違いだけ): `test_task_status_single_definition` (設計の写し)・`test_needs_director_releases_assignment` / `test_projection_recovery_on_lock` / `test_plan_sh_state_store_cutover` (孤児の枠を手で作る・
  拒否の行・E3 の終了コード)・`test_s5_writers_lock_and_atomic` (verifying の拒否の行)・`test_plan_result_file` (verify-result は検証待ちから)・`test_plan_assignment_transaction` (`_retire_caller_slot`)・
  `tests/plan-assignment-identity.bats` (不正な AGENT_NAME の done は card の worker の枠を外す。bats)・`test_fail_evidence` (done / fail の書き手の検出が Controller 呼び出しも数える)・`test_task_controller_time_args_and_closed_findings` (引数の表)・`test_task_controller_has_no_callers_yet` (plan.sh が呼んでよい操作)・`test_plan_sh_compat_s3` (表にした違いだけ)。

### 16.7 E4 以降に送るもの

- `update --reset` / `retire` が試行を release / fail にするのは **E4a** (§7)。それまで reset は試行を閉じず、次の reserve の手順 0 か `--close-execution` が閉じる (テストで固定)。
- **E5 (提案)**: 名乗りなしの done / fail / needs-director / verify-result を拒否する (§9.2 の 1)。条件は監査ログで `caller_check=unverified` が crewvia 本体の task で 0 件になること。
- **`--mission ""` と他の `opts.get(...)` の真偽 (§16.5 の F1)**: 範囲外の残り。backlog。
- 拒否コード `TASK_ALREADY_RESERVED` の名前 (E2 の申し送り): pull が legacy / running の card を `--task` で再 pull したとき stderr に出る。E3 では**変えなかった** (E2 の拒否文言・テストを動かさない)。名前を `TASK_NOT_AVAILABLE` 等に変えるかは E4。

### 16.8 Codex 2 巡目 (t032 / PR #273): 再送の確認は、値を計算して経路を選ぶより前に

- **P2**: `cmd_verify_result` は冪等の確認より前に `rework_count + 1` を計算し、その値で `fail_execution` / `mark_task` を選んでいた。上限の 1 つ手前 (max 3・count 1) の fail は count 2 で試行を閉じ、
  **再送は 3 を計算して `mark_task` を選び、終わった試行を exit 3 で拒否**した (冪等の成功にならない)。上限ちょうどの fail は `needs_human_review` にしたあと、**再送のたびに count が増え検証の記録 (`**Verdict:**` の節) が重複**した。
- **修正**: count を増やす前・経路を選ぶ前に「この判定がもう記録されているか」を確かめる。fail は `fail_execution(VERIFICATION_REJECTED, dry_run=True)` が `IDEMPOTENT` (試行が既に `VERIFICATION_REJECTED`)。needs_human_review の verdict / 上限に達した fail は、
  task が既に `needs_human_review` で (nhr の verdict なら常に・fail なら count が既に上限以上のとき) 同じ判定の再送とみなす。名乗り (照合) は dry_run が先に行うので、違う試行の再送は今までどおり exit 3。
  nhr の verdict で人間の判断待ちになった後 (count は増えていない) の fail は**新しい判定**で、1 度だけ記録され、その再送が冪等になる。
- **族ごとの掃除 (「冪等の確認より前に状態から値を計算して経路を選ぶ」箇所)**: E3 で Controller に移した操作を 1 つずつ見た。

| 操作 | 値を計算する箇所 | 再送の経路 | 結果 |
|---|---|---|---|
| done | `completed_at` / `pr_number` (D1〜D2) | **dry_run が先** (E3 の初版から) | 冪等 (exit 0)・何も書かない |
| fail | `completed_at` / 証拠 | dry_run が先 (初版から) | 冪等 |
| needs-director | reason の整形のみ (状態に依存しない) | Controller が `IDEMPOTENT` を返す | 冪等 |
| ready-for-verification / verifying | 無い | status が既に進んでいるので `INVALID_TRANSITION` (exit 2) | 何も書かない (拒否の行だけ) |
| verify-result pass | `completed_at` (状態に依存しない) | Controller が `IDEMPOTENT` | 冪等 |
| **verify-result fail / needs_human_review** | **`rework_count + 1` で経路を選ぶ** | **直した (上記)** | 冪等 |

  境界のある値 (`rework_count`・`attempt` / `execution_count`) は、境界の手前・ちょうど・超えた後の再送を `tests/test_execution_e3_resend_idempotent.py` (13 件) が固定する (card・record・枠・Verification の節・監査の `ok` 行・count が増えない)。
  赤の実証: 修正前の plan.sh で同テストが 4 件 FAILED (`assert 3 == 0` 等)・修正後は緑。

### 16.9 Codex 3 巡目 (t033 / PR #273): 再送の根拠は状態の推測ではなく、この試行に結び付いた記録

- **P2**: t032 の `already_escalated` は「前の報告があった」ことを **task の status と rework_count だけ**から推し量った。`update --status needs_human_review` の後の最初の `verify-result needs_human_review --notes ...` は、検証の記録を 1 つも残さず成功 (冪等) で返り、
  fail で上限に達してエスカレーションした後の**違う** needs_human_review の判定と notes も飲み込まれた。
- **修正**: `## Verification` の各項目に `**Execution:** <この試行の id>` の行を足し (試行なしは `-`)、再送は「**同じ試行・同じ verdict・同じ notes の項目が card に既にある**」ときだけ (`_verification_recorded`。status・回数は見ない)。
  試行が既に終わっている行 (pass / 上限手前の fail) は Controller の記録 (`execution_end_code`) が根拠で、さらに notes が一致する項目が無ければ **exit 3 (conflict)** で飲まない。
- **「再送と判定する根拠」の列 (族の掃除)**:

| 操作 | 再送と判定する根拠 | 違う内容の 2 回目 (verdict / notes / PR 番号) |
|---|---|---|
| done / fail / needs-director | 試行の終了 record (`execution_end_code` + 名乗りの照合。Controller の `IDEMPOTENT`) **+ 要求の中身が card と同じこと** (t034。§16.10) | 名乗り付きは conflict (exit 3・何も書かない)・名乗りなしは遷移の拒否 (exit 2)。新しい記録にはならない。**t033 の時点では中身を比べず exit 0 で飲んでいた (t034 で直した)** |
| verify-result pass | 試行の終了 record (`execution_end_code` + 名乗りの照合。Controller の `IDEMPOTENT`) | 試行が終わっているので名乗り付きは conflict (exit 3)・名乗りなしは遷移の拒否 (exit 2)。新しい記録にはならない |
| ready-for-verification / verifying | 無い (status が進んでいれば `INVALID_TRANSITION` exit 2) | 同上・何も書かない |
| verify-result fail (上限手前) | 試行の終了 record + **同じ notes の Verification 項目** | notes が違えば exit 3 (飲まない) |
| verify-result needs_human_review / 上限に達した fail | **同じ試行・verdict・notes の Verification 項目** (status / count は根拠にしない) | 新しい項目として記録される (status は needs_human_review のまま) |

  根拠が status・回数の推測の行は 0 件。赤の実証: 修正前の plan.sh で新テスト 4 件が FAILED・修正後は緑 (`tests/test_execution_e3_resend_idempotent.py`)。

### 16.10 Codex 4 巡目 (t034 / PR #273 + #272): 同じ ID・**違う中身**の再送を exit 0 で飲まない

- **P2-1 (#273)**: t032 / t033 が直したのは「**同じ**中身の再送が二重に書かない」と verify-result の notes だけだった。done / fail / needs-director の IDEMPOTENT 分岐は
  中身を見ずに exit 0 を返していた。実測 (t015): `done t001 "first" --pr 5 --execution X` → `done t001 "CORRECTED" --pr 6 --execution X` が exit 0・card は最初のまま
  (pr_number 5・Result も最初)。`needs-director "reason A"` → `"reason B"`、`fail --no-head x` → `fail --head <sha>` も同じ。E3 の前は 2 回目はすべて exit 2 だったので、
  打ち直した Worker が気付けた。**E3 が「気付く手段」を黙って外していた**。
- **修正**: IDEMPOTENT の分岐で**要求を card と比べる**。違えば **exit 3** (`EXECUTION_ALREADY_TERMINAL`・固定の文言 + **違う項目の名前だけ**・何も書かない・名乗られた値も card の中身も出さない)。
  同じなら今までどおり exit 0。比べ方は書き込みと同じ正規化:

  | コマンド | 比べるもの (要求 ↔ card) | 正規化 |
  |---|---|---|
  | done | `--pr` ↔ `pr_number` / `--no-pr` ↔ `no_pr_waiver` (かつ `pr_number` が空) / Result 本文 ↔ `## Result` | 理由の空白を畳む・Result は前後の空白を除く。**`--pr` も `--no-pr` も無い再送は PR について何も主張しない** (最初の done が付けずに通った・`update --pr-number` で入れた番号を持つ card がある) |
  | fail | `--head` ↔ `fail_head` / `--no-head` ↔ `fail_head_waiver` / handoff ↔ `handoff_path` | head は完全 SHA に解決 (略称の再送は同じ)・理由の空白を畳む・handoff は normpath。**handoff を付けない再送は「handoff なし」の主張** (card に残っていれば違う中身)。`--head` と `--no-head` の両方・どちらも無い再送は最初の fail が通った形ではないので違う中身 |
  | needs-director | reason ↔ `needs_director_reason` (要約) + 長い理由は本文の `## Needs-Director 詳細` | 200 字超は要約と全文の両方を比べる (末尾だけ違う再送も違う中身) |
  | verify-result pass | 同じ試行・同じ verdict・同じ notes の `## Verification` 項目 (t033 と同じ根拠) | 上の fail と同じ。**pass は t033 で漏れていた** (notes が違う再送が exit 0 で飲まれた。この巡で直した) |

- **「違う内容の 2 回目」の列 (t032 / t033 の「同じ内容の再送」の表を全コマンドに広げた。実装から導いて、実測した結果)**:

  | コマンド | 同じ中身の 2 回目 | 違う中身の 2 回目 | 根拠 |
  |---|---|---|---|
  | pull (予約の再開) | 再開 (新しい試行を作らない・exit 0) | 中身を持つ引数が無い (識別は agent と task)。別の agent は `TASK_ALREADY_RESERVED` (exit 1) | §6 / E2 |
  | done | exit 0・何も書かない | **conflict (exit 3)**: `--pr` / `--no-pr` / Result のどれか | `_done_resend_differences` |
  | fail | exit 0・何も書かない | **conflict (exit 3)**: head / no-head / handoff のどれか | `_fail_resend_differences` |
  | needs-director | exit 0・何も書かない | **conflict (exit 3)**: reason | `transition_to_needs_director` の戻りと card を比べる |
  | ready-for-verification | status が進んでいるので exit 2 (`INVALID_TRANSITION`)・何も書かない | 同じ (引数は `--execution` だけで、中身の違いは無い) | `ACCEPTS_FROM` |
  | verifying | 同上 (exit 2) | 同じ (違う `--verifier` も exit 2・何も書かない) | `ACCEPTS_FROM` |
  | verify-result pass | exit 0・何も書かない | **conflict (exit 3)**: notes | 同じ notes の Verification 項目 |
  | verify-result fail (上限手前) | exit 0・何も書かない | **conflict (exit 3)**: notes | t033 |
  | verify-result needs_human_review / 上限に達した fail | exit 0・何も書かない | 新しい項目として記録される (status は needs_human_review のまま) | t033 (人間の判断待ちに追記する運用) |
  | update --close-execution | 閉じた後は何もしない (exit 0) | 中身を持つ引数が無い (`--mission` だけ) | §16.2 の 7 |
  | update --reset | (E3 では Controller を通らない) | — | E4 |

  **exit 0 で中身を捨てる行は 0 件** (「新しい項目として記録される」行は捨てずに残す)。赤の実証: 修正前の plan.sh で `tests/test_execution_e3_resend_differs.py` の
  conflict を期待する行が FAILED (done / fail / needs-director の 3 例を含む)・修正後は緑。同じ中身の再送は両方で exit 0。
  逆を固定していた `test_a_resend_of_done_does_not_redo_the_derived_writes_or_the_mission_done` (`--pr 99` の 2 回目が exit 0) は、同じ中身は成功・違う中身は conflict に直した。
- **P2-2 (#272)**: `tests/test_plan_sh_execution_flag_rollback.py` の `_compat_sha()` は `git log --all --grep=e3-execution-flag-compat -n 1` (いちばん新しい一致)
  を使っていた。#273 の **squash commit の本文**に互換 commit の件名が写る (`* e3-execution-flag-compat: ...`) ので、merge 後は squash commit が先に当たり、E3 の plan.sh を
  「戻し先」として取り出して 3 件赤になる (CI は浅い clone で skip されるので CI では気付けない)。**候補の commit から `scripts/plan.sh` に `def _execution_caller` を含むものを除く**。
  赤の実証: #273 の squash の形 (互換 commit の上に E3 の tree + 互換の件名を本文に持つ commit) を積んだ隔離 clone で、修正前 3 failed・修正後 3 passed。
  (memory `contrast-test-dies-when-its-subject-merges` と同じ型: 履歴の「いちばん新しい一致」は自分の subject が merge されると別の物を掴む)
- **P3**: §16.4 の `update --close-execution` は #273 で足したので revert すると消える (roll forward の後にしか使えない) と訂正した。`kai-review.sh` は pull の出力から
  `execution_id` を読めず名乗りなしへ切り替えるとき、stderr に 1 行 (`execution_id を読めませんでした`) 残す (E5 の観察で数える。
  `tests/test_kai_review_warns_when_it_cannot_name_the_attempt.py`。警告を外すと 4 件 FAILED)。

## 17. E4a の実績 (t016): 退役が試行の ID で照合され、`update --reset` / retire が試行を閉じる

### 17.1 何が変わったか (producer の切り替え。世代の照合は残す)

- **退役 marker / progress に `task_execution_id` を書く** (`lib_retirement.read_task_execution_id`。card の今の試行が ACTIVE のときだけ ID を束縛する。
  DETACHED / TERMINAL / legacy は `None` = 世代だけで束縛。card が読めない・欄が壊れているときは `UNKNOWN_EXECUTION_ID` で ID を書かない)。`task_started_at` も今までどおり書く。
- **`plan.sh retire` は `--execution <id>` か `--started-at <世代>` のどちらかが必須**。両方なら ID が優先。**空の明示指定 (`--execution ""` / `--started-at ""`) は exit 1** で
  何も書かない (presence で見る。env や名乗りなしに倒さない)。名乗りの ID が今の試行でなければ exit 3 (名乗られた値はエラーに出さない)。
  試行の欄が壊れた card は名乗りがあっても止める。同じ ID の再送 (retire で閉じた試行) は exit 0・何も書かない。
- **試行の閉じ方**: retire は RESERVED を release、RUNNING を fail (終了コード `RETIRED`)。`update --status pending --reset` は RUNNING を `RESET_BY_DIRECTOR` で fail、
  RESERVED を release、旧コードが残した DETACHED を abandoned で閉じる。legacy card は task だけ。同名の後任の枠 (identity の ID が違う) は消さない。
- **assignment の照合** (`assignment_execution_verdict`): marker と identity の両方に ID があれば ID で比べる (世代が同じでも別の試行なら OTHER)。どちらかに無ければ今の世代。
- **actor**: watchdog が retire を打つとき、dispatcher が reap を打つとき `AGENT_NAME` を `watchdog` / `dispatcher` に固定 (Director のシェルから継いだ名前を監査行に載せない)。
- **Director の閉じ方** (`update --status in_progress --reset` → フラグなしの `done`) と `reap-orphan-assignment` は E4a の後も通る
  (`test_the_cutover_review_task_closing_flow_*` / `test_the_directors_reap_of_an_orphan_slot_*`)。

### 17.2 E4b に送るもの

世代 (`started_at`) の照合と旧形式 marker (`task_execution_id` なし) の読み口を外すのは E4b (t025。**§18 で実施済み**)。§7 の条件 (E4a が両デーモンで動いている・旧形式 marker = 0・legacy の進行中 = 0) が先。

### 17.3 戻し方 (E4a)

PR を revert し、`scripts/sync-main-checkout.sh` で主 checkout を ff する (watchdog・dispatcher が読むコードを変えるので restart が要る。同スクリプトの restart 判定に乗る)。
marker / progress に残った `task_execution_id` は旧コードが読み捨てる (世代の欄は残してある)。E4a の間に閉じた試行の欄・record は残ってよい。
revert 中に retire / reset された task の試行は閉じられない (E2 の間と同じ形。`running` の残りは roll forward の後に `update --close-execution` で閉じる)。

### 17.4 検証

`tests/test_execution_e4a_retire_reset.py` / `tests/test_execution_e4a_daemon_env.py`。赤の実証は `python3 tests/red_proof_e4a_retire.py` (12 変異。R01 照合を世代に戻す・
R02 照合を外す・R03 空の `--execution` を指定なしに倒す ほか)。R01/R02 は classify の 2 枚目の層 (identity の ID) でも止まるので、変異は名乗りそのものを差し替える形にした。

## 18. E4b の実績 (t025): 世代 (`started_at`) の照合と旧形式 marker の読み口を外した

設計は §2.1 の #4・#8・#11・#12 と §2.4 の 4・§7。E4a (§17) が書き始めた `task_execution_id` だけが退役の証拠になり、世代は照合に使わない。

### 18.1 merge 条件 (§7 の (0)〜(2)) — 本番を**読むだけ**で数えた結果 (2026-10-03)

| 条件 | 数え方 | 結果 |
|---|---|---|
| (0) E4a が両デーモンで動いている | 主 checkout `git log -1` / `ps -o pid,lstart` の dispatcher・watchdog | HEAD `3dd3d7d` (E4a #274・2026-10-03T00:49:06+09:00)。dispatcher 3182381 (00:50:00)・watchdog 3182676 (00:50:01) は sync より**後**に起動。新しい marker の `task_execution_id` は、まだ退役が起きていないので**確かめられていない** (merge の直前に Director が次の退役で確かめる) |
| (1) legacy の進行中 | holding の status (in_progress / ready_for_verification / verifying / needs_human_review) で `current_execution_id` の無い card (`store-check` の `reported:legacy_execution` と同じ数え方) | **1 件**: `queue/missions/20260912-minerva-stage0-1/tasks/t017.md` (needs_human_review・worker なし・started_at なし・枠なし・execution の欄なし) |
| (2) 旧形式 marker | `registry/retirements/*.json` / `*.progress.json` で `task_execution_id` の無いもの | **0 件** (`registry/retirements/` は空) |

**(1) の 1 件は設計 (§7) の「legacy の進行中」に当たらない — E4b の後も壊れず、照合できない経路が生まれない。** E4b が外すのは
#4 (identity と世代の比較)・#8 (retire の世代名指し)・#11 (marker と identity の世代の比較)・#12 (marker の世代) で、4 つとも
**worker・枠・started_at のどれかを前提にする**。この card はどれも持たない: 退役 marker は枠を持つ Worker の task にだけできる
(束縛する試行も無い: `read_task_execution_id` = None)、`plan.sh retire` はどの ID でも worker 不一致で exit 3、枠が無いので identity と比べる
相手がいない。閉じる道 (`verify-result` / `update --reset` / 閉じた後の再 pull) は照合に世代を使わない (名乗りなしの経路。
`caller_check` の**ラベル**が `legacy_generation` / `no_execution` になるだけ)。`tests/test_execution_e4b_legacy_holding_card.py`
(同じ形の card を隔離 queue に置き、`verify-result pass|fail` / `update --reset` / 再 pull が通り、試行の欄を発明せず、
`retire` が exit 3 で何も書かないことを 6 本で示す)。Minerva の再開 (ユーザーが週末に再開予定) は E4b の影響を受けない。
**この判断はテストで示したが、数え方の定義 (§7 (1)) には入る**ので、merge の可否は Director が最終確認する (入れ替えるのは Director)。

### 18.2 外したもの・残したもの (E3 の `--execution` 互換・E4a の新旧両読みの仕分け)

| 対象 | 処置 | 理由 |
|---|---|---|
| #4 `Txn.classify_assignment(generation)` / `retire_assignment(generation)` | **外した** (引数は `execution_id`。None = 「いま card が示す実行」) | 世代の比較 |
| `lib_execution.identity_matches(generation=)` / `execution_matches(started_at=)` | **外した** (ID だけ。ID の無い identity は SUCCESSOR・撤去しない) | 世代の比較 |
| #8 `plan.sh retire --started-at` | **外した** (未知のオプション。`--execution` 必須) | 世代の名指し |
| #11 `assignment_execution_verdict(generation)` | **外した** (ID の無い identity は `EXEC_UNREADABLE` = 保留) | 世代の比較 |
| #12 `_bound_generation` / marker の `task_started_at` の読み書き / `read_task_started_at` / `UNKNOWN_STARTED_AT` | **外した**。marker は `task_started_at` を書かない。壊れた / 無い / 旧形式の ID は保留 | 旧形式 marker の読み口 |
| #13 `_settle_terminated` の `--started-at` | **外した** (`--execution` だけ) | 同上 |
| R-1 (#5) の legacy card の世代照合 (`generation_mismatch`) | **外した** (ID のある card だけ identity の ID と比べる) | legacy は「試行なし」 (§5.2) |
| `read_task_execution_id` が TERMINAL を束縛しない | **変えた** (ACTIVE / TERMINAL を束縛。legacy / DETACHED は None) | 世代が無い今、終わった試行を束縛しないと「ID を記録できていない」保留 = Director への通知になる (E4a は世代で quiet に通っていた)。TERMINAL の試行への `plan.sh retire` は exit 3 (何も書かない・quiet) |
| E3 の `--execution` (done 等 6 コマンド) | **残す** | 公開された名乗りの口。世代の照合ではない。rollback 先の互換 (#272) |
| `CHECK_LEGACY_GENERATION` / `CHECK_DETACHED` (監査の `caller_check` のラベル) | **残す** | 名乗りなしの経路の**ラベル**で、世代を比べない (読み手の互換。名前の変更は監査ログの語彙変更) |
| `attempt_view` の DETACHED (a) (`started_at` ≠ `execution_reserved_at`) | **残す** | 名乗りの照合ではなく、旧コードの pull が card を取り直したことを card 1 枚から見分ける検出 (§9.3 の roll forward) |
| `identity.started_at` を書くこと | **残す** (比べない) | projection の不変条件 (state-store.md §2) と rollback 先の旧コードが読む |
| watchdog の idle 時計 / Taskvia / `log_to_obsidian.sh` | **残す** | 時刻としての使い道 (§2.3) |

### 18.3 E2 の CAS (`_pull_cas_ok`) は**外さない**

CAS の `started_at` の項は名乗りを世代で照合するものではなく、**この pull 自身が書いた予約 (X) が card にそのまま残っているか**を card 1 枚で確かめる
(旧形式の書き手が status / worker / started_at だけを動かし、execution の欄を更新しない形への備え。§6 の Codex 3 巡目 P1・赤の実証 E05)。
外す根拠は「旧書き手が本番に残っていない」ことだが、**それは証明できない**: sync 後の plan.sh は新しいコードだが、rollback (§9.3) で旧コードに戻れる
(E4a の plan.sh に戻れば `update --reset` は試行を閉じるが、E3 以前に戻れば閉じない)。1 項を外す利得より、外した後の穴 (reset の上書き) の方が大きいので残す。
`_pull_cas_ok` の docstring に同じ理由を書いた。

### 18.4 族の掃除 — §11.1 の grep を同じコマンドで数え直した (`git grep`・追跡ファイルのみ。origin/main (E4a) → この PR)

| パターン | 本体 (`scripts/ hooks/ crewvia` − `scripts/test_`) | テスト | 処置 |
|---|---|---|---|
| `started_at` | 142 → 94 | 257 → 242 | 残りは分類済み (下) |
| `started-at` | 17 → 3 (コメント 3: 「外した」の説明と `--expect-started-at` の履歴) | 34 → 14 (外した後の拒否のテスト・説明) | 全件 |
| `task_started_at` | 15 → 2 (コメント 2: 旧形式の説明) | 18 → 15 (旧形式 marker を作るテスト) | 全件 |
| `read_task_started_at` / `UNKNOWN_STARTED_AT` / `_bound_generation` | 4 / 8 / 7 → **0** | 3 / 2 / 0 → **0** | 構造ガードが 0 を固定 |
| `classify_assignment` | 22 → 22 (引数が世代から ID に変わった) | 10 → 13 | 全件 (`retire_assignment` 9・テスト 14 も同じ引数の変更) |
| `assignment_execution_verdict` | 6 → 6 | 4 → 4 | 引数から世代を除いた |
| `execution_matches` / `identity_matches` | 7 → 6 / 7 → 7 | 7 → 8 / 3 → 6 | 引数から世代を除いた |

残りの `started_at` (本体 94) の分類 (照合に使うものが残っていないこと): 書く側 (`reserve` が `now` を card と `execution_reserved_at` に・reset / retire / `verify-result fail` が null・`publish_assignment` が identity に・
監査の `generation` 欄・R-1 が identity を作り直す) / 時刻 (watchdog の idle 時計・`now_iso` の Taskvia・`log_to_obsidian.sh`) /
card 1 枚の検出 (`attempt_view` の DETACHED (a)・`is_detached_a`) / CAS の項 (§18.3) / `lib_daemon_watch.*` と `lib_mux.py` の `generation` は別の意味 (デーモン・server の世代)。
**`started_at` を名乗り (引数・marker・identity) と比べる式は 0**: `tests/test_execution_e4b_no_generation_readers.py` が (a) 外した読み口の語が本体に戻らない (b) 照合の関数に世代の引数が無い
(c) `lib_retirement.py` に `started_at` / `generation` を含む比較式が無い、を固定する。

文書 (エージェントへの指示): `agents/director.md` の `retire` の例と Rule 5 の行から `--started-at` を外した (ID の無い旧形式の card は `plan.sh update … --reset`)。
`knowledge/daemon-authority.md` の retire の節・`knowledge/plan-sh-strict-args.md`・`knowledge/empty-vs-unobservable.md`・`scripts/CLAUDE.md`・`tests/` の
`--started-at` を使っていた 4 本 (`test_registry_isolation`・`test_retirement_wait_and_timeout_notice`・`test_plan_sh_state_store_cutover`・`test_execution_e3_caller_table` の説明) と bats 9 本を ID に直した。

### 18.5 E4a の Codex / review から持ち越した 5 件の処置

1. **(t018) 今の ID・古い世代・ID なしの identity の組**: 世代は引数ですらなくなった (`--started-at` は未知のオプション)。ID だけで card と照合し、枠の identity に ID が無ければ
   「この試行の枠」と証明できないので exit 3・何も書かない (世代で補わない)。ID のある identity なら通る。テスト `test_the_current_id_retires_even_when_the_identity_carries_no_id_…`。
2. **(t019 #1) `_bound_execution` が壊れた ID で世代に倒れる**: 壊れた ID は None = 保留 (kill の前は `phase=unprovable`・後は `_cleanup_deferred`)。`test_a_marker_with_a_malformed_execution_id_…` /
   `test_a_dead_worker_with_a_malformed_execution_id_…` (5 形: 非 hex・空・int・None・旧形式)。
3. **(t019 #2) 消し込みの対象に入れる**: `--started-at` のテスト 4 本と `plan-sh-strict-args.md:47` を直した (上)。
4. **(t019 #3) 旧形式 marker + 同じ世代の同名後任は見分けられない (E4a の既知の residual)**: 旧形式の読み口ごと外して閉じた (旧形式は保留)。
5. **(t019 #4) red proof R09 の KeyError**: `env.get("AGENT_NAME")` (assert の失敗になる)。

### 18.6 戻し方 (E4b)

PR を revert し、`scripts/sync-main-checkout.sh` で主 checkout を ff する (watchdog・dispatcher が読む `lib_retirement.py` を変えるので両デーモンの restart が要る。同スクリプトの restart 判定に乗る)。
E4a のコードに戻るので新旧の marker を両方読める (§17.3)。E4b の間に書かれた marker は `task_started_at` を持たない — E4a のコードはそれを「旧形式でない」marker として ID だけで読めるので問題ない
(ID の無い marker は E4a でも世代 = 無し → 保留)。E4b の間に `--started-at` を使えなかった Director の手順は、戻すと再び使える。

### 18.7 検証

`tests/test_execution_e4a_retire_reset.py` (退役の全経路・旧形式 / 壊れた ID の保留・TERMINAL の束縛)・`tests/test_execution_e4b_no_generation_readers.py` (構造ガード)・
`tests/test_execution_e4b_legacy_holding_card.py` (§18.1 の 1 件)・`tests/test_state_store_transaction.py` / `tests/test_task_controller_unit.py` (照合の規則)・
`tests/plan-assignment-identity.bats`。赤の実証: `python3 tests/red_proof_e4b_generation.py` (7 変異: 世代の名指しを戻す・ID の無い identity を一致にする・保留を外す・壊れた ID を使う・TERMINAL を束縛しない・
旧形式の世代を証拠に戻す・後任を後任と見ない) と、更新した `tests/red_proof_e4a_retire.py` (R01 は欠番・11 変異) — 全部「狙ったテスト名の assert の失敗」で RED。


---

## 19. 01c の実績の締め (t021): cutover / rollback の観測・backlog・後続への引き継ぎ

01a の `state-store.md` §10・01b の `git-policy.md` §16 と同じ形。本番の観察は t020 (コード変更なし・観察のみ。観察時 origin/main = 主 checkout HEAD = `4bf3f2e`、E2・E3 (前提 #272 含む)・E4a・E4b をすべて祖先に持つ)。

### 19.1 cutover と rollback (§9.1 の表の実績)

| 段 | merge (JST) | 本番で変わったこと (t020 が観察できたもの) | 戻し方 | 実際に戻したか |
|---|---|---|---|---|
| E2 `d0cd944` (#271) | 10-02 06:02 | pull が ID を発行。t020 自身の pull で card (`current_execution_id`・running・attempt 1)・identity・`executions/` の record・監査 pull 行 2 本・`.crewvia-env`・pull の JSON の **5 点が一致** | revert → `scripts/sync-main-checkout.sh`。card の欄・record・identity の欄は残ってよい (§9.3) | 戻していない |
| E3 前提 `b9b2abb` (#272) / E3 `a19a0f9` (#273) | 10-02 13:37 / 13:58 | done 等が ID を照合 (違う ID は exit 3・`refused:` 行)・`caller_check`。E3 以降の done: `verified` 8・needs-director `verified` 4・`no_execution` 3 (Director の開いた card・update)・`unverified` 1 (E3 merge 直後の旧プロンプトの Worker の done) | revert → sync。**#272 が先に入っているので `--execution` は戻し先でも読み捨てで通る** (§16.4) | 戻していない |
| E4a `3dd3d7d` (#274) / E4b `4bf3f2e` (#275) | 10-03 00:49 / 07:40 | marker / progress に `task_execution_id`・watchdog が ID で retire (actor=`watchdog`)・世代の照合なし | revert → sync (両デーモン restart) | 戻していない |

どの段も rollback は**実行していない**。手順は書いただけでなく、E3 は旧コードの plan.sh を履歴から取り出して `--execution` 付きの 6 コマンドが通ることをテストで確かめた (§16.4)。E4 は §9.5 の往復の表で確かめた。

観察できたこと・できていないこと (t020):
- 観察用 mission (後で archive) の 2 人の Worker で: 他人の task の done は `--execution` でも env でも exit 3 `EXECUTION_NOT_CURRENT`・card / record / assignments の sha256 は前後で不変・監査に `refused:EXECUTION_NOT_CURRENT` が 2 行だけ増えた。
- 退役は、実在しない window に**手書きの marker** (`task_execution_id` 入り) を置き、本物の watchdog に cleanup させて観察した。card は `execution_status=failed` / `execution_end_code=RETIRED`、marker と progress は消滅、監査 retire 行は actor=`watchdog`・`caller_check=verified`。
- **限界**: 実 pane の SIGTERM / SIGKILL の経路は通っていない (window gone の cleanup-only 経路)。本番の Worker は殺していない。自然発生の退役は観察期間中に無かった。
- dispatcher / watchdog は E4b merge (07:40:40) の**後** (07:40:48 / 07:40:49) に起動し、`registry/daemons/*.version.json` の head が `4bf3f2e`。新しい版にしか書けない行 (監査の `execution_id` / `caller_check` / retire の actor) も観察できた (§9.1 の 3 点の証明)。
- `store-check` は観察の前後とも 6 件で出力に差分なし、通知台帳に新しい行なし。E3 以降の `refused:` は観察用の 2 件だけで、**本番の Worker・Director の操作が拒否された例は 0**。
- 観察後の片付け: 観察用 mission は archive へ。worktree・`task/*` branch・`queue/missions` の一覧・`state.yaml`・`registry/retirements/`・`queue/assignments/` が前後で同一。

### 19.2 backlog (族ごと。01c の完了を止めない)

各 review / Codex / QA の Result で「backlog」「Director 判断」とされたもの。直すときはまず同じ族のものをまとめて洗う (族の直し漏れが 01c で繰り返し P2 になった)。

**(A) 拒否・報告の見え方**
1. (E1 review t007) `lint_plan.check_execution_fields` のメッセージが frontmatter の `id` 欄を使う。識別子はファイル名 (不変条件 2) なので表示だけの問題だが、揃える。
2. (E1 review t007) `reserve_task` の `TASK_NOT_ELIGIBLE` / `TASK_NOT_FOUND` は `_refuse` を通らず、監査ログに拒否の行が残らない (`TASK_ALREADY_RESERVED` は残る)。
3. (E1 review t007) `execution_fields_invalid` / `record_unreadable` / `record_malformed` / `record_superseded_active` (手編集起因の報告) を消す Controller の操作がまだ無い。`update --close-execution` は DETACHED で active な試行だけを閉じる (§16.2 の 7)。
4. (E1 review t007) `now` は、時刻形でない世代形の文字列 (`'yesterday'`) をまだ通す (`_check_now` が `_check_generation` と同じ検査)。**E2 で pull 側を確かめた結果**: pull は `now_generation()` で自分で作る (plan.sh:3885。呼び出し元から受けない) ので、通る入り口は Controller を直接呼ぶ側だけ。時刻形の検査を足すかは、直接呼ぶ側が増えたときに決める。
5. (E4b review t028) R-1 の `reported:generation_mismatch` が、ID の無い holding card では出なくなった (§18.2)。本番で該当するのは Minerva t017 だけで、worker が無いので別の finding (`holding_without_worker`) になる。
6. (E4b review t028) E4a の書いた「task 付きで `task_execution_id: null` の marker」が E4b の merge の瞬間にあると、`_cleanup_deferred` の通知が 1 件増える。今回の merge 時は 0 件。

**(B) pull / 準備ロック / worktree**
7. (E2 t009 / t011) 異なる task の同時 pull で `git worktree add` が git のロック競合 (`config.lock`) で W5 → `needs_director` になる (負荷下 2〜3%、E2 の前と同率)。helper の再試行で直す (E2 が増やした問題ではない)。
8. (E2) `executions/<task>.prepare.lock` が 0 バイトで残り、archive にも移る。掃除の規則を決める (消してよいが、いつ消すかが無い)。
9. (E2) pull が card だけ書いて死んだ窓では枠が無く、dispatcher が idle と見て別の task を送る。旧コードと同種で、収束する (reserved の再開は同じ Worker の再 pull だけ)。
10. (E2) `_resume_reserved` が枠を Controller を通さずに書く (E4 以降の候補。§15.2 の 1 の「持ち主が card から公開し直す」は回復の向きと同じなので害は確認していない)。
11. (E2) 準備ロックの記述子を継承した、detach した子孫がいる間は再 pull が待たされる (§6.1 / §15.6)。
12. (E2 Codex P3) compat テストに `or True` が残っている (緑のまま何も確かめない箇所。直す)。

**(C) verify-result・reset・docs**
13. (E3 t013) 上限に達して `needs_human_review` になった後、notes の違う `verify-result fail` を打つたびに `rework_count` が上限を超えて増える。§16.9 の「新しい項目として記録」どおりだが、**上限を超えた後の回数の扱い**が文書に無い。決める。
14. (E3 t013) `docs/qa-operations.md:113` が §16.5 の F4 の表に無い (族の洗い出しの漏れ)。
15. (E4a review t019) `update --reset` に他の欄 (`--skills` 等) を併せると、Controller の書き込みと `save_task` の 2 回に分かれる (同じロックの中)。間で crash すると reset だけが残るが、他の欄が付かないだけで壊れない。
16. (E4b review t028) `scripts/plan.sh` のコメント: :62 / :6731 が `--started-at` を「exit 1」と書く (実際は使い方の誤りで exit 2)。:4866 / :6981 のコメントが `generation=None` のまま。

**(D) テストの後始末・QA**
17. (E3 t013) QA ハーネスのテスト用 tmux セッション (`crewvia-retiretest-*`) が残り、隔離のデーモンのコピーが動き続けた (Director が停止)。テストの後始末の族 (`tests/CLAUDE.md` の隔離の規則に「ハーネスの終了時に自分の session を殺す」を足す候補)。
18. (E3 t013) QA の所見のうち、Director 判断とされた 4 件は t013 の Result に残っている (本節の 13・14 を含む)。

### 19.3 判断の記録と、後続への引き継ぎ

**判断**
- **R2** (§9.5 の 6): 旧コードの reset が `started_at` を null にするので「間に B がいた」痕跡は card から消える。どちらも持ち主がいない (pending) ので書き込みは起きない。「封印」案 (新コードの reset が `execution_reserved_at` を空にして印にする) は、旧コードの経路で同じ印が残らず答えを揃えられないので採らなかった。
- **E2 の CAS は置き換えでなく AND** (§2.1 の 7・§15.2 の 2・§18.3。Codex 3 巡目 P1): 移行中は旧形式の書き手が status / worker / `started_at` だけを動かすため。E4b でも外さなかった。
- **E3 の戻し方**: 戻し先が `--execution` を受け付けて読み捨てる互換 (#272) を E3 の**前**に別 PR で入れた (§16.4。Codex 3 巡目 P2)。
- **E5 (名乗りなしの done / fail を拒否する段) は 01c に入れず後続に送った** (Director 判断)。条件は §9.2 の 1: 監査の `caller_check=unverified` が 0 件 (監査は欠けうるので唯一の根拠にしない・§1.6 の 11)・target_dir の task の ID の受け渡しが worker.md にある・生きているセッションが、ID を渡す版の後に起動している (§9.4 の E5 行)。

**後続へ (vNext 01 の完了と、次に送るもの)**
1. **E5**: t020 の時点で `unverified` は crewvia 本体の task で E3 直後の 1 件だけ (以後は `verified` か `no_execution`)。ただし **Director 自身の done に `--execution` が付いているか**・**target_dir の task の ID の受け渡し (worker.md)** は未確認。着手前に再集計する。
2. `reported:execution_active_on_finished_task` 5 件 (t012 / t013 / t014 / t032 / t033。E2〜E3 の間に終わった task): 自然には消えない。`update --close-execution` で閉じる (§16.1) か、許容して残すかを Director が決める。
3. `reported:holding_without_worker` の Minerva t017 (既存。Minerva の再開で解消)。
4. 範囲外として後続 mission に送ったもの: Execution status の `stale` / `abandoned` / `recovered` (原案 EXEC-04)・退役の実 pane 経路の本番観察 (自然発生待ち)・`caller_check` の `legacy_generation` / `detached_execution` ラベルの整理 (出力は 0 件。監査ログの互換を見て決める)。
5. 本節 §19.2 の backlog。
6. 手書き marker による退役の観察の手順は、memory `observe-retirement-with-handwritten-marker-on-nonexistent-window` に残した。

01c で 01a / 01b から引き継いだもの (state-store.md §10.3 の 1・§0・git-policy.md §16.4) は、Execution ID・pull の冪等化 (N8)・G1 の CAS・世代の置き換え (E4b) として入った。**残るのは上の 1〜4 と §19.2 だけ**。

---

## 20. E5 の設計 (mission 20261008-e5-reject-unnamed-reports t001): 名乗りなしの報告を拒否する

§19.3 の後続 1。**この節は設計だけで実装しない**。根拠は 2026-10-08 時点の main `01f0d26` と `queue/audit/transitions-*.jsonl` 全 9 本 (10-01 以降)。
監査は欠けうる (§1.6 の 11) ので、出どころは (A) 監査の実測、(B) 実装からの列挙の 2 本で別々に出し、突き合わせた。

### 20.1 (A) unverified の出どころ (監査の実測)

`caller_check` を持つ行は 10-01 21:03 以降。内訳 (op × caller_check): done は verified 47・no_execution 3・**unverified 2**、needs-director は verified 30・**unverified 1**、
`update` は no_execution 7・**unverified 2**、pull 100 / retire 6 は verified のみ。`refused:` は `EXECUTION_NOT_CURRENT` の 2 行 (t020 の観察用) だけ。
**fail / ready-for-verification / verifying / verify-result の行は 1 本も無い** (本番で打たれていない。この 4 つの経路は実測が無く、§20.2 の実装の列挙だけが根拠)。
verified の done は全部 Worker (Ren 22・Arjun 9・Luna 7・Kai-codex 4・Seo 2・ほか 3)。**Director (Sora) の done が verified になった例は 0 件**。

| # | 時刻 (UTC) | 行 | 誰が | 何があった | 分類 |
|---|---|---|---|---|---|
| 1 | 10-02 04:58 | done 01c/t015 | Sora (Director) | Director が Worker の代わりに done。`--execution` を付けていない (director.md:207 の例は付ける形だが「付けなくても通る」と書いてある) | **経路の穴 (Director の手順)** |
| 2 | 10-05 17:49 | update kai-review-full-findings/t001 `in_progress→pending` | Sora | Director の `update --status pending --reset` (回復) | **対象外 (§20.3)** |
| 3 | 10-05 18:31 | update director-escalation-telegram/t026 `in_progress→pending` | Sora | 同上 | **対象外 (§20.3)** |
| 4 | 10-05 21:10 | needs-director director-escalation-telegram/t006 | **Seo** (review Worker) | セッションの記録を見ると、pull の JSON には `execution_id` があったが、セッション中の Bash で `--execution` / `EXECUTION_ID` を使った呼び出しが 0 回。最後の呼び出し (`cd <worktree> && ./scripts/plan.sh needs-director … --result-file …`) は `source .crewvia-env` もしていない | **Worker の手順が ID を持ち越さない** |
| 5 | 10-08 05:30 | done pull-reject-empty-agent/t005 | **Ren** (code Worker・今日) | 同じ型。pull は `plan.sh pull --task t005 --mission …` を env 無しの素の Bash で打ち、done も `cd <worktree> && ./scripts/plan.sh done … --pr 285` だけ。セッション中 `--execution` / `EXECUTION_ID` は 0 回 | **同上** |

なお Director の 3 つの `no_execution` done (10-02 t018/t019/t028) は、Director が `update --reset` で開いた card (試行なし) で、E5 の対象外 (§20.3)。

**#4 と #5 の共通の原因** (記録の読み取りで言えること。Worker の system prompt は記録に残らないので、worker.md が読まれていたかは未確認):
1. Worker に最初に届く指示文が ID に触れない。dispatcher の割り当て文 (`dispatcher.sh:2675-2678`: 「タスク t005 (mission=…) を実行して。plan pull --task … で取得後、作業→plan done で完了。」) と
   start.sh の起動文 (`:946` / `:948`) はどちらも `--execution` を言わない。Ren の t005 も Seo の t006 もこの文面で始まり、この文面どおりに動いた。
2. ID は pull の JSON の中にあるだけ。Claude Code の Bash は呼び出しごとに env が消える (worker.md:83) ので、`export EXECUTION_ID` (worker.md:326) は次の呼び出しに残らず、
   done を打つ呼び出しで `${EXECUTION_ID:+--execution …}` が空になる。**空になっても何の警告も出ない**。
3. 名乗りなしで通ったときの警告が実装に無い。§5.2 は「E3 では通す (warn を stderr に 1 行)」と書いているが、`lib_task_controller.py:361` は `PROCEED, CHECK_UNVERIFIED` を返すだけで、
   `plan.sh` に warn を出す箇所は見つからない (`grep -n unverified scripts/plan.sh` が 0 件)。**Ren も Seo も、自分の報告が名乗りなしだったことを知る手段が無かった**。
   (`kai-review.sh:270-271` の「ID を読めなかった」警告は kai-review の内側の話で、Worker の手順とは別。)

### 20.2 (B) 呼び出し元の列挙 (実装から)

6 コマンド (done / fail / needs-director / ready-for-verification / verifying / verify-result) を `agents/` `skills/` `scripts/` `hooks/` `README.md` `docs/` から拾った。

| 呼び出し元 | 今 ID を渡すか・どこから得るか | PR-1 での直し方 |
|---|---|---|
| **dispatcher の割り当て文** `dispatcher.sh:2675-2678` | 渡さない・触れない | 文面に追記: 「pull の JSON の `execution_id` を、報告 (done / fail / needs-director / ready-for-verification) の `--execution` に渡す」。ID は割り当て時点では未発行 (pull が発行する) ので値は入れられない |
| **start.sh の起動文** `:946` `:948` | 同上 | 同じ追記 (target_dir 版 `:946` も) |
| **plan.sh pull の出力** | JSON に `execution_id` / `attempt` (worker.md:278) | JSON は変えない。**stderr に 1 行**、そのまま貼れる形で: `[plan.sh] 報告には --execution <ex-…> を付ける (done / fail / needs-director / ready-for-verification)`。Seo の pull は `2>&1 \| head` で、Worker の pull は素の Bash なので見える。stdout の JSON を壊さない |
| **worker.md** | 手順 (:286-294, :324-332, :661-686, :808) は ID を渡す形。**ただし ID なしの例が 9 か所残る** (t003 で `grep` し直した全数): :124 (フロー図)・:165 (表の `plan done t011 "result" --mission <slug>`)・:189 (target_dir 節の `plan done t002 …`)・:395 (watchdog タイムアウトの `plan.sh fail <task_id> "$HANDOFF_PATH" --head …`)・:571 (`--result-file <path>` の説明文中の `plan done …`)・:575 (クォート付きヒアドキュメントの `plan done "$TASK_ID" --result-file - …`)・:608 (`plan needs-director <id> "<stderr>"`)・:1026 (まとめ)・:1027 (散文。対象外) | 例は全部 `${EXECUTION_ID:+--execution "$EXECUTION_ID"}` 付きに揃える。**「env は呼び出しごとに消える」を手順の最初に**書き、`export` ではなく done の**同じ呼び出しの中で** `.crewvia-env` を source するか card の ID を `plan.sh status` で読み直す形を勧める |
| **worker-codex.md** :43, :131-133, :175-177, :190-191 | 渡さない例 (:43 の `plan.sh done t003 --pr 42 --mission <slug> "PR #42 ..."` を含む) | 例を揃える。実際の Kai-codex の経路は kai-review.sh (下)。手動の経路は Director が `kai-review.sh` を手で打つ形 (:191) |
| **kai-review.sh** :160-271 | 渡す。pull の JSON から取り `EXEC_ARGS` で done / needs-director (17 か所) に付ける。JSON が読めなければ名乗りなし + stderr に固定文言 (:270)。**`--skip-pull` (:85/:245/:276) と dry-run は pull しないので `EXEC_ARGS` が空のまま — PR-2 の後は done / needs-director とも `EXECUTION_REQUIRED` で拒否される** (`--skip-pull` は「task が既に in_progress」の再実行用で、そのとき card は active の試行を持つ) | **PR-1 の範囲**: `--skip-pull` のときは card の `current_execution_id` を読んで `EXEC_ARGS` に入れる (verifier-dispatcher.sh と同じ読み方)、または手動で `--execution <id>` を受け取れるようにする (どちらかに決めて実装。読めなければ名乗りなし + 同じ警告)。dry-run は書かないので対象外。**E5 の後は「JSON が読めない」経路も拒否に変わる** — 拒否文が `error_code=` で出るので `call_needs_director` の失敗の扱いを PR-2 で確かめる (Kai-codex の done が止まると codex-review が恒久に止まる。memory `pull-refusal-must-spare-orphan-assignments` と同じ型) |
| **skills/crewvia-qa** :181 / :240 | 文は渡す形 (:181)。**例のコマンド (:240 の `needs-director`) は渡さない** | :240 の例に `${EXECUTION_ID:+--execution "$EXECUTION_ID"}` を足す |
| **skills/crewvia-plan-review** :146 | 文は渡す形。完了・差し戻しのコマンド例 (:70-71, :110-111, :136-137, :180-182, :218) は ID なしの略記 | 例に付ける (表のセルの略記も含む) |
| **agents/verifier.md** :7, :58-75 (:67 / :70 / :75 の `verify-result` の例) | 本文で ID を渡すと言い、例は「省略している」 | 例を実形にする |
| **verifier-dispatcher.sh** :261-274 (verifying)・:419-422 (verify-result の指示文) | 渡す。card の `current_execution_id` を読む (active のときだけ)。env は `pop` | 変更不要 (本番 0 プロセス。card に ID が無ければ試行が無く対象外) |
| **agents/director.md** :207-211・:300-309・:325・:1066 (`plan.sh done <id> --pr <N> --mission <slug> --result-file <path\|->` の例) | done の例は `--execution` 付き。ただし **「付けなくても通る」と書いてある**。fail の例・needs-director の説明は ID に触れない | PR-1 で「付ける」に書き換え (拒否は PR-2)。Director が代理で打つ done / fail / needs-director は必ず `plan.sh status` の `ex-…` を渡す |
| **dispatcher の Director 向け通知文** `dispatcher.sh:2308` ([review-refused]) | `plan.sh done {task_id} --mission {slug}` — 渡さない。ただし直前の `update --status in_progress --reset` で開いた card (試行なし) への done なので対象外 | 変更不要。他の通知文 (2308 以外) に 6 コマンドを打たせる文面は見つからない |
| **target_dir の task** (worktree も `.crewvia-env` も無い) | worker.md:324-332 が JSON から `EXECUTION_ID` を取る (**唯一の入手経路**)。監査に該当する done は 1 本も無い | 実測が無い。PR-1 の後に隔離環境で target_dir の task を 1 本通して確かめる (§20.5) |
| **hooks/** | 6 コマンドを打たない (grep で出るのは判定ロジックのコメントと文面だけ) | 変更不要 |
| **scripts/bin/plan** | env を継承して exec するだけ | 変更不要 |
| **README.md** :876, :889, :918 | ID に触れない | 文の補足のみ |
| **watchdog / lib_retirement / dispatcher の自動報告** | 6 コマンドを打たない (retire は E4 で `--execution` 済み) | 対象外 |

**列挙を機械に任せる** (t002 のレビュー指摘 P2-2: サイトを人が数えると漏れる。実際 t001 の表は worker.md の 3 か所しか挙げておらず、上の 9 か所が正): PR-1 に構造テストを入れる —
`agents/*.md` と `skills/**/*.md` の中の **6 コマンドの例** (fenced code の行、および `plan(.sh)? <cmd> <引数…>` の形のインラインコード。コマンド名だけの言及は対象外) は、
すべて `--execution` か `${EXECUTION_ID:+` を同じ文 (行、またはヒアドキュメントの開始行まで) に含む。**漏れたら赤**。例外は理由つき allowlist (キーは行の literal)。
列挙は `grep -rnE 'plan(\.sh)? +(done|fail|needs-director|ready-for-verification|verifying|verify-result)\b' agents skills` を起点に、実装 (テスト) から導く。表を直すだけで終えない。

(A) と (B) の突き合わせ: #4 #5 は「指示文が ID に触れず、手順の例にも ID の無いものが残り、名乗りなしが黙って通る」の 3 つが同時に働いた結果で、(B) の上 2 行 + worker.md / skills の例に当たる。
#1 は director.md の「付けなくても通る」。**監査に出ない経路** (fail / ready-for-verification / verifying / verify-result) は (B) で形を揃えるしかなく、PR-1 の後に 1 本ずつ通す (§20.5)。

### 20.3 拒否の対象と対象外

- **対象**: 6 コマンドで、card が **active (reserved / running) の試行を持ち、呼び出しが名乗らない** とき (`_authorize` の `# 名乗りなし` の `view == ACTIVE` の分岐、`lib_task_controller.py:360-361`)。**Director の done / fail / needs-director も対象** (§5.3。Director 専用の抜け道は作らない)。
- **対象外 (今のまま通す)**: 試行なしの card (`no_execution`・`legacy_generation`)・DETACHED (`detached_execution`)・TERMINAL で名乗りなし (`no_execution`)。これらは task の遷移だけで試行の欄を触らない — 拒否すると、Director が `update --reset` で開いた card の done が通らなくなる。
- **`update` は対象外で、拒否の候補に入れない** (§5.3): 手動の回復の出口。Worker が死んで誰も ID を名乗れない状況で、Director が `update --status pending --reset` で card を開ける唯一の経路。E5 で塞ぐと、止まった task の出口が消える。
  `update` の `unverified` は E4a 以降 `--reset` が active な試行を閉じるときに付く印で、**通常運用で出続ける** (表 #2 #3)。**E5 の「0 件」の条件は 6 コマンドだけで数える** (`update` / `retire` / `pull` を除く)。
- exit は 3 (照合の族)、`error_code` は新設 **`EXECUTION_REQUIRED`** (`lib_execution.EXIT_CODES` に 3 で足す)。既存の `EXECUTION_NOT_FOUND` は「その card はその ID を発行していない」の意味で、名乗りなしには使わない (直し方の文が違う)。
  拒否文 (固定文言 + 識別子だけ。名乗られた値は無いので出す物も無い):
  `{slug}/{tid}: この task は実行中の試行があります。報告には今の試行の execution id を名乗ってください (--execution <ex-…>。ID は pull の JSON の execution_id か \`plan.sh status --mission {slug}\` の進行中の行 [ex-… attempt N])。何も書いていません。` + 最後の行 `[plan.sh] error_code=EXECUTION_REQUIRED`。
- **何も書かない**: 既存の拒否と同じ `_refuse` (監査に `refused:EXECUTION_REQUIRED` の 1 行だけ。card・record・枠は 1 バイトも書かない)。**task-graph の再生成は確定で起きている**: 既存の `_controller_die` は `die()` (普通の `SystemExit`) で、plan.sh 末尾の dispatch が再生成する (t002 の確認)。
  なので **PR-2 で `UsageExit` の型にまとめて直す** — 新しい `EXECUTION_REQUIRED` と既存の `EXECUTION_NOT_CURRENT` / `EXECUTION_NOT_FOUND` / `EXECUTION_ALREADY_TERMINAL` の 4 つ (memory `no-write-refusal-must-use-usageexit`。PR #285 の pull と同じ指摘)。
- `--execution ""` / 空の `CREWVIA_EXECUTION_ID` の拒否 (exit 1) は今のまま。

### 20.4 段取り

**PR-1 (呼び出し元を揃える。plan.sh の判定は変えない)**
1. §20.2 の表の「直し方」を全部入れる: dispatcher / start.sh の文面・worker.md / worker-codex.md / skills / verifier.md / director.md の例と文・pull の stderr の 1 行。
2. **名乗りなしの警告を実装する** (§5.2 に書いてあって無かったもの): 名乗りなしで `PROCEED, UNVERIFIED` になるとき stderr に固定文言 1 行 (`[plan.sh] 名乗りなしで報告しました (execution id を --execution で渡してください)。将来は拒否されます`)。
   終了コード・書き込み・監査の行は変えない。**これで Worker は自分が名乗っていないことを初めて知り、観察期間の機械が数えられる** (監査の `unverified` + 警告)。
3. **`kai-review.sh --skip-pull` を直す** (§20.2): card の `current_execution_id` を読んで `EXEC_ARGS` に入れる、または `--execution <id>` を受ける。
4. **構造テスト**: agents/ と skills/ の 6 コマンドの例はすべて `--execution` か `${EXECUTION_ID:+…}` を含む。漏れたら赤 (§20.2)。
5. テストは「警告が出る / 名乗れば出ない / 出ても exit 0 と書き込みが今と同じ」「文面が `--execution` に触れる (dispatcher の割り当て文・start.sh の起動文)」。

**観察 (本番)**: 生きている全セッション (Worker・Director) が **PR-1 の後に起動** していること (プロセスの起動時刻 > PR-1 の sync。prove-which-code-version-a-spawned-task-ran の 3 点) + 6 コマンドの `caller_check=unverified` が 0 件、を **一定期間** (2 ミッション分または 7 日の長い方。§20.5 の決定 1)。
監査は欠けうる (§1.6 の 11) ので、機械の根拠に **警告の stderr** と、fail / ready-for-verification / verifying / verify-result を 1 本ずつ隔離環境で通した記録を足す。**Director の代理の done / fail / needs-director が 1 本以上 verified で通った** ことも条件に入れる (今は 0 件。表 #1)。
旧プロンプトのまま生きている長寿命のセッションは、読み替えでは救えない (§9.4 の E5 行) — PR-1 の後に再起動する。

**PR-2 (拒否の cutover。ユーザー承認)**
- `_authorize` の名乗りなし + ACTIVE の分岐を `_refuse(EXECUTION_REQUIRED)` に。§20.3 の task-graph の確認。`kai-review.sh` の「JSON が読めない」経路 (:270) の挙動 (拒否されたら `needs_director` に倒れるか、止まるか) をテストで確かめる。
- テスト: 名乗りなし × 6 コマンド × (active / terminal / 試行なし / detached) の表 (`test_execution_e3_caller_table.py` に行を足す)・拒否が何も書かない (card / record / 枠 / task-graph の sha256)・`update` が通る・**`kai-review.sh --skip-pull` の done / needs-director が名乗って通る** (PR-1 の直しの回帰)・既存 3 拒否と新規の 4 つが task-graph を再生成しない (sha256)。**赤の実証** (分岐を戻すと対象のテストだけが赤)。
- **rollback**: revert → `scripts/sync-main-checkout.sh`。戻し先は名乗りなしを通す E3 のコードで、`--execution` も今どおり照合するので、**#272 のような別 PR の互換は不要** (戻しで新しく拒否される呼び出しが無い)。
  新しい `error_code` は plan.sh 以外が読まない (監査の `refused:` 行は文字列のまま残る)。
- **PR-2 の前に「拒否したら何が止まるか」を確かめる**: (a) 観察期間の監査 + 警告の 0 件、(b) 隔離 queue に PR-2 のコードを入れ、Worker の実セッション相当 (素の Bash で pull → done) を worker.md の手順どおりに通して全 6 コマンドが通る、(c) 本番での dry-run は PR-1 の警告の期間がそのまま当たる (拒否しないで警告だけ出す期間)。別の段は足さない。

### 20.5 決定 (2026-10-08, ユーザーが画面で 7 点とも推奨どおり承認)

1. **観察期間** = 2 ミッション分または 7 日の長い方。
2. **警告は stderr のみ**。監査の形は変えない (監査には既に `unverified` が残る)。
3. **Director の done / fail / needs-director も `--execution` 必須** (例外経路を作らない)。Director の代理報告が止まる場面は `update --status pending --reset` で開ける (§20.3)。
4. **target_dir の task は隔離環境で 1 本通して確かめる**。実 target_dir の mission での確認は待たない。
5. **拒否コード `EXECUTION_REQUIRED` を新設する** (`EXECUTION_NOT_FOUND` に寄せない。直し方の文が違う)。
6. **既存の拒否 (`NOT_CURRENT` / `NOT_FOUND` / `ALREADY_TERMINAL`) の task-graph 再生成も PR-2 でまとめて `UsageExit` 型に直す** (§20.3 で再生成は確定)。
7. **ID 持ち越しの構造的な直し (ID を持つラッパー等) は別 mission**。拒否文に ID を出す案は採らない (agent 名から ID を引くのと同じで、§5.2 の捨てた案と同じ理由)。PR-1 / PR-2 の範囲外。

### 20.6 PR-1 の実績 (t005, mission 20261008-e5-reject-unnamed-reports)

- **plan.sh の判定は変えていない**。足したのは stderr の 2 行だけ: (1) 名乗りなしで `PROCEED, UNVERIFIED` になった 6 コマンドの警告 (`lib_task_controller.UNNAMED_WARNING`。`_authorize(warn=…)`、dry_run と retire / reset / G1 の fail には出さない)、(2) pull の `報告には --execution ex-… を付ける`。
- §20.2 の表の対応: dispatcher の割り当て文・start.sh の起動文 (2 か所) → `--execution` と pull の JSON の `execution_id` に触れる / worker.md → 例を `--execution ex-…` に揃え、「env は呼び出しごとに消える」ので pull の JSON の値を会話で持ち越す形を手順の前に (**当初の「同じ呼び出しで `.crewvia-env` を source し直す」形は P1 で撤回 — 下の §20.7**) / worker-codex.md・verifier.md・skills 2 本・director.md → 例と文を実形に (director.md は「付けなくても通る」を「必ず付ける」に) / kai-review.sh `--skip-pull` → card の `current_execution_id` (active のときだけ) を読む、`--execution <id>` でも渡せる、読めなければ警告 (読み方は §20.7 で締めた) / verifier-dispatcher.sh・hooks・scripts/bin/plan → 変更なし。
- 構造テスト `tests/test_execution_e5_doc_examples_name_the_execution.py`: agents/ skills/ の 6 コマンドの例 (fenced の論理行・地の文のインラインコードの行) がすべて `--execution` か `${EXECUTION_ID:+` を含む。直す前の実測は 39 例中 36 が名乗りなし。
- 挙動テスト `tests/test_execution_e5_unnamed_report_warning.py`、互換テスト `test_plan_sh_compat_s3.py` は E5 の stderr 2 行だけを取り除いて golden と比べる。
- **merge 後に dispatcher restart が要る** (割り当て文は dispatcher.sh の python に埋まっている)。Worker / Director は PR-1 の後に起動し直す (§20.4 の観察の条件)。

### 20.7 PR #287 の Codex P1 (t009): 報告の ID は自分の pull の JSON から持ち越す

- **指摘**: 20.6 の手順 (「同じ呼び出しで `.crewvia-env` を source し直す」「`plan status` から取り直す」) は、Director の reset → 別 Worker の再 pull で `.crewvia-env` (plan.sh は worktree を再利用して**新しい試行の ID で上書き**する) と card の今の試行が置き換えの試行の ID になるため、古い Worker がその ID を名乗って照合を通り、置き換えの試行を done / fail できる。**§5.2 の捨てた案 (cwd の worktree の `.crewvia-env` を読んで補う) と同じ穴**で、20.6 はそれを手順として書いてしまっていた。
- **決定**: 報告に使う ID は**その Worker 自身の pull の JSON の `execution_id` だけ**。pull の直後に控え、以後の報告には `--execution ex-…` を**リテラルで**書く (Bash の env は呼び出しごとに消えるので、会話の中で値を持ち越す)。`.crewvia-env` / `plan status` / card の `current_execution_id` から報告用の ID を**取り直す手順は agents/ skills/ README から全部消した** (worker.md の `$EXECUTION_ID` を使う例も `--execution ex-…` のリテラルに)。ID が分からなくなったら取り直さず、名乗りなしで報告する (今は警告で通る) か `needs-director` で Director に聞く。
- **Director の代理報告は例外ではなく別物**: Director は `plan.sh status` で**見て判断した試行を名指し**する (§5.3)。Worker の「自分の報告の ID を取り直す」とは、名乗る主体と意味が違う (Director は reset 後ならその新しい試行を判断し直す立場)。文面でこの違いを書いた。
- **kai-review.sh `--skip-pull`**: card を読むのは**起動時の 1 回だけ**で、`SKIP_EXECUTION_ID` / `EXEC_ARGS` に固定し、報告の時点で読み直さない。報告の時点で試行が替わっていれば、固定した ID を plan.sh が照合して exit 3 で拒否する (置き換えの試行を名乗って通ることは無い)。加えて、card の `worker` が自分 (`--agent`) と違うときは読まない (他の Worker が持つ試行を引き継がない)。`--execution <id>` を渡されたらそれを優先。
- **構造テスト** `tests/test_execution_e5_docs_carry_the_id_from_pull_not_refetch.py`: agents/ skills/ の fenced code (コメント行・ヒアドキュメント本文を除く) と報告コマンドを含むインラインコードに、`--execution` へ変数/コマンド置換を渡す・`EXECUTION_ID` を env/コマンドから代入する・`source .crewvia-env` と報告が同じ文・`current_execution_id` を読む・`plan status` と `--execution` が同じ文、の形が無い。直す前の worker.md で 4 件・crewvia-qa SKILL.md で 1 件が赤。
- plan.sh の env フォールバック (`--execution` が無いとき `CREWVIA_EXECUTION_ID` を読む) は PR-1 では残す。`.crewvia-env` を source した呼び出しで `--execution` を付け忘れると同じ穴が開くので、文書は**常にリテラルの `--execution`** を要求している (フォールバックの扱いは PR-2 の拒否と一緒に決める)。

### 20.8 PR #287 の Codex 2 巡目 (t011): 空の `--execution` は拒否・警告は遷移の検査の後

- **P1 (kai-review.sh)**: `--skip-pull --execution "$X"` で `$X` が空だと、「フラグが無い」と区別がつかず card の `current_execution_id` を採用して名乗っていた。**フラグが渡されたか**を `EXECUTION_FLAG_GIVEN` に持ち、渡されて空 (値が無い末尾の `--execution` も) なら引数の解析の直後に exit 1 (card を読まず・pull / done / needs-director を打たない。plan.sh の `--execution ""` = exit 1 と揃えた)。省略したときだけ card を読む。
- **P2 (lib_task_controller)**: 名乗りなしの警告を `_authorize` の中で出していたので、遷移が拒否される報告 (`mark_task` 経路: 例 in_progress の task への `verifying`、`_finish` 経路を Controller から直接呼んだとき) でも「名乗りなしで報告しました」が出ていた。`_authorize` は警告を出さず、`_warn_unnamed(check)` を**遷移の検査 (`_check_task_status` と試行の遷移表) が全部通った後**にだけ呼ぶ (dry_run は出さない)。plan.sh 経由の done / fail / needs-director は plan.sh が dry_run で先に検査するので、この誤りは `mark_task` 経路でだけ見えていた。
- **同じ型 (検査の前の副作用・出力) の棚卸し**: `lib_task_controller` の `print` / `sys.std*` は `_warn_unnamed` の 1 か所だけ。検査より前に書く操作は拒否の行 (`_refuse` の監査の `refused:` 行) だけで、これは拒否そのものの記録。他の経路に無い。
- テスト: `tests/test_execution_e5_warning_after_transition_check.py` (plan.sh 経由・kai-review.sh) / `..._controller.py` (Controller 直接)。赤の実証: 欠陥版 (警告を `_authorize` に戻す・`-z` で省略扱いに戻す) で 9 + 8 件が赤、直すと全緑。
