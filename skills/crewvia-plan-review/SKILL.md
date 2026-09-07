---
name: crewvia-plan-review
description: Use when reviewing a Director's mission plan before Worker execution begins. Invoke when assigned a task with skills containing "planning", or when asked to review task decomposition, dependencies, and skill assignments in a crewvia mission.
---

# Crewvia Plan Review スキル

Director が作成したミッションプランをレビューし、問題点・改善案を報告する。

---

## Step 1: プランの全体像を把握する

```bash
# active mission の一覧と詳細を確認
./scripts/plan.sh status
./scripts/plan.sh status --mission <slug>
```

確認すべき情報:
- ミッションの目的（mission.yaml の title）
- タスク一覧とその依存関係
- 各タスクのスキル割り当てと優先度

---

## Step 2: 以下の観点でレビューする

### 2-1. タスク粒度

- [ ] 1タスクが大きすぎないか（複数の独立した作業が1タスクに詰め込まれていないか）
- [ ] 逆に細かすぎないか（1行の変更が独立タスクになっていないか）
- [ ] 各タスクの title と description から成果物が明確に読み取れるか

### 2-2. 依存関係の妥当性

- [ ] 不要な依存で直列化されていないか（並列実行できるのに blocked-by が付いている）
- [ ] 必要な依存が抜けていないか（前提タスクなしに実行すると壊れるタスクがある）
- [ ] 循環依存がないか
- [ ] ファンアウト（並列→集約）パターンが活用されているか

### 2-3. スキル割り当て

- [ ] 各タスクに適切なスキルタグが付与されているか
- [ ] registry に該当スキルの Worker が存在するか（不在なら Director に新規登録を提案）
- [ ] 1タスクに不必要に多くのスキルが付いていないか

### 2-4. QA カバレッジ

- [ ] 成果物がある実装タスクに QA タスクがセットで積まれているか
- [ ] QA タスクの blocked-by が実装タスクに正しく設定されているか
- [ ] QA タスクの description に検証観点が記載されているか（なければ追記を提案）

### 2-5. 優先度と実行順序

- [ ] 優先度がミッションの目的と整合しているか（クリティカルパスが high になっているか）
- [ ] ブロッカーになるタスク（多数の後続が依存）が適切に高優先度になっているか

### 2-6. 成果物の完結性

- [ ] ミッションの目的を達成するために必要なタスクが全て揃っているか
- [ ] 最終成果物（PR merged / デプロイ完了 等）までカバーされているか
- [ ] review タスクが含まれているか（PR を伴う場合）

### 2-7. review タスク必須チェック

- [ ] code / docs / typescript / python / bash 等の skill を持つ task が 1 つでも存在する場合、
      その後段に review skill の task が設定されているか
- [ ] review task の blocked_by が対象 task を正しく指しているか
- [ ] review task の description に「verdict LGTM → plan.sh done、
      要修正 → plan.sh needs-director」が明示されているか

---

## 推奨 task template

### review task

タイトル例: `PR #XXX code review findings 確認`

```yaml
skills: [review]
blocked_by: [<PR を作った task の ID>]
priority: high
description: |
  対象 PR: #XXX (branch: <branch-name>)

  review 観点:
  - correctness: バグ・ロジックエラー
  - reuse/efficiency: 重複・非効率なコード
  - doc cross-reference: ドキュメントとコードの整合性
  - test: テストカバレッジ・テストの妥当性

  required_evidence:
  - findings 一覧（severity: critical/high/medium/low 付き）
  - 各 finding の修正/放置判断

  verdict rule:
  - findings なし、または全て low で放置可 → plan.sh done (「LGTM: 問題なし」を明記)
  - 修正が必要な finding あり → plan.sh needs-director (finding 一覧と修正提案を記載)
```

### PR fix task

タイトル例: `PR #XXX fix: <内容>`

```yaml
skills: [<対象 PR と同じ skill>]
blocked_by: [<review task の ID>]
priority: high
description: |
  対象 PR: #XXX (head branch: <branch-name>)

  作業手順:
  1. worktree に cd する (または既存 worktree を利用)
  2. git checkout <branch-name>  # branch mismatch 防止のため PR head branch を明示的に checkout
  3. 実装・修正を行う
  4. git push origin <branch-name>

  required_evidence:
  - push 済み commit hash
  - gh pr view #XXX --json commits で commit が PR に含まれることを確認

  verdict rule:
  - PR commits に自分の commit が含まれる → plan.sh done
  - 含まれない (branch mismatch 等) → plan.sh needs-director (branch mismatch として報告)
```

---

## Step 3: レビュー結果を報告する

`plan.sh done` に渡す結果サマリーのフォーマット:

