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
[WARN] timeout: task t002 has no timeout specified (default will apply)
[OK]   dependency: no circular dependencies found
[OK]   skill: all task skills match skill-permissions.yaml
Exit: 0 (1 warning)

$ plan.sh lint 20260418-auth-refactor --strict
[OK]   frontmatter: all required fields present (3 tasks)
[FAIL] timeout: task t002 has no timeout specified (default will apply)
[OK]   dependency: no circular dependencies found
[OK]   skill: all task skills match skill-permissions.yaml
Exit: 1 (strict mode: 1 warning treated as failure)
```
