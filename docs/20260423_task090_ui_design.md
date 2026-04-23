# Taskvia 新 UI ワイヤーフレーム設計書

作成: 2026-04-23 / Counselor Troi (task_090_pa_troi)
参照: `docs/plans/20260423_task090.md` §4 Phase A / Data Phase A 分析 `20260423_task090_taskvia_ui_analysis.md`
対象: Geordi Phase D 実装の根拠資料

---

## 0. Admiral 確定方針サマリー

| # | 論点 | 方針 |
|:---:|---|---|
| 1 | UI 位置 | **(C) バッジ + 独立タブ両方** |
| 2 | リアルタイム性 | **(a) polling 5 秒** |
| 3 | Rework 履歴 | **(Y) `rework_count` 常時表示** |
| 4 | feature flag | `CREWVIA_VERIFICATION_UI=disabled` 時は全 verification UI を非表示 |

---

## 1. バッジ仕様

### 1.1 5 状態定義

| 状態名 | Tailwind クラス | アイコン | aria-label |
|---|---|---|---|
| `pending` | `bg-zinc-700/20 text-zinc-400 border-zinc-600` | `○` | "verification: pending" |
| `verifying` | `bg-sky-500/20 text-sky-400 border-sky-500/30` | `🔍` | "verification: in progress" |
| `verified` | `bg-emerald-500/20 text-emerald-400 border-emerald-500/30` | `✓` | "verification: passed" |
| `failed` | `bg-red-500/20 text-red-400 border-red-500/30` | `✕` | "verification: failed" |
| `rework:n` | `bg-orange-500/20 text-orange-400 border-orange-500/30` | `↩` | "verification: rework (n times)" |

色覚バリア対応: 色だけに頼らず、アイコン記号を必ず併用すること。

### 1.2 表示位置

Task カード右上（Tailwind の絶対配置 `absolute top-2 right-2`）。
既存の Approval badge（⚠️ 承認）が表示中の場合は Approval badge の下 `top-9 right-2` にずらす。

```
┌─────────────────────────────────┐
│ [タスクタイトル]       ○ pending│  ← バッジ右上
│ Assignee: Geordi                │
│ Priority: HIGH                  │
│                                 │
│ ⚠️ 承認 1件    [▼ 展開]         │
└─────────────────────────────────┘
```

Data 参照: `TaskCard` コンポーネント L338-346 付近の Approval badge 直後に挿入。

### 1.3 rework_count サマリ形式

`rework:n` バッジのラベル: `↩ rework: 2/3`（実施済み回数 / max_rework）。

```tsx
// 実装イメージ
const label = rework_count > 0
  ? `↩ rework: ${rework_count}/${max_rework ?? 3}`
  : icon + " " + STATUS_LABEL[status];
```

---

## 2. Verification Queue タブ設計

### 2.1 Header nav への追加

現状 `type Tab = "board" | "logs"` を `"board" | "logs" | "verification"` に拡張。
nav 配列 (page.tsx L951) に `"verification"` を追加する。

```
┌────────────────────────────────────────────┐
│  Board   Logs   Verification (2)           │
│  ─────                                     │
└────────────────────────────────────────────┘
```

タブ名右に pending 件数バッジ（`ready_for_verification` + `verifying` の合計）を表示する。
件数が 0 の場合はバッジを非表示（空の `"(0)"` は出さない）。

### 2.2 画面レイアウト

```
┌─────────────────────────────────────────────────────┐
│ Verification Queue                    [フィルタ ▼]  │
├─────────────────────────────────────────────────────┤
│ 🗂 mission: m-qa-2026                               │
│   ┌─────────────────────────────────────────────┐   │
│   │ task_090_pa_troi  [🔍 verifying]            │   │
│   │ Verifier: Beverly                           │   │
│   └─────────────────────────────────────────────┘   │
│   ┌─────────────────────────────────────────────┐   │
│   │ task_090_pa_data  [○ pending]               │   │
│   │ ─                                           │   │
│   └─────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────┤
│ 🗂 mission: m-qa-2026-b                             │
│   ┌─────────────────────────────────────────────┐   │
│   │ task_087_worf     [↩ rework: 1/3]           │   │
│   └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

#### グルーピング
- mission slug ごとにセクション分け（`mission:index` から取得）
- mission 内の並び順: `verifying` → `ready_for_verification` → `rework:n` の順（status priority）
- mission 間の並び順: 最新アクティブ mission を先頭（mission の `created_at` 降順）

#### フィルタ（ドロップダウン）
- `すべて` （デフォルト）
- `検証待ち (ready_for_verification)`
- `検証中 (verifying)`
- `要 rework`

フィルタは URL クエリではなく React state（tab 遷移でリセット）で管理。

#### データ取得
既存 `loadTasks` の結果から `status === "ready_for_verification" || "verifying" || rework_count > 0` でクライアントフィルタ。**新規 fetch 不要**（Data 分析 §4.2 の推奨に従う）。

### 2.3 空状態

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│              ✓ Verification Queue is clear          │
│          すべてのタスクが確認済みです              │
│                                                     │
└─────────────────────────────────────────────────────┘
```

