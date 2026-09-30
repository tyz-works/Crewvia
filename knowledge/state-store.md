# State Store (vNext 01a) — 設計 (ADR)

vNext 01a の実装 PR (S1〜S5) が従う設計。**コードの根拠はすべて main `e6d6801`**
(改訂案が検証した `f68e323` の 4 コミット後。C1〜C4 が入った版) の行番号。

- 入力: 原案 `~/obsidian/proposals/crewvia/crewvia-vnext-mission-01-control-plane-foundation.md`
  (§3.1 / §6 / §8.2 STATE-01〜06 / §10.1〜10.3 / §11 AC-01 / §14)、
  改訂案 `~/obsidian/proposals/crewvia/20260930_crewvia-vnext-mission-01-revision.md`
  (§2 実測 / §3 / §4-R2・R4・R5・R6 / §7 ユーザー決定)
- ユーザー決定 (改訂案 §7): **R2** — 呼び出し元ゼロの lib は通常どおり merge、
  **呼び出し側を移す PR は merge 前にユーザーの承認を取る** (`plan.sh` は主 checkout
  から直接実行されるので merge = cutover)。env の停止スイッチで新旧を切り替えない
  (不変条件 5)。**R3** — 01a / 01b / 01c の 3 分割
- PR と段の対応: S1 = t004 / S2 = t008 / S3 = t012 / S4 = t016 / S5 = t020、
  本番確認 = t024、記録 = t025

## 0. このミッションでやらないこと (01b / 01c に送る)

| 送り先 | 項目 | 01a での扱い |
|---|---|---|
| 01c | Execution ID (`ex-<hex>`)・attempt 番号・`executions/<id>.yaml` | 作らない。監査ログの `execution_id` 欄は **null 固定で予約**し、今の試行の識別は `generation` 欄 (= card の `started_at`) に出す |
| 01c | done / fail / needs-director の**呼び出し元の照合** (今は誰でも終わらせられる。plan.sh:4084-4092 / :4306-4308) | 変えない。S1 の遷移表は「今どの status から受け付けているか」を写すだけ |
| 01c | 遷移の**幅を狭める** (done が pending から通る・verify-result が pending から通る・fail が needs_director から通る) | 変えない (§1.4 に現状として書く) |
| 01c | `verify-result fail` を新しい試行にする / `update --status in_progress` の世代 | 変えない |
| 01c | pull の冪等化 (同じ Worker の再 pull が同じ task を返す) | 変えない (§2.3 の R-1 は projection を戻すだけ) |
| 01b | worktree 作成失敗で主 checkout に残る (GIT-05。plan.sh:3462) | 変えない |
| 01b | 再 pull で `.crewvia-env` が書き直されない (worktree が既にあると作成が失敗する) | 変えない。S5 は `.crewvia-env` の**書き方** (原子的) だけを直す |
| 01b | branch / base / PR base / fetch 失敗の扱い | 触らない |
| 範囲外 | mission の status 語彙 (drafting / reviewing / ready / in_progress / done) | S1 は task の status だけ。mission 側は同じ作法で後続 |

---

## 1. status の語彙と許可遷移 (S1)

### 1.1 実測: 実際に書かれる status (task card)

| status | 書き手 (e6d6801) | 備考 |
|---|---|---|
| `pending` | add (plan.sh:2935) / update --reset (:5518) / retire outcome=reset (:5885) / propagate_pr_number の blocked→pending (:3982) / update --status | |
| `in_progress` | pull (:3365) / verify-result fail (:4682) / update --status | kai-review.sh も pull 経由 |
| `needs_director` | needs-director (:3812) / retire outcome=needs_director (:5892) | **lint (lint_plan.py:83) と update (plan.sh:5479) の許可集合に無い** |
| `done` | done (:4160) / update --status | |
| `failed` | fail (:4327) / update --status | |
| `ready_for_verification` | ready-for-verification (:4604) / update --status | |
| `verifying` | **verifier-dispatcher.sh:445-448 だけ** (ロックなし) / update --status | |
| `verified` | verify-result pass (:4669) / update --status | |
| `needs_human_review` | verify-result (:4676 / :4684) / update --status | |
| `blocked` | update --status / 手書き (drafting 中。`blocked_reason` 必須: lint_plan.py:85-87) | |
| `skipped` | update --status のみ | Director の「中止」。plan.sh:928 の案内も skipped |
| `verification_failed` | update --status のみ | 書くコードは無い |
| `cancelled` | **書き手なし** (update も拒否) | lib_dep_rules.py:51 / plan.sh:977 / :3848 に現れる |
| `corrupted` | 書かない (読み取りの擬似 status。lib_task_cards.py:101) | |

本番 queue の全カード (missions + archive) の実測 (2026-09-30、読み取りのみ):
done 701 / skipped 38 / pending 34 / blocked 5 / in_progress 2 / needs_human_review 1。
**`cancelled` は 0 件、過去にも書かれたことが無い。**

### 1.2 実測: status の集合が定義されている場所 (コピーの一覧)

| 場所 | 中身 | 問題 |
|---|---|---|
| lint_plan.py:83-89 `VALID_STATUSES` | 11 値 | `needs_director` が無い → plan.sh 自身が書く card が lint FAIL |
| plan.sh:5479-5482 update の `valid_statuses` | 11 値 | 同上 (`needs_director` を書けない) |
| plan.sh:404 `TERMINAL_STATUSES` | done / verified / skipped | dispatcher.sh:206 と**二重定義** |
| dispatcher.sh:206 `TERMINAL_STATUSES` | 同上 | |
| taskvia-sync.sh:297 | `('done','verified','skipped')` のリテラル | 3 つ目のコピー |
| lib_dep_rules.py:51 / :56 | DEAD=(cancelled) / HELD=(failed) | 依存の規則 (単一定義。不変条件 3) |
| dispatcher.sh:215 `RELEASED_WORK_STATUSES` | TERMINAL ∪ DEAD ∪ HELD | |
| plan.sh:908 `ORPHAN_ASSIGNMENT_FINISHED_STATUSES` | 同上 | dispatcher と同じ式の別定義 |
| plan.sh:3071 | 同上 ∪ {pending} | 3 つ目 |
| plan.sh:3848 `PR_NOT_AWAITED_STATUSES` | done / verified / failed / skipped / cancelled / verification_failed | |
| plan.sh:414 `STATUS_ICON` / :966 `TASK_GRAPH_STATUS_MAP` / :1016 `TASK_GRAPH_PANE_STATUSES` | 表示・task-graph | |
| plan.sh:4084-4092 done / :4306-4308 fail / :3807-3809 needs-director / :4598 ready-for-verification / :4651-4653 verify-result / retire `RETIRE_RETIRABLE_STATUS` | コマンドごとの「受け付ける from」 | 受け付ける集合がコマンドごとに違う (§1.4) |
| dispatcher.sh:650 / :664 / :693 / :2040 / :2804 / :2856 / :2901 他 | 個別の比較 | |
| verifier-dispatcher.sh:392 | `== 'ready_for_verification'` | |

### 1.3 決定

**定義を 1 か所に置く: 新モジュール `scripts/lib_task_status.py`。**

- 中身はデータだけ (関数は分類の問い合わせ程度)。I/O を持たない。`lib_dep_rules.py` と同じ作法
  (import して使う。値を並べ直したコピーはテストで落とす)
- 定義するもの:
  - `TASK_STATUSES` — 書いてよい status の全集合 (§1.1 から `cancelled` を除いた 12 値)
  - `TERMINAL_STATUSES` — 依存を満たす完了 (done / verified / skipped)
  - `ASSIGNMENT_HOLDING_STATUSES` — assignment を持ったままの status
    (in_progress / ready_for_verification / verifying / needs_human_review。§2 の projection の定義に使う。
    plan.sh:1016 のコメントの実測と一致)
  - `WAITS_ON_DIRECTOR_STATUSES` — needs_director (dispatcher.sh:664 の問い)
  - `ACCEPTS_FROM[command]` — コマンドごとの受け付ける from の集合 (§1.4)
- `lib_dep_rules.py` は依存の意味 (HELD / DEAD) を持ち続ける。語彙そのものは `lib_task_status` から取り、
  `HELD_DEP_STATUSES ⊆ TASK_STATUSES` をテストで固定する
- `RELEASED_WORK_STATUSES` (dispatcher.sh:215 / plan.sh:908 / :3071) は `lib_task_status` に 1 つだけ置き、3 か所がそれを参照する

**`cancelled` は語彙から消す** (書き手を作る案は捨てる)。
- 理由: 書き手が 0、本番カードも 0。Director の「やらない」は既に `skipped` が担っており
  (plan.sh:928 の案内・依存を満たす・task-graph で `[skip]`)、同じ意味の 2 つ目を作るとまた
  コピーの揃え漏れが生まれる。書き手を作ると `update` / lint / task-graph / dep_rules / Taskvia の
  5 か所を同時に足すことになり、使われない値のために S1 の差分が倍になる
- 影響: `DEAD_DEP_STATUSES` が空になる。空の規則は残さず、定数と分岐 (lib_dep_rules.py:81) を消す。
  `cancelled` と書かれた手書きカードは lint が FAIL し、依存判定は「知らない status = 満たされていない」
  (保留側) に倒れる。0 件なので移行は無い
- 捨てた案: 残して「読めるが書けない」にする — 読み手の分岐だけが生き残り、次に誰かが
  `cancelled` を書いたとき何が起きるか誰も知らない状態が続く

**`needs_director` を lint と update の許可集合に入れる** (2026-09-28 に踏んだバグ)。
`update --status needs_director` は reason 無しで書けてしまうので、`needs_director_reason` の無い
`needs_director` は lint で FAIL にする (`blocked` と `blocked_reason` と同じ作法)。

**`verifying` / `needs_human_review`** は語彙に残す。`verifying` の書き手はロック外の
verifier-dispatcher.sh だけなので、S5 で `plan.sh` のサブコマンド経由に移す (§5)。

### 1.4 許可遷移の表 (S1 は**現状を写す**。狭めるのは 01c)

S1 ではコマンドの受け付ける集合を**変えずに**表へ移す。狭めるのは呼び出し元の照合と一緒に
01c の Controller で行う (照合なしに狭めると、今動いている回復手順 —— Director の
`update --status` → `done` —— がどこで拒否されるか読めなくなる)。
例外は上の 2 点 (`needs_director` を書ける・`cancelled` を消す) だけ。

