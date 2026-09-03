# Dispatcher blocked_by 厳密化 — 調査レポート

**タスク**: t003 (mission: 20260902-crewvia-improvements)  
**担当**: Yasmin  
**調査日**: 2026-09-02

---

## 1. 調査概要

memory `[[crewvia-dispatcher-blocked-by-loose]]` の「Dispatcher が blocked_by の全 done を厳密チェックしていない疑いがある」という記録をもとに、現在の実装を精査した。

**結論**: **現在の実装は正しい**。疑いの原因となった regression (PR #108) および Dispatcher 側の欠落 (PR #116 Fix B 確認) はすでに修正済みまたは最初から実装済みであることが確認できた。

---

## 2. 現在の実装確認

### 2.1 `dispatcher.sh` — unblocked_pending 構築ロジック

```python
# dispatcher.sh 行 ~336 (load_all_tasks)
TERMINAL_STATUSES = {'done', 'verified', 'skipped'}
done_ids_by_mission = {}
task_statuses_by_mission = {}
for slug in active_missions:
    tasks = list_tasks_for_mission(slug)
    done_ids = {m['id'] for m, _ in tasks if m.get('status') in TERMINAL_STATUSES}
    done_ids_by_mission[slug] = done_ids
    task_statuses_by_mission[slug] = {m['id']: m.get('status') for m, _ in tasks}
```

```python
# dispatcher.sh 行 ~659-664 (unblocked_pending 構築)
done_ids = done_ids_by_mission.get(slug, set())
task_statuses = task_statuses_by_mission.get(slug, {})
bb = meta.get('blocked_by') or []
if any(dep not in done_ids
       and task_statuses.get(dep) not in ('failed', 'cancelled')
       for dep in bb):
    continue  # blocked → unblocked_pending に含めない
```

### 2.2 `plan.sh pull` — 二層目 defense-in-depth

#### `--task` 指定 (dispatcher→Worker 割当パス)

```python
# plan.sh 行 ~1243-1249
bb = meta.get('blocked_by') or []
unmet = [dep for dep in bb if dep not in done_ids
         and task_statuses.get(dep) not in ('failed', 'cancelled')]
if unmet:
    die(f"task '{specific_task}' is blocked by unfinished dependencies: {unmet}")
```

#### 自動選択パス (Worker が自律的に pull するケース)

```python
# plan.sh 行 ~1285-1290
bb = meta.get('blocked_by') or []
if any(dep not in done_ids
       and task_statuses.get(dep) not in ('failed', 'cancelled')
       for dep in bb):
    blocked_count += 1
    continue
```

**両パスで同一ロジックを使用している**。

---

## 3. シナリオ別動作確認 (シミュレーション検証)

全シナリオを Python シミュレーションで検証した結果、全 PASS:

| dep ステータス | 期待値 (blocked?) | 実際 | 判定 |
|---|---|---|---|
| `pending` | ✓ blocked | ✓ blocked | **PASS** |
| `in_progress` | ✓ blocked | ✓ blocked | **PASS** |
| `done` | ✗ unblocked | ✗ unblocked | **PASS** |
| `verified` | ✗ unblocked | ✗ unblocked | **PASS** |
| `skipped` | ✗ unblocked | ✗ unblocked | **PASS** |
| `failed` | ✗ unblocked | ✗ unblocked | **PASS** |
| `cancelled` | ✗ unblocked | ✗ unblocked | **PASS** |
| 存在しない (typo 等) | ✓ blocked | ✓ blocked | **PASS** |
| `done` + 別の `pending` | ✓ blocked | ✓ blocked | **PASS** |
| 全 `done` | ✗ unblocked | ✗ unblocked | **PASS** |

---

## 4. PR #108 との整合性確認

**PR #108** (fix: pull ガードが failed/cancelled dep を誤って blocking 扱いする regression):

修正前:
```python
unmet = [dep for dep in bb if dep not in done_ids]  # ← failed も blocking 扱いしていた
```

修正後:
```python
unmet = [dep for dep in bb if dep not in done_ids
         and task_statuses.get(dep) not in ('failed', 'cancelled')]
```

この修正は現在の `plan.sh pull` (`--task` パス・自動選択パス双方) に反映済み。
`dispatcher.sh` の `unblocked_pending` 構築ロジックも同一パターンを使っている。

---

## 5. アーキテクチャ上の重要な差異

| | `dispatcher.sh` | `plan.sh pull` |
|---|---|---|
| ロック取得 | **なし** | `fcntl.LOCK_EX` (QUEUE_DIR/.lock) |
| タスクファイル読み取り | ファイルシステム直読み | ロック下で読み取り (一貫性保証) |
| 役割 | best-effort スケジューラ | 権威的実行ゲートキーパー |

**重要**: Dispatcher はロックなしでタスクファイルを読むため、稀に stale なスナップショットを見る可能性がある。ただし:

1. `plan.sh` は `_atomic_write` (tmp + os.replace + fsync) でファイルを書くため、半書き込みは発生しない
2. Dispatcher が stale な状態で「ブロック解除」と判断しても、Worker が `plan.sh pull` を実行する時点でロック付きの再チェックが走る (defense-in-depth)
3. Dispatcher の通知キャッシュ (`NOTIFY_TTL=300s`) が同一通知の再送を抑制する

この二層設計は**意図的かつ正しい**。

---

## 6. t007 (実装タスク) への引き継ぎ

### 実装変更の要否:

| 変更 | 必要性 | 理由 |
|---|---|---|
| `dispatcher.sh` blocked_by チェック修正 | **不要** | 実装済みで正しい |
| `plan.sh pull` blocked_by チェック修正 | **不要** | PR #108 で修正済み |
| `plan.sh pull` JSON 出力改善 (blocked 拒否時) | 推奨 | 可観測性向上 |
| `dispatcher.sh` blocked タスクのデバッグログ | 推奨 | 診断性向上 |
| **BATS 回帰テスト追加** | **必須** | 再 regression 防止 |

### BATS テストケース (t007 が実装すべき内容):

`tests/dispatcher-blocked-by.bats` に以下のテストを追加すること:

1. `t001` が `pending` の時、`blocked_by: [t001]` の task を `plan.sh pull --task` できないこと
2. `t001` が `done` の時、`blocked_by: [t001]` の task が正常に pull できること
3. `t001` が `failed` の時、`blocked_by: [t001]` の task が pull できること (PR #108 整合性)
4. 存在しない dep (`t999`) を `blocked_by` に持つ task は pull できないこと
5. 自動選択パスで blocked タスクが候補に含まれないこと (`diag.reason == 'all_blocked'`)

### コード変更案 (オプション: ログ改善):

```python
# dispatcher.sh — unblocked_pending スキップ時にログ出力
unmet_deps = [dep for dep in bb if dep not in done_ids
              and task_statuses.get(dep) not in ('failed', 'cancelled')]
if unmet_deps:
    log(f"[blocked] task {meta.get('id')} (mission={slug}) — unmet deps: "
        f"{unmet_deps} ({[task_statuses.get(d) for d in unmet_deps]})")
    continue
```

---

## 7. 参考: 関連 PR

| PR | 内容 |
|---|---|
| PR #108 | plan.sh pull: failed/cancelled dep を誤って blocking 扱いする regression 修正 |
| PR #116 Fix B | dispatcher.sh の blocked_by チェック実装済み確認 (t002 調査で確認) |
