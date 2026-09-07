# dispatcher.sh fix 反映には稼働 tab の restart が必要

## 問題の説明

`scripts/dispatcher.sh`（または依存ファイル）の fix を PR で main に merge しても、
**稼働中の `dispatcher` tab は旧コードで動き続ける**。
dispatcher tab を kill → respawn しないと fix が反映されない。

**理由**: dispatcher tab は `bash scripts/dispatcher.sh` を起動時に一度読み込んで実行し続けるプロセス。
ファイルを再読み込みする仕組みはない。`git pull` で working tree の `dispatcher.sh` が更新されても、
既に起動している bash プロセスの embedded Python (`PYEOF` ブロック) はロードされない。

**過去の誤診断事例**: mission 20260905-dispatcher-idle-worker-fix で PR #154 merge 後、
dispatcher tab を restart せずに再検証すると **OBS-1 バグが再現し続ける**ように見えた。
「fix が効いていない」と誤診断するリスクがあった。

---

## 影響を受けるプロセス

| プロセス | 再起動が必要なケース |
|----------|-------------------|
| `dispatcher` tab | `scripts/dispatcher.sh` / `scripts/lib_mux.py` の変更 |
| `watchdog` tab | `scripts/watchdog.py` / `scripts/watchdog.sh` の変更 |
| Worker tab | `scripts/start.sh` の変更（スキル割り当て等） |
| `Sora-director` | `agents/director.md` / `hooks/*.sh` の変更（プロンプト・hook 反映） |

---

## restart 手順

### dispatcher の restart 例

```bash
# 1. main に fix を pull (worktree 外の crewvia repo root で実行)
cd /path/to/crewvia
git fetch origin main && git pull --ff-only origin main

# 2. 稼働中の dispatcher tab を kill
python3 scripts/lib_mux.py kill dispatcher

# 3. dispatcher tab を respawn
REPO="$PWD"
python3 scripts/lib_mux.py spawn dispatcher \
  "cd '${REPO}' && bash '${REPO}/scripts/dispatcher.sh'" \
  "${REPO}"

# 4. 起動確認 (ログの最終行が "Starting dispatcher (PID ...)" になっていること)
tail -3 logs/dispatcher/dispatcher-$(date +%Y%m%d).log
```

### watchdog の restart 例

```bash
python3 scripts/lib_mux.py kill watchdog
REPO="$PWD"
python3 scripts/lib_mux.py spawn watchdog \
  "cd '${REPO}' && python3 '${REPO}/scripts/watchdog.py'" \
  "${REPO}"
```

---

## 教訓

> **「fix が効いていない」と誤診断する前に restart を確認すること。**

dispatcher / watchdog の挙動がおかしいと感じたとき、まず以下を確認する:

1. 直近で関連する PR を merge したか？
2. merge 後に該当 tab を restart したか？
3. restart していなければ restart してから再検証する

restart は **fix 効果検証の前提** であるため、mission 完了後のクリーンアップ手順にも組み込むこと。

---

## 関連

- MEMORY: `dispatcher-restart-after-merge` — この運用上の落とし穴の背景
- `scripts/lib_mux.py` — kill / spawn の実装
- `knowledge/ops.md` — その他の運用手順