テキストは `text-zinc-500 text-sm text-center py-16`。

### 2.4 ローディング状態

タブ初回切替時は Board タブと同じ loading skeleton（`animate-pulse bg-zinc-800 rounded-xl h-16`）を mission 別に 2〜3 行分描画。
polling 5s の再取得中はスケルトンを出さない（既存データを保持、フラッシュ抑制）。

---

## 3. Card 展開時の rework 履歴表示

### 3.1 挿入位置

`TaskDetailDialog` (page.tsx L255-261 の Assignee ブロック直後) に追加。

### 3.2 レイアウト (cycle 別 verdict + 失敗原因)

```
┌──────────────────────────────────────────────┐
│ 担当: Geordi                                 │  ← 既存 Assignee
├──────────────────────────────────────────────┤
│ Verification                                 │
│   Verifier: Beverly                          │
│   ↩ rework: 2/3                              │
├──────────────────────────────────────────────┤
│ Rework 履歴                                  │
│                                              │
│  cycle 1  ✕ failed                          │
│  2026-04-22 14:32  Beverly                   │
│  "AC item 3 の実装漏れ: polling 間隔が..."  │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─           │
│  cycle 2  ✕ failed                          │
│  2026-04-22 16:15  Beverly                   │
│  "修正後も edge case で 404 が発生..."      │
└──────────────────────────────────────────────┘
```

#### 行数目安
- Verifier / rework_count セクション: **3 行**（ラベル + Verifier + rework count）
- 各 cycle 行: **4 行**（cycle 番号+verdict / 日時+Verifier / 原因サマリ / セパレーター）
- max_rework=3 の最大ケース: **3×4 + 3 = 15 行**（セパレーター除く実質 12 行）

#### データソース
crewvia `registry/verification/<task_id>.json` の cycle 別エントリを Taskvia が `PATCH /api/missions/[slug]/tasks/[id]` で受け取り、`rework_history` フィールド（JSON 配列）として Redis に保存。

```typescript
// rework_history エントリ構造 (crewvia → Taskvia push)
interface ReworkEntry {
  cycle: number;
  verdict: "failed" | "conditional_pass";
  verifier: string;
  timestamp: string;
  notes: string;  // 失敗原因サマリ（100文字以内推奨）
}
```

履歴が空（rework_count=0）の場合は Rework 履歴セクション自体を非表示。
展開/折りたたみ UI は不要（常時表示）。3 件超えの場合は新しい順で表示し、古い cycle から 1 件分のみ折りたたむ（「+ x 件表示」リンク）。

---

## 4. Polling 5s UX 配慮

### 4.1 フラッシュ抑制

React state 更新は新旧データが同一の場合 `JSON.stringify` 比較でスキップする。
Verification Queue タブは既存 Board ポーリングの結果を subscribe するだけで、独自の `setInterval` を追加しない。

```typescript
// 既存 loadTasks 内の更新ロジックに追加
if (JSON.stringify(latest) !== JSON.stringify(prev)) {
  setTasks(latest);
}
```

### 4.2 loading indicator

- **初回ロード**: スケルトン表示（`animate-pulse`）
- **polling 更新中**: 表示なし（既存データを保持して無音更新）
- **fetch エラー時**: タブ上部に `text-red-400 text-xs` のインラインエラーバー 1 行

```
⚠ データ取得に失敗しました。5秒後に再試行します。
```

エラーが 3 回連続した場合は手動更新ボタン（`⟳ 更新`）を表示。

### 4.3 Smart polling 拡張 (Geordi 注意)

既存の `hasActive` 判定（Data 分析 [CRITICAL-2] 参照）:

```typescript
// 変更前
const hasActive = latest.some((t) => t.status === "in_progress");

// 変更後
const hasActive = latest.some(
  (t) => t.status === "in_progress" || t.status === "verifying"
);
```

