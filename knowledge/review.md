# review ナレッジベース

> このファイルは review を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## レビュープロトコル

1. `gh pr view {PR_URL}` で概要確認
2. `gh pr diff {PR_URL}` でコード変更を精査する
3. 問題なければ Bot として承認: `gh-app-review approve {PR番号}`
4. マージ: `gh pr merge {PR番号} --squash --delete-branch`
   - `gh-app-review merge` は "Resource not accessible by integration" エラーが出ることがあるため、通常の `gh pr merge` を使う
5. マージ完了後、Director に報告する

## 注意事項

- 承認は必ず `gh-app-review approve` を使うこと（同一アカウントのセルフ承認は GitHub が拒否する）
- `~/bin/gh-app-review` と `~/.key/elni-net-reviewer.*.private-key.pem` が存在することが前提
- トークン取得に失敗する場合は `gh-app-review token` でデバッグ

## Verification Push 運用

QA レイヤーが verification 結果を Taskvia に push するフロー。

```
plan.sh verify-result <task_id> <verdict> [rework_count]
    → scripts/taskvia-verification-sync.sh
    → POST $TASKVIA_URL/api/verification
    → Taskvia Board: バッジ更新 (5s polling で自動反映)
```

**前提**: Taskvia Board にバッジを表示するには、先に `POST /api/request` で approval card が作成されている必要がある（task 実行開始時に hooks が自動実行）。`POST /api/verification` だけではバッジは表示されない。

**TTL**:
- `verification:{task_id}`: 7 日
- `verification:index:{slug}`: TTL なし（lazy cleanup）
- `approval:{id}`: 600 秒 — verification push は approval card 作成後 5 分以内に実施すること

**no-op 条件**: `TASKVIA_TOKEN` 未設定 or `CREWVIA_TASKVIA=disabled` 時はスキップ。

---

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-15 t005 Edit -->

<!-- log: 2026-04-15 t005 Edit -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-15 t005 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-21 t001 Bash -->

<!-- log: 2026-04-22 t001 Bash -->