| コマンド | 受け付ける from (現状) | to | 根拠 |
|---|---|---|---|
| pull | pending (+ 依存・skill・target の条件) | in_progress | plan.sh:3365 |
| needs-director | in_progress | needs_director | :3807-3809 |
| done | done / verified / failed / skipped / needs_director **以外すべて** | done | :4084-4092 |
| fail | done / verified / failed / skipped **以外すべて** (needs_director も通る) | failed | :4306-4308 |
| ready-for-verification | in_progress | ready_for_verification | :4598 |
| verify-result | done / verified / skipped / failed **以外すべて** | verified / in_progress / needs_human_review | :4651-4653 |
| retire | `RETIRE_RETIRABLE_STATUS` (in_progress) | pending / needs_director | :5855 付近 |
| update --status | 任意 (Director の手動操作) | `TASK_STATUSES` のどれでも | :5479 |
| (S5 新設) verifying | ready_for_verification | verifying | verifier-dispatcher.sh:445 の置き換え |

### 1.5 `lib_task_status` を参照すべき箇所 (S1 の作業一覧)

lint_plan.py:83 / plan.sh:404, :414, :908, :966, :1016, :3071, :3807, :3848, :4084, :4306, :4598, :4651, :5479
/ dispatcher.sh:206, :215, :664 / taskvia-sync.sh:297 / lib_dep_rules.py:51, :56, :81 /
verifier-dispatcher.sh:392。
構造テスト: 「status のリテラル集合 (`{'done', ...}` / `('done', ...)`) が `lib_task_status.py` の外に
2 値以上並んでいたら赤」を AST で。**検査したリテラルの件数を出す** (0 件で PASS しない。
memory `registry-dir-single-definition-and-vacuous-static-guards`)。

### 1.6 実装 (S1 / t004) — 設計からの差と、挙動が変わる箇所

`scripts/lib_task_status.py` (データだけ。I/O なし・import なし) に §1.3 の集合を置き、§1.5 の全箇所と
`grep` で足した箇所 (`EXECUTING_STATUSES` = 環境変数の mission を信じてよい `in_progress` / `verifying`、
`PR_NOT_AWAITED_STATUSES`、`PANE` の和) を寄せた。plan.sh の遷移拒否は `refuse_transition()` 1 か所。
構造ガードは `tests/test_task_status_single_definition.py` (`.py` 全部 + `.sh` の heredoc 内 python の
2 値以上の status リテラルを AST で。実測: ブロック 72 / リテラル 1605 / 検出 4 = 全部 allowlist
(ペインの agent status 2・mission の status 1・status 表示の分岐 1。理由付き・該当が無くなったら赤))。

**§1.3 からの差 (実装で決めたこと)**
1. `HELD_DEP_STATUSES` の**定義**を `lib_task_status` に置いた (設計は `lib_dep_rules` に残す案)。
   `RELEASED_WORK_STATUSES = TERMINAL | HELD` を `lib_task_status` に置く以上、HELD が向こうに無いと
   循環 import になる。`lib_dep_rules.HELD_DEP_STATUSES` は同じオブジェクトの再公開で、依存の**意味**
   (`unmet_dependencies` の規則) は従来どおり `lib_dep_rules` だけが持つ。
2. 遷移の拒否の終了コードは **2** (依頼文の受入条件どおり)。これまでの拒否は 1 だった。
3. 依頼文の受入例「done を pending から拒否」は**採らなかった**。§0 / §1.4 が「done が pending から通る」を
   01c の Controller で狭める項目として明示しており、S1 は現状を写す。赤の実証は設計が拒否する遷移
   (語彙に無い status からの done / fail / verify-result / needs-director / ready-for-verification) で行った。
   `update --status` → done の回復手順 (Director) も今のまま通る。

**挙動が変わる箇所 (全部)**

| 箇所 | 前 | 後 |
|---|---|---|
| done / fail / verify-result / needs-director / ready-for-verification が**拒否する**遷移 (done・verified・failed・skipped から、needs-director / ready-for-verification は in_progress 以外から、done は needs_director から) | exit 1 | **exit 2** (何も書かない点は同じ。文面が `<command> は status=… の task には使えません (受け付けるのは: …)` に統一) |
| done / fail / verify-result を、**語彙に無い status** (手書きの `cancelled`・status 欄なし) の card に | 通った (「以外すべて」に含まれた) | **exit 2 で拒否** (本番に該当 0 件) |
| `update --status needs_director` | invalid status | **書ける** |
| lint: `needs_director` の card | FAIL (unknown status) | 通る。**`needs_director_reason` が無ければ FAIL** (`blocked_reason` と同じ作法) |
| `cancelled` の依存 | 満たされた (下流 READY) | 満たされない・保留でもない (待つ)。task-graph は上流 `[status不明]`+blocked / 下流 waiting |
| 手書きの `cancelled` を指す assignment | 「手放した」= 孤児として塞がない・reap 対象 | 知らない status = 塞ぐ側 (証明できない孤児は孤児と扱わない) |
| `update --status cancelled` / lint の `cancelled` | 拒否 / FAIL | 同じ (変化なし) |

逆方向 (今まで拒否されていたのに通るようになる遷移) は **`update --status needs_director` だけ**。
retire (`PRECONDITION_UNMET` = 3)・pull (`pending` だけ) は変わらない。

**本番 queue での lint** (`queue/missions` + `queue/archive` の 65 mission を複製、修正前 / 後の plan.sh で
`plan.sh lint --mission <slug>`): FAIL 7 / WARN 45 で**完全に同一** (diff なし)。FAIL は既存の
`blocked` の `blocked_reason` 欠落 5 と `deliverable` 未宣言 2、WARN は既存の skill 未登録・deliverable・timeout。
本番の task の status は done 706 / skipped 38 / pending 30 / blocked 5 / in_progress 3 / needs_human_review 1 で、
`needs_director` も `cancelled` も 0 件なので、増える理由が無い。

**戻し方**: PR revert → `scripts/sync-main-checkout.sh` (ff とデーモン restart)。dispatcher は常駐なので
restart しないと旧集合のまま (memory `merged-daemon-code-is-inert-until-restart`)。env の停止スイッチは付けない。
card は 1 バイトも書き換えていないので、戻しても旧コードがそのまま読める (`needs_director_reason` を
lint が要求するのは新しい lint だけで、旧 lint は `needs_director` を unknown status と FAIL にするのが従来どおり)。

---

## 2. 複数ファイル更新の crash モデル (S4)

### 2.1 方式: カードが正本、assignment と `.identity` が projection

採用: 原案 STATE-04 の選択肢 2 (単一の正本 + 再生成できる projection)。改訂案 §4-R4 の推奨どおり。

- **正本 (authority)**: task card (`queue/missions/<slug>/tasks/tNNN.md`) の `status` / `worker` / `started_at`
- **projection**: `queue/assignments/<agent>` (本文 `<mission>:<task>`) と `<agent>.identity` (JSON)
- **projection の定義** (lib_task_status の語彙で書く):
  card が `in_progress` かつ `worker = A` かつ `started_at = G` のとき、
  `assignments/A` = `<slug>:<tid>` かつ `A.identity.started_at = G` であるべき
- **書く順序の規則**: 1 つのコマンドの中で、**正本の書き込みがコミット点**。
  - 始める側 (pull): 正本を先に書き、projection を後で書く (今の plan.sh:3365-3375 の順のまま)
  - 終わらせる側 (done / fail / needs-director / update --reset / retire): 正本を書き、projection を後で消す (今の順のまま)
  - 正本以外の**派生値** (依存先カードの `pr_number`) は、正本より**前に**書く (done の順を変える。§2.2)。
    ただし派生値を書き始める**前に、その値の出どころ (自分の card の `pr_number`) を正本に永続化する**。
    派生値だけが先に残ると、再実行が違う値を渡したときに正本と派生値が別の値で確定する (§2.2 done の D1)
  - どこで落ちても「正本が書かれる前 = コマンドが起きなかった」「正本が書かれた後 = コマンドが起きた」の
    どちらかになり、後者の projection の欠けだけを次のロック取得時に作り直せばよい

**ジャーナル方式 (選択肢 1) を採らない理由**:
1. 回復に要る情報は正本 (card) に既にある。ジャーナルは同じ事実の 2 つ目の置き場で、
   「ジャーナルと card が食い違う」という新しい状態を生む (族 C: 新しいガードが新しい失敗状態を作る。
   memory `a-new-guard-creates-a-new-state`)
2. PREPARED / COMMITTING / COMMITTED の marker 自体の書き込み・fsync・掃除が crash 注入点を増やす
   (原案 §10.3 の 7 点のうち 3 点はジャーナルがあるから生まれる点)
3. 01a の複数ファイル更新は pull / done / add の 3 種で、どれも「正本 1 枚 + 派生」に並べ替えられる。
   並べ替えで閉じないもの (§2.2 の「報告のみ」) は、ジャーナルがあっても推測修復になる
- 採る条件 (将来): 正本が 2 枚以上に分かれる操作 (01c の Execution record + card) が現れ、
  どちらも他方から再生成できないと分かったとき。01c の ADR で再判断する
- 原案 STATE-04 の「PREPARED / COMMITTING / COMMITTED を識別できる」は、
  正本の status と projection の有無の組で識別する (PREPARED = 正本未書き込み、
  COMMITTING = 正本あり・projection 未完、COMMITTED = 両方一致)。**原案 §14-14「per-file atomic
  replace だけを multi-file transaction と呼ぶ」には当たらない** —— 呼ばない。
  「per-file の原子的書き込み + 書く順序 + 正本からの projection 再生成」がこの方式の名前

### 2.2 途中で落ちた点ごとの結果 (次のロック取得時)

凡例: **R-n** = §2.3 の回復規則。「報告のみ」= 書かずに見える形で知らせる (推測修復しない)。

**pull** (card → identity → assignment。plan.sh:3365-3375。lock 外で Taskvia・worktree・`.crewvia-env` :3425-3465)

| 落ちた点 | 残る状態 | 次のロック取得時 | 根拠 |
|---|---|---|---|
| card を書く前 | 変化なし | 何もしない | |
| card の後・identity の前 | card in_progress (A, G) / identity なし or 前の世代の残骸 / assignment なし | **R-1**: identity(G) → assignment を書く | 正本が「A が G で持っている」と言っている |
| identity の後・assignment の前 | card (A, G) / identity(G) / assignment なし | **R-1** (identity は同じ内容で上書き、冪等) | |
| assignment の後・lock 解放の前 | 一致 | 何もしない | |
| lock の外 (Taskvia / worktree / `.crewvia-env`) | 一致。worktree・env が無い | queue としては正常。worktree の欠けは 01b (GIT-05) | Worker は JSON を受け取っていない |

**done** (S4 で順を変える)

| 段 | 書くもの | 意味 |
|---|---|---|
| D0 | (書かない) 前提の検査。今の検査 (:4084-4135) に 2 つ足す: **(a)** 伝播先のうち「上流の `deliverable: pr` がこの task 1 つだけ」の card に、`--pr N` と**違う** pr_number が既にあれば拒否 (exit 3、何も書かない。食い違う card を列挙)、**(b)** 自分の card に pr_number があるのに `--no-pr` なら拒否 | 再実行で違う番号・「PR なし」を渡されたときに、前の実行が残した派生値と食い違ったまま完了させない |
| D1 | 自分の card の `pr_number = N` だけ (status は in_progress のまま) | **番号の永続化**。以後、正本が「この task の PR は N」と言う。同じ N の再書き込みは冪等 (:4103-4110 が同値を許す) |
| D2 | 依存先の pr_number (propagate_pr_number :3943) | 派生値 |
| D3 | 自分の card `status = done` (+ Result・completed_at) | **done のコミット点** |
| D4 | assignment 撤去 | projection |
| D5 | mission done | 派生値 (報告のみの対象) |

