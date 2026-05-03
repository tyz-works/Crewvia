# python ナレッジベース

> このファイルは python（Python 開発）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

## 2026-05-03 Claude Code JSONL セッションログの構造

`.claude/projects/<slug>/*.jsonl` の各行は独立した JSON オブジェクト。重要な型:

- `type=assistant`: AI ターン。`message.usage` に token 情報 (input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens)
- `type=system, subtype=compact_boundary`: コンパクションイベント。`compactMetadata.durationMs` / `preTokens` / `postTokens` を持つ
- `type=user`: ユーザーターン（メッセージ境界の検出に使える）
- `timestamp` フィールドは ISO 8601 (Z suffix)、`datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()` でパース可能

`usage.input_tokens` は実際には非常に小さい（3〜数十）こともある — キャッシュヒット時は cache_read_input_tokens がほぼ全て。

## 2026-05-03 Python -c で sys.exit() しても複数 print の出力が残る問題

`python3 -c "... ; print(fname); sys.exit(0)"` の結果を bash variable に収めようとすると、
sys.exit() が効いても前の print 出力が改行なしで連結されることがある。
`sys.stdout.write(fname)` + `> /tmp/file.txt` 経由でも同じ現象が起きた。
安全策: ファイル名を直接ハードコードするか `head -1` で最初の行だけ取り出す:
```bash
COMPACT_FILE=$(python3 -c "..." | head -1)
```

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
