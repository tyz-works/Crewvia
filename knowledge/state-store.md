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
  - 正本以外の**派生値** (依存先カードの `pr_number`) は、正本より**前に**書く (done の順を変える。§2.2)
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

**done** (S4 で順を変える: 依存先の pr_number → card done → assignment 撤去 → mission done)

| 落ちた点 | 残る状態 | 次のロック取得時 | 根拠 |
|---|---|---|---|
| pr_number 伝播の途中 (S4 後) | 依存先の一部に pr_number、card は in_progress | 何もしない。Worker / Director の**再 done で完了** (伝播は冪等: plan.sh:3943、自分の card の pr_number も同値の再指定を許す :4103-4110) | 依存先は blocked_by が満たされないので dispatch されない |
| card done の後・assignment 撤去の前 | card done / assignment が done の task を指す | **R-2**: 孤児として撤去 | 今の reap-orphan-assignment と同じ判定 (`ORPHAN_ASSIGNMENT_FINISHED_STATUSES` :908 / cmd_reap_orphan_assignment :5909) |
| assignment 撤去の後・mission done の前 | 全 task 終端 / mission in_progress | **報告のみ** (store-check に出す) | `update --status done` でも同じ状態が今でも正当に作れる (update は mission 完了を判定しない)。正当な状態と区別できないので書かない |
| (現状の順のまま落ちた場合) card done の後・伝播の前 | card done / 依存先に pr_number 無し | **報告のみ** (dispatcher.sh:2286 の既存通知がそのまま出る) | Director が `update --pr-number null` で消した状態と区別できない。**だから順を変える** |

**その他の複数ファイル更新**

| コマンド | 順序 | 落ちた点 | 次のロック取得時 |
|---|---|---|---|
| fail / needs-director / retire | card → assignment 撤去 (:4345→:4357 / :3816→:3826 / :5898→:5899) | card の後 | **R-2** (card が in_progress でなくなった → assignment は孤児) |
| update --reset | card pending → assignment 撤去 (:5610→:5625) | card の後 | **R-2** |
| add | card → mission `next_task_id` (:2949→:2953) | card の後 | **R-3**: `next_task_id` を「既存の最大 tNNN + 1」以上に進める。**今は次の add が同じ tNNN を黙って上書きする** (:2926-2927 に存在確認が無い) |
| verify-result pass / ready-for-verification | card → (mission done) | card の後 | mission 側は「報告のみ」(done と同じ) |
| archive | `shutil.move(mission)` → state.yaml (:5199→:5202) | move の後 | **R-4**: state.yaml の `active_missions` に在るが `missions/` に無く `archive/` に在る slug を外す |
| retirement marker (lib_retirement) | 自前の R1 プロトコル (marker が先) | — | 01a では触らない (§5) |

### 2.3 回復規則 (これ以外は書かない)

| 規則 | 条件 (すべて満たすときだけ書く) | 書くもの |
|---|---|---|
| **R-1** projection を作る | card が `in_progress`・`worker = A` (非空)・`started_at = G` (非空) / `assignments/A` が **ABSENT** (classify_assignment :2058 が `ENOENT` だけを ABSENT にする。:2084) | `A.identity` (G) → `assignments/A`。`publish_assignment` と同じ関数 |
| **R-2** 孤児 projection を消す | `assignments/A` が指す card が、**assignment を撤去するコマンド自身の書く status** のどれか: 手放し済み (`ORPHAN_ASSIGNMENT_FINISHED_STATUSES` :908 = done / verified / skipped / failed。今の `reap-orphan-assignment` :5909 の判定) ∪ needs_director (needs-director / retire) ∪ worker が空の pending (update --reset / retire reset) | assignment → identity の撤去 (`retire_assignment` :2239。classify を通す) |
| **R-3** 採番を進める | `tasks/` に `next_task_id` 以上の tNNN.md がある | mission の `next_task_id = max + 1` |
| **R-4** archive 済みを外す | slug が `active_missions` に在り、`missions/<slug>` が無く `archive/<slug>` が在る | state.yaml から外す (`default_mission` も archive と同じ規則で) |

