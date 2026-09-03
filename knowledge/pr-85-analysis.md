# PR #85 分析: Worker auto permission mode

**調査日**: 2026-09-02  
**担当**: Jiwon (research,docs)  
**対象PR**: #85 `feat: Worker を auto permission mode で起動` (feat/worker-auto-mode, 2026-05-12 open)

---

## 1. PR #85 の内容

### 変更箇所 (scripts/start.sh, +2/-2 lines)

```diff
# tmux モード
-  LAUNCH_CMD="$ENV_EXPORTS; cd '$WORK_DIR'; claude${MODEL_CLI_ARG}"
+  LAUNCH_CMD="$ENV_EXPORTS; cd '$WORK_DIR'; claude${MODEL_CLI_ARG} --permission-mode auto"

# inline モード
-  exec claude "${MODEL_FLAG[@]}" "${PROMPT_FLAG[@]}"
+  exec claude "${MODEL_FLAG[@]}" "${PROMPT_FLAG[@]}" --permission-mode auto
```

### 狙い
- Worker が権限プロンプトなしで自律的に動作する
- `--permission-mode auto` で Claude Code 側の native permission prompting を無効化

---

## 2. 現在の main との比較

### 2-A. `--permission-mode auto` は main に存在しない

main の start.sh に `--permission-mode` は一切ない（grep 確認済み）。

### 2-B. 代替機能が main で実装済み

| 機能 | PR #85 の実装 | main の実装 |
|------|--------------|------------|
| Worker 権限プロンプト抑制 | `--permission-mode auto` | `hooks/pre-tool-use.sh` + `config/skill-permissions.yaml` + `settings.json` permissions.allow |
| 許可制御の粒度 | Claude Code native (soft/hard deny のみ) | スキル別 allow/deny パターン + Taskvia 承認フロー |
| 危険操作ブロック | auto mode の soft/hard deny | `_global.deny` 設定 + settings.json permissions.deny |

### 2-C. main の設計思想

現在の main は「**権限プロンプトなし**」と「**Taskvia 承認ゲート維持**」を両立させている:

1. `pre-tool-use.sh` hook が全ツール呼び出しを受け付け
2. `skill-permissions.yaml` の allow パターンにマッチ → `permissionDecision: "allow"` を返す（プロンプトなし）
3. deny パターンにマッチ → `permissionDecision: "deny"` でブロック
4. どちらにもマッチしない操作 → Taskvia 承認フローへ (既存設計を維持)
5. `settings.json` の permissions.allow も多数の一般的操作をカバー

### 2-D. PR #85 との重大な差分（conflict）

PR #85 が main に対して遅れている主な変更点（抜粋）:

- `CREWVIA_REPO_ROOT` / `CREWVIA_QUEUE` export 追加
- TARGET_DIR モードの絶対パス hook injection (settings.local.json)
- `SETTINGS_FLAG` / `--settings` フラグ機構
- systemPrompt 書き込み先を `settings.json` → `settings.local.json` に変更
- `KICKOFF_MSG` の絶対パス化リファクタリング
- watchdog v2 (watchdog.py) の tmux モード統合

main は 120+ commits 先行しており、PR #85 をそのまま rebase するには全項目で conflict 解決が必要。

---

## 3. 問題点: `--permission-mode auto` を追加すると何が壊れるか

PR #85 を現在の main に適用した場合の副作用:

1. **Taskvia 承認フローが無力化される**  
   `skill-permissions.yaml` のどのパターンにもマッチしない操作は本来 Taskvia に流れるが、
   `--permission-mode auto` があると Claude Code が自動承認してしまい、Taskvia を素通りする。

2. **セキュリティ設計の一貫性が崩れる**  
   hook によるきめ細かな allow/deny が機能している環境に auto mode を重ねると、
   hook が deny を返さない限り全操作が通過する。hook の crash や fallthrough 時の
   フォールバック挙動が変わる。

3. **テスト計画が古い**  
   PR #85 のテスト計画 (`./scripts/start.sh worker code typescript` を確認) は
   現在の start.sh の引数仕様と合わない可能性がある。

---

## 4. 判断

### **推奨: Option C — Close (完全に不要)**

#### 理由

1. **機能重複**: Worker の権限プロンプト抑制は hook + settings.json allow list で既に実現されている。
   `--permission-mode auto` を追加しても Worker の体験は変わらない
   （hook が `allow` を返せばプロンプトは出ない）。

2. **機能衝突**: `--permission-mode auto` は Taskvia 承認フロー (fallthrough 時) を破壊する。
   これは現在の設計の根幹部分。

3. **Conflict 多大**: main と 120+ commits の差分があり、start.sh に大規模な競合が発生する。
   merge コストが価値を上回る。

4. **設計方針の転換**: PR #85 作成時 (2026-05-12) は hook-based permission system が存在しなかった。
   その後の開発で hook + Taskvia が主権限制御機構となり、`--permission-mode auto` の
   存在意義が消えた。

---

## 5. Close 時の推奨コメント文面 (t009 用)

```
## クローズ理由

PR #85 が実装しようとした「Worker の権限プロンプト抑制」は、
現在の main (PR #101, #107 以降) で別のアプローチにより実現済みです。

### 現在の実装
- `hooks/pre-tool-use.sh` + `config/skill-permissions.yaml` によるスキル別 allow/deny
- `.claude/settings.json` の permissions.allow リストによる一般操作の自動許可

### `--permission-mode auto` を追加しない理由
Claude Code の native auto mode を有効にすると、
`skill-permissions.yaml` にマッチしない操作が Taskvia 承認を素通りしてしまいます。
現在の設計では「よく使う操作は自動許可、新規操作は Taskvia で確認」という方針を採用しており、
`--permission-mode auto` はこれと相容れません。

main との diff も 120+ commits あり、rebase コストも高い。

ラベル: `既に main で実現済み`
```

---

## 6. 参考ファイル

- `hooks/pre-tool-use.sh` — 権限決定ロジック本体
- `config/skill-permissions.yaml` — スキル別 allow/deny パターン定義
- `.claude/settings.json` — permissions.allow/deny 設定
- PR #101 (MERGED) — systemPrompt 書き込み先 settings.local.json 化
- PR #107 (MERGED) — hook crash guard 修正
- PR #122 (MERGED) — TARGET_DIR hook injection