`verifying` を加えないと検証中でも 20s idle ポーリングに落ちる。

---

## 5. Feature Flag `CREWVIA_VERIFICATION_UI=disabled` 時の挙動

### 5.1 対象スコープ

| UI 要素 | disabled 時 |
|---|---|
| Card verification バッジ | 非表示 |
| Header nav `Verification` タブ | 非表示 |
| `TaskDetailDialog` の Rework 履歴セクション | 非表示 |
| Header のタブ件数バッジ | 非表示 |
| API呼び出し（verification 系）| クライアントから呼ばない |

### 5.2 実装パターン

```typescript
// Next.js: 環境変数はビルド時埋め込み or Runtime Config
const verificationEnabled =
  process.env.NEXT_PUBLIC_CREWVIA_VERIFICATION_UI !== "disabled";

// 使用箇所
{verificationEnabled && (
  <VerificationBadge status={task.verificationStatus} ... />
)}
```

`NEXT_PUBLIC_` プレフィックスで CSR に渡す。Vercel 環境変数に設定する。

### 5.3 disabled 時の既存動作

Board タブ / Logs タブはそのまま動作。approval フロー影響なし。
既存の Task 型に verification フィールドを optional で持たせてあるため、disabled 時でも型エラーなし。

---

## 6. アクセシビリティ最低要件

### 6.1 色覚バリア対応

- すべてのバッジはアイコン記号（○ / 🔍 / ✓ / ✕ / ↩）を色と必ず併用
- Tailwind の `text-*` color だけに頼った情報伝達を禁止
- 色のテキスト説明を `aria-label` で補完（§1.1 の `aria-label` 列を参照）

### 6.2 aria-label / role 要件

```tsx
<span
  className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${badgeClass}`}
  role="status"
  aria-label={`verification: ${ariaLabel}`}
>
  {icon} {label}
</span>
```

`role="status"` を付けると Screen Reader が状態変化を読み上げる（polling 更新時も）。

### 6.3 キーボードナビゲーション

- Verification Queue タブは既存 nav ボタンと同じ `focus:ring-2` スタイルを踏襲
- `TaskDetailDialog` の rework 履歴は `<div role="list">` + 各 cycle に `role="listitem"`
- ESC / Tab キーによるダイアログ操作は既存実装（L205-225）をそのまま継承

---

## 7. Geordi Phase D 実装者向け警告ポイント

### ⚠ W-1: STATUS_LABEL / STATUS_COLOR の exhaustive check

`Task["status"]` に新値を追加したら `STATUS_LABEL` と `STATUS_COLOR` の両 Record を**必ず同時に**更新すること。漏れると `tsc --noEmit` がビルドエラーを出す（Data 分析 [CRITICAL-3] 参照）。

### ⚠ W-2: Smart polling hasActive 判定

`verifying` を `hasActive` に含めないと、検証中でも 20s idle ポーリングに落ちる。§4.3 の変更パターンを適用すること（Data 分析 [CRITICAL-2] 参照）。

### ⚠ W-3: Approval card キー名前空間

`approval:index` / `approval:{id}` キーには**絶対に触れない**。verification 用は独立した名前空間（`verification:*` 等）を使うこと。Lua スクリプトが SHA キャッシュで動作しており、キー名変更は NOSCRIPT エラーを引き起こす（Data 分析 [CRITICAL-1] / [CRITICAL-4] 参照）。

### ⚠ W-4: PATCH open update にバリデーションを追加しない

`PATCH /api/missions/[slug]/tasks/[id]` は意図的にノーバリデーション設計。Geordi がバリデーションを追加すると既存 crewvia `plan.sh` の status 書き換えが壊れる（Data 分析 [CRITICAL-5] 参照）。

---

## 8. Data Phase A との相互参照

| この文書セクション | Data 成果物での対応箇所 |
|---|---|
| §1.2 バッジ挿入位置 | §4.1 TaskCard L338-346 付近 |
| §2.1 nav 拡張 | §4.2 Header nav L37-38 / L950-964 |
| §2.2 データ取得方針 | §4.2 「既存 loadTasks を再利用可能」 |
| §3.2 rework_history フィールド | §7.2 分離型の必要性 |
| §4.3 Smart polling 拡張 | §5 [CRITICAL-2] |
| §7 W-1/W-2/W-3/W-4 | §5 [CRITICAL-1〜5] |

---

*作成: Counselor Troi — task_090_pa_troi / Phase A*
