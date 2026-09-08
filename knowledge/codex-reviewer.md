# Codex Reviewer (Kai-codex) ナレッジベース

> Codex CLI を reviewer として使う際の手順・制約・運用ノウハウ。
> Phase 1 (review 限定) で導入 (mission: 20260907-codex-reviewer-phase1)。
> **Phase 2 (2026-09-08)**: Dispatcher が `codex-review` skill task を自動 spawn するようになった。
> Kai-codex は registry/workers.yaml に登録された正規 worker で、plan.sh pull/done 経由で Taskvia 同期と task_count bump が自動で走る。
> **Phase 3 (2026-09-08, mission: 20260908-codex-reviewer-phase3, PR#180)**: Codex 自身の self-review で見つかった
> kai-review.sh の欠陥 (主 working tree 汚染・stale branch review・findings 判定の fail-open・固定 /tmp パス衝突) を
> まとめて修正。詳細は下記「Phase 3: kai-review.sh 安定化」を参照。

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

## Phase 3: kai-review.sh 安定化 (2026-09-08, PR#180)

Kai-codex 自身に PR#173 (kai-review.sh 初版) を self-review させたところ、以下の欠陥が見つかり、
まとめて修正した (`scripts/kai-review.sh` のヘッダーコメントに実装レベルの詳細あり)。

### 主 working tree 非破壊化 + stale branch review 対策 (F1/F1b)

- **旧実装の問題**: `$CREWVIA_REPO_ROOT`（主リポジトリ）で直接 `git checkout` していたため、fetch 失敗時に
  古い local branch のまま review してしまい (F1)、review 後も主 WT の HEAD が PR branch に切り替わったまま
  残っていた (F1b、他 Worker の worktree 運用と衝突しうる)。
- **対処**: `refs/pull/<PR#>/head` を origin から一意な local ref (`refs/kai-review-fetch/pr-<N>-$$`) へ
  fetch し、そこから専用の使い捨て git worktree (`mktemp -d` + `git worktree add --detach`) を作り、
  その中で `codex exec -C <worktree> review` を実行する。主 WT の HEAD は一切動かさない。fetch は
  fork PR や削除済み branch でも `refs/pull/<PR#>/head` が GitHub 上に残っている限り機能する
  (旧実装の `origin/<branch>` 参照は fork PR や削除済み branch で必ず失敗していた)。
- worktree・fetch した一時 ref は `trap cleanup EXIT` で成功/失敗どちらの経路でも必ず削除する。

### findings 判定ロジックの変遷 (F2 → F-1/F-3 → [P2])

現物確認 (codex-cli 0.144.5) の結果、`codex exec review` の実際の出力は JSON ではなく自然文 +
`- [P0]`〜`- [P3]` タグ付き箇条書きだった。判定ロジックは QA re-review を重ねて 3 段階で堅牢化した:

1. **F2**: 「LGTM キーワードが無ければ needs-director」という旧ルールは、clean な review でも
   LGTM と言わないため常に誤発火していた。`[P#]` タグの有無を主判定に切り替えた。
2. **F-1 / F-3 (QA FAIL)**: タグ抽出が `[P1-3]` のみで `[P0]` を拾えていなかった (自動 done 側に
   倒れる危険な欠陥)。また critical キーワードの safety net が "No critical issues found." のような
   **否定文脈の健全な報告**まで拾って誤発火していた。→ `[P0]` を判定対象に含め、構造化シグナル
   ([P#] タグ or JSON) が得られた場合は keyword fallback を使わない (`HAD_SIGNAL`) ように変更。
3. **[P2] (Seo 最終レビュー, t018)**: JSON 経路の入口ゲート (`jq -e '.findings | length'`) が
   `.findings` 欠損/null でも `length` が `0` を返し `-e` が exit 0 になる fail-open だった
   (**3 回連続で同じ「自動承認側に倒れる」構造の欠陥**: [P0] 抜け → JSON 内側の allowlist →
   JSON 入口ゲート)。`.findings | arrays | length` に変更し、真の配列でない限り JSON 経路に
   入らないようにした。

**教訓**: レビューゲートの判定ロジックは、迷ったら「修正必要」側に倒す（fail-closed）ことを
毎回明示的に確認すること。denylist（危険と確認できないものは全部危険側）で組むほうが、
allowlist（安全と確認できたものだけ安全側）より事故りにくい。

### `--dry-run` (F6, Director 指示)

`--skip-pull` は plan.sh pull だけを飛ばす（task が既に in_progress な場合の再実行用）のに対し、
`--dry-run` は plan.sh への書き込み（pull/done/needs-director）を一切行わず、判定結果を stdout に
表示するだけ。実 PR に対する smoke test・動作確認で実タスクの status を壊さないための区別。

### 一時ファイルの mktemp 化 (F3)

固定 `/tmp/kai-review-output.txt` は codex-review task が並列実行された場合（手動起動との衝突含む）に
相互上書きする事故を招くため、出力ファイル・stderr ファイルとも `mktemp` で一意化し、cleanup trap で
削除する。

### `codex-review` skill の正式登録

`plan.sh lint` が「skill 'codex-review' not in skill-permissions.yaml」警告を出していた積み残しを解消
するため、`config/skill-permissions.yaml` に `codex-review: {allow: [], deny: []}` として登録した。
Codex CLI プロセスは Claude Code の PreToolUse hook を経由しないため、この allow/deny は実行時には
適用されない（known skill 一覧に載せるためだけの登録）。

---

## 運用上の注意（Phase 3 で判明）

### codex-review の鶏と卵問題

Dispatcher は常に **main 版の `scripts/kai-review.sh`** を起動する
（`dispatcher.sh` の `KAI_REVIEW_SH` は `$CREWVIA_REPO_ROOT/scripts/kai-review.sh` を指す絶対パス）。
そのため **kai-review.sh 自体を修正する PR は、merge されるまで自分自身で dogfood できない**
（Phase 3 で実際に 4 回空振りした）。

- kai-review.sh を修正する task の `codex-review` review task（自己レビュー）は、
  **fix PR が main に merge されてから** 積むこと。merge 前に積んでも旧コードでレビューされる。
- Director は §12 の PR 運用同様、「常駐プロセスが読み込むファイルの fix は merge 後に
  効果を確認する」原則を codex-review にも適用する。

### Result の記録方法

`plan.sh done` は複数行を安全に扱う（`build_task_body` 経由で body に書くだけで frontmatter には
触れない）。1 行制約が必要なのは `plan.sh needs-director` の reason だけ
（frontmatter の `needs_director_reason` に直接書かれるため）。task ファイルの直接編集
（heredoc 等）は禁止 — Worker が長時間ハングする事故につながる（詳細: `agents/worker.md` §5）。

---

## Phase 4 候補（未着手）

Phase 2/3 の実運用結果を見て、以下は着手していない検討事項として残っている:

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