`--no-pr` / 番号なしの done は D1・D2 を飛ばす (D0 → D3)。

| 落ちた点 | 残る状態 | 次のロック取得時 | 根拠 |
|---|---|---|---|
| D1 の後・D2 の途中 | card in_progress・**pr_number = N** / 依存先の一部に N | 何もしない。再 done で完了 (次の §2.6 の表) | 依存先は blocked_by が満たされないので dispatch されない。正本と派生値は同じ N で、食い違いは「未伝播の依存先がある」だけ |
| D3 の後・D4 の前 | card done / assignment が done の task を指す | **R-2**: 孤児として撤去 | 今の reap-orphan-assignment と同じ判定 (`ORPHAN_ASSIGNMENT_FINISHED_STATUSES` :908 / cmd_reap_orphan_assignment :5909) |
| D4 の後・D5 の前 | 全 task 終端 / mission in_progress | **報告のみ** (store-check に出す) | `update --status done` でも同じ状態が今でも正当に作れる (update は mission 完了を判定しない)。正当な状態と区別できないので書かない |
| (現状の順のまま落ちた場合) card done の後・伝播の前 | card done / 依存先に pr_number 無し | **報告のみ** (dispatcher.sh:2286 の既存通知がそのまま出る) | Director が `update --pr-number null` で消した状態と区別できない。**だから順を変える** |

