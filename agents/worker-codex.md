# Codex Worker (Kai-codex) — システムプロンプト

> **このドキュメントは Director が読んで Kai-codex を理解するための参照文書であり、
> Codex CLI に直接読み込まれるシステムプロンプトではない。**
>
> **Phase 2 (2026-09-08) 以降**: 起動は Dispatcher が自動で行う。Director の
> 手動介入は codex CLI 不在時のフォールバック時のみ。

---

## 名前と役割

**名前**: Kai-codex（Codex Reviewer）

**役割**: PR の diff を非インタラクティブに review し、LGTM / NEEDS FIX / BRANCH MISMATCH の verdict を返す。

**⚠️ Codex Kai (Kai-codex) と Claude Kai の区別**

`registry/workers.yaml` には `name: Kai, skills: [code, database, typescript]` の **Claude Kai** が既存登録されている。

Phase 2 で登録した **Codex Kai** の worker 名は **`Kai-codex`** に決めており、Claude Kai と名前衝突しない:

| 項目 | Claude Kai (既存) | Codex Kai = Kai-codex (Phase 2) |
|---|---|---|
| 実体 | Claude Code Worker | Codex CLI (`codex exec`) |
| スキル | code / database / typescript | **codex-review**（専用 skill） |
| 起動方法 | `bash scripts/start.sh worker code` | Dispatcher が自動 spawn（Phase 2）／手動: `bash scripts/kai-review.sh` |
| registry エントリ | あり（既存） | **あり（Kai-codex）** |
| Taskvia sync | あり（hook 経由） | あり（plan.sh pull/done 経由で PATCH） |

---

## Phase 2 スコープ (現行)

Phase 2 では **Kai-codex を dispatcher が自動 spawn する**。Priya は `skills: [codex-review]` + `pr_number: N` の task を積むだけで、Director の手動介入なしで review が走る。

**運用フロー (Phase 2)**:

1. Priya が計画時に codex-review task を積む:
   ```bash
   plan.sh add "PR#42 Codex review" --skills codex-review --blocked-by t003 --pr-number 42
   ```
2. Dispatcher が 5s poll で検知 → `nohup kai-review.sh --pr 42 --task tXXX --mission <slug> --agent Kai-codex` を background spawn
3. kai-review.sh が `plan.sh pull` で task を in_progress にし、Taskvia PATCH を発火
4. codex exec (--output-schema, t006 以降) が完了したら `plan.sh done` または `plan.sh needs-director` で状態遷移 + Taskvia sync + registry の task_count 自動 bump

**Dispatcher の安全策**:

- `queue/assignments/Kai-codex` が存在する間は 2 重 spawn しない
- `pr_number` が frontmatter に無い codex-review task は spawn せず warning log を出して skip する（Director escalation）
- codex-review skill は `DIRECTOR_ONLY_SKILLS` と同型で「no worker 起動要求」通知を抑制する
- 通常の Worker への assign ループでも codex-review skill を defense-in-depth で skip する

**Phase 3 以降で検討する拡張**:

- bash / code / docs / qa スキルの Codex 化
- worker-names.yaml の pool に Kai-codex を含める（現在は registry 直登録のみ）
- pre/post-tool-use hook 相当の approval / knowledge log 実装
- LGTM 一致時の自動 merge 承認

---

## Kai-codex の起動方法

### Phase 2: Dispatcher が自動 spawn する（常用パス）

Priya が計画時に codex-review task を積めば、Dispatcher が自動で `kai-review.sh` を呼ぶ。Director は基本的に何もしない。

```bash
# Priya の plan-review skill から発行される typical な add コマンド
plan.sh add "PR#<N> Codex review (Kai)" \
  --skills codex-review \
  --blocked-by t<impl_task> \
  --pr-number <N> \
  --priority medium
```

Dispatcher の spawn ログは `logs/kai-spawn/<slug>-<task_id>-<epoch>.log` に残る。

### 手動起動（フォールバック）

codex CLI 不在などで自動 spawn が失敗した場合、Director が直接呼ぶ:

```bash
# 前提: codex CLI がインストールされていることを確認
which codex   # → /home/tkadmin/.nvm/versions/node/v24.18.0/bin/codex

# 起動コマンド
bash scripts/kai-review.sh \
  --pr <PR番号> \
  --task <task_id> \
  --mission <mission-slug> \
  [--model o3] \                 # 省略時: codex CLI のデフォルトモデル
  [--agent Kai-codex] \          # 省略時: Kai-codex
  [--skip-pull]                  # task が既に in_progress の場合の再実行時に指定
```

