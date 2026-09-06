# Worker Tab 消滅検知 (Vanished Worker Detection)

## 問題の説明

Worker が `plan.sh pull` 直後にクラッシュすると、以下の状態になる:

1. task の status が `in_progress` のまま残る（`plan.sh pull` が書いた状態）
2. Worker の heartbeat が停止する
3. herdr tab（`Name-worker`）が消滅する

この状態では:
- **Rule 5** は `state()` が `pane not found → unknown` を返すため発火しない
  （Rule 5 は「pane が存在するが入力待ち」の状態を検知する仕組みであるため）
- **Dispatcher の blocked-stuck 検知 (Rule 2)** は Worker がいる前提で動作するため消滅 Worker を検知しない
  （idle Worker がいないと判断して stuck 検知ループ自体がスキップされる）

結果: task は `in_progress` のまま永久に立ち往生し、Director が手動監視しない限り気付けない。

参考: `crewvia-worker-tab-vanish-rule5-gap` (MEMORY.md)

---

## Dispatcher が検知して Director に通知する仕組み

`scripts/dispatcher.sh` の `dispatch()` ループ内、`can_handle` 通知セクションの直後に
**vanished worker 検知セクション**を追加した。

### 検知条件（4 条件 AND）

| 条件 | 内容 |
|------|------|
| 条件A | `task.status == 'in_progress'` |
| 条件B | `task.worker` フィールドに Worker 名が記録されている |
| 条件C | 当該 Worker の tab (`Name-worker`) が `_window_agent_names`（live windows）に存在しない |
| 条件D | heartbeat ファイルが存在しないか `mtime > AGENT_PRESENCE_TTL (600s)` で stale |

4 条件をすべて満たす場合のみ通知。

### 通知しない場合（除外条件）

- **条件C 不満足（tab 存在）**: Worker は正常稼働中 → skip
- **条件D 不満足（heartbeat fresh）**: herdr の transient pane_list failure で tab が一時的に見えないだけかもしれない → skip  
  （PR #154 で導入した heartbeat-fresh 優先ロジックと整合）

### 重複通知防止

```python
notify_key = f'vanished_worker_{slug}_{task_id}'
```

`should_notify()` / `record_notify()` による TTL キャッシュ（デフォルト 300s）で抑制。
同一 task に対して 5 分以内の重複通知は発生しない。

### 通知メッセージ例

```
task t003 (mission: 20260907-crewvia-backlog-5) の worker Haruto の tab が消滅しています。
plan.sh update t003 --status pending --reset --mission 20260907-crewvia-backlog-5 で復旧してください。
```

### 実装箇所

`scripts/dispatcher.sh` — `dispatch()` 関数内:
- `can_handle` 通知ループの後
- `# Handoff detection` セクションの前

---

## Director のリカバリ手順

vanish 通知を受けた Director は以下の手順で復旧する:

### 1. task を pending にリセット

```bash
# crewvia リポジトリのルートで実行
scripts/plan.sh update <task_id> --status pending --reset --mission <mission_slug>
```

`--reset` は `worker` / `started_at` フィールドをクリアして再 pull 可能な状態に戻す。

### 2. worktree のクリーンアップ（必要な場合）

Worker が worktree を作っていた場合は、dangling worktree を削除する:

```bash
# worktree 一覧を確認
git -C /path/to/crewvia worktree list

# 不要な worktree を削除
git -C /path/to/crewvia worktree remove --force .claude/worktrees/<mission>/<slug>
```

### 3. 新しい Worker を起動

```bash
# lib_mux.sh 経由で Worker を起動
source scripts/lib_mux.sh
mux_spawn "Haruto-worker" "bash scripts/start.sh worker --name Haruto"
```

または Dispatcher の「no_worker」通知を待って新しい Worker 起動要求を受け取る。

---

## テスト

`tests/test_dispatcher_worker_vanish.py` に regression テストを追加:

| テストケース | 内容 | 期待結果 |
|------------|------|---------|
| case_a | in_progress + tab 存在 | 通知しない |
| case_b | in_progress + tab 消滅 + heartbeat stale | **通知する** |
| case_c | in_progress + tab 消滅 + heartbeat fresh | 通知しない (生存中の可能性) |
| case_d | pending + tab 消滅 | 通知しない (in_progress でない) |
| case_e | 混合 4 タスク | 条件満足の 1 件のみ通知 |
| case_f | heartbeat ファイル missing + tab なし | **通知する** |
| case_g | dispatcher.sh 構文確認 | vanish 実装済み確認 |

```bash
# テスト実行
python3 -m pytest tests/test_dispatcher_worker_vanish.py -v
python3 -m pytest tests/test_heartbeat_gap_regression.py -v  # regression なし確認
```

---

## 関連

- `scripts/dispatcher.sh` — 実装箇所（vanished worker 検知セクション）
- `tests/test_dispatcher_worker_vanish.py` — 上記 regression テスト
- `tests/test_heartbeat_gap_regression.py` — heartbeat gap (OBS-1) fix テスト（既存）
- MEMORY: `crewvia-worker-tab-vanish-rule5-gap` — 問題発見の経緯
- MEMORY: `obs-1-fix-heartbeat-freshness-gap` — heartbeat gap fix (PR #154)