**D1 と D0 (a) の両方を置く理由** (PR #255 Codex 1 巡目 P2-1): 伝播を正本より前にしただけだと、
`done --pr 123` が D2 の途中で落ちたあと `done --pr 456` で再実行すると、自分の card には番号が無いので
:4103 の食い違い検査を通り、propagate_pr_number は設定済みの依存先を飛ばす (:3970) ので、
**正本は 456・レビュー対象は 123** で完了する。同じ引数での冪等性だけでは防げない。
- D1 で番号を先に正本へ置けば、違う番号の再実行は :4103 がそのまま拒否する (新しい検査は要らない)
- それでも正本の番号は Director が `update --pr-number` で書き換えられる (やり直しの PR を作った・
  打ち間違いを直した)。そのあと `done --pr 456` を打つと、正本は 456 で依存先には 123 が残っている。
  これを拾うのが D0 (a)。**今は「Director が手で入れた値を上書きしない」で黙って飛ばしている**
  (:3970) が、「上流がこの task 1 つだけ」の依存先で値が食い違うのは、どちらかが古い番号である以外に
  説明が無い。飛ばすと P2-1 と同じ結果になるので、S4 で拒否に変える。出口は拒否メッセージに出す:
  依存先を `update <dep> --pr-number <正しい番号>` で直してから再 done (どちらの番号が正しいかは
  自動で決めない)
- 合流点 (上流に pr の task が 2 つ以上) の依存先は今のまま書かない・拒否もしない (:3973。番号が一意でない)

**その他の複数ファイル更新**

| コマンド | 順序 | 落ちた点 | 次のロック取得時 |
|---|---|---|---|
| fail / needs-director / retire --outcome needs-director | card → assignment 撤去 (:4345→:4357 / :3816→:3826 / :5898→:5899) | card の後 | **R-2** (card が in_progress でなくなった → assignment は孤児。card の worker は残るので旧所有者は card から分かる) |
| update --reset / retire --outcome reset | card pending・**worker / started_at を null** (:5518-5521 / :5885-5888) → assignment 撤去 (:5625 / :5899) | card の後 | **R-2**。ただし **card から旧所有者が消えている** ので、旧所有者の枠は §2.5 の**逆引き** (`assignments/` のうちこの card を指す枠) でしか見つからない |
| add | card → mission `next_task_id` (:2949→:2953) | card の後 | **R-3**: `next_task_id` を「既存の最大 tNNN + 1」以上に進める。**今は次の add が同じ tNNN を黙って上書きする** (:2926-2927 に存在確認が無い) |
| verify-result pass / ready-for-verification | card → (mission done) | card の後 | mission 側は「報告のみ」(done と同じ) |
| archive | `shutil.move(mission)` → state.yaml (:5199→:5202) | move の後 | **R-4**: state.yaml の `active_missions` に在るが `missions/` に無く `archive/` に在る slug を外す |
| retirement marker (lib_retirement) | 自前の R1 プロトコル (marker が先) | — | 01a では触らない (§5) |

### 2.3 回復規則 (これ以外は書かない)

| 規則 | 条件 (すべて満たすときだけ書く) | 書くもの |
|---|---|---|
| **R-1** projection を作る | card が `in_progress`・`worker = A` (非空)・`started_at = G` (非空) / `assignments/A` が **ABSENT** (classify_assignment :2058 が `ENOENT` だけを ABSENT にする。:2084) / **所有の証拠**: `worker = A` かつ `in_progress` の card が、`queue/missions/` の全 mission でこの 1 枚だけだと**読んで確かめた** (§2.5「所有の証拠の走査」。読めない card が 1 枚でもあれば確かめられない = 書かない) | `A.identity` (G) → `assignments/A`。`publish_assignment` と同じ関数 |
| **R-2** 孤児 projection を消す | (枠 A は、呼び出し元の枠・card の worker の枠・**card を指す枠の逆引き** (§2.5) のどれで見つけてもよい) `assignments/A` が指す card が、**assignment を撤去するコマンド自身の書く status** のどれか: 手放し済み (`ORPHAN_ASSIGNMENT_FINISHED_STATUSES` :908 = done / verified / skipped / failed。今の `reap-orphan-assignment` :5909 の判定) ∪ needs_director (needs-director / retire) ∪ worker が空の pending (update --reset / retire reset) | assignment → identity の撤去 (`retire_assignment` :2239。classify を通す) |
| **R-3** 採番を進める | `tasks/` に `next_task_id` 以上の tNNN.md がある | mission の `next_task_id = max + 1` |
| **R-4** archive 済みを外す | slug が `active_missions` に在り、`missions/<slug>` が無く `archive/<slug>` が在る | state.yaml から外す (`default_mission` も archive と同じ規則で) |

**R-2 を「holding でない status すべて」にしない理由**: Director が作業中の card に `update --status blocked` を付けるのは今でも正当な操作で、そのとき Worker は動いており assignment は残っている。「holding でない」で消すと、その Worker が dispatcher から idle に見え、退役 = **動いている Worker を殺す**経路になる (memory `destruction-needs-provenance-not-classification`)。消してよいのは、撤去を伴うコマンドが書く status —— つまり「このコマンドが途中で落ちた」以外に説明の無い組 —— だけ。それ以外 (blocked / verification_failed / ready_for_verification 以外の手書きの status 等) で assignment が残っているものは store-check が報告するだけ。

**R-1 の条件を `in_progress` だけに絞る理由**: 正本→projection の欠けが crash で生まれるのは
pull の窓だけで、pull が書く status は in_progress だけ。ready_for_verification / verifying /
needs_human_review で assignment が無い状態は crash では作れないので、見つけたら「表に無い食い違い」
(§2.5) として報告する。

**R-1 で A の枠が ABSENT でないとき** (`assignments/A` が別の task を指す):
- 指す先が手放し済み → R-2 で消してから R-1 (今の publish と同じ扱い: agent_busy_elsewhere :3032 の docstring :3047)
- 指す先も in_progress (A が 2 枚の card を持つ) / UNVERIFIABLE → **書かない**。§2.5 の「報告のみ」
  (コマンドは止めない。今の前提検査がそのまま効く —— 例: pull は agent_busy_elsewhere :3032 が拒否する)

**R-1 で A の枠が ABSENT でも、A が in_progress の card を 2 枚持つとき** (PR #255 3 巡目 P2-a):
枠が無いので上の「指す先」からは 2 枚目が見えない。名指しの card だけを見る範囲 (`done` / `update` /
`pull --task` 等) で R-1 の条件を判定すると、2 枚目を知らないまま A の枠を 1 枚目に向けて書いてしまい、
「2 枚持つときは書かない」の約束が pull の検証 (agent_busy_elsewhere) の中でしか守られない。
だから R-1 の条件に**所有の証拠**を入れる: 書く直前に、`worker = A` の in_progress card を全 mission で数え、
名指しの card 1 枚だけのときだけ書く。2 枚以上 → `reported:duplicate_owner` (両方の card を列挙)、
読めない card がある → `reported:owner_unprovable` (読めない card を列挙)。どちらも**書かずに**コマンド本体へ進む。
- 書かない側に倒す理由: 2 枚のうちどちらが A の本当の作業かを正本は言っていない。どちらかに枠を向けると
  もう片方は「A が持っているのに dispatcher から見えない」になり、推測修復になる。書かなければ
  状態は crash の直後と同じ (今も回復が無いので、今より悪くならない)
- 出口: 片方への `update --reset` / `done` / `fail` (これで 1 枚になり、次のロック取得で R-1 が書く)。
  読めない card は Director の手編集 + `plan.sh lint` (§2.5 の `[破損]` の行と同じ)

**回復はコマンドの前提検査より前に走り、コマンドが拒否されても残る** (PR #255 1 巡目 P2-2 / P2-3 の族の掃除)。
回復は「ロックを取った直後、そのコマンドが前提を検査する前」に 1 回走り、修復は**それ自体で完結した書き込み**
として監査ログに `op=recover` の行を残す。その後にコマンド本体が今の前提検査で拒否されても (例: 同じ
`update --reset` を打ち直したら card は既に pending・同じ `needs-director` を打ち直したら in_progress でない)、
修復は巻き戻さない。コマンドの「exit 3 は 1 バイトも書かない」は**コマンド本体の書き込み**についての約束で、
回復の書き込みは含まない。
- こうしないと「同じ引数で再実行」が前提検査で弾かれ、回復が一度も走らずに projection の欠け / 孤児が残る
- 回復は正本を書かない (§2.4 の 2.) ので、コマンドが拒否された後に残るのは「正本が既に言っていることに
  projection を合わせた」結果だけで、コマンドが起きたかどうかの答えは変わらない

**回復の記録はコマンド本体の記録と別の経路で、回復の中で即時に残す** (PR #255 3 巡目 P2-b)。
コマンド本体の監査ログの行は「トランザクションが例外なく抜けたとき」に書く (§4)。今の拒否は `die` =
`SystemExit` で、トランザクションを**例外で**抜ける。回復の行をこの経路に乗せると、crash 後の再実行
(例: 同じ `needs-director`) が回復で孤児の枠を消し、前提検査で `die` し、**修復は残るのに `op=recover` の行は
残らない**。だから:
- `Txn.recover()` は、修復 1 件の書き込みが終わるたびに、その `op=recover result=repaired:R-n` の行を
  **その場で (ロックの中で) 監査ログに追記してから**次の修復に進む。報告 (`reported:<理由コード>`) も同じく
  その場で追記する。recover() が戻った時点で、回復の行はすべてファイルにある
- だからその後にコマンド本体が `die` しても、`StoreWriteError` で落ちても、回復の行は消えない。
  本体の行 (`op=<サブコマンド>`) だけが今どおり「例外なしのときだけ」
- 残る欠け: 修復の書き込みの後・その行の追記の前で落ちると 1 行欠ける。§4「crash で行が欠ける」と同じ
  許容で、欠けるのは最大 1 行 (修復ごとに追記するので、まとめて最後に追記する案より少ない)
- 行の順序: 回復の行も本体の行もロックの中で追記するので、ファイルの順 = 書き込みの順のまま (§4)

**Director の手動操作との衝突**: `update --status in_progress` (--reset なし) は worker / started_at を
残したまま開き直せるので、R-1 の条件を満たし、前の worker 名で assignment が作られうる。
- これは害の小さい側に倒れる (Worker を殺さない。dispatcher から busy に見え、Rule 5 /
  vanished 検出が Director に知らせる)
- S1 で `update --status <ASSIGNMENT_HOLDING_STATUSES のどれか>` に `--reset` か `--worker` を
  要求するかは**決めない** (受け付ける集合を変えない方針。§1.4)。S4 の QA (t017) でこの経路を
  1 行として観察し、害があれば 01c に送る

### 2.4 回復が新しい正しいトランザクションを巻き戻さないことの論証 (原案 §12.1)

1. **回復はロックの中でしか走らず、全書き込みもロックの中でしか走らない** (S3・S5 の後。
   構造ガード §6 が「ロックの外の書き込み」を CI で止める)。だから回復が走っている間に
   「途中のトランザクション」は存在しない。回復が見る食い違いは、**完了したトランザクションの結果か、
   落ちたトランザクションの残骸**のどちらかしかない
2. 回復は**正本を書かない** (R-3・R-4 は mission / state の派生値。task card の status / worker /
   started_at には一切書かない)。正しいトランザクションの結論は正本にあるので、それが巻き戻る経路が無い
3. R-1 は「正本が言っていることに projection を合わせる」だけ。R-2 は「正本が手放したと言っている
   projection を消す」だけ。どちらも**後任のもの**を消さない: R-2 の撤去は `classify_assignment` を
   通すので、同じ task を後任が pull し直していれば ASSIGN_SUCCESSOR / MINE の判定で守られる
   (今の reap と同じ。classify_assignment :2058 / cmd_reap_orphan_assignment :5909)
4. 冪等: R-1〜R-4 は条件が成り立たなくなる状態へ書くので、2 回目は何もしない
5. 残る穴 (閉じないと明言する): ロックの外の書き手が残っている間 (S5 より前) は 1. が成り立たない。
   だから **S4 は S3 の後、S5 の前**という順で、S4 の R-1 / R-2 は plan.sh の書き手しか相手にしない
   (verifier-dispatcher の `verifying` と pre-compact の本文書き込みは status / worker / started_at の
   組を変えない or 変えても R-1 の条件外 —— verifying は in_progress ではない)

### 2.5 回復の走査範囲と、食い違いの扱い

**走査範囲: そのコマンドが名指ししている task・その task を指す枠・呼び出し元の Worker の枠だけ。**
mission 全体・queue 全体の card は走査しない。**例外は 1 つだけ**: R-1 が書く**直前**の「所有の証拠の走査」
(下)。R-1 の他の条件 (card in_progress・worker 非空・枠 ABSENT) が範囲内で揃ったときにしか走らない。

| コマンド | 回復の対象 |
|---|---|
| pull (--task なし) | 呼び出し元 A の `assignments/A` と、A が worker の in_progress card (agent_busy_elsewhere :3052-3056 が `mission_search_order` の mission を読む —— `--mission` 付きならその 1 つだけ :2972。**所有の証拠には使わない**。R-1 を書くなら下の走査を別に行う) + 選んだ card + 選んだ card を指す枠 (逆引き) |
| pull --task / done / fail / needs-director / ready-for-verification / verify-result / update / retire | 名指しの card + その card の worker の枠 + **名指しの card を指す枠 (逆引き)** + 呼び出し元の枠 |
| add | その mission の `tasks/` の列挙 (R-3) |
| archive / init | state.yaml (R-4) |
| reap-orphan-assignment | 名指しの Worker の枠 (R-2 の条件は §2.3 に揃える。今の :5909 は needs_director と worker が空の pending を消さない —— S4 で R-2 と同じ集合にする) |

**逆引き** (PR #255 1 巡目 P2-2): `queue/assignments/` の本体ファイル (`.identity` と `.restarting` を除く) を
列挙し、1 行の本文が `<slug>:<tid>` に一致する枠を拾う。`update --reset` / `retire --outcome reset` は
assignment を撤去する**前に** card の worker / started_at を null にする (:5518-5521 / :5885-5888) ので、
その間で落ちると card からは旧所有者が分からない。card の worker だけを見る範囲では、Director が同じ card を
打ち直しても旧所有者の枠に届かず R-2 が走らない。別の Worker が pull すると旧所有者の枠が残ったままになる。
- 列挙するのは `assignments/` の 1 ディレクトリだけで、件数は同時に存在する Worker の数 (十数件) で頭打ち。
  各ファイルは 1 行。queue 全体の card の走査とは桁が違うので、理由 1 に反しない
- 逆引きで読めない枠 (UNVERIFIABLE) は「この card を指していない」と証明できないが、**消さない・止めない**
  (下の「報告のみ」)。読めない枠は今でも publish の前に agent_busy_elsewhere が拒否するので、その Worker の
  次の pull で表に出る
- 捨てた案: reset の順序を変える (assignment 撤去 → card)。撤去の後・card の前で落ちると card は A の
  in_progress のままなので R-1 が枠を作り直し「reset は起きなかった」に戻るが、その間 dispatcher から A が
  idle に見え、別 task の送信・退役の判定に乗る (終わらせる側は正本が先、の §2.1 の規則にも反する)
- 捨てた案: card に `reset_from_worker` を残す。正本に「回復のためだけの欄」を足すと、欄の掃除と
  「欄と assignment が食い違う」状態が新しく生まれる (族 C)

- 理由 1: **ロックを握る時間を増やさない**。queue 全体の走査を毎回入れると、全 Worker と両デーモンの
  plan.sh が待たされる (maybe_refresh_task_graph :1873 が同じ理由でロックの外にある)
- 理由 2: **1 枚の事故で全体を止めない** (t009 以来の向き)。他の mission の壊れた card が、
  無関係な Worker の done を止める経路を作らない
- 理由 3: t024 の本番観察の波及を切る。t024 は本番の他の mission / 並行する Worker に書き込まない

**所有の証拠の走査** (PR #255 3 巡目 P2-a。R-1 の条件の最後の 1 つ。§2.3):
- **読む範囲**: `queue/missions/` の全 mission (`active_missions` に在るかを問わない。`init --inactive` や
  一時停止で外した mission にも in_progress の card は残りうる) の `tasks/tNNN.md`。`archive/` は読まない
  (archive は mission が終わった後にしか動かない)。読むのは frontmatter の `status` と `worker` だけ
- **数えるもの**: `worker = A` かつ `status = in_progress` の card。名指しの card 1 枚だけ → R-1 を書く。
  2 枚以上 → 書かない (`reported:duplicate_owner`)。`[破損]` の card が 1 枚でもある → その card の worker を
  読めないので「A のものでない」と証明できない → 書かない (`reported:owner_unprovable`)
- **agent_busy_elsewhere の読みを流用しない理由**: あれは pull の検証で、読む範囲が `mission_search_order`
  (`--mission` 付きならその mission だけ)。R-1 の証拠としては狭い。pull の検査の範囲は変えない (拒否を足さない)
- **理由 1 (ロック時間) との両立**: 走るのは「card は A の in_progress なのに A の枠が無い」ときだけで、
  これは crash の残骸 (pull の窓で落ちた) か Director の `update --status in_progress` の直後にしか無い。
  平常時のコマンドは R-1 の他の条件で先に外れ、この走査は 1 回も走らない (安いスキップを高い判定より前に置く。
  memory `cheap-skip-must-precede-expensive-gate`)
- **理由 2 (1 枚の事故で全体を止めない) との両立**: 読めない card があっても**コマンドは止めない**。止まるのは
  R-1 の書き込みだけで、状態は crash の直後のまま (今は回復そのものが無いので、今より悪くならない)。
  報告の行が読めない card を名指しするので、Director が直す場所は分かる
- 捨てた案: R-1 を「名指しの card に枠を向ける」まで緩め、§2.3 の「2 枚持つときは書かない」を捨てる。
  2 枚のどちらが A の作業かを正本は言っていないので、推測修復になる

**「表に無い食い違い」のとき: 報告だけして、コマンドは止めない** (PR #255 1 巡目 P2-3)。
範囲内で §2.3 の条件に合わない食い違いを見つけたら、stderr に 1 行・監査ログに
`op=recover result=reported:<理由コード>` を 1 行残し、**書かずに**コマンド本体へ進む。コマンド本体は
**今の前提検査 (§1.4 の表) のまま**受け付けるか拒否する。回復は拒否を 1 つも足さない。

旧案 (範囲内の食い違いで exit 3) を捨てた理由: 食い違いの多くは §1.4 で維持するコマンドが**正常に作れる**
状態で、それを「破損」として全操作を止めると、その状態から抜ける操作 —— Director の `update --reset`・
`verify-result pass` —— まで拒否され、**復旧の出口が 1 つも無い状態**ができる。例: pending の card に
`verify-result needs_human_review` (今は許可 :4652) → needs_human_review で assignment 無し →
旧案では update も verify-result も exit 3。正常に作れる状態を破損と呼ばず、読めない状態 (UNVERIFIABLE /
`[破損]`) は今の各コマンドの扱い (消さない・書かない) にそのまま任せる。

| 食い違い (範囲内) | 正常な操作で作れるか (作る操作) | 回復 | 出口 (そのまま受け付けられるコマンド) |
|---|---|---|---|
| ready_for_verification / verifying / needs_human_review で assignment が無い | 作れる (`update --status` :5479 / pending・blocked 等への `verify-result needs_human_review` / verify-result fail が max_rework に届く :4675) | 報告のみ | `verify-result pass/fail/needs_human_review`・`update --reset`・`update --status`・`done` (Director) |
| in_progress で worker が空 | 作れる (worker の無い card への `update --status in_progress` / `verify-result fail` :4682) | 報告のみ (R-1 は worker が非空のときだけ) | `update --reset`・`update --worker <A>` (→ 次のロック取得で R-1)・`done` / `fail` (Director)・`verify-result` |
| A が in_progress の card を 2 枚持つ | 作れる (`update --status in_progress --worker A`) | 報告のみ (R-1 は書かない §2.3。枠が ABSENT でも所有の証拠の走査が `duplicate_owner` で止める —— どのコマンドの回復でも同じ) | 片方への `update --reset`・`done` / `fail`。A の `pull` は今の agent_busy_elsewhere が拒否 (今と同じ) |
| assignment が blocked / verification_failed / ready_for_verification 等 (R-2 の集合の外) を指す | 作れる (`--reset` なしの `update --status` —— Worker は動いている) | 報告のみ (R-2 にしない理由は §2.3) | `update --reset` (card の worker の枠を今の :5625 で撤去)・`update --status <R-2 の集合>` → 次のロック取得で R-2・`reap-orphan-assignment` (R-2 の集合に入った後) |
| assignment / identity が UNVERIFIABLE (読めない・旧形式・形違い) | crash では作れない (書き手はすべて原子的)。権限・手作業・旧 plan.sh | 報告のみ (消さない。classify_assignment :2058 の今の結論) | その Worker の `pull` は今どおり拒否。card 側の操作 (`done` / `fail` / `update --reset` / `verify-result`) は card を書いて枠を残す (今どおり warn)。**枠の出口は Director の手作業での退避** (今と同じ。読めないものを plan.sh に消させる経路は 01a で足さない —— 証拠の無い破壊になる) |
| card が `[破損]` | crash では作れない (atomic write)。手編集 | 報告のみ | card を名指しするコマンドは今どおり読めずに止まる (今と同じ)。出口は Director の手編集 + `plan.sh lint` |
| 全 task 終端で mission が in_progress | 作れる (`update --status done`) | 報告のみ (§2.2) | 今と同じ (`archive` 等) |

- **01a が出口を減らさないことの論証**: 回復は拒否を足さず (上)、書くのは R-1〜R-4 の projection と派生値だけで
  正本を書かない (§2.4)。だから、ある状態で受け付けられるコマンドの集合は S4 の前と同じ (§1.4 の表)。
  S4 で拒否が増えるのは done の D0 (§2.2) の 2 つだけで、どちらも拒否メッセージに出口 (`update --pr-number`)
  を書き、`update` 自体は拒否されない
- 止まる範囲も今と同じ: 回復は範囲外を読まないので、範囲外の食い違いがコマンドに影響する経路は無い。
  **本番の全 plan.sh 呼び出しが止まる経路は作らない**

**本番で観察する手順 (t024 向けの設計の制約)**:
- 読み取り専用の `plan.sh store-check [--mission <slug>]` を S4 で足す (`QUEUE_READONLY_SUBCOMMANDS`。
  ロックを取らない。R-1〜R-4 と「表に無い食い違い」を**書かずに**列挙し、件数を出す)。
  ロック無しなので途中のトランザクションを食い違いとして 1 回見うる → 「2 回連続で出たものだけが本物」と出力に書く
- t024 は本番で store-check を叩くだけ。crash の注入は**本番では行わない** (原案 §14-2)。
  注入は t013 / t017 の隔離 queue (CREWVIA_QUEUE を付け替えた複製) でだけ行う

### 2.6 族の表: 途中で落ちる各点 × 再実行 (PR #255 1 巡目の族の掃除)

1 巡目の P2 ×3 は同じ族 —— **複数ファイル・複数段の操作の途中で落ちたときに、正本 / projection / 回復の出口の
どれかが失われる** —— だった。§2.2 は「次のロック取得時」だけを見ており、**その次に来る操作が何か**
(同じ引数の再実行 / 違う引数の再実行 / 別の操作) を見ていなかった。3 件ともそこから漏れた。
下の表で全操作についてそれを並べる。各行で確かめること: **(正本)** 正本が一意に決まる (card が言っている
ことが唯一の答えで、派生値・projection はそれに合わせられるか、食い違いが拒否メッセージか報告で見える)、
**(出口)** その状態から抜けるコマンドが少なくとも 1 つ受け付けられる。

前提 (§2.3 末尾): 回復はコマンドの前提検査より前に走り、コマンドが拒否されても修復は残る。

3 巡目 (P2-a / P2-b) で 2 列を足した。1・2 巡目の表は「修復が何を書くか」を並べたが、**修復が何を根拠に
書くか**と**修復の記録がどの経路で残るか**を並べていなかった。2 件ともそこから漏れた:
- **(証拠)** 「回復が根拠にする証拠」列: 回復がその行で読むカード・枠の全部。R-1 を書く行には必ず
  「所有の証拠の走査」(§2.5) が入る。名指しの card だけで R-1 を書く行があれば P2-a の再発
- **(記録)** 「回復の記録の経路」列: 修復の行は `recover()` の中で即時に追記され (§2.3 末尾)、本体の
  拒否・例外と無関係に残る。「本体が例外なしで抜けたときに書く」経路に回復の行が乗っている行があれば P2-b の再発

| 操作 | 落ちた点 | 同じ引数で再実行 | 違う引数で再実行 | 別の操作 | 回復が根拠にする証拠 (読むカード・枠) | 回復の記録の経路 | 正本 / 出口 |
|---|---|---|---|---|---|---|---|
| pull | card の後・枠の前 | `pull --task` 同じ card: 回復が R-1 で A の枠を作る → card が pending でないので今どおり拒否。`pull` (--task なし): R-1 → agent_busy_elsewhere が「A は in_progress の card を持つ」で拒否 | 別の `--task`: R-1 → 同じ拒否 | 別 Worker B の `pull --task` 同じ card: 逆引きで A の枠を確認 (R-1 済みなら A のもの) → pending でないので拒否。Director `update --reset`: R-1 → reset が card を pending に・:5625 で A の枠を撤去 | 名指し (選んだ) card (A, G) + `assignments/A` (ABSENT) + 逆引き (この card を指す枠は無い) + **所有の証拠の走査** (全 mission の `worker = A` の in_progress がこの 1 枚) | R-1 の `repaired:R-1` を `recover()` の中で即時追記 → その後の pull の拒否 (`die`) でも残る。2 枚目がある / 読めない card があれば `reported:duplicate_owner` / `owner_unprovable` を同じく即時 | 正本 = card (A, G)。Worker は JSON を受け取っていないので「持っているのに知らない」。これは今と同じで、dispatcher の Rule 5 (idle-with-task) が Director に上げる。出口: Director の `update --reset` |
| pull | lock の外 (Taskvia / worktree / env) | queue は一致。再 pull は上と同じく拒否 | 同左 | 同左 | 選んだ card + `assignments/A` (一致) + 逆引き。一致なので書く修復が無い | 回復の行なし。pull 本体の行は前回の実行が例外なしで抜けたときに既に書かれている | 正本・projection とも一致。worktree の欠けは 01b (GIT-05)。出口: `update --reset` |
| done | D0 | 何も書いていない | — | — | 名指し card + その worker の枠 + 逆引き + 呼び出し元の枠 (食い違い無し) | 回復の行なし。D0 の拒否は書かない (§4「拒否・報告を書くか」) | 起きなかった |
| done | D1 の後・D2 の途中 | D0 通過 (依存先の値は N か空) → D1 冪等 → D2 が残りを埋める → 完了 | `--pr M`: :4104 が拒否 (正本は N)。`--no-pr`: D0 (b) が拒否 | `fail`: card failed・pr_number N は残る。依存先の N は正本と一致 (held なので dispatch されない)。`update --reset` → 再 pull → 新しい PR M で done: :4104 が拒否 → `update --pr-number M` → 再 done: D0 (a) が依存先の N を列挙して拒否 → 依存先を `update --pr-number M` → done | 名指し card (in_progress・枠あり) + 枠。R-1 / R-2 の条件外。依存先の pr_number は**回復の対象外** (D0 が本体の検査として読む) | 回復の行なし。done 本体の行は再実行が D3 まで例外なしで抜けたとき | 正本の番号 = N、Director が書き換えれば M。**依存先と正本が食い違ったまま done が通る経路は無い** (D0 (a))。出口: `update --pr-number` (自分 / 依存先)。これが P2-1 の行 |
| done | D3 の後・D4 の前 | 回復が R-2 (A の枠) → 「already done」で今どおり拒否 | 同左 | dispatcher の `reap-orphan-assignment` / A の次の `pull`: R-2 | 名指し card (done) + card の worker の枠 (A。classify で名指しの card を指すと確かめる) + 逆引き | R-2 の行を `recover()` の中で即時追記 → 本体の「already done」(`die`) でも残る | 正本 = done。出口: 回復が自動で閉じる |
| done | D4 の後・D5 の前 | 「already done」で拒否 | 同左 | — | 名指し card + 枠 (撤去済み)。mission の status は回復の対象外 (store-check が報告) | 回復の行なし | 正本 = 全 task done。mission は報告のみ (§2.2)。出口: 今と同じ |
| fail / needs-director | card の後・枠撤去の前 | 回復が R-2 (card の worker の枠) → 今の前提検査で拒否 (already failed / in_progress でない) | 同左 (`--head` 違い・理由違いも同じ) | Director `update --reset`: R-2 → reset。Worker の `done`: R-2 → failed / needs_director なので今どおり拒否 | 名指し card (failed / needs_director) + card の worker の枠 + 逆引き | R-2 の行を `recover()` の中で即時追記 → 本体の前提検査の拒否 (`die`) でも残る。**P2-b の行** | 正本 = failed / needs_director。出口: 回復 + Director の `update --reset` |
| update --reset | card (pending・worker null) の後・枠撤去の前 | 回復が**逆引き**で A の枠を見つけ R-2 → reset を書き直す (同じ値) | `update --status in_progress --worker B`: 逆引き R-2 (回復は本体より前なので card はまだ pending・worker 空) → 本体 → 次のロック取得で B の R-1 | B の `pull --task`: 逆引き R-2 → B に公開。A の次の `pull`: 自分の枠で R-2。A の `done` (reset に気付かず報告): 逆引き R-2 → done は pending を受け付ける (今と同じ挙動。§1.4) | 名指し card (pending・worker 空) + **逆引き** (旧所有者 A の枠。card からは分からない) + 呼び出し元の枠 | R-2 の行を `recover()` の中で即時追記。reset 本体の行は本体が例外なしで抜けたとき | 正本 = pending・所有者なし。**旧所有者の枠は逆引きで必ず見つかる**。出口: 同じ card を名指しする全コマンド・A 自身の次の呼び出し。これが P2-2 の行 |
| retire --outcome reset | 同上 (:5885 → :5899) | watchdog の再呼び出し: 逆引き R-2 → 本体は in_progress でないので今どおり exit 3 | `--outcome needs-director`: 同左 | 同上 | 同上 (逆引きが旧所有者の枠を見つける) | R-2 の行を `recover()` の中で即時追記 → 本体の exit 3 でも残る | 同上 |
| retire --outcome needs-director | card の後・枠撤去の前 | R-2 (card の worker の枠) → 本体は exit 3 | 同左 | Director の `update --reset` | 名指し card (needs_director) + card の worker の枠 + 逆引き | R-2 の行を `recover()` の中で即時追記 → 本体の exit 3 でも残る | 正本 = needs_director。出口: 回復 |
| reap-orphan-assignment | 本体を消した後・`.identity` を消す前 (retire_assignment :2239 の順) | 「既にありません」 | — | 次の `publish_assignment` が identity を先に書くので上書きされる | 名指しの Worker の枠 + それが指す card (classify)。本体が既に無ければ ABSENT | 回復の行なし (本体が ABSENT なので R-2 の条件外)。reap 本体の行は撤去したとき。残る identity は store-check | 本体が無い = ABSENT (classify は本体を先に読む)。残る identity は無害。store-check が件数を出す |
| verify-result | card の後・mission の前 (pass) | 「already verified」で拒否。回復は R-2 (verified は手放し済み) | `fail` / `needs_human_review`: 同じく拒否 | — | 名指し card (verified) + card の worker の枠 + 逆引き | R-2 の行を `recover()` の中で即時追記 → 本体の「already verified」(`die`) でも残る | 正本 = verified。mission は報告のみ。出口: 今と同じ |
| verify-result fail (→ in_progress) | card 1 枚の書き込み | — | — | 次のロック取得で worker・started_at が非空で枠が無ければ R-1 (§2.3 末尾の `update --status in_progress` と同じ扱い) | 次のロック取得のコマンドが名指しする card (A, G) + `assignments/A` (ABSENT) + **所有の証拠の走査** | R-1 の行 (または `reported:duplicate_owner` / `owner_unprovable`) を `recover()` の中で即時追記 | 正本 = in_progress (A, G)。出口: `update --reset` / `verify-result` |
| archive | move の後・state.yaml の前 | 回復が R-4 → mission が無いので今どおり拒否 | 別の slug: その slug に対して通常どおり | `--mission <slug>` の他コマンド: mission が無いので今どおり拒否。`init` / 他の `archive`: R-4 | state.yaml の `active_missions` + `missions/<slug>` と `archive/<slug>` の有無 | R-4 の行を `recover()` の中で即時追記 → 本体の「mission が無い」(`die`) でも残る | 正本 = `archive/<slug>`。出口: 回復 |
| add | card の後・`next_task_id` の前 | 回復が R-3 で `next_task_id` を進める → 本体は**次の番号で 2 枚目を作る** (同じ内容の card が 2 枚) | 違う内容の add: R-3 → 次の番号で作る。前の card は残る | `list` / dispatcher: 前の card は通常の pending として見える | その mission の `tasks/` の列挙 + mission.yaml の `next_task_id` | R-3 の行 (前回の残りの tNNN を `detail` に) を `recover()` の中で即時追記。add 本体の行は本体が例外なしで抜けたとき | 正本 = 2 枚とも正当な card (壊れてはいない)。R-3 の修復行 (stderr と監査ログ) に**前回の残りの tNNN**を出すので Director が気付ける。出口: `update <tNNN> --status skipped`。今は次の add が同じ tNNN を**黙って上書き**する (§2.2) ので、それよりは見える |
| ready-for-verification | card 1 枚の書き込み | — | — | — | 名指し card + 枠 + 逆引き。ready_for_verification を指す枠は R-2 の集合外 (§2.3) なので書く修復は無い | 報告があればその行を即時追記。本体の行は例外なしのとき | 単一ファイル。原子的書き込みで閉じる |

表から外したもの: add 以外の単一ファイルの書き込み (atomic_write_text で「書かれたか・書かれていないか」の
2 通りしか無い)、lib_retirement の marker (自前の R1 プロトコル。§5)。

---

## 3. State Store 書き込み lib の API (S2)

**モジュール: `scripts/lib_state_store.py`** —— 読み取り専用の `lib_task_cards.py` と対になる
書き込みの唯一の入口。不変条件 1 (「queue / registry / config を開くコードは lib_task_cards を通す」)
を書き込みにも広げる: **queue を書くコードは lib_state_store を通す**。
読み取りは lib_state_store の中でも lib_task_cards を使う (`Unreadable` をそのまま受ける)。

S2 の時点では**呼び出し元ゼロ** (R2)。plan.sh は S3 で移る。

### 3.1 公開関数

```python
# ── 原子的な書き込み (ロックとは独立。ロック外の書き手 (.crewvia-env 等) もこれを使う) ──
atomic_write_text(path, text, *, mode=None) -> None
    # 同じディレクトリに mkstemp('.<name>.tmp.') → write → flush → fsync(file)
    # → 既存ファイルの mode を引き継ぐ (無ければ mode 引数 / 0o644) → os.replace → fsync(親 dir)
    # 失敗は StoreWriteError(path, op, errno) を raise。tmp は必ず消す (今の _atomic_write :692-710 と同じ)
atomic_remove(path) -> bool
    # unlink → fsync(親 dir)。ENOENT は False を返す (「無かった」)。それ以外は StoreWriteError

# ── トランザクション (queue/.lock) ──
transaction(queue_dir, *, op, actor, nonblocking=False) -> ContextManager[Txn]
    # 1. queue/.lock を flock (nonblocking=True は LOCK_BUSY と同じ扱い: LockBusy を raise)
    # 2. 同じプロセスで入れ子なら即 NestedTransaction を raise (待たない)
    # 3. Txn を返す。with を抜けたら (例外なしのとき) コマンド本体の監査ログの行を追記してから unlock
    #    (回復の行はこの経路に乗せない。recover() の中で即時に追記済み。§2.3 末尾 / §4)

class Txn:
    load_card(slug, tid) -> (meta, body)          # ロックの中で読み直す。読めなければ CardUnreadable
    load_mission(slug) / load_state()
    write_card(slug, tid, meta, body)            # atomic_write_text
    write_mission(slug, data) / write_state(state)
    publish_assignment(agent, slug, tid, generation)   # identity → 本体 (今の :2040 の順)
    retire_assignment(agent, slug, tid, generation) -> verdict   # classify を通す (:2239)
    recover(scope) -> list[Repair]               # §2.3 の R-1〜R-4 を scope (§2.5。逆引きを含む) の中だけで (S4)
                                                 # コマンドの前提検査より前に呼ぶ。報告は返すが raise しない (§2.5)
                                                 # R-1 は書く直前に所有の証拠の走査 (§2.5) を行う
                                                 # 修復・報告 1 件ごとに op=recover の行をその場で監査ログに追記する
                                                 # (with の出口を待たない。後で本体が die しても残る。§2.3 末尾)
    record(mission, task, from_status, to_status, generation=None, detail=None)  # 監査ログの 1 行を予約

# ── 診断 (ロックを取らない・書かない) ──
diagnose(queue_dir, scope) -> list[Finding]      # store-check の本体 (S4)

# ── ロック外の小さな共有ファイル (queue/.lock を取らないもの) ──
locked_update_json(path, lock_path, fn) -> dict  # 専用ロック + 読み直し + atomic_write。taskvia map 用 (S5)
```

### 3.2 決めたこと

| 項目 | 決定 | 捨てた案と理由 |
|---|---|---|
| トランザクションの意味 | **ロック + ロック内の読み直し + 順序どおりの即時書き込み + 監査ログ**。書き込みはステージしない | ステージして最後にまとめて書く案: 「まとめて書く」の途中で落ちればジャーナルが要る。§2.1 の順序の規則が crash モデルそのものなので、書く順を呼び出し側が書いたとおりに保つほうが検証しやすい |
| ロックの再入 | **再入しない。入れ子は即エラー** (`NestedTransaction`)。今の plan.sh にも再入は無く (with_lock :1909)、同じプロセスで 2 回目の flock を別の fd で取ると**自分を待って止まる** | 再入を許す案: 内側の書き込みが外側の一部としてコミットされることが呼び出し側から見えなくなり、監査ログの 1 トランザクション = 1 行が崩れる。deadlock テスト (原案 STATE-02) は「入れ子で待たずに例外になる」を固定する |
| ロックの順序 | `queue/.lock` が最も外側。registry のロック (lib_registry :66)・task-graph のロック・taskvia map のロックを握ったまま `queue/.lock` を取らない | lib_retirement の `queue_transaction` (:322) も同じファイルを nonblocking で取る。S2 は `transaction(nonblocking=True)` と同じ意味に揃え、S5 以降で lib_retirement を移すかは別に判断 (§5) |
| ロックの中でしないこと | subprocess・ネットワーク・LLM・全 mission の走査 (原案 §14-15) | |
| fsync の範囲 | **ファイル + 親ディレクトリ** (replace と unlink の後)。ディレクトリを作ったときはその親も | 親 dir の fsync は今どこにも無い。親 dir の fsync が `EINVAL` / `ENOTSUP` を返すファイルシステムだけ黙って続行し、**それ以外の OSError は raise** (「できなかった」を「した」にしない) |
| tmp の名前と残骸 | `mkstemp(dir=親, prefix='.<name>.tmp.')`。先頭 `.` で `tNNN.md` の列挙 (TASK_FILENAME_RE :105) に絶対に当たらない | 今の `path.tmp.<pid>` は pid の再利用で衝突しうる。残骸は store-check が件数を出す (自動削除はしない —— 書き手が生きているかをファイル名から証明できない) |
| 失敗の戻り値 | 書けない = 例外 (`StoreWriteError` / `LockFailed` / `LockBusy` / `CardUnreadable` / `NestedTransaction`)。**None / False / {} に潰さない** | lib_retirement.write_json_atomic (:379) の「False を返す」型は呼び出し側が無視しうる。plan.sh 側は例外を die(msg, code) に写す (LockBusy → 4、前提外れ → 3、それ以外 → 1) |
| file mode | 既存ファイルの mode を引き継ぐ | mkstemp は 0o600 で作るので、引き継がないと Worker / デーモン / hooks の間で読めなくなる |
| 直列化 | YAML / frontmatter の直列化は今の plan.sh の関数 (`serialize_frontmatter` / `dump_yaml`) を lib に移し、出力をバイト単位で変えない | S3 の QA (t013) が固定 fixture の前後比較で確かめる |

---

## 4. 監査ログ (S3)

| 項目 | 決定 | 理由 / 捨てた案 |
|---|---|---|
| 置き場所 | **`queue/audit/transitions-YYYYMMDD.jsonl`** (UTC 日付) | queue/ は丸ごと gitignore (.gitignore:35)、`CREWVIA_QUEUE` を付け替えた隔離実行で自動的に隔離先に書かれる (test isolation を追加の仕組みなしで満たす)。registry/ 案: registry/ は一部が追跡下 (workers.yaml) で、状態遷移の記録はデーモンの観察ではなく queue の変更の記録なので queue 側 |
| いつ書くか | **コマンド本体の行**: トランザクションが**例外なく終わったとき**、ロックを離す**前**に 1 行。**回復の行** (`op=recover`): `Txn.recover()` の中で、修復・報告 1 件ごとに**その場で** (ロックの中で) 追記する。本体の拒否 (`die` = `SystemExit`)・例外を待たない (§2.3 末尾。PR #255 3 巡目 P2-b) | ロックの中で書けば行の順序 = コミットの順序。ロックの外だと 2 つのトランザクションの行が入れ替わりうる。回復の行を本体と同じ「例外なしのとき」に乗せると、修復は残るのに行が消える (crash 後の再実行は前提検査で `die` することが多い) |
| 1 行の欄 | `ts` (µs UTC) / `txn_id` (uuid4 hex) / `op` (サブコマンド名 or `recover`) / `mission` / `task` / `actor` (AGENT_NAME。無ければ `director` か呼び出し元の名前: `dispatcher` / `watchdog` / `kai-review` / `unknown`) / `pid` / `from_status` / `to_status` / `generation` (card の started_at) / `execution_id` (**null 固定**。01c が埋める) / `result` (`ok` / `repaired:R-n` / `reported:<理由コード>`) / `files` (書いたパスの queue からの相対パス一覧) | 原案 STATE-06 の欄 + generation / files。**書かないもの**: Result 本文・reason 本文・description (原案「Task 内容を log へ出さない」)、env、token |
| 拒否・報告を書くか | 回復の報告 (§2.5 の「報告のみ」。`op=recover result=reported:<理由コード>`) だけ書く。通常の前提外れ (done の二重実行・D0 の拒否等) は書かない | 通常の拒否は既に stderr と exit code がある。回復の報告は「表に無い食い違いがあった」証拠なので残す。回復はコマンドを拒否しない (§2.5) ので `refused` の行は無い |
| ローテーション | 日付ごとのファイル。**01a では自動削除しない** | 1 行 ~300 B × 数百 / 日で年 100 MB に届かない。削除を足すと「消してよいか」の判定 (族 C) が増える。量が問題になったら archive と同じ手動運用を足す |
| 書けないとき | **状態遷移は止めない**。stderr に `[plan.sh warn] audit log を書けませんでした (<path>: <errno>)` を 1 行、exit code は変えない。store-check が「audit log に書けない」を件数付きで出す | 止める案: 監査ログのディレクトリ 1 つの権限・容量の問題で**本番の全 plan.sh が止まる** —— 新しいガードの失敗状態が全体停止になる (族 C。memory `a-new-guard-creates-a-new-state`)。監査ログは正本ではなく回復にも使わない (§2.1) ので、欠けても状態は正しい。欠けたことは見える形で残す (黙って捨てない) |
| crash で行が欠ける | コミット (正本の書き込み) の後・監査ログの前に落ちると 1 行欠ける。回復も同じく、修復の書き込みの後・その行の追記の前に落ちると 1 行欠ける (修復ごとに追記するので最大 1 行)。**許容し、ここに明記する** | 行を先に書く案: 起きなかった遷移の行が残る (こちらのほうが誤読を生む) |

---

## 5. 全書き手の一覧 (S3 / S5)

改訂案 §2-A を e6d6801 で数え直した。**S5 の t020 は着手時に再計測して更新してよい** (Director 追記)。
計測方法: `scripts/*.py scripts/*.sh hooks/*.sh` (test_ を除く) の `open(..,'w'|'a')` / `write_text` /
`os.replace` / `os.remove` / `unlink` / `>` `>>` / `touch` / `mkdir -p` を列挙し、書き先が queue/ か registry/ のものを残した。

### 5.1 queue/

| ファイル | 書き手 (行) | ロック | 原子性 | 01a | 理由 |
|---|---|---|---|---|---|
| `missions/*/tasks/tNNN.md` | plan.sh の各コマンド (save_task :847) | queue/.lock | tmp+replace+fsync (:692) | **S3** で lib へ | |
| 同上 | plan.sh `_apply_risk_flags` (:4712、呼び出し :5096) | **なし** (`with_lock(_do_verdict)` :5093 の後) | save_task | **S5** で `_do_verdict` の中へ | |
| 同上 | verifier-dispatcher.sh `update_task_fields` (:257-313、呼び出し :445) | **なし** | tmp+replace、fsync なし | **S5**: `plan.sh verifying <tid> --verifier <name> --mission <slug>` を新設して置き換え | status を書く唯一のロック外経路 |
| 同上 (本文の Pre-Compact Snapshot 節) | hooks/pre-compact.sh (:44-61) | **なし** | `open('w')` 上書き (非原子的) | **S5**: plan.sh のサブコマンド経由 (mission を `CREWVIA_MISSION_SLUG` で名指し) | 加えて :39 は `find ... -path "*/tasks/${task_id}.md" \| head -1` で **task id だけで探す** —— 別の mission の同じ tNNN に書きうる (不変条件 2 の族) |
| `missions/*/mission.yaml` | plan.sh save_mission (:824) | queue/.lock | 同上 | **S3** | |
| `missions/*/plan_review.verdict` | review-plan.sh (:622-623) | なし | tmp + mv | 寄せない | 書き手 1 者・run_id で鮮度を確かめる読み手 (t018)。原子性はある |
| `state.yaml` | plan.sh save_state (:713) | queue/.lock | 同上 | **S3** | |
| `assignments/<agent>` / `.identity` | plan.sh publish / retire (:2040 / :2239) | queue/.lock | 同上 / unlink (親 dir fsync なし) | **S3** (S4 で projection 化) | |
| `assignments/<agent>.restarting` | benchmark-ctx.sh (:107 / :307 `touch`) | なし | touch | 寄せない | ベンチマーク専用・中身なし・存在だけの印 |
| `.taskvia-map.json` | plan.sh (:2365 / :2382)、taskvia-sync.sh (:144) | **なし** | `open('w')` | **S5**: `locked_update_json` (専用ロック) | 外部ミラーのキャッシュ。queue/.lock を取らないのは、前後に HTTP があり、正本ではないため |
| `pre-compact-fallback.log` | hooks/pre-compact.sh (:66) | なし | 追記 | 寄せない | ログ |
| `.lock` | plan.sh / lib_retirement | — | — | — | |
| (新) `audit/*.jsonl` | lib_state_store | queue/.lock | 追記 (O_APPEND) | S3 | §4 |

### 5.2 queue の外 (worktree)

| ファイル | 書き手 | 01a | 理由 |
|---|---|---|---|
| `<worktree>/.crewvia-env` | plan.sh pull (:3460、lock 外) | **S5**: `atomic_write_text` (ロックは不要 —— worktree はその task 専用) | Worker が source する途中で読まれうる。再 pull で書き直されない問題は 01b |

### 5.3 registry/

| ファイル | 書き手 | ロック / 原子性 | 01a | 理由 |
|---|---|---|---|---|
| `workers.yaml` | lib_registry.write (:182) | 専用ロック (:66) / **`open('w')` 非原子的** | **S5**: 書き方だけ `atomic_write_text` に。ロックは lib_registry のまま | queue の状態ではない (Worker の名簿)。queue/.lock に入れると done のロック外 bump (:4246) と順序が逆になる |
| 同上 (初期化) | assign-name.sh (:32-34) | **なし** / `printf >` | **S5**: lib_registry 経由に | |
| `heartbeats/*` | hooks/post-tool-use.sh (:87)、kai-review.sh (:210) | なし / 上書き | 寄せない | 毎回再生成される mtime の印。壊れても次の tool 使用で直る。lock を足すと全 tool 呼び出しが queue を待つ |
| `activity/*`, `notifications/*`, `approvals/*.tsv`, `*.log`, `watchdog-observations.jsonl` | hooks / デーモン | 追記 | 寄せない | ログ・観察記録。正本ではない |
| `retirements/*` | lib_retirement (write_json_atomic :379 他) | queue/.lock (nonblocking) の中 / tmp+replace、fsync なし | **寄せない** (プリミティブの共有だけ後続で検討) | 退役は lib_retirement が唯一の定義を持つプロトコル (R1: 意図を先に永続化)。書き方を変えると knowledge/daemon-authority.md の論証をやり直すことになり、01a の範囲を超える |
| `daemons/*` (notified-state・review-refusals 等) | lib_daemon_state (:241)、lib_review_refusal (:93)、dispatcher.sh | 各自 | 寄せない | 不変条件 7: 消してよい台帳。消えても再生成される |
| `mux/*.json` | lib_mux (:1651) | 専用ロック | 寄せない | mux 抽象の記録。sweep の規則は lib_mux が持つ |
| `workers/<name>/` | lib_worker_target (:139) | tmp+replace | 寄せない | TARGET_DIR の記録。書き手 1 者 |
| `handoffs/*` | Worker / plan.sh の退避 (:5390) | O_EXCL で確保した名前への replace | 寄せない | 退避は既に安全な形 |
| `task-graph/tasks.json` | plan.sh (lock 外、専用ロック) | tmp+replace | 寄せない | 再生成物 |

「寄せない」ものの共通の根拠: **正本ではない** (再生成できる / ログ / 別の lib が唯一の定義を持つ)。
queue/.lock に入れると、正本を守るためのロックが観察の書き込みで混む。

---

## 6. 構造ガード (S5)

**「lib_state_store を通らない queue / registry への書き込みが増えたら CI が赤」**。
先例 `tests/test_queue_reads_go_through_the_guard.py` (読み取り側。AST で全部拾い allowlist で照合) と同じ形で、
`tests/test_queue_writes_go_through_the_store.py` を作る。

| 項目 | 決定 |
|---|---|
| 検出の向き | **allowlist** (理由付きで載っているものだけ通す)。denylist は新しい書き方で必ず穴が開く |
| 対象 | Python: `scripts/*.py` と、`scripts/*.sh` / `hooks/*.sh` の **全** `<<'PYEOF'` ブロック (先例は 1 ブロック目だけを見る :74 —— pre-compact.sh・dispatcher.sh は複数ブロックを持ちうるので全部)。**先例の `AUDITED_MODULES` (:338) は hooks/・kai-review.sh・benchmark-ctx.sh・assign-name.sh を含まない** ので、対象は「列挙した一覧」ではなく**ディレクトリの glob** で決め、除外を理由付きで書く |
| Python で拾う形 | `open(..., 'w'/'a'/'x'/'+')` (位置引数と `mode=` の両方)・`os.open` の `O_WRONLY/O_RDWR/O_CREAT`・`Path.write_text/write_bytes/open('w')`・`os.replace/rename/remove/unlink`・`Path.unlink/rename/replace`・`shutil.move/copy*`・`os.fdopen(fd, 'w')`・`json.dump(obj, f)` |
| bash で拾う形 | `>` / `>>` / `tee` / `touch` / `mv` / `cp` / `rm` の行で、書き先の語に `queue` / `QUEUE` / `assignments` / `registry` / `REGISTRY` / `tasks/` / `.md` を含むもの。bash は変数を解決できないので**変数名で拾い、誤検出は allowlist に理由付きで載せる** (見逃しより誤検出を取る) |
| 実際の呼び出し形で | 検出器のテストに**本物のコードから切り出した形**を入れる (`str(self.plan_sh)` の型の見逃し。memory `structural-guard-must-match-real-call-shape`): `open(tmp, 'w')` / `tmp` への `write_text` 呼び出し / `with LOG_FILE.open('a')` / `open(map_path, 'w')` / pre-compact の `open(task_file, 'w')` / verifier-dispatcher の `open(tmp,'w')`+`os.replace` / `printf ... > "$REGISTRY_YAML"` / `touch "$HEARTBEATS_DIR/$AGENT"` |
| 空虚な PASS を防ぐ | (1) **検査した書き込みの件数を出し、下限を assert** (e6d6801 で Python 側の書き込み系の呼び出しは数十件ある。下限は S5 が実測した件数に置く。0 件や急減は検出器の故障)、(2) allowlist の死んだ行を落とすテスト (先例 :860 と同じ)、(3) **陽性対照**: 合成ソースの各形を検出器が拾うことをパラメタライズで固定 (先例 :1096 と同じ)、(4) worktree で走らせても対象が 0 件にならないこと (memory `registry-dir-single-definition-and-vacuous-static-guards`: 除外判定が worktree のパスに全件当たった前例) |
| 赤の実証 | S5 の PR 説明に「pre-compact.sh の書き込みを元に戻すとこのテストが赤」の実行結果を載せる (memory `regression-test-must-prove-red`) |
| 文書を対象にしない | 対象は `.py` と `.sh` の中のコードだけ。`.md` を文字列で走査すると、ガードを説明する文書自体が引っかかる (この PR が `scripts/test_registry_lock.sh` の近接検査 —— `.md` も走査する —— で実際に赤になった。memory `red-proof-for-a-text-pattern-guard-trips-itself`) |
| 読み取りの先例との関係 | 読み取りの allowlist と書き込みの allowlist は**別ファイル**。同じ検出器ヘルパ (`_python_source` 等) は共有してよいが、表は混ぜない |

---

## 7. cutover と rollback (R2)

| PR | 本番で変わること | merge 前 | merge 後に Director が本番で確かめること | 戻し方 |
|---|---|---|---|---|
| S1 (t004) | lint が `needs_director` を通す・`cancelled` を FAIL / update が `needs_director` を書ける / 定義の置き場所 (挙動は同じ) | **ユーザー承認** (t007 経由) | `plan.sh lint` を本番の active mission 全部に: FAIL が増えていない (`needs_director` の card は減る方向)。dispatcher / plan.sh の status 表示が前と同じ | PR revert → `scripts/sync-main-checkout.sh` (ff + デーモン restart) |
| S2 (t008) | **なし** (呼び出し元ゼロ) | 通常 merge | `grep -rn lib_state_store scripts hooks` が lib 自身とテストだけ | revert |
| S3 (t012) | plan.sh の書き込みが lib 経由 (fsync に親 dir が増える) / `queue/audit/` ができ行が増える | **ユーザー承認** (t015) | 実 Worker の pull / done が通常どおり終わる / `queue/audit/transitions-<今日>.jsonl` に行が増える / card のバイト表現が変わっていない (直前の git 状態が無いので、S3 前後に取った 1 枚の card の sha を比べる) / stderr に audit の warn が出ていない | revert → sync-main-checkout。`queue/audit/` は残ってよい (読み手がいない) |
| S4 (t016) | 次のロック取得時の R-1〜R-4 (前提検査より前・逆引きを含む) / done の順序 (D0〜D5) と D0 の 2 つの拒否 / 範囲内の「表に無い食い違い」の報告 (止めない) / `reap-orphan-assignment` の R-2 集合 / `plan.sh store-check` | **ユーザー承認** (t019) | `plan.sh store-check` を 2 回: 本番の既存の食い違いの一覧 (0 件でなくてよい —— **merge 前に一度走らせた結果**と比べる) / audit に `repaired:R-n` が出たら 1 件ずつ Director が妥当か確かめる / `reported:` の行を store-check の一覧と突き合わせる / done の D0 の拒否が出たら拒否メッセージの出口 (`update --pr-number`) で抜けられた | revert → sync-main-checkout。R-1〜R-4 が書いたものは正本を変えていないので、戻しても旧コードがそのまま読める (原案 §9.4) |
| S5 (t020) | verifier-dispatcher・pre-compact・risk flags・`.crewvia-env`・taskvia map・workers.yaml の書き方 / `plan.sh verifying` 新設 / CI の構造ガード | **ユーザー承認** (t023) | compaction が起きた Worker の card に snapshot 節が付く (正しい mission の card に) / `.crewvia-env` が次の pull で作られ source できる / workers.yaml の task_count が done で増える / CI 緑 | revert → sync-main-checkout。**verifier-dispatcher は常駐デーモンなので restart が要る** (memory `merged-daemon-code-is-inert-until-restart`)。hooks は次の tool 呼び出しから新旧が入れ替わる |

共通: **merge 後に主 checkout を ff するまで本番は旧コードのまま** (memory `main-checkout-lags-after-pr-merge`)。
Director の確認は `git -C <主 checkout> log -1` が merge commit であることから始める。
env の停止スイッチは付けない (不変条件 5)。戻しは常に revert。

---

## 8. 禁止事項・不変条件との整合 (確認表)

| 原案 §14 / 不変条件 | この設計 |
|---|---|
| §14-1 main checkout での実装 | 全 PR は worktree。t024 の本番確認は読み取り (`store-check`) と通常運用の観察だけ |
| §14-2 稼働中 queue へのテスト書き込み | crash 注入・並行テストは `CREWVIA_QUEUE` を付け替えた隔離 queue だけ。監査ログも隔離 queue に書かれる (§4) |
| §14-3・4 hot reload / 自動 cutover | cutover は各 PR の merge + ユーザー承認 + sync-main-checkout (§7)。自動 restart なし |
| §14-5 未承認の state migration | migration なし。`cancelled` は 0 件。R-1〜R-4 は食い違いの修復で、形式の移行ではない |
| §14-6 Dispatcher に遷移 authority | dispatcher は書かないまま。語彙を import するだけ |
| §14-7 同じ規則の二重実装 | 語彙は lib_task_status、書き込みは lib_state_store、依存は lib_dep_rules。コピーは構造テストで落とす (§1.5・§6) |
| §14-8 Worker 等による state の直接更新 | verifier-dispatcher・pre-compact を plan.sh 経由に (S5)。構造ガードで固定 |
| §14-9 Agent 名 / Task ID だけで完了 | **01a では閉じない** (01c)。現状のまま (§0) |
| §14-14 per-file replace を multi-file transaction と呼ぶ | 呼ばない。順序 + projection 再生成で定義 (§2.1) |
| §14-15 lock 内の network / LLM | Taskvia・worktree・task-graph はロックの外のまま。lib に「ロック内で subprocess しない」を書く |
| §14-16 silent 修復 | 修復は R-1〜R-4 の 4 つだけで、すべて監査ログに残る (`recover()` の中で 1 件ごとに即時追記。後でコマンド本体が拒否されても消えない §2.3 末尾)。それ以外は書かずに stderr と監査ログに報告する (止めない。止めると復旧の出口が消える §2.5) |
| §14-17 unknown field / 本文の削除 | 直列化は今の関数を移すだけ (バイト単位で同じ)。S3 の QA で前後比較 |
| §14-18・19 新 DB / Message Bus | なし |
| 不変条件 1 (読み取りは lib_task_cards) | lib_state_store の読みも lib_task_cards。書き込み側に同じ規則を広げる |
| 不変条件 2 (識別子はファイル名) | lib の関数は `(slug, tid)` をファイル名から組み立てる。pre-compact の task id だけの探索を直す (S5) |
| 不変条件 3 (依存は lib_dep_rules) | 依存の意味は lib_dep_rules に残す。語彙だけ lib_task_status から取る |
| 不変条件 4 (デーモン再起動は lib_daemon_watch) | cutover の restart は sync-main-checkout.sh (内部で lib_daemon_watch restart) |
| 不変条件 5 (env 停止スイッチなし) | 付けない。回復の ON/OFF も env にしない |
| 不変条件 6 (handoff_path は絶対パス) | 触らない |
| 不変条件 7 (台帳は消してよい) | registry/daemons は寄せない (§5.3) |

---

## 9. S1〜S5 のテスト観点 (QA task への申し送り)

- S1 (t005): 本番 queue の複製 (`cp -a` した隔離 queue) で lint → FAIL の差分が `needs_director` の減少だけ / 語彙のコピー検出テストの陽性対照と件数
- S2 (t009): 独立プロセスで同じ queue に 2 本以上のトランザクション (mock lock ではなく実 flock)、入れ子で待たずに例外、原子的書き込みの各段 (write / fsync / replace / 親 fsync) で例外を注入して元のファイルが読める、crash 注入 20 回反復、欠陥版 (親 fsync を消す・tmp を消さない) で赤
- S3 (t013): 固定 fixture で S3 前後の card / mission / state のバイト比較、監査ログの行と欄、書き込み途中の SIGKILL
- S4 (t017): §2.2 の表の各行を隔離 queue で作り、次のロック取得で表どおりになる / **進行中を巻き戻さない** (後任が pull し直した状態で R-2 が後任を消さない) / `update --status in_progress` (§2.3 末尾) の観察 / 本番 queue の複製で store-check の棚卸し / **§2.6 の表の各行を「落とす → 同じ引数 / 違う引数 / 別の操作」で再現** し、正本と出口が表どおりか (特に P2-1: `done --pr 123` を D2 の途中で落とし `--pr 456` で拒否される、P2-2: reset を枠撤去の前で落とし逆引きで旧所有者の枠が消える、P2-3: §2.5 の表の各食い違いで出口のコマンドが通る、3 巡目 P2-a: A の in_progress card を 2 枚 (うち 1 枚は `--mission` で名指ししない別 mission・`init --inactive` の mission) にし A の枠を消して、`done` / `update` / `pull --task` のどれで回復が走っても枠が書かれず `reported:duplicate_owner` が出る。`[破損]` card を 1 枚置いて `owner_unprovable`。欠陥版 (所有の証拠の走査を消す) で赤、3 巡目 P2-b: `needs-director` を枠撤去の前で落とし、同じ `needs-director` の再実行が exit 3 になった後に監査ログに `op=recover result=repaired:R-2` の行がある。欠陥版 (回復の行を with の出口で書く) で赤)
- S5 (t021): 書き手ごとの同時更新と強制終了、構造ガードの陽性対照・件数・赤の実証
