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
