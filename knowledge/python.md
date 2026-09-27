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

## 2026-09-28 Ren発見: red proof のテストが「別のガード」で偶然緑になり、狙った欠陥を見逃す

`tests/leaked_descendants._open_pidfd_verified(pid, expected_start)` の
`signal.pidfd_send_signal` 不在ゲート漏れ (P2-2) を確かめるテストで、`expected_start=0`
(ダミー値) を渡していた。ゲートを外す欠陥を注入して `red_proof_t047.sh` を走らせたら、
このテストだけ赤にならなかった —— `expected_start=0` は実際の starttime とまず一致しないので、
関数内の**別の**再確認 (`recheck[2] != expected_start` → pid 再利用の疑いとして `None` を返す)
が、確かめたかった signal 側のゲートより**先に**同じ `None` を返し、欠陥があってもテストが
偶然パスしてしまった。

教訓: 複数の独立したガードが同じ戻り値 (`None` / `False` 等) に収束する関数を単体テストする
ときは、**そのガード以外の分岐が絶対に発火しない入力**を選ぶこと。ダミー値・境界値を安易に
使うと、狙ったガードを経由せずに「たまたま」正しい戻り値になり、そのガードを外しても
テストが赤くならない (memory: `red-proof-defeated-by-backstops` と同族 — バックストップが
別ファイルではなく**同じ関数内の別の if 分岐**でも起こる)。防ぎ方: 欠陥を注入して
`red_proof_*.sh` を実際に走らせ、狙ったテストが赤くなることを確認する。「書いた・通った」
だけで済ませない。

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
