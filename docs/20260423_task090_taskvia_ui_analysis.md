# Taskvia 既存 UI 分析レポート

作成: 2026-04-23 / Data (task_090_pa_data)
対象: `~/workspace/Taskvia` — Next.js 15 App Router + Upstash Redis
目的: Geordi Phase D の verification バッジ / Verification Queue タブ実装における既存デグレ防止

---

## 1. 既存 Board 描画フロー

### 1.1 レンダリング方式

`src/app/page.tsx` の先頭に `"use client"` — **完全 CSR (Client-Side Rendering)**。
`layout.tsx` に特別な指定なし。Server Component なし、SSR/SSG なし。

```
ブラウザ → Next.js → page.tsx (CSR) → Server Actions / fetch() → Upstash Redis
```

### 1.2 ポーリング間隔（現状）

| データ | 方式 | 間隔 | 条件 |
|---|---|---|---|
| Tasks (Board) | Smart polling (`setTimeout` 再帰) | **5s** (active) / **20s** (idle) | `in_progress` タスクが存在する場合は 5s、それ以外は 20s |
| Approval cards | `setInterval` (固定) | **5s** | 常時 |
| Agents | `setInterval` (固定) | **5s** | 常時 |
| Mission requests | `setInterval` (固定) | **20s** | 常時 |
| Logs | 一回取得 | タブ切替時のみ | `tab === "logs"` に切り替わった瞬間のみ fetch |

**Visibility API 対応**: `document.visibilityState !== "visible"` の場合はタスクポーリングを一時停止し、フォーカス復帰時に即座に再開する (page.tsx L753-768)。Approval / Agents / Requests の `setInterval` は停止しない（常時動作）。

### 1.3 タブ構成

```typescript
type Tab = "board" | "logs";  // page.tsx:38
```

現状 2 タブ (`Board` / `Logs`)。nav は page.tsx L950-964。

---

## 2. 既存 Redis Key 一覧

| Key パターン | 型 | TTL | 変更 | 説明 |
|---|---|---|---|---|
| `approval:{id}` | String (JSON) | 600s | **変更不可** | 承認カード本体 |
| `approval:index` | List | なし | **変更不可** | 承認カード ID 一覧 (LRANGE 0-99) |
| `approval_token:{token}` | String (JSON) | 900s → consumed=60s | **変更不可** | ntfy ワンタイムトークン |
| `mission:{slug}` | String (JSON) | なし | 変更不可 | ミッションメタデータ |
| `mission:index` | List | なし | 変更不可 | ミッション slug 一覧 |
| `mission:{slug}:tasks:{id}` | String (JSON) | **なし** | **フィールド追加可** | タスクハッシュ |
| `mission:{slug}:tasks:index` | List | なし | 変更不可 | タスク ID 一覧 |
| `mission_request:{id}` | String (JSON) | なし | 変更不可 | 依頼フォーム投稿 |
| `mission_requests:index` | List | なし | 変更不可 | 依頼 ID 一覧 |
| `agent:{name}` | String (JSON) | Heartbeat TTL | 変更不可 | エージェント状態 |
| `agent:index` | Set | なし | 変更不可 | エージェント名一覧 |
| `agent:logs` | List | なし | 変更不可 | ログエントリ (LPUSH) |

**Verification 追加候補キー** (既存キーは変更しない):

| 新 Key パターン | 型 | TTL | 内容 |
|---|---|---|---|
| `mission:{slug}:tasks:{id}` (フィールド追加) | — | — | `verifier`, `rework_count`, `max_rework`, `verification_mode` を既存 JSON に追加 |

既存の `PATCH /api/missions/[slug]/tasks/[id]` は `body.result`, `body.status`, `body.assignee` の任意フィールドを受け付ける open update 設計 (route.ts:40-44)。`body.verifier`, `body.rework_count` をここに追加するだけで Redis 書き込みまで通る — **API ルート変更コスト が最小**。

---

## 3. 既存 API パターン

### 3.1 Bearer 認証の実装箇所

`src/lib/auth.ts` の `isAuthorized(req)` を**各 route.ts 内で個別に呼ぶ**方式。middleware 方式ではない。

```typescript
// src/lib/auth.ts
export function isAuthorized(req: Request): boolean {
  const token = (process.env.TASKVIA_TOKEN ?? "").trim();
  if (!token) return true;   // 未設定時はオープン
  return req.headers.get("Authorization") === `Bearer ${token}`;
}
```