**R-2 を「holding でない status すべて」にしない理由**: Director が作業中の card に `update --status blocked` を付けるのは今でも正当な操作で、そのとき Worker は動いており assignment は残っている。「holding でない」で消すと、その Worker が dispatcher から idle に見え、退役 = **動いている Worker を殺す**経路になる (memory `destruction-needs-provenance-not-classification`)。消してよいのは、撤去を伴うコマンドが書く status —— つまり「このコマンドが途中で落ちた」以外に説明の無い組 —— だけ。それ以外 (blocked / verification_failed / ready_for_verification 以外の手書きの status 等) で assignment が残っているものは store-check が報告するだけ。

**R-1 の条件を `in_progress` だけに絞る理由**: 正本→projection の欠けが crash で生まれるのは
pull の窓だけで、pull が書く status は in_progress だけ。ready_for_verification / verifying /
needs_human_review で assignment が無い状態は crash では作れないので、見つけたら「表に無い食い違い」
(§2.5) として報告する。

**R-1 で A の枠が ABSENT でないとき** (`assignments/A` が別の task を指す):
- 指す先が手放し済み → R-2 で消してから R-1 (今の publish と同じ扱い: agent_busy_elsewhere :3032 の docstring :3047)
- 指す先も in_progress (A が 2 枚の card を持つ) / UNVERIFIABLE → **書かない**。§2.5 の停止

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

### 2.5 回復の走査範囲と、止めるときの範囲

**走査範囲: そのコマンドが名指ししている task と、呼び出し元の Worker の枠だけ。**
mission 全体・queue 全体は走査しない。

| コマンド | 回復の対象 |
|---|---|
| pull (--task なし) | 呼び出し元 A の `assignments/A` と、A が worker の in_progress card (今の agent_busy_elsewhere :3052-3056 が既に全 active mission の card を読んでいる。その読みを使い、追加の走査はしない) + 選んだ card |
| pull --task / done / fail / needs-director / ready-for-verification / verify-result / update / retire | 名指しの card + その card の worker の枠 + 呼び出し元の枠 |
| add | その mission の `tasks/` の列挙 (R-3) |
| archive / init | state.yaml (R-4) |
| reap-orphan-assignment | 今のまま (名指しの Worker の枠) |

- 理由 1: **ロックを握る時間を増やさない**。queue 全体の走査を毎回入れると、全 Worker と両デーモンの
  plan.sh が待たされる (maybe_refresh_task_graph :1873 が同じ理由でロックの外にある)
- 理由 2: **1 枚の事故で全体を止めない** (t009 以来の向き)。他の mission の壊れた card が、
  無関係な Worker の done を止める経路を作らない
- 理由 3: t024 の本番観察の波及を切る。t024 は本番の他の mission / 並行する Worker に書き込まない

**「表に無い食い違い」のとき**: 範囲内で §2.3 の条件に合わない食い違い
(A が in_progress の card を 2 枚持つ / holding status なのに assignment が無い (in_progress 以外) /
assignment・identity が UNVERIFIABLE / card が `[破損]`) を見つけたら:
- **そのコマンドだけ** exit 3 (`PRECONDITION_UNMET`。1 バイトも書かない) で止め、食い違いを 1 行で出す
- 止まるのはその task / その Worker の操作だけ。他の task・他の Worker・デーモンの `retire --no-wait`
  (exit 4) は影響を受けない。**本番の全 plan.sh 呼び出しが止まる経路は作らない**:
  回復は範囲外を読まないので、範囲外の食い違いで die する経路がそもそも無い
- 監査ログに `op=recover result=refused` を 1 行残す (§4)

**本番で観察する手順 (t024 向けの設計の制約)**:
- 読み取り専用の `plan.sh store-check [--mission <slug>]` を S4 で足す (`QUEUE_READONLY_SUBCOMMANDS`。
  ロックを取らない。R-1〜R-4 と「表に無い食い違い」を**書かずに**列挙し、件数を出す)。
  ロック無しなので途中のトランザクションを食い違いとして 1 回見うる → 「2 回連続で出たものだけが本物」と出力に書く
- t024 は本番で store-check を叩くだけ。crash の注入は**本番では行わない** (原案 §14-2)。
  注入は t013 / t017 の隔離 queue (CREWVIA_QUEUE を付け替えた複製) でだけ行う

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
    # 3. Txn を返す。with を抜けたら (例外なしのとき) 監査ログを追記してから unlock

