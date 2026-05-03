# typescript ナレッジベース

> このファイルは typescript（TypeScript / JavaScript）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

## 2026-05-03 Node 25 で TypeScript を npm install なしに実行する方法

Node 22.6+ の `--experimental-strip-types`（Node 23.6+ からは `--strip-types`）で TypeScript ファイルを直接実行できる。
Node 25.x では `--strip-types` が安定版として使える。

```bash
# テスト実行（npm install 不要）
node --strip-types --test tests/utils.test.ts

# TS ファイルを直接実行
node --strip-types src/index.ts
```

注意点:
- `import type` 構文を必ず使う（型だけの import は `import type` でないとエラーになる）
- 型チェックは行わない（型エラーがあってもランタイムは通る）
- package.json に `"type": "module"` を設定し、import パスに `.ts` 拡張子を使う

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
