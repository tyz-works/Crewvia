# plan.sh review 信頼性問題 修正ノート

**ミッション**: 20260908-launch-reliability (t002)
**対象ファイル**: `scripts/review-plan.sh`, `scripts/wait_for_plan_review.sh` (新規),
`scripts/normalize_plan_review_verdict.py` (新規), `hooks/pre-tool-use.sh`,
`config/skill-permissions.yaml`, `agents/plan_reviewer.md`, `scripts/plan.sh`
**関連テスト**: `scripts/test_plan_review_write_guard.sh`,
`scripts/test_normalize_plan_review_verdict.sh`, `scripts/test_wait_for_plan_review.sh`

---

## 症状 (2 ミッション連続で発生、毎回 Director の手動 workaround が必要だった)

`./scripts/plan.sh review <slug>` が 3 つの独立したパターンで壊れていた:

1. **verdict 形式不一致**: plan-reviewer (Opus) が規定形式 `**Verdict:** approve` ではなく
   `## 総合判定: **GO**` のように書き、`scripts/review-plan.sh` の
   `grep -q '^\*\*Verdict:\*\*'` に一致せず 600s タイムアウトする。しかも
   plan-reviewer が `mission.yaml` まで規格外の値 (`status: active` /
   `last_verdict: GO`) に書き換えてしまい、`plan.sh launch` (status=='ready' 必須)
   まで壊れる二次被害があった。
2. **出力先の詐称**: pane に「保存先: plan_review.md」と表示しながら実際には
   ファイルを一切書き出さない。
3. **【最重要】古い plan_review.md の誤採用**: `review-plan.sh` はレビュー開始前に
   既存の `plan_review.md` を削除していなかったため、前 cycle の残骸が残っていると
   待機ループが即座に成立し、**古い判定がそのまま新しい判定として採用される**。
   本ミッション自身の cycle 2 で実際に発生し、cycle 1 と完全に同一の内容
   (対応済みの指摘) が再度 `revise` として返り、`cycle_count` だけを浪費した。
   タイムアウトより質が悪い障害モード。

## 根本原因

パターン1の二次被害 (mission.yaml 破壊) の真因は個別に見つかった副次バグ:
**`review-plan.sh` は plan-reviewer 起動時に `CLAUDE_SKILL=plan_review` は
export していたが、`SKILLS=plan_review` を export していなかった。**
`hooks/pre-tool-use.sh` の per-skill 権限チェックは `SKILLS` 環境変数を見て
`config/skill-permissions.yaml` の該当セクションを適用するが、`CLAUDE_SKILL` は
どこからも参照されない死んだ変数だった。結果として plan-reviewer セッションは
`skill-permissions.yaml` の `plan_review` セクション (Bash 全面禁止 /
Edit・MultiEdit 禁止) を **完全にバイパスして** 動いていた。

さらに、たとえ `SKILLS` を正しく export しても、`skill-permissions.yaml` の
`plan_review.allow: Write` は非 Bash ツールの bare token signature
(`"Write"`, file_path を含まない) にしかマッチできないため、**「plan_review.md
だけ書ける」という制約はそもそも表現不可能** だった (実質「どの Write でも許可」
と同義)。`agents/plan_reviewer.md` の「plan_review.md 以外への出力禁止」は
プロンプト層のお願いに過ぎず、構造的な強制力が無かった。

## 対応 (Description の対応方針 1・2・4 + 必須項目をすべて実施)

### 1. mission.yaml を plan-reviewer に触らせない (方針4、最重要)

- `review-plan.sh` が plan-reviewer 起動時に `SKILLS=plan_review` を明示 export
  するように修正 (mux 経由・inline フォールバックの両方)。これにより
  `skill-permissions.yaml` の Bash 全面禁止 / Edit・MultiEdit 禁止が実際に働く。
