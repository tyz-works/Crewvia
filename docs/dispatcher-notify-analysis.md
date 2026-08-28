# dispatcher.sh — pull-notify 挙動調査 + 修正案 3 種

> 調査日: 2026-08-28  
> 対象: `scripts/dispatcher.sh`（main ブランチ HEAD）  
> タスク: t002 / mission: 20260828-dispatcher-director-only

---

## 1. 通知対象タスクの判定ロジック

### 全体フロー

```
dispatch() 呼び出し
│
├─ unblocked_pending リストを構築 (L648-665)
│   pending かつ blocked_by が全て done/failed/cancelled の task のみ
│
├─ 各 live Worker window に対して assignment 処理 (L676-755)
│   └─ idle Worker + スキルマッチした task → tmux send-keys でタスク割当
│
└─ Sora-director への "Worker 起動" 通知 (L757-773)  ← ★ 調査対象
    for task in unblocked_pending:
      if no live Worker can handle task:
        if should_notify(f"no_worker_{task_id}"):
          tmux_send('crewvia:Sora-director', "要求スキル ... の Worker を起動してください")
```

### 通知条件 (L757-773)

```python
for slug, meta in unblocked_pending:          # (1) unblocked_pending のみ対象
    task_skills = set(meta.get('skills') or [])
    can_handle = any(                          # (2) live Worker のスキル判定
        task_skills.issubset(
            set((workers.get(w['agent_name']) or {}).get('skills') or [])
        )
        for w in windows                       # windows = tmux live windows
    )
    if not can_handle:                         # (3) 担当不可 → 通知
        notify_key = f"no_worker_{task_id}"
        if should_notify(notify_key):          # (4) TTL チェック
            tmux_send('crewvia:Sora-director', ...)
```

