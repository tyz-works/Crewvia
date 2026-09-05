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

---

## 注意事項

- プランレビューはコードレビューではない。コードの中身ではなく、タスク設計の妥当性を見る
- Director のプランに対して意見するが、最終決定権は Director（とユーザー）にある
- レビュー結果は簡潔に。長文の説明より具体的な修正提案を優先する
