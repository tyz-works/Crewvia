# Dispatcher Idle Worker 認識バグ 修正ノート

**ミッション**: 20260905-dispatcher-idle-worker-fix  
**対象ファイル**: `scripts/dispatcher.sh`  
**関連テスト**: `tests/test_dispatcher_fix.py`, `tests/test_regression_no_false_notify.py`

---

## 症状

task done 直後 (2-4s) に idle かつ skill match の Worker がいても認識せず、
Director に新規 Worker 起動要求を発火するバグ。

**再現率**: 4/4 = 100% 決定論的

| # | done task | 起動要求 task | idle Worker | 経過秒 |
|---|---|---|---|---|
| 1 | t002 (Haruto, bash+code) | t003+t004 | Haruto | 4s |
| 2 | t006 (Finn, qa) | t007 | Minjun (docs) | 2s |
| 3 | t024 (Haruto, bash+code) | t025 (docs) | Minjun | 2s |
| 4 | t025 (Minjun, docs) | t026 (qa) | Finn | 4s |

---

## 根本原因

2つの独立した bug が絡んでいた。

### Fix 1: herdr pane_list failure による誤通知

**問題**: `can_handle` チェックが `windows`（herdr pane_list の結果）ベースだった。

```python
# OLD: herdr pane_list が [] を返すと can_handle=False → 誤通知
can_handle = any(
    task_skills.issubset(set((workers.get(w['agent_name']) or {}).get('skills') or []))
    for w in windows
    if ...
)
```

herdr の pane_list は task done 直後の状態遷移中に transient に `[]` を返すことがある。
このとき `can_handle=False` → 「Worker を起動してください」を Director に誤送信。

**修正**: `can_handle` 判定を `_alive_workers`（heartbeat file ベース）に変更。

```python
# NEW: heartbeat file は herdr の状態に依存しない
_hb_dir = REGISTRY_DIR / 'heartbeats'
_alive_workers: set = set()
if _hb_dir.exists():
    _now_hb = time.time()
    for _hb_file in _hb_dir.iterdir():
        if _hb_file.is_file() and not _hb_file.name.startswith('.'):
            try:
                if _now_hb - _hb_file.stat().st_mtime <= AGENT_PRESENCE_TTL:
                    _alive_workers.add(_hb_file.name)
            except OSError:
                pass

can_handle = any(
    task_skills.issubset(set((workers.get(name) or {}).get('skills') or []))
    for name in _alive_workers
    if (workers.get(name) or {}).get('role', 'worker') == 'worker'
)
```

- Heartbeat ファイルは Worker が独自に書き込む（herdr と無関係）
- `AGENT_PRESENCE_TTL`（600s = 10分）以内なら alive と判定
- Director（role=director）はフィルタで除外

---

### Fix 2: Rule 2 mtime race による誤 shutdown

**問題**: blocked task の stuck 検出が pending task の mtime のみを見ていた。

blocker task が done になると：
1. blocker の mtime が更新される
2. pending task の mtime はそのまま（古い）
3. → Rule 2 が「stuck（古い mtime）」と判定 → Worker を kill

**修正A**: blocker が in_progress なら skip

```python
has_active_blocker = any(
    task_statuses_by_mission.get(s, {}).get(dep) == 'in_progress'
    for s, m in matching_pending
    for dep in (m.get('blocked_by') or [])
)
if has_active_blocker:
    log(f"[Rule 2 skip] {agent_name}: blocker task is in_progress → chain progressing")
    # → Worker を kill しない
```

**修正B**: `_chain_newest_mtime` — blocker の mtime も考慮

```python
def _chain_newest_mtime(slug, meta):
    """pending task mtime と直接 blocker mtime の max を返す"""
    mtimes = [_task_mtime(slug, meta)]
    for dep_id in (meta.get('blocked_by') or []):
        dep_file = MISSIONS_DIR / slug / 'tasks' / f"{dep_id}.md"
        try:
            mtimes.append(dep_file.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes)

newest_mtime = max(_chain_newest_mtime(s, m) for s, m in matching_pending)
```

---

## 診断方法

Fix 1 のバグ切り分けには `_alive_workers` ビルド直後のログを見る：

```
[diag] _alive_workers: {'Haruto', 'Finn', 'Minjun'}
[diag] windows: []   ← ここが [] なら Fix 1 のバグ条件が成立していた
```

Fix 2 のバグは Rule 2 ログで切り分け：

```
# OLD: 誤 kill
[Rule 2] Haruto: all matching tasks blocked for 45s ≥ 30s — sending shutdown

# NEW: 正しくスキップ
[Rule 2 skip] Haruto: blocker task is in_progress → chain progressing, worker kept alive
```

---

## テストカバレッジ

| ファイル | テスト数 | 内容 |
|---|---|---|
| `test_dispatcher_fix.py` | 10 | Fix 1 × 5, Fix 2 × 4, 統合 × 1 |
| `test_regression_no_false_notify.py` | 7 | 5連続 task done で誤通知 0/5 確認 |
| `test_skill_perms.py` | 66 | regression 確認（変更なし） |
| **合計** | **83** | 全 PASS |

---

## 関連メモリ

- `~/.claude/projects/-home-tkadmin-workspace-crewvia/memory/dispatcher-idle-worker-recognition-bug.md`
  - 観測記録とワークアラウンド
- `knowledge/dispatcher-blocked-by-analysis.md`
  - blocked_by チェックの分析（関連する Rule 2 の前身）
