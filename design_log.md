# Design Log: Taskvia ミッション UI の依存関係表示バグ調査

**Task**: t001 / mission: 20260415-deps-display-debug  
**Date**: 2026-04-15  
**Worker**: Yuki (research, typescript)

---

## 調査手順

### 1. plan.sh の Taskvia sync 関数を確認 (L510-544)

`_taskvia_request` は best-effort。例外を全て捕捉して WARNING を stderr に出すだけで、
呼び出し元には `None` を返す。`TASKVIA_URL` / `TASKVIA_TOKEN` が未設定なら即 `return None`。

`taskvia_sync_init(slug, title)` → `POST /api/missions`  
`taskvia_sync_add(slug, task_id, title, skills, priority, blocked_by)` → `POST /api/missions/{slug}/tasks`

blocked_by は正しく送信されていた (L537-544)。

### 2. Taskvia API ルートを確認

`POST /api/missions` (route.ts:24-45):
```typescript
await redis.set(`mission:${slug}`, JSON.stringify(mission));
await redis.lpush("mission:index", slug);  // ← 冪等でない
```

`POST /api/missions/[slug]/tasks` (tasks/route.ts:30-59):
```typescript
await redis.set(`mission:${slug}:tasks:${id}`, JSON.stringify(task));
await redis.lpush(`mission:${slug}:tasks:index`, id);  // ← 冪等でない
```

**問題**: 同一 slug/task に対して複数回呼ばれると `lpush` が重複エントリをインデックスに追加する。

### 3. taskvia-sync.sh との二重呼び出し問題を特定

`plan.sh add` は `taskvia_sync_add` でインラインに同期を試みる。
`taskvia-sync.sh` は `.taskvia-map.json` でトラッキングするが、plan.sh のインライン呼び出しは
このマップを更新しない。

結果として同一ミッション/タスクが 2 回 POST され得る:
1. `plan.sh add` → `POST /api/missions/{slug}/tasks` → index に push
2. `taskvia-sync.sh` → map にエントリなし → 再度 `POST` → index に重複 push

### 4. `.taskvia-map.json` の状態確認

```
queue/.taskvia-map.json には 20260415-taskvia-deps のエントリのみ存在。
20260415-deps-display-debug のエントリなし。
```

→ `plan.sh add` のインライン sync が失敗していた (API エラーを WARNING で隠蔽)、
かつ `taskvia-sync.sh` もまだ実行されていなかったため、タスクが Taskvia に存在しなかった。

### 5. taskvia-sync.sh を手動実行

```
[taskvia-sync] 登録: 20260415-deps-display-debug:t001 (blocked_by=[])
[taskvia-sync] 登録: 20260415-deps-display-debug:t002 (blocked_by=['t001'])
```

t002 の blocked_by が正しく登録された。UI の依存関係バッジが表示されるようになった。

---

## 根本原因

**二点の根本原因が複合:**

1. **API の冪等性欠如**  
   `POST /api/missions` と `POST /api/missions/{slug}/tasks` が重複呼び出しに対して
   `lpush` を無条件実行する。同一 task が複数回登録されるとインデックスに重複エントリが
   残り、UI でタスクが二重表示される。

2. **plan.sh インライン sync の無音失敗**  
   `_taskvia_request` が全例外を飲み込む best-effort 設計のため、API 障害・タイムアウト・
   認証エラー等があっても呼び出し元は気づけない。`taskvia-sync.sh` 側は `.taskvia-map.json`
   を参照するが plan.sh はこのファイルを更新しないため、sync 状態が不整合になる。

---

## 修正内容

### taskvia repo: API の冪等化

**`src/app/api/missions/route.ts`**  
POST ハンドラで既存エントリの有無を確認し、新規のときのみ `lpush` を実行:

```typescript
const existing = await redis.get(`mission:${slug}`);
const isNew = !existing;
await redis.set(`mission:${slug}`, JSON.stringify(mission));
if (isNew) {
  await redis.lpush("mission:index", slug);
}
return Response.json({ mission }, { status: isNew ? 201 : 200 });
```

**`src/app/api/missions/[slug]/tasks/route.ts`**  
同様に task の POST を冪等化:

```typescript
const existing = await redis.get(`mission:${slug}:tasks:${id}`);
const isNew = !existing;
await redis.set(`mission:${slug}:tasks:${id}`, JSON.stringify(task));
if (isNew) {
  await redis.lpush(`mission:${slug}:tasks:index`, id);
}
return Response.json({ task }, { status: isNew ? 201 : 200 });
```

---

## 改善提案 (requires_approval)

- plan.sh の `_taskvia_request` に失敗時の可視化を追加する (exit code / summary を stdout に出す)
- `taskvia-sync.sh` を plan.sh init/add の後に自動実行するか、`.taskvia-map.json` を plan.sh も更新するよう統一する