- `hooks/pre-tool-use.sh` に **"Plan-review write scope guard"** を新設。
  `SKILLS` に `plan_review` が含まれるセッションの Write/Edit/MultiEdit/
  NotebookEdit を、書き込み先が `queue/missions/<slug>/plan_review.md` である
  場合のみ許可し、それ以外 (mission.yaml・task ファイル等) は deny する。
  skill-permissions.yaml の設定ミスや将来の緩和ではバイパスできない構造的ガード。
- `agents/plan_reviewer.md` の禁止事項に mission.yaml への書き込み禁止を明記し、
  Bash が使えなくなった分 Step 1 の手順を `Glob` ツールに置き換えた。

### 2. 古い plan_review.md の誤採用を防ぐ (必須パターン3、最重要)

- `review-plan.sh` はレビュー開始前に既存の `plan_review.md` を `rm -f` する。
- 開始時刻 (`REVIEW_START_EPOCH`) を記録し、ポーリング判定
  (`scripts/wait_for_plan_review.sh` に切り出し) は **mtime が開始時刻以降の
  ファイルだけ** を「今回の実行の出力」とみなす (rm が何らかの理由で効かなかった
  場合の二重の安全策)。古いファイルは無視して待ち続け、来なければ
  `TIMEOUT_NONE` としてタイムアウトする — 誤って古い判定を採用しない
  (「倒れる方向」を安全側に倒す)。

### 3. verdict 別表記の受理・正規化 (方針2)

- `scripts/normalize_plan_review_verdict.py` を新設。規定形式が無い場合のみ、
  `## 総合判定` 見出し付近から `GO`/`NO-GO`/`承認`/`却下`/`要修正`/`差し戻し` 等の
  既知の別表記を検出し、ファイル冒頭に規定形式の `**Verdict:**` 行を追記する
  (元の内容は残す。誤検知防止のため見出し付近のみを探索範囲にする)。
  判定不能な場合は何もせず失敗を返す (当て推量しない)。

### 4. plan_reviewer.md の指示強化 (方針1)

- ファイル冒頭に「★ 最重要」セクションを追加し、`**Verdict:**` 行を
  出力の1行目に置くことを最優先事項として明記。

### 5. タイムアウト時の挙動改善

- `scripts/wait_for_plan_review.sh` はタイムアウト理由を `TIMEOUT_FRESH`
  (ファイルは書かれたが判定が読めなかった) と `TIMEOUT_NONE` (ファイル自体が
  無い) に区別する。
- `scripts/plan.sh` の `cmd_review` は `TIMEOUT_FRESH` 相当 (= `plan_review.md`
  が存在するのに `review-plan.sh` が失敗) の場合、Director 向けのエラー
  メッセージで「再レビューではなく手動確認」を促す。

## 設計上のポイント: ポーリングロジックの切り出し

`review-plan.sh` 本体は `claude` CLI を spawn するため end-to-end ではテスト
しにくい。ポーリング判定だけを `wait_for_plan_review.sh` に切り出すことで、
claude を一切起動せずに3パターン (規定形式 / 別表記 / 前 cycle の残骸) を
決定論的に回帰テストできるようにした。同じ理由で verdict 正規化ロジックも
独立した Python スクリプトに切り出してある。

---

## 追記 (t004, mission 20260909-dead-config-sweep): 散文パースから構造化出力への移行

**症状**: 上記パターン1 (verdict 形式不一致) を PR #188 で「対応」したはずが、
その後も plan-reviewer (Opus) が禁止形式 (`## 総合判定: **GO**`) で出力する
事故が再発し、`normalize_plan_review_verdict.py` の別表記救済でも拾えず
600s タイムアウト → Director が capture して手動で `plan_review.md` /
`mission.yaml` を書く事態になった。**「プロンプト指示 (`★ 最重要` セクション)
だけでは機構として成立していない」ことの再実証**。

