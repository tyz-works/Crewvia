# bash ナレッジベース

> このファイルは bash（シェルスクリプト・コマンド実行）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->

## 2026-04-30 fzf --preview スクリプトはファイルに書き出して渡す

fzf の `--preview` に複雑な jq 式を直接埋め込もうとすると、bash のクォートエスケープが地獄になる。
`mktemp` で一時スクリプトを生成し、`--preview="$tmpscript {1} ..."` の形で渡すと安全。

単一クォートの heredoc（`<<'PREVEOF'`）でスクリプトを書き出せば、`$1`, `$2` 等が展開されず意図通りに書き込まれる。
jq の文字列内では `` が ESC として解釈されるので、ANSI カラーコードも問題なく出力できる。

## 2026-05-03 bash 配列は関数に渡せない — グローバル変数 + インデックスで共有する

bash の配列は関数の引数として直接渡せない（`"${arr[@]}"` で要素展開すると文字列の並びになり配列メタデータが失われる）。
グローバル変数として宣言し、関数内でインデックス参照する方が安全。
または `local -n ref="$1"` (nameref) を使えば参照渡しが可能（bash 4.3+）。

## 2026-05-03 plan.sh add の出力から task ID を取得する

`plan.sh add` は `Added: {slug}/{task_id} — {title}` 形式で出力する。
`grep -oE '\bt[0-9]+\b' | head -1` でタスク ID (例: t001) を抽出できる。

## 2026-05-03 JSONL diff でセッションファイルを特定するパターン

Claude Code セッションの JSONL ファイルは `~/.claude/projects/<path-mapped>/*.jsonl` に保存される。
パスマッピング: `echo "$dir" | sed 's|^/||; s|/|-|g'`

起動前後の diff でセッションファイルを特定:
```bash
before=$(ls ~/.claude/projects/<key>/*.jsonl 2>/dev/null | sort || true)
# ... start Claude ...
after=$(ls ~/.claude/projects/<key>/*.jsonl 2>/dev/null | sort || true)
new_file=$(comm -23 <(echo "$after") <(echo "$before") | head -1)
```

## 2026-04-30 jq でイテレーション + 文字列結合は [.[] | ...] | join("\\n") パターン

jq で複数要素をまとめて1つの出力にしたい場合、`.[]` をそのまま文字列に連結しようとすると各要素が独立した出力になる。
`[.[] | "..." ] | join("\n")` で配列に集約してから join するのが正しいパターン。