```
プランレビュー結果: [mission: <slug>]

チェック項目:
  ✅ タスク粒度: 適切
  ✅ 依存関係: 問題なし
  ⚠️ スキル割り当て: t003 に typescript が不足
  ✅ QA カバレッジ: 全実装タスクに QA あり
  ✅ 優先度: クリティカルパスが high
  ❌ 成果物の完結性: review タスクが未登録

修正提案:
  1. t003 に --skills "code,typescript" を追加
  2. t005 の後に review タスクを追加 (--skills review --blocked-by t004,t005)

総合判定: 修正後 GO / GO / STOP
```

### 判定基準

| 判定 | 意味 |
|------|------|
| **GO** | 問題なし。Worker 起動してよい |
| **修正後 GO** | 軽微な修正が必要。Director が修正すれば即実行可 |
| **STOP** | 重大な問題あり。タスク分解をやり直すべき |

### verdict expression rule (review Worker 向け)

review skill の Worker がタスク完了を報告する際の verdict 表現ルール:

| 状況 | verdict | plan.sh コマンド |
|------|---------|----------------|
| findings なし / 全て low で放置可 | LGTM: 問題なし（理由を明記） | `plan.sh done` |
| 修正が必要な finding あり | NEEDS FIX: <finding 一覧と修正提案> | `plan.sh needs-director` |
| PR commits に自分の commit が含まれない | BRANCH MISMATCH: <branch 名と状況> | `plan.sh needs-director` |

**重要**: 「修正すれば問題ない」と自己判断して `plan.sh done` しない。修正要否の判断は Director に委ねる。

---

## Codex reviewer (Kai-codex) の使い分け

> Phase 1 (2026-09-07) で導入、**Phase 2 (2026-09-08) で自動化**。詳細手順は `knowledge/codex-reviewer.md` 参照。

Priya がプラン設計時に **Claude (Seo) のみ / Seo + Kai-codex の 2 人体制** を判断する基準：

### 2 人体制を推奨するケース

- **重要 mission**（main / staging に影響する code 変更）
- **Claude 生成コードの review**（同一モデル bias を避けたい）
- **critical bug 疑いのある大きな diff**（大規模リファクタリング / auth / billing 等）
- **LLM 特有の誤りパターンが懸念される場面**（hallucination / edge case 見落とし）

### Claude (Seo) 1 人で十分なケース

- docs-only の変更（MEMORY 更新 / README / knowledge 追記等）
- minor fix（1-5 行程度の typo fix / コメント修正）
- 設定ファイルの単純変更

### verdict 突合フロー

2 人体制の場合、**Priya は Seo と Kai-codex の両方の review task をプランに積む**（Phase 2 以降）。

```yaml
# Seo review task（Claude が実行）
- title: "PR#XXX review (Seo)"
  skills: [review]
  blocked_by: [<実装 task の ID>]
  description: |
    対象 PR: #XXX
    verdict rule: LGTM → plan.sh done / 要修正 → plan.sh needs-director

# Kai-codex review task（Dispatcher が自動 spawn）
- title: "PR#XXX Codex review (Kai)"
  skills: [codex-review]
  blocked_by: [<実装 task の ID>]
  pr_number: XXX          # ★ 必須。dispatcher が --pr で渡す
  description: |
    Codex CLI による adversarial review。Seo と独立に判定する。
```

`plan.sh add` の呼び出し例:
```bash
plan.sh add "PR#42 review (Seo)"      --skills review        --blocked-by t003
plan.sh add "PR#42 Codex review (Kai)" --skills codex-review  --blocked-by t003 --pr-number 42
```

| 突合結果 | Director の対応 |
|---|---|
| 両方 LGTM | merge 承認（Seo に gh pr merge 指示） |
| どちらか NEEDS FIX | findings を統合して修正タスクを判断 |
| BRANCH MISMATCH | 優先度最高、即対応 |

### Phase 2 の Priya への注意

- **codex-review skill task には `--pr-number` を必ず指定する**。指定なしだと dispatcher は spawn せず warning を出す
- Kai-codex は registry に登録済みなので `plan.sh done` で task_count が自動 bump される
- Kai-codex のカードは Taskvia カンバンに表示される（plan.sh pull 経由で in_progress → done が sync される）
- タイムアウト時は Director が `rm queue/assignments/Kai-codex` + `plan.sh update <task> --reset` で復旧する

---

## 注意事項

- プランレビューはコードレビューではない。コードの中身ではなく、タスク設計の妥当性を見る
- Director のプランに対して意見するが、最終決定権は Director（とユーザー）にある
- レビュー結果は簡潔に。長文の説明より具体的な修正提案を優先する