| エンドポイント | 認証 | 方式 |
|---|---|---|
| `GET /api/cards` | **なし (公開)** | Lua スクリプト最適化のため意図的に省略 |
| `POST /api/approve-token/[token]` | **なし** | token 自体が秘密 |
| `POST /api/deny-token/[token]` | **なし** | token 自体が秘密 |
| `GET /api/status/[id]` | Bearer | isAuthorized |
| `POST /api/request` | Bearer | isAuthorized |
| `GET/POST /api/missions/**` | Bearer | isAuthorized |
| `PATCH /api/missions/[slug]/tasks/[id]` | Bearer | isAuthorized |
| `POST /api/log` | Bearer | isAuthorized |
| Server Actions (actions.ts) | CSRF のみ | Next.js 内蔵 CSRF |

**Geordi 注意**: 新規 verification API を追加する場合は `isAuthorized(req)` を先頭に呼ぶパターンを踏襲すること。Server Action として実装する場合は Bearer 不要（CSRF 保護が効く）。

### 3.2 zod 使用状況

**zod は使われていない**。バリデーションはすべて手書き (`typeof x === "string"`, `!title.trim()` 等)。新規 API でも既存パターンに合わせて手書きが統一感を保てる。

---

## 4. 新 UI 組み込み影響箇所

### 4.1 Verification バッジ差し込み箇所

**場所**: `TaskCard` コンポーネント (page.tsx L300-370)、Approval Badge の直後

```tsx
{/* 既存 — Approval badge (L338-346) */}
{count > 0 && (
  <button className="... bg-amber-500/10 ...">
    ⚠️ 承認 {count}件
  </button>
)}

{/* ↓ ここに挿入 — Verification status badge */}
{task.status === "ready_for_verification" && (
  <div className="text-[11px] px-2 py-1 rounded-lg bg-sky-500/10 border border-sky-500/30 text-sky-400 font-medium w-full text-center">
    🔍 検証待ち
  </div>
)}
{task.status === "verifying" && (
  <div className="text-[11px] px-2 py-1 rounded-lg bg-violet-500/10 border border-violet-500/30 text-violet-400 font-medium w-full text-center">
    ⚙️ 検証中
  </div>
)}
{task.status === "verification_failed" && (
  <div className="text-[11px] px-2 py-1 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 font-medium w-full text-center">
    ❌ 検証失敗
  </div>
)}
```

差し込み行: **L347 付近** (既存 Approval badge ブロックの閉じ `}` の直後)。

### 4.2 Header nav 拡張 (Verification Queue タブ)

**場所**: page.tsx L37-38 (型定義) + L950-964 (nav JSX)

```typescript
// 変更前
type Tab = "board" | "logs";

// 変更後
type Tab = "board" | "logs" | "verification";
```

```tsx
// nav 配列 (L951) に "verification" を追加
{(["board", "logs", "verification"] as const).map((key) => (
  <button key={key} ...>
    {key === "board" ? "Board" : key === "logs" ? "Logs" : "Verification"}
  </button>
))}
```

**新規ビューコンポーネント**: `VerificationQueueView` を page.tsx 内に追加。polling は tasks から `status === "ready_for_verification" || status === "verifying"` でフィルタ — **既存 loadTasks を再利用可能**、新規 fetch 不要。

### 4.3 Card 展開 UI の状態管理

**TaskDetailDialog** (page.tsx L197-296) は `selectedTask: Task | null` state で管理 (L703)。
ESC キー / 背景クリックで閉じる実装済み (L205-209, L220-225)。

Verification 詳細の追加場所: **L255-261 の Assignee ブロック直後**:

```tsx
{/* 既存 Assignee */}
<div className="bg-zinc-800 rounded-xl p-3 ...">
  担当: {task.assignee}
</div>

{/* ↓ 追加 — Verification 情報 */}
{task.rework_count !== undefined && task.rework_count > 0 && (
  <div className="bg-amber-500/5 border border-amber-500/20 rounded-xl p-3 ...">
    <div className="text-[10px] text-amber-400 uppercase">Rework</div>
    <div className="text-sm">{task.rework_count} / {task.max_rework ?? 3}</div>
  </div>
)}
{task.verifier && (
  <div className="bg-zinc-800 rounded-xl p-3 ...">
    <div className="text-[10px] text-zinc-500 uppercase">Verifier</div>
    <div className="text-sm text-violet-300">{task.verifier}</div>
  </div>
)}
```