class Txn:
    load_card(slug, tid) -> (meta, body)          # ロックの中で読み直す。読めなければ CardUnreadable
    load_mission(slug) / load_state()
    write_card(slug, tid, meta, body)            # atomic_write_text
    write_mission(slug, data) / write_state(state)
    publish_assignment(agent, slug, tid, generation)   # identity → 本体 (今の :2040 の順)
    retire_assignment(agent, slug, tid, generation) -> verdict   # classify を通す (:2239)
    recover(scope) -> list[Repair]               # §2.3 の R-1〜R-4 を scope の中だけで (S4)
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
| いつ書くか | トランザクションが**例外なく終わったとき**、ロックを離す**前**に 1 行 | ロックの中で書けば行の順序 = コミットの順序。ロックの外だと 2 つのトランザクションの行が入れ替わりうる |
| 1 行の欄 | `ts` (µs UTC) / `txn_id` (uuid4 hex) / `op` (サブコマンド名 or `recover`) / `mission` / `task` / `actor` (AGENT_NAME。無ければ `director` か呼び出し元の名前: `dispatcher` / `watchdog` / `kai-review` / `unknown`) / `pid` / `from_status` / `to_status` / `generation` (card の started_at) / `execution_id` (**null 固定**。01c が埋める) / `result` (`ok` / `repaired:R-n` / `refused:<理由コード>`) / `files` (書いたパスの queue からの相対パス一覧) | 原案 STATE-06 の欄 + generation / files。**書かないもの**: Result 本文・reason 本文・description (原案「Task 内容を log へ出さない」)、env、token |
| 拒否 (exit 3) を書くか | 回復の拒否 (§2.5) だけ書く。通常の前提外れ (done の二重実行等) は書かない | 通常の拒否は既に stderr と exit code がある。回復の拒否は「状態が壊れている」証拠なので残す |
| ローテーション | 日付ごとのファイル。**01a では自動削除しない** | 1 行 ~300 B × 数百 / 日で年 100 MB に届かない。削除を足すと「消してよいか」の判定 (族 C) が増える。量が問題になったら archive と同じ手動運用を足す |
| 書けないとき | **状態遷移は止めない**。stderr に `[plan.sh warn] audit log を書けませんでした (<path>: <errno>)` を 1 行、exit code は変えない。store-check が「audit log に書けない」を件数付きで出す | 止める案: 監査ログのディレクトリ 1 つの権限・容量の問題で**本番の全 plan.sh が止まる** —— 新しいガードの失敗状態が全体停止になる (族 C。memory `a-new-guard-creates-a-new-state`)。監査ログは正本ではなく回復にも使わない (§2.1) ので、欠けても状態は正しい。欠けたことは見える形で残す (黙って捨てない) |
| crash で行が欠ける | コミット (正本の書き込み) の後・監査ログの前に落ちると 1 行欠ける。**許容し、ここに明記する** | 行を先に書く案: 起きなかった遷移の行が残る (こちらのほうが誤読を生む) |

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
| S4 (t016) | 次のロック取得時の R-1〜R-4 / done の順序 / 範囲内の「表に無い食い違い」で exit 3 / `plan.sh store-check` | **ユーザー承認** (t019) | `plan.sh store-check` を 2 回: 本番の既存の食い違いの一覧 (0 件でなくてよい —— **merge 前に一度走らせた結果**と比べる) / audit に `repaired:R-n` が出たら 1 件ずつ Director が妥当か確かめる / exit 3 が特定の task 以外で出ていない | revert → sync-main-checkout。R-1〜R-4 が書いたものは正本を変えていないので、戻しても旧コードがそのまま読める (原案 §9.4) |
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
| §14-16 silent 修復 | 修復は R-1〜R-4 の 4 つだけで、すべて監査ログに残る。それ以外は exit 3 で止めて見せる |
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
- S4 (t017): §2.2 の表の各行を隔離 queue で作り、次のロック取得で表どおりになる / **進行中を巻き戻さない** (後任が pull し直した状態で R-2 が後任を消さない) / `update --status in_progress` (§2.3 末尾) の観察 / 本番 queue の複製で store-check の棚卸し
- S5 (t021): 書き手ごとの同時更新と強制終了、構造ガードの陽性対照・件数・赤の実証
