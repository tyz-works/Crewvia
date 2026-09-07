# Codex Reviewer (Kai) ナレッジベース

> Codex (Kai) を reviewer として使う際の手順・制約・運用ノウハウ。
> Phase 1 (review 限定) で導入。mission: 20260907-codex-reviewer-phase1

---

## Kai の起動手順

### 基本コマンド

```bash
bash scripts/kai-review.sh \
  --pr <PR番号> \
  --task <task_id> \
  [--mission <mission-slug>] \   # 省略時は plan.sh の auto-detect に依存（指定推奨）
  [--model o4-mini]              # 省略時デフォルト: o4-mini
```

> **⚠️ Kai review は Priya のプランの task として組み込まない**。`skills: [review]` で積むと Dispatcher が Seo (Claude) に誤 assign するリスクがある。Director が重要 PR を判断して手動で呼び出すこと。Phase 2 で `codex-review` 専用 skill 名が導入されたらこの制約は解除される。

### モデル選択の目安

| モデル | 用途 |
|---|---|
| `o4-mini`（デフォルト） | 通常 review・速度重視 |
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

## hook 不足の制約と workaround（Phase 1）

Codex CLI は Claude Code の pre/post-tool-use hook を**持たない**。

| 機能 | Claude Worker | Codex Kai (Phase 1) |
|---|---|---|
| Taskvia approval リクエスト | 自動（hook） | **なし**（Director 手動判断） |
| Taskvia カンバン表示 | あり | **なし**（Kai カードは出ない） |
| knowledge ログ自動投稿 | あり（hook） | **なし**（findings は task ファイルに残る） |
| heartbeat / watchdog 連携 | あり | **なし**（Director が目視でタイムアウト管理） |
| registry task_count 更新 | 自動（plan.sh done 内） | **手動 or スキップ**（Phase 1 許容） |

**運用上の対処**:
- `kai-review.sh` の終了 exit code で成否を確認（exit 0 = plan.sh 呼び出し成功）
- findings は `/tmp/kai-review-output.txt` と task の Result セクションに残る
- Kai が止まった場合は Director が手動で `plan.sh needs-director` を呼ぶ

---

## Phase 2 予定（dispatcher 統合後）

Phase 2 で検討する拡張（実運用 smoke test 後に判断）:

- `start.sh --engine codex` で bash / code / docs / qa の Codex 化
- `kai-review.sh` 内から Taskvia API を直接呼び出す wrapper 実装
- Dispatcher が Kai に直接 assign 通知（現在は Director が手動起動）
- `plan.sh done` が Codex Kai の task_count も自動インクリメント
- `worker-names.yaml` に engine 情報（codex / claude）を含める

---

## 参考ファイル

- `agents/worker-codex.md` — Kai の identity・操作手順（Director 向け詳細版）
- `scripts/kai-review.sh` — Kai 起動スクリプト実体
- `codex:codex-cli-runtime` skill — Codex CLI の詳細 syntax
- `codex:gpt-5-4-prompting` skill — Codex prompt 設計指針
- `knowledge/review.md` — Claude Seo の review ナレッジ（比較参考）