### 4.4 影響箇所サマリー

| 場所 | 変更種別 | 推定行数 |
|---|---|---|
| `Task` 型 (actions.ts L183-192) | フィールド追加 (optional) | +5行 |
| `STATUS_LABEL` (page.tsx L183-188) | エントリ追加 | +5行 |
| `STATUS_COLOR` (page.tsx L190-195) | エントリ追加 | +5行 |
| `type Tab` (page.tsx L38) | union 拡張 | +1行 |
| nav 配列 (page.tsx L951) | 要素追加 | +1行 |
| `KanbanPage` state (page.tsx L688+) | Tab 対応ビュー追加 | +10行 |
| `TaskCard` (page.tsx L338-346 付近) | バッジ追加 | +15行 |
| `TaskDetailDialog` (page.tsx L255-261 付近) | Verifier/rework 表示 | +20行 |
| `PATCH /api/missions/[slug]/tasks/[id]` (route.ts) | フィールドパススルー追加 | +3行 |
| Header counter badge (page.tsx L906-945) | `ready_for_verification` 件数表示 | +8行 |
| **合計** | — | **≈ 73行** |

---

## 5. Geordi Phase D が壊してはいけない既存動作

### [CRITICAL-1] Approval card ポーリングチェーン

```typescript
// page.tsx:791
setApprovalCards(data.filter((c) => c.status === "pending"));
```

`ApprovalCard.status` の型は `"pending" | "approved" | "denied"`。`GET /api/cards` の Lua スクリプトはキー名 `approval:index` と `approval:{id}` を使う。**この 2 つのキー名を変更・削除すると承認フロー全体が崩壊する**。

→ 検証キー (`verification:{id}`) は `approval:` とは独立した名前空間で作ること。

### [CRITICAL-2] Smart polling 間隔ロジック

```typescript
// page.tsx:746
const hasActive = latest.some((t) => t.status === "in_progress");
const delay = hasActive ? POLL_ACTIVE_MS : POLL_IDLE_MS;
```

タスクに `"verifying"` や `"ready_for_verification"` などの新ステータスを追加した場合、これらは `"in_progress"` に該当しない → **アクティブ作業中でも 20s 低頻度ポーリングに落ちる可能性がある**。

→ 修正案: `hasActive` の判定を `["in_progress", "verifying"].some(...)` に拡張すること。

### [CRITICAL-3] `STATUS_LABEL` / `STATUS_COLOR` の型網羅性

```typescript
const STATUS_LABEL: Record<Task["status"], string> = { ... };
const STATUS_COLOR: Record<Task["status"], string> = { ... };
```

これらは `Record<Task["status"], ...>` — **TypeScript の exhaustive check が効いている**。`Task["status"]` に新しい値を追加した場合、これらのオブジェクトにも対応するエントリを追加しないとビルドエラーになる。

→ Geordi は型拡張と同時に両 Record を必ず更新すること。漏れると `tsc --noEmit` が FAIL する。

### [CRITICAL-4] `GET /api/cards` 認証なし + Lua スクリプト

```typescript
// api/cards/route.ts:14-21
const CARDS_SCRIPT = `
local ids = redis.call('LRANGE', KEYS[1], 0, 99)
...
return redis.call('MGET', unpack(keys))
`;
```

- 認証なし（公開エンドポイント）— モバイルブラウザからの承認操作に使用
- Lua スクリプトは `scriptLoad` で SHA キャッシュ済み — スクリプト本体変更時は SHA が変わり NOSCRIPT エラーが発生するが、retry ロジックで自動回復する

→ このファイルは変更不要。新 verification API は別ルートに追加すること。

### [CRITICAL-5] `PATCH` の open update 設計

```typescript
// api/missions/[slug]/tasks/[id]/route.ts:40-44
if (body.status !== undefined) task.status = body.status;
if (body.assignee !== undefined) task.assignee = body.assignee;
if (body.result !== undefined) task.result = body.result;
```

バリデーションなし — 任意の `status` 文字列を書ける。これは意図的な設計（Crewvia が `plan.sh verify-result` で status を書き換えられるようにする）。Geordi は **このエンドポイントにバリデーション追加をしない**こと（既存 Crewvia 連携が壊れる）。新フィールドはこの PATCH に追加する形で対応できる。

---

## 6. 既存 components の再利用可能性

**専用 `<Badge>` コンポーネントファイルは存在しない**。`src/components/` ディレクトリは未作成。

