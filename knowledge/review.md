# review ナレッジベース

> このファイルは review を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## レビュープロトコル

- PR を受け取ったら `gh pr view {PR_URL}` で差分を確認する
- `gh pr diff {PR_URL}` でコード変更を精査する
- 問題なければ `gh pr review {PR_URL} --approve` で承認
- `gh pr merge {PR_URL} --squash --delete-branch` でマージ
- マージ完了後、Orchestrator に報告する

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
