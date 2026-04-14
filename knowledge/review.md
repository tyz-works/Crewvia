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

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
