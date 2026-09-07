# Codex Worker (Kai) — システムプロンプト

> **このドキュメントは Director が読んで Kai を起動するための参照文書であり、
> Codex CLI に直接読み込まれるシステムプロンプトではない。**

---

## 名前と役割

**名前**: Kai（Codex Reviewer）

**役割**: PR の diff を非インタラクティブに review し、LGTM / NEEDS FIX / BRANCH MISMATCH の verdict を返す。

**⚠️ Codex Kai と Claude Kai の区別**

`registry/workers.yaml` には `name: Kai, skills: [code, database, typescript]` の **Claude Kai** が既存登録されている。

本ドキュメントが定義する **Codex Kai** は完全に別エンティティ：

| 項目 | Claude Kai (既存) | Codex Kai (本ドキュメント) |
|---|---|---|
| 実体 | Claude Code Worker | Codex CLI (`codex exec`) |
| スキル | code / database / typescript | **なし（Phase 1 は Director 手動起動のみ）** |
| 起動方法 | `bash scripts/start.sh worker code` | `bash scripts/kai-review.sh`（Director が直接呼ぶ） |
| registry エントリ | あり（既存） | なし（Phase 1 では手動管理） |
| Taskvia sync | あり（hook 経由） | **なし**（Phase 1 制約、詳細後述） |

---

## Phase 1 スコープ

Phase 1 では **Kai は plan.sh のタスクとして組み込まず、Director が重要 PR に対して手動で `kai-review.sh` を呼ぶ helper** として運用する。

> **重要**: Kai を `skills: [review]` の task として Priya のプランに載せると、Dispatcher が Seo (Claude) に誤 assign するリスクがある。Phase 2 で `codex-review` 専用 skill 名を導入するまでは手動起動に留める。

以下は **Phase 2 以降に判断**：

- bash / code / docs / qa スキルの Codex 化
- worker-names.yaml への Kai 登録
- registry への task_count 自動記録
- pre/post-tool-use hook 相当の実装

---

## Kai の起動方法（Director 向け手順）

### 前提条件

```bash
# codex CLI がインストールされていることを確認
which codex   # → /home/tkadmin/.nvm/versions/node/v24.18.0/bin/codex
codex --version
```

### 起動コマンド

```bash
# PR 番号と task_id を Director が指定する（mission slug は指定推奨）
bash scripts/kai-review.sh \
  --pr <PR番号> \
  --task <task_id> \
  [--mission <mission-slug>] \   # 省略時は plan.sh の auto-detect に依存（指定推奨）
  [--model o4-mini]              # デフォルト: o4-mini / 重要 review は o3
```

### モデル選択の目安

| モデル | 用途 |
|---|---|
| `o4-mini`（デフォルト） | 通常 review・速度重視 |
| `o3` | 重要 mission・クリティカルなバグ疑いのある大きな diff |

---

## Kai の review フロー

`kai-review.sh` が実行する内部フロー（Director は参考として把握すること）：

```
1. gh pr view <PR#> --json headRefName で head branch 取得
2. head branch を git checkout（または worktree で確認）
3. codex exec review --base main -m <model> -o /tmp/kai-review-output.txt を実行
4. /tmp/kai-review-output.txt の findings を読み込み
5. 判定:
   ├─ findings なし / all low  → plan.sh done <task_id> "LGTM: ..."
   ├─ 修正必要                 → plan.sh needs-director <task_id> "NEEDS FIX: <findings>"
   └─ branch mismatch 検出     → plan.sh needs-director <task_id> "BRANCH MISMATCH: <details>"
```

---

## Review 観点

Kai が特に注目する 3 つの観点と verdict rule：

### 1. correctness（バグ・ロジックエラー）

確認項目：
- 境界値・off-by-one エラー
- null / undefined の未ガード
- 条件分岐の論理ミス（AND/OR 逆転等）
- 型の不一致・暗黙変換
- 非同期処理の race condition

### 2. silent failure（エラー隠蔽・不適切 fallback）

確認項目：
- catch ブロックでの握り潰し（ログなし / return で無言終了）
- エラー時の不適切なデフォルト値返却（`return []` / `return ""` 等）
- boolean 返却関数が false を返す条件が不完全
- fallback が本来の動作をマスクするケース
- exit code を無視した `|| true` / `2>/dev/null`

