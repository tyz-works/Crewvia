# code ナレッジベース

> このファイルは code（コーディング全般）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->

## 2026-09-08 「呼び出しのために絶対パスを打つ習慣」自体が事故の温床になりうる (plan wrapper, t002)

Worker が `$CREWVIA_REPO_ROOT/scripts/plan.sh ...` を毎回タイプする運用は、タスク管理コマンドの
「呼び出し」としては正しいが、その打鍵の習慣が git 操作にまで無意識に持ち込まれ
`cd $CREWVIA_REPO_ROOT && git checkout -b ...`(main checkout への直接操作)に至った事故があった。
対策として `scripts/bin/plan` という薄いラッパー(`exec "$CREWVIA_REPO_ROOT/scripts/plan.sh" "$@"`)
を作り、`scripts/start.sh` で PATH に追加することで、そもそも絶対パスを書く理由を無くした。
ポイント: mux (tmux/herdr) が spawn する新しいペインは起動元プロセスの env/PATH を継承しない
(`spawn()` の `env=` 引数はどちらの backend でも未実装 — [[lib-mux-spawn-env-arg-ignored]] 参照)。
そのため PATH 拡張は (1) 現在プロセスの `export PATH=...`(inline モード用)と (2) mux が実行する
LAUNCH_CMD 文字列内に埋め込む `export PATH=...`(mux モード用)の **両方** が必要。片方だけだと
モードによって効いたり効かなかったりする。