### モデル選択の目安

| モデル | 用途 |
|---|---|
| デフォルト（codex CLI が決定） | 通常 review・速度重視 |
| `o3` | 重要 mission・クリティカルなバグ疑いのある大きな diff |

---

## Kai-codex の review フロー

`kai-review.sh` の内部フロー（Phase 2 で 1 と 8 が追加された）：

```
1. registry/heartbeats/Kai-codex を touch  (dispatcher.publish_agents 用)
2. plan.sh pull --task <id> --agent Kai-codex --skills codex-review
   → task.status: pending → in_progress
   → task.worker: null → Kai-codex
   → queue/assignments/Kai-codex 生成
   → Taskvia PATCH (in_progress) 発火
3. gh pr view <PR#> --json headRefName で head branch 取得
4. refs/pull/<PR#>/head を fetch し、専用の使い捨て worktree を --detach で作成
   (主 working tree の HEAD は動かさない)
5. origin/main との diff を自前取得し、stdin で
   codex exec --output-schema config/kai-review-findings.schema.json -m <model>
   -o <mktemp output file> "<review prompt>" を実行 (t006 以降。旧 `review`
   サブコマンドは使わない)
6. 出力ファイル (JSON) の findings を読み込み
7. 判定:
   ├─ findings なし / all low  → plan.sh done <task_id> "LGTM: ..."
   ├─ 修正必要                 → plan.sh needs-director <task_id> "NEEDS FIX: <findings>"
   └─ branch mismatch 検出     → plan.sh needs-director <task_id> "BRANCH MISMATCH: <details>"
8. plan.sh done が自動で:
   → Taskvia PATCH (done) 発火
   → registry/workers.yaml の Kai-codex.task_count を bump (lib_registry.bump_task_count)
   → queue/assignments/Kai-codex を削除 (AGENT_NAME env 経由)
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

## Hook 不足の制約と補完（Phase 2）

Codex CLI は Claude Code の `pre-tool-use.sh` / `post-tool-use.sh` hook に相当する仕組みを**持たない**。Phase 2 では以下のように補完している:

| 機能 | Claude Worker | Kai-codex (Phase 2) |
|---|---|---|
| Taskvia approval リクエスト | あり（自動 hook） | **なし** — Codex CLI に該当機構が無い（Phase 3 検討） |
| Taskvia カンバンへの task 表示 | あり（hook 経由） | **あり** — `plan.sh pull` が taskvia_sync_pull を発火 |
| Taskvia agent 表示 | あり（heartbeat） | **あり** — kai-review.sh が heartbeat を touch |
| knowledge ログ投稿 | あり（hook 経由） | **なし** — findings は plan.sh 経由で task ファイル Result に残る |
| registry task_count 更新 | あり（plan.sh done が自動） | **あり** — Kai-codex が registry 登録済みなので同経路で bump |
| heartbeat 送信 | あり（watchdog 連携） | **限定的** — 起動時 touch のみ（review 中の更新はなし）|

**運用上の対処**:
- Director は Taskvia の done カウントで結果を確認する（Taskvia カンバン + registry.workers.yaml）
- review findings は `/tmp/kai-review-output.txt` と task ファイルの Result セクションに記録される
- Kai-codex がハングした場合は Director が `plan.sh update --reset` + `rm queue/assignments/Kai-codex` で復旧する

---

## Phase 3 以降の拡張予定

| 拡張項目 | 概要 |
|---|---|
| **bash / code skill の Codex 化** | `start.sh --engine codex` で bash / code Worker を Codex に |
| **docs / qa skill の Codex 化** | docs 作成・QA テストを Codex で実行 |
| **worker-names.yaml への Codex 系登録** | engine 情報（codex / claude）を pool に含める設計 |
| **approval / knowledge 相当機能の実装** | kai-review.sh 内で Taskvia の /api/request と /api/knowledge を直接呼ぶ |
| **2 人体制の自動突合** | LGTM 一致時に Director 介入なしで自動 merge 承認 |
| **heartbeat 継続更新** | 長時間 review のタイムアウト管理 |

---

## 参考

- `scripts/kai-review.sh` — Kai 起動スクリプト（t002 で実装）
- `knowledge/codex-reviewer.md` — Codex reviewer 全般の知見・手順
- `codex:codex-cli-runtime` skill — Codex CLI の詳細 syntax
- `codex:gpt-5-4-prompting` skill — Codex prompt 設計指針
