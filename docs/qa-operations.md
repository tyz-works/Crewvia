# QA オペレーションガイド

このドキュメントでは、Crewvia の品質検査ツールの使用方法を説明する。

---

## plan.sh lint — 静的品質検査

Mission の YAML ファイルを静的解析し、フォーマット・依存関係・スキル・タイムアウトの整合性を検査する。

### 基本的な使い方

```bash
# Mission の静的検査を実行
plan.sh lint <mission_slug>

# WARN も FAIL として扱う strict モード
plan.sh lint <mission_slug> --strict
```

### 出力フォーマット

```
[OK]   <category>: <message>   # 正常
[WARN] <category>: <message>   # 警告（--strict で FAIL 扱い）
[FAIL] <category>: <message>   # 失敗（exit 1）
```

### 検査カテゴリ

| カテゴリ | 検査内容 |
|---------|---------|
| `frontmatter` | 必須フィールド・型・有効値の検査 |
| `dependency` | 循環依存・未定義参照の検査 |
| `skill` | タスクスキルと skill-permissions.yaml の整合確認 |
| `timeout` | タスク timeout と timeout-profiles.yaml の整合確認 |

### exit code

| code | 意味 |
|------|------|
| `0` | [FAIL] なし（正常） |
| `1` | [FAIL] あり、または --strict 時に [WARN] あり |

### CI 連携の例

```bash
# [FAIL] があれば CI を止める
plan.sh lint my-mission || exit 1
```

### 実行例

```bash
$ plan.sh lint 20260418-auth-refactor
[OK]   frontmatter: all required fields present (3 tasks)
[OK]   task/t001: all checks passed
[OK]   task/t002: all checks passed
[OK]   dependency: no circular dependencies found
[OK]   skill: all task skills match skill-permissions.yaml
Exit: 0

$ plan.sh lint 20260418-auth-refactor --strict
[OK]   frontmatter: all required fields present (3 tasks)
[WARN] timeout: task t003 timeout 7200s exceeds profile maximum 3600s
[OK]   dependency: no circular dependencies found
[OK]   skill: all task skills match skill-permissions.yaml
Exit: 1 (strict mode: 1 warning treated as failure)
```

> **注意**: `timeout` 未指定のタスクは検査対象外としてスキップされる（WARN も出ない）。
> WARN/FAIL が発生するのは timeout 値が timeout-profiles.yaml の許容範囲を外れた場合のみ。

---

## Verifier Dispatcher

`ready_for_verification` 状態のタスクを自動検出し、idle Verifier に assign するデーモン。Worker Dispatcher とは独立して稼働する。

### 起動

```bash
# バックグラウンドで起動
nohup ./scripts/verifier-dispatcher.sh > logs/verifier-dispatcher.log 2>&1 &
echo $! > /tmp/verifier-dispatcher.pid
```

### 停止

```bash
kill $(cat /tmp/verifier-dispatcher.pid)
```

### 監視

```bash
# ログ確認
tail -f logs/verifier-dispatcher.log

# 動作確認（ready_for_verification タスクが verifying に遷移するか）
plan.sh status --all | grep -E "ready_for_verification|verifying"
```

### 設定

| 環境変数 | デフォルト | 説明 |
|---|---|---|
| `VERIFIER_POLL_INTERVAL` | 30 | ポーリング間隔（秒）|
| `NOTIFY_TTL` | 60 | 通知 dedup 有効期間（秒）|

### 自己検証の禁止

Verifier Dispatcher は同一 agent が同じ task の worker かつ verifier になることを自動的に防ぐ。Worker が `plan.sh ready-for-verification t001` を呼ぶと、Dispatcher は worker フィールドと異なる idle Verifier を探して assign する。

---

## risk flag 上流→下流伝播

Plan Reviewer が `risk_flags` を付けたタスクは、`plan.sh review` の完了時に自動的に `verification.mode` が昇格する。

### 昇格ルール

`light < standard < strict` の順で、**より厳しい方が優先**される。

- Director が手動で `strict` と書いたタスクは、Plan Reviewer の `standard` 判定で上書きされない
- Plan Reviewer が `strict` を推奨したタスクは、Director の `standard` 設定より優先される

### rework 学習ループ

`rework_count >= max_rework` に達した task の情報は `knowledge/director.md` に自動追記される。Director は計画時にこのファイルを参照することで、過去の失敗パターンを活かせる。

```bash
# 学習ログを確認
cat knowledge/director.md
```
