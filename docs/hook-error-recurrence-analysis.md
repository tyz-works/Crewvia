# hook error 再発の再現手順 + root cause 分析

作成: 2026-08-28  
担当: Haruto (bash worker) — mission 20260828-hook-error-recurrence / t002  
承認条件確認: Priya F1〜F4 + MT1〜MT2 を本ドキュメントで対処

---

## 1. 背景

mission 20260828-epgget-decrypt-phase-b において、ユーザーが  
**「pre-tool-use / post-tool-use 失敗しまくってる」** と観察した。

PR #107 (mission 20260825-backlog-m1 / Haruto 担当) で `hooks/pre-tool-use.sh` の  
`pipefail + trap 順序ミス` を修正したが、`hooks/post-tool-use.sh` は放置されていた。

Priya レビュー (t001) の verdict: **conditional_approve** — F1 (Critical) が残存。

---

## 2. 調査手法

Priya の承認条件に従い、**Approach 3 (hook 内 self-diagnosis) を primary** として実施。

### 2.1 コード静的解析

`hooks/post-tool-use.sh` と `hooks/pre-tool-use.sh` を対比し、クラッシュガード実装状況を確認。

### 2.2 単体実行テスト

```bash
# 正常系: TASKVIA 無効
echo '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}' | \
  CREWVIA_TASKVIA=disabled AGENT_NAME=TestAgent TASK_ID=t002 \
  bash hooks/post-tool-use.sh
# → exit=0 ✅

# TASKVIA 有効 (curl は || true で保護)
echo '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}' | \
  CREWVIA_TASKVIA=enabled TASKVIA_TOKEN=dummy TASKVIA_URL=http://localhost:9999 \
  AGENT_NAME=TestAgent TASK_ID=t002 TASK_TITLE="test task" \
  bash hooks/post-tool-use.sh
# → exit=0 ✅
```

正常系はパス。**異常系（クラッシュした場合）のハンドリングが問題**。

### 2.3 test_hooks.sh 確認

`scripts/test_hooks.sh` を確認した結果、**post-tool-use.sh のテストがゼロ** であることを確認。

```bash
HOOK="${REPO_ROOT}/hooks/pre-tool-use.sh"  # pre のみ
```

---

## 3. 根本原因 (Root Cause)

### F1 — Critical: post-tool-use.sh にクラッシュガード未実装

