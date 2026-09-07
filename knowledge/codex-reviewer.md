# Codex Reviewer (Kai-codex) ナレッジベース

> Codex CLI を reviewer として使う際の手順・制約・運用ノウハウ。
> Phase 1 (review 限定) で導入 (mission: 20260907-codex-reviewer-phase1)。
> **Phase 2 (2026-09-08)**: Dispatcher が `codex-review` skill task を自動 spawn するようになった。
> Kai-codex は registry/workers.yaml に登録された正規 worker で、plan.sh pull/done 経由で Taskvia 同期と task_count bump が自動で走る。

---

## Kai-codex の起動手順

### Phase 2: Priya が plan に codex-review task を積むだけ（常用パス）

```bash
plan.sh add "PR#<N> Codex review (Kai)" \
  --skills codex-review \
  --blocked-by t<impl_task_id> \
  --pr-number <N> \
  --priority medium
```

- `--skills codex-review` — 専用 skill 名。Dispatcher がこの skill を検知して `kai-review.sh` を自動 spawn する
- `--pr-number <N>` — frontmatter に PR 番号を刻む。Dispatcher が spawn 時に `--pr` として渡す。**必須**（無いと spawn せず warning）
- `--blocked-by` — 実装 task の完了後に review を走らせる典型パターン

Dispatcher spawn 後のフロー: `kai-review.sh` が `plan.sh pull` → `codex exec review` → `plan.sh done` を実行。Director の手動介入は不要。

### 手動起動（フォールバック）

自動 spawn が失敗したとき、または smoke test で個別に呼ぶとき:

```bash
bash scripts/kai-review.sh \
  --pr <PR番号> \
  --task <task_id> \
  --mission <mission-slug> \
  [--agent Kai-codex] \
  [--model o3] \
  [--skip-pull]                  # task が既に in_progress の場合
```

### モデル選択の目安

| モデル | 用途 |
|---|---|
| デフォルト（codex CLI が決定） | 通常 review・速度重視 |
| `o3` | 重要 mission・大きな diff・クリティカルバグ疑い |

### 前提確認

```bash
# codex CLI が利用可能か確認
which codex   # → /home/tkadmin/.nvm/versions/node/v24.18.0/bin/codex
codex --version
```

---

## 2 人体制での verdict 突合フロー

Phase 1 では Claude (Seo) と Codex (Kai) の **2 人体制**で review する。

```
[Director が review task を 2 つ積む]
  ├─ Seo (Claude)  → plan.sh done "LGTM" / needs-director "NEEDS FIX"
  └─ Kai (Codex)   → kai-review.sh 経由で plan.sh done / needs-director

[2 人の verdict 突合]
  両方 LGTM                → merge 承認（Director が Seo に gh pr merge 指示）
  どちらか NEEDS FIX       → Director が findings を統合して判断
  BRANCH MISMATCH 検出     → 優先度最高、即 Director 判断
  両方 NEEDS FIX (一致)    → 修正タスクを積んで fix → re-review
```

### いつ 2 人体制を使うか

- **重要 mission**（main / staging に影響する code 変更）: 2 人体制推奨
- **軽量 mission**（docs-only / MEMORY 更新 / minor fix）: Seo 1 人で十分
- **特に有効な場面**:
  - Claude 生成コードの review（同一モデル bias 回避）
  - critical bug 疑いのある大きな diff
  - LLM 特有の誤りパターン（hallucination / edge case 見落とし）

**使い方の流れ**: Seo の review task が完了したタイミングで Director が手動で `kai-review.sh` を呼ぶ。Kai review は plan task には載せない（Dispatcher の誤 assign を防ぐため）。

---

## Codex CLI の特徴

Phase 1 で使用する呼び出し形式:

```bash
codex exec review --base main \
  -m <model> \
  --ephemeral \            # セッション保存を省略
  -o /tmp/kai-review-output.txt
```

- **`exec review`**: 非インタラクティブ review モード（`-p` 相当）
- **`--base main`**: main との diff を対象にする
- **`--ephemeral`**: セッション状態を保存しない（CI 相当の使い方）
- **出力先 `-o`**: findings を指定ファイルに書き出す

### codex rescue との違い

| 用途 | コマンド |
|---|---|
| Codex Reviewer (Kai) | `codex exec review --base main ...` |
| Codex Rescue (既存 skill) | `codex:codex-rescue` skill 経由の対話的セッション |

---

## hook 不足の制約と workaround

Codex CLI は Claude Code の pre/post-tool-use hook を**持たない**。Phase 2 では plan.sh + dispatcher でカバーできる箇所を全て埋めた:

| 機能 | Claude Worker | Kai-codex (Phase 2) |
|---|---|---|
| Taskvia approval リクエスト | 自動（hook） | **なし** — Codex CLI に該当機構が無い（Phase 3 検討） |
| Taskvia カンバン表示 | あり（hook） | **あり** — plan.sh pull が taskvia_sync_pull を発火 |
| Taskvia agents 表示 | あり（heartbeat） | **あり** — kai-review.sh が起動時に registry/heartbeats/Kai-codex を touch |
| knowledge ログ自動投稿 | あり（hook） | **なし** — findings は plan.sh 経由で task ファイル Result に残る |
| heartbeat / watchdog 連携 | あり | **限定的** — 起動時 touch のみ（review 中は更新なし） |
| registry task_count 更新 | 自動（plan.sh done 内） | **自動** — Kai-codex が registry 登録済みなので同経路 |

**運用上の対処**:
- Dispatcher の spawn ログ: `logs/kai-spawn/<slug>-<task_id>-<epoch>.log`
- review findings は `/tmp/kai-review-output.txt` と task の Result セクションに残る
- Kai-codex がハングしたら:
  ```bash
  rm queue/assignments/Kai-codex   # spawn ロック解除
  plan.sh update <task_id> --status pending --reset --mission <slug>   # task 復旧
  ```

---

## Phase 3 予定

Phase 2 の実運用結果を見て、Phase 3 で以下を検討する:

- `start.sh --engine codex` で bash / code / docs / qa の Codex 化
- kai-review.sh 内で Taskvia の /api/request (approval) と /api/knowledge を直接呼ぶ
- `worker-names.yaml` に engine 情報（codex / claude）を含める
- LGTM 一致時の自動 merge 承認
- heartbeat 継続更新（長時間 review のタイムアウト管理）

---

## 参考ファイル

- `agents/worker-codex.md` — Kai の identity・操作手順（Director 向け詳細版）
- `scripts/kai-review.sh` — Kai 起動スクリプト実体
- `codex:codex-cli-runtime` skill — Codex CLI の詳細 syntax
- `codex:gpt-5-4-prompting` skill — Codex prompt 設計指針
- `knowledge/review.md` — Claude Seo の review ナレッジ（比較参考）