**判定条件まとめ:**
| 条件 | 実装状況 |
|------|---------|
| task.status == 'pending' | ✅ |
| blocked_by が全て解決済み | ✅ (PR #108 で追加) |
| live Worker がスキルマッチしない | ✅ |
| **director-only skill を含む task の除外** | ❌ **未実装 — 実害あり** |

---

## 2. 各調査項目の詳細

### 2-1. director-only skill の扱い

**現状: 除外ロジックなし**

`unblocked_pending` の構築ループ (L648-665) でスキル内容のフィルタリングはない。
空スキルのみ警告して除外する処理はあるが、`director-only` スキルは通常スキルとして扱われる。

```python
task_skills = set(meta.get('skills') or [])
if not task_skills:
    log(f"WARNING: task ... has no skills — dispatcher cannot assign it")
    continue
# ← ここに director-only チェックなし
unblocked_pending.append((slug, meta))
```

**結果:** `skills: [director-only]` や `skills: [ops]` を持つ task は
`unblocked_pending` に含まれ、対応 Worker が存在しない限り **60 秒ごとに通知が繰り返される**。

---

### 2-2. blocked_by チェック

**現状: PR #108 で既に実装済み**

```python
# L654-659 (PR #108 で追加)
bb = meta.get('blocked_by') or []
if any(dep not in done_ids
       and task_statuses.get(dep) not in ('failed', 'cancelled')
       for dep in bb):
    continue  # blocked task はスキップ
```

- `done_ids`: TERMINAL_STATUSES = `{'done', 'verified', 'skipped'}` の task ID セット
- `task_statuses`: `{task_id: status}` のマップ、`failed`/`cancelled` は blocking しない
- PR #108 は `plan.sh pull` と `dispatcher.sh` の両方に同じロジックを適用

**結論: Fix #2 (blocked_by 尊重) は現行コードで解決済み。**
t001 背景に記載の「blocked task も通知」バグは当時の旧バージョンで発生し、
PR #108 により dispatcher.sh にも修正が反映された。

---

### 2-3. NOTIFY_TTL の実装

```python
NOTIFY_TTL = 60   # seconds (L30)
NOTIFY_CACHE = "/tmp/dispatcher-notify-cache.$$.json"  # $$ = bash PID (L29)
```

```python
def should_notify(key):
    cache = load_notify_cache()          # NOTIFY_CACHE を JSON 読み込み
    if key not in cache:
        return True
    return time.time() - cache[key] > NOTIFY_TTL

def record_notify(key):
    cache = load_notify_cache()
    cache[key] = time.time()
    NOTIFY_CACHE.write_text(json.dumps(cache))
```

**重複判定の key:**
| 通知種別 | key フォーマット |
|---------|----------------|
| Worker 不在通知 | `no_worker_{task_id}` |
| タスク割当 | `assign_{agent_name}_{task_id}` |
| Worker shutdown | `shutdown_{agent_name}` |

**問題点:**
1. **PID 固有キャッシュ**: `$$.json` → dispatcher.sh 再起動でキャッシュリセット → 60 秒待たずに即再通知
2. **TTL が 60 秒**: タスクが長時間 pending のまま (blocked / no worker) の場合、毎分通知が飛ぶ
3. **key に mission_slug が含まれない**: `no_worker_{task_id}` は task_id のみ。mission 間で task_id が衝突すると誤 suppress (実際は稀)

---

## 3. 前 PR #108 との関連

| 修正内容 | plan.sh | dispatcher.sh |
|---------|---------|---------------|
| blocked_by 厳密チェック (L654-659) | ✅ | ✅ (PR #108 で同時適用) |
| failed/cancelled を blocking 対象外 | ✅ | ✅ (PR #108 で同時適用) |
| TERMINAL_STATUSES に 'verified' 追加 | ✅ | ✅ (PR #108 で同時適用) |
| director-only 除外 | — | ❌ **未対応** |

---

## 4. 修正案 3 種

### 修正案 A: director-only skill 除外（Fix #1）

**対象**: 実害あり・最優先

#### 実装スケッチ

```python
# L660 付近、unblocked_pending.append の直前に挿入
DIRECTOR_ONLY_SKILLS = {'director-only', 'ops'}  # Director が直接実行するスキル

task_skills = set(meta.get('skills') or [])
if not task_skills:
    log(f"WARNING: task {meta.get('id')} has no skills — dispatcher cannot assign it")
    continue
# ↓ 追加
if task_skills & DIRECTOR_ONLY_SKILLS:
    # director-only task は Worker 割当対象外 → unblocked_pending に含めない
    continue
unblocked_pending.append((slug, meta))
```

**Trade-off:**
| 観点 | 評価 |
|-----|------|
| 効果 | director-only 通知を完全に停止 |
| リスク | DIRECTOR_ONLY_SKILLS の定義漏れで通知が止まらない |
| 保守性 | スキル名をハードコードするため worker-names.yaml との乖離リスク |
| 代替 | skills に 'director-only' を含む task を unblocked_pending からも除外するので assignment も止まる (期待値通り) |

**推奨度: ⭐⭐⭐ (最優先実装)**

---

### 修正案 B: blocked_by 尊重（Fix #2）

**対象: 既に解決済み — 実装不要**

PR #108 (commit `6bbcf6d`) で `dispatcher.sh` の `unblocked_pending` 構築に
blocked_by ガードが追加済み。追加実装は不要。

**t003 スコープ縮小**: 本修正案の実装工数は t003 から除外できる。

---

### 修正案 C: NOTIFY_TTL 延長 + persistent キャッシュ（Fix #3）

**対象**: 通知頻度削減・ノイズ低減

#### オプション C-1: TTL のみ延長（最小変更）

```python
NOTIFY_TTL = 300   # 60s → 5分
```

**Trade-off:** dispatcher.sh 再起動でリセットされる問題は残る。

#### オプション C-2: PID 非依存キャッシュ + TTL 延長（推奨）

```python
NOTIFY_CACHE = Path("/tmp/dispatcher-notify-cache.json")  # $$ を除去
NOTIFY_TTL = 600   # 60s → 10分
```

再起動後も前回の通知時刻が保持され、不要な再通知を防ぐ。

#### オプション C-3: mission_slug を key に含める（堅牢化）

```python
notify_key = f"no_worker_{slug}_{task_id}"  # slug を追加
```

mission をまたいだ task_id 衝突リスクを排除。

**Trade-off:**
| オプション | 効果 | 変更コスト | リスク |
|-----------|------|------------|-------|
| C-1 TTL のみ | 通知間隔 5x | 1行 | 再起動後リセット |
| C-2 persistent + TTL | 再起動後も dedup | 2行 | /tmp に永続ファイルが残る |
| C-3 slug 追加 | key 衝突排除 | 数行 | 既存 cache の無効化 (初回のみ再通知) |

**推奨度: ⭐⭐ C-2 + C-3 の組み合わせ**

---

## 5. 修正優先度まとめ

| Fix | 実害 | 実装規模 | 優先度 |
|-----|------|----------|--------|
| A: director-only 除外 | **高** (毎分通知) | S (数行) | 🔴 **最優先** |
| B: blocked_by 尊重 | — | — | ✅ 解決済み・対応不要 |
| C: TTL 延長 + persistent | 中 (再起動後ノイズ) | S (2-3行) | 🟡 A と同時実装推奨 |

---

## 6. t003 実装への引き継ぎ事項

1. **Fix B は実装不要** — t003 スコープから除外してよい
2. **Fix A の実装箇所**: `dispatch()` 内 `unblocked_pending` 構築ループ (L660 付近)
3. **DIRECTOR_ONLY_SKILLS の定義**: `director-only` は確定。`ops` は現在 director が直接実行するスキルなら含める
4. **Fix C の推奨構成**: `NOTIFY_CACHE` のパスを `$$.json` → `dispatcher-notify-cache.json` に変更 + `NOTIFY_TTL = 600`
5. **回帰リスク**: dispatcher.sh は Worker 起動ループの中核。修正後は `tests/test_blocked_by_guard.sh` (9 テスト) の全通過を確認すること

## PR タイトル案

`docs: dispatcher.sh の pull-notify 挙動調査 + 修正案 3 種`