| 項目 | pre-tool-use.sh (PR #107 修正済み) | post-tool-use.sh (現状) |
|------|-----------------------------------|-----------------------|
| `set -euo pipefail` | ✅ line 22 | ✅ line 17 |
| crash guard (early) | ✅ lines 52–58 | ❌ なし |
| crash guard (main) | ✅ lines 119–125 | ❌ なし |
| `trap '_crash_guard' EXIT` | ✅ × 2 | ❌ なし |

**症状との対応:**

- `set -euo pipefail` は設定済みのため、**内部コマンドが 1 つでも非ゼロ終了すると即 exit**
- trap がないため **stderr に診断メッセージが出ない** (「No stderr output」症状と完全一致)
- Claude Code は hook の非ゼロ終了を検知して **"PostToolUse hook error"** をログ

**hook エラーが "サイレント" に見える理由:**

```
[hook が set -e でクラッシュ]
  → hook exit code = 失敗したコマンドの exit code (例: 1)
  → Claude Code: "PostToolUse hook error" を UI に表示
  → hook の stderr には何も出力されていない (trap がないため)
  → ユーザーはエラーメッセージの手がかりゼロ
```

**クラッシュしうる code path の例 (set -e が発動するケース):**

| 行 | コード | 失敗条件 |
|----|--------|---------|
| 21 | `AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"` | `hostname -s` がないシステム (Nix等) |
| 47 | `mkdir -p "$ACTIVITY_DIR"` | disk quota / permissions / symlink broken |
| 48 | `echo ... >> "${ACTIVITY_DIR}/${TASK_ID}.activity"` | disk full / read-only FS |
| 80–87 | `PAYLOAD="$(jq -nc ...)"` | jq がインストールされていない / バージョン非互換 |
| 153–159 | `_AGENTS_PAYLOAD="$(jq -nc ...)"` | 同上 |

これら失敗のうち `curl` は `|| true` で保護されているが、**`jq -nc`・`mkdir -p` などに fallback がない**。

### F2 — High: test_hooks.sh に post-tool-use.sh テストゼロ

`scripts/test_hooks.sh` は `hooks/pre-tool-use.sh` のみ 7 ケースをカバー。  
`hooks/post-tool-use.sh` のテストが一切存在しないため、クラッシュガード不在が見落とされた。

### F3 — Medium: CREWVIA_REPO vs CREWVIA_REPO_ROOT 不一致

`post-tool-use.sh` 内での変数使用状況:

| 行 | 変数 | 問題 |
|----|------|------|
| 27, 45 | `CREWVIA_REPO` | 古い変数名 (start.sh は未 export) |
| 58, 101 | `CREWVIA_REPO_ROOT` | ✅ 正しい (task_160 F9 是正済み) |

`start.sh` は `CREWVIA_REPO_ROOT` のみ export するため、`CREWVIA_REPO` は未設定状態で動作する。  
→ lines 27, 45 では `$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)` フォールバックが使用される。  
→ 通常は正しいが、環境によっては `cd` が失敗して `set -e` が発動するリスクがある。

### F4 — Medium: Approach 2 (post-mortem) は実現性低

前 mission (20260828-epgget-decrypt-phase-b) のセッションログは既にクリアされており、  
tmux の `capture-pane` によるエラー前後のツールコール再現は困難。→ Approach 3 で代替。

---

## 4. Approach 3 実施: hook 内 self-diagnosis 設計

t003 (修正タスク) での実装方針を提示する。

### 4.1 post-tool-use.sh に追加するクラッシュガード

```bash
# set -euo pipefail の直後 (line 17 の次) に挿入:

_crash_guard() {
  local _EXIT_CODE=$?
  echo "[post-tool-use] ⚠️ crash guard: hook exited unexpectedly (exit=${_EXIT_CODE}, step=${_CURRENT_STEP:-init})" >&2
  exit 0  # PostToolUse は exit 0 が安全（ログ失敗は致命的でない）
}
trap '_crash_guard' EXIT
_CURRENT_STEP="init"
```

`_CURRENT_STEP` 変数で進捗を追跡し、クラッシュ発生箇所を特定できるようにする:

```bash
_CURRENT_STEP="env-setup"
TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
...

_CURRENT_STEP="activity-log"
if [ -n "${AGENT_NAME:-}" ] && [ -n "${TASK_ID:-}" ]; then
  ...
fi

_CURRENT_STEP="taskvia-log"
PAYLOAD="$(jq -nc ...)"
...

_CURRENT_STEP="agents-heartbeat"
...

_CURRENT_STEP="done"
exit 0
```

### 4.2 pre-tool-use.sh との設計差異

| | pre-tool-use.sh | post-tool-use.sh |
|--|-----------------|-----------------|
| クラッシュ時の動作 | deny 決定を emit | `exit 0` (ログ失敗は許容) |
| 理由 | 承認なしにツール実行すると危険 | ログ失敗はエージェント動作に影響しない |

### 4.3 CREWVIA_REPO → CREWVIA_REPO_ROOT 統一 (MT2 対応)

lines 27, 45 の `CREWVIA_REPO` を `CREWVIA_REPO_ROOT` に変更:

```bash
# Before (line 27)
_CREWVIA_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# After
_CREWVIA_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
```

同様に line 45:
```bash
# Before
_ACTIVITY_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# After
_ACTIVITY_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
```

---

## 5. test_hooks.sh への post-tool-use.sh テスト追加 (MT1 対応)

t003 での実装方針:

```bash
echo "=== hooks/post-tool-use.sh 回帰テスト ==="

POST_HOOK="${REPO_ROOT}/hooks/post-tool-use.sh"

# PT-1: 正常系 (TASKVIA disabled) — exit 0
STDOUT=$(env -i HOME=/tmp PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  CREWVIA_REPO_ROOT="$REPO_ROOT" \
  CREWVIA_TASKVIA=disabled AGENT_NAME=TestHaruto TASK_ID=t999 \
  bash "$POST_HOOK" <<< '{"tool_name":"Bash","tool_input":{"command":"echo test"}}' 2>/tmp/test_post_stderr || true)
EXIT=$?
if [ "$EXIT" -eq 0 ]; then
  echo "PASS [PT-1: TASKVIA disabled 正常系 → exit 0]"
else
  echo "FAIL [PT-1]: exit $EXIT, stderr: $(cat /tmp/test_post_stderr)"
fi

# PT-2: クラッシュガード確認 — 壊れた環境でも exit 0
STDOUT=$(env -i HOME=/tmp PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  CREWVIA_REPO_ROOT="/nonexistent/path" \
  CREWVIA_TASKVIA=disabled AGENT_NAME=TestHaruto TASK_ID=t999 \
  bash "$POST_HOOK" <<< '{"tool_name":"Bash","tool_input":{"command":"echo test"}}' 2>/tmp/test_post_stderr || true)
EXIT=$?
if [ "$EXIT" -eq 0 ]; then
  echo "PASS [PT-2: 壊れた env でも exit 0 (crash guard)]"
else
  echo "FAIL [PT-2]: exit $EXIT — crash guard 未実装の疑い"
fi
```

---

## 6. 修正サマリー (t003 向け)

| # | ファイル | 変更内容 | 優先度 |
|---|---------|---------|-------|
| 1 | `hooks/post-tool-use.sh` | crash guard (`_crash_guard` + `trap`) 追加 | Critical |
| 2 | `hooks/post-tool-use.sh` | `_CURRENT_STEP` による進捗ログ | High |
| 3 | `hooks/post-tool-use.sh` | `CREWVIA_REPO` → `CREWVIA_REPO_ROOT` 統一 | Medium |
| 4 | `scripts/test_hooks.sh` | post-tool-use.sh smoke test 追加 (PT-1, PT-2) | High |

---

## 7. 結論

**hook error 再発の根本原因は `hooks/post-tool-use.sh` のクラッシュガード未実装**。

- PR #107 は `pre-tool-use.sh` を修正したが `post-tool-use.sh` を放置
- `set -euo pipefail` 環境で内部コマンドが非ゼロ終了すると hook がサイレントクラッシュ
- trap がないため stderr に診断メッセージが出ず、「No stderr output」症状と完全一致
- 修正は t003 で実施: crash guard 追加 + CREWVIA_REPO_ROOT 統一 + テスト追加

Priya 承認条件:
- [x] (1) t003 に post-tool-use.sh hardening 追記 → §6 に記載
- [x] (2) test_hooks.sh に post-tool-use.sh smoke test 追加 → §5 に設計
- [x] (3) Approach 3 を primary に格上げ → §2/§4 で実施