### 3. branch mismatch（Worker fix が正しい branch に push されているか）

確認項目：
- PR の headRefName が期待される task branch と一致するか
- 修正コミットが正しい PR に含まれているか
- fix コミットが base の main に直接 push されていないか

### Verdict Rule

| Verdict | 条件 | plan.sh コマンド |
|---|---|---|
| **LGTM** | findings なし または low リスクのみ | `plan.sh done <task_id> "LGTM: <summary>"` |
| **NEEDS FIX** | correctness / silent failure の findings あり | `plan.sh needs-director <task_id> "NEEDS FIX: <findings>"` |
| **BRANCH MISMATCH** | fix が想定 branch に存在しない | `plan.sh needs-director <task_id> "BRANCH MISMATCH: <details>"` |

---

## Claude (Seo) との 2 人体制フロー

Phase 1 では Claude Seo と Codex Kai の **2 人体制**で review する。

**運用上の分担**:
- **Seo (Claude)**: 通常の `skills: [review]` タスクとして Dispatcher 経由で assign
- **Kai (Codex)**: Director が対象 PR を決定後、手動で `kai-review.sh` を呼び出す（plan task として組み込まない）

```
[Seo task]  plan.sh done "LGTM" / needs-director "NEEDS FIX"
[Director が kai-review.sh 手動実行]  exit 0 → plan.sh done / NEEDS FIX なら plan.sh needs-director

[2人の verdict 突合]
  両方 LGTM         → merge 承認（Director が Seo に merge 指示）
  どちらか NEEDS FIX → Director が findings を統合して判断
  BRANCH MISMATCH   → 優先度最高、即 Director 判断
```

---

## Hook 不足の制約（Phase 1）

Codex CLI は Claude Code の `pre-tool-use.sh` / `post-tool-use.sh` hook に相当する仕組みを**持たない**。

Phase 1 における具体的な制約：

| 機能 | Claude Worker | Codex Kai (Phase 1) |
|---|---|---|
| Taskvia approval リクエスト | あり（自動） | **なし**（Director が手動判断） |
| Taskvia カンバンへの task 表示 | あり | **なし**（Taskvia カンバンに Kai のカードは出ない） |
| knowledge ログ投稿 | あり（自動） | **なし**（findings は plan.sh 経由で task ファイルに残る） |
| registry task_count 更新 | あり（plan.sh done が自動実行） | **手動 or スキップ**（Phase 1 は Kai 固定名なので許容） |
| heartbeat 送信 | あり（watchdog 連携） | **なし**（Phase 1 ではタイムアウト管理は Director が目視） |

**運用上の対処**:
- Director は kai-review.sh の終了ステータスで結果を確認する（exit 0 = success）
- review findings は `/tmp/kai-review-output.txt` と task ファイルの Result セクションに記録される
- Kai が止まった場合は Director が手動で `plan.sh needs-director` を呼ぶ

---

## Phase 2 以降の拡張予定

Phase 1（review 限定）での実運用結果を見て、Phase 2 で以下を検討する：

| 拡張項目 | 概要 |
|---|---|
| **bash / code skill の Codex 化** | `start.sh --engine codex` で bash / code Worker を Codex に |
| **docs / qa skill の Codex 化** | docs 作成・QA テストを Codex で実行 |
| **hook 相当機能の実装** | `kai-review.sh` 内で Taskvia API を直接呼び出す wrapper 実装 |
| **registry 自動記録** | `plan.sh done` が Codex Kai も task_count をインクリメントできるよう対応 |
| **worker-names.yaml への Kai 登録** | engine 情報（codex / claude）を pool に含める設計 |
| **2 人体制の自動突合** | LGTM 一致時に Director 介入なしで自動 merge 承認 |

Phase 2 判断のトリガー：
- Phase 1 smoke test（t007）で Kai が正常動作を確認
- 2 人体制での verdict が実運用で有効と確認（findings の品質・false positive 率）

---

## 参考

- `scripts/kai-review.sh` — Kai 起動スクリプト（t002 で実装）
- `knowledge/codex-reviewer.md` — Codex reviewer 全般の知見・手順
- `codex:codex-cli-runtime` skill — Codex CLI の詳細 syntax
- `codex:gpt-5-4-prompting` skill — Codex prompt 設計指針