代わりに `page.tsx` 内でインラインの Tailwind クラス + 定数マップが使われる:

```typescript
// ✅ 再利用可能なパターン (page.tsx)
const PRIORITY_BADGE: Record<string, string> = {
  high:   "bg-red-500/20 text-red-400 border-red-500/30",
  medium: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  low:    "bg-zinc-700 text-zinc-400 border-zinc-600",
};

// ✅ 同パターンで追加可能
const VERIFICATION_STATUS_BADGE: Record<string, string> = {
  ready_for_verification: "bg-sky-500/20 text-sky-400 border-sky-500/30",
  verifying:              "bg-violet-500/20 text-violet-400 border-violet-500/30",
  verification_failed:    "bg-red-500/20 text-red-400 border-red-500/30",
  verified:               "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  needs_human_review:     "bg-orange-500/20 text-orange-400 border-orange-500/30",
};
```

`<span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${...}`}>` パターンが 3 箇所以上で統一使用中。**Geordi は新コンポーネントファイルを作らず、このインラインパターンに乗ること**。コンポーネント化は別タスクのスコープ。

---

## 7. TypeScript 型の追加ポイント

### 7.1 既存 Task 型 (actions.ts L183-192)

```typescript
// 現状
export interface Task {
  id: string;
  title: string;
  status: "pending" | "in_progress" | "done" | "blocked";
  assignee: string | null;
  skills: string[];
  priority: "high" | "medium" | "low";
  blocked_by: string[];
  created_at: string;
}
```

```typescript
// 追加後 (optional フィールドで後方互換)
export interface Task {
  id: string;
  title: string;
  status: "pending" | "in_progress" | "done" | "blocked"
        | "ready_for_verification" | "verifying" | "verification_failed"
        | "verified" | "needs_human_review";
  assignee: string | null;
  skills: string[];
  priority: "high" | "medium" | "low";
  blocked_by: string[];
  created_at: string;
  // verification fields (optional — 既存タスクは undefined)
  verifier?: string | null;
  rework_count?: number;
  max_rework?: number;
  verification_mode?: "light" | "standard" | "strict" | "research";
}
```

`status` 拡張は TypeScript の exhaustive map (`STATUS_LABEL`, `STATUS_COLOR`) の更新を必ず伴う。

### 7.2 分離型の必要性

verification result の詳細 (verdict / notes / timestamp / cycle) は Task 型に直接持たせず、Crewvia 側の `registry/verification/` に置き、Taskvia 側は Task の簡易フィールド (`verifier`, `rework_count`) だけ持つ設計が推奨。Taskvia UI は summary 表示のみ担当する。

---

## 8. Phase 0 メモの再確認 (task_089 → Phase A 引き継ぎ)

> 「`/api/status/{id}` に `verifier` / `rework_count` 追加が必要になる可能性」

**確認結果**: `GET /api/status/[id]` は `approval:{id}` カードのステータスを返す (task_089 分析済み)。Verification の結果は **task オブジェクト** (`mission:{slug}:tasks:{id}`) に保存されるべきであり、approval card とは別物。`/api/status/{id}` への追加は不要。

Crewvia `plan.sh verify-result` → `PATCH /api/missions/[slug]/tasks/[id]` で `verifier`/`rework_count` を書き込み → Taskvia UI がポーリングで取得 — このフローで完結する。

---

## 付録: 読んだファイル一覧

| ファイル | サイズ | 主要確認内容 |
|---|---|---|
| `src/app/page.tsx` | 1069行 | 全 UI コンポーネント + polling ロジック |
| `src/app/actions.ts` | 309行 | 全 Server Actions + 型定義 |
| `src/app/layout.tsx` | 34行 | メタデータ + フォント設定 |
| `src/app/api/cards/route.ts` | 65行 | Lua スクリプト最適化 |
| `src/app/api/cards/[id]/route.ts` | 27行 | DELETE 実装 |
| `src/app/api/log/route.ts` | 25行 | ログ POST |
| `src/app/api/missions/route.ts` | 53行 | ミッション CRUD |
| `src/app/api/missions/[slug]/tasks/route.ts` | 67行 | タスク GET/POST |
| `src/app/api/missions/[slug]/tasks/[id]/route.ts` | 49行 | タスク DELETE/PATCH |
| `src/app/api/requests/route.ts` | 152行 | 依頼フォーム API |
| `src/lib/auth.ts` | 12行 | Bearer 認証ヘルパー |