**検討した案** (Description 記載の A〜D):
- **(A) 別ファイルに1語だけ書かせる**: 採用せず。`skill-permissions.yaml` の
  `plan_review.allow: Write` が bare token signature (file_path 無し) にしか
  マッチできず「plan_review.md だけ書ける」制約が技術的に表現不可能な設計
  (このファイル冒頭「根本原因」参照) のため、追加ファイルを許可するには
  hook 権限をさらに緩めるか複雑化させる必要があり、新しい攻撃面/バグ面が増える。
- **(B) claude CLI の構造化出力機能を使う**: **採用**。`claude --help` で
  `--json-schema <schema>` (structured output validation) の実在を確認し、
  `claude --output-format json --json-schema '...'` を実機で検証 (Haiku で
  実行、`structured_output` フィールドにスキーマ通りの `{"verdict":...}` が
  返ることを確認)。Write 権限を一切変更せずに済む — 検証されるのは「セッション
  の最終応答」であり、`plan_review.md` への Write tool 呼び出しとは独立した
  チャネルのため。
- **(C) normalizer を賢くする**: 不採用。Description が明示するとおり、
  本 mission 内で kai-review.sh が「散文の判定は本質的にヒューリスティックで
  完全解が無い」ことを 6 回の実測欠陥で示した教訓と同型であり、normalize 側の
  対症療法をこれ以上重ねても同じ穴が別表記で再発するだけと判断。
- **(D) 現状維持**: 不採用。早期打ち切り (`_EARLY_BREAK_STREAK`) で待ち時間
  自体は既に軽減されているが、「Director の手動介入」自体は解消されておらず、
  実機で再発が確認されている以上、機構化の価値がある。

**実装**: `scripts/review-plan.sh` の plan-reviewer 起動コマンド (mux 経路・
inline フォールバック経路の両方) に `--output-format json --json-schema
<config/plan-review-verdict.schema.json>` を追加。既存の polling
(`wait_for_plan_review.sh`) / normalize 経路は**そのまま残し、置き換えない**
— これらが失敗した場合 (`WAIT_STATUS != OK`) のみ、`review-plan.sh` が
plan-reviewer プロセスの stdout ログ (`/tmp/plan_reviewer_$$.log`) から
`structured_output.verdict` を機械的に取り出し、`plan_review.md` 冒頭に
規定形式の行を追記して rescue する（`normalize_plan_review_verdict.py` と
同じ「原文は残す」idempotent な prepend パターン）。

**倒れる方向**: スキーマファイル欠如・JSON パース失敗・`"type":"result"` 行が
0件/複数件・verdict が3値以外、のいずれでも rescue は何もしない
(fail-closed。既存の polling/normalize 経路の判定をそのまま採用し、タイムアウト
なら Director 手動確認に倒れる — 当て推量で verdict を捏造しない)。mux 経路は
`review-plan.sh` が plan-reviewer プロセスの終了を直接待たない (`plan_review.md`
の mtime 安定化だけで判定) ため、rescue 実行前に短い bounded retry (最大 24秒、
失敗経路でのみ発火) を挟んで構造化出力の書き込みタイミングを待つ。

**副次効果**: `agents/plan_reviewer.md` の「★ 最重要」セクションから、散文
フォーマットの厳格な指示 (`**Verdict:**` を1行目に書けという命令) を撤去。
plan-reviewer は verdict の書式を一切気にする必要がなくなった (Write する
`plan_review.md` の内容は Summary/Issues として従来どおり重要だが、判定その
ものは CLI が強制する)。

**関連ファイル (追加分)**: `config/plan-review-verdict.schema.json` (新規),
`scripts/test_review_plan_json_rescue.sh` (新規)。既存の
`scripts/test_wait_for_plan_review.sh` / `scripts/test_normalize_plan_review_verdict.sh`
/ `scripts/test_review_plan_pane_leak.sh` / `scripts/test_review_plan_director_identity.sh`
は無改修のまま green (スキーマファイル欠如時は自動で機能を無効化し、既存の
scratch テストセットアップ — config/ を用意していない — でも review-plan.sh
全体を落とさない設計にしたため)。
