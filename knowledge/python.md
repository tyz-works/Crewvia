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

## 2026-09-27 `git status --porcelain` は既定で ignored ファイルを数えない

`git status --porcelain=v1 --untracked-files=all` は ignored なファイルを一切含まない。
`.env` 等を `.gitignore` している repo で「クリーンかどうか」を破壊操作の判定材料にするなら、
`--ignored=matching` も足して `!! ` prefix の行を別扱いすること（`worktree_gc.py` t057 / PR#239 F1）。
`git worktree remove` は `--force` 無しでも ignored ファイルの削除を許すので、この見落としは実際に
secrets を消す方向に倒れる。`--ignored=matching` はディレクトリ全体が ignore パターンにマッチする場合、
中身を 1 つずつ列挙せず親ディレクトリ 1 行にまとめる（`--untracked-files=all` の未追跡ディレクトリ展開とは
挙動が違う）。

## 2026-09-27 lsof は一部のプロセスで失敗しても部分出力を出しつつ非 0 を返す

`subprocess.run(['lsof', ...])` の `stdout` が空でないことだけを「完全なスキャンができた」根拠にすると、
一部のプロセスの検査に失敗した部分出力を「見つからなかった (= 使われていない)」と誤読しうる
(`worktree_gc.py` t057 / PR#239 F3)。`returncode != 0` を先にチェックして拒否すること。

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
