# plan.sh TARGET_DIR 呼び出し問題 — 分析 + 修正実装スケッチ

作成日: 2026-08-28  
Mission: `20260828-plan-sh-target-dir` / Task: `t002`  
Author: Wei (Worker)

---

## 1. 問題の概要

Worker が `TARGET_DIR` を指定して起動された場合（例: `TARGET_DIR=~/workspace/nasne-epg`）、
`worker.md` や kickoff message 内の相対パス `./scripts/plan.sh` を実行すると、
現在の cwd が crewvia リポジトリ外であるため **ファイルが存在せず失敗する**。

### 失敗パターン（Seo 事故の再現手順）

```
1. start.sh が TARGET_DIR=~/workspace/nasne-epg で Worker (Seo) を起動
2. Kickoff message:
     "ミッション開始。./scripts/plan.sh pull --agent Seo --skills review,bash --target-dir ~/workspace/nasne-epg でタスクを取得し..."
3. Seo の cwd = ~/workspace/nasne-epg
4. ./scripts/plan.sh を実行 → nasne-epg/scripts/plan.sh は存在しない → エラー
5. Seo が報告: "plan.sh スクリプトが存在しないため ./scripts/plan.sh done は実行できません"
6. Director が代行 done → 手動作業が発生
```

### 影響を受けるケース

| ケース | 影響 |
|--------|------|
| TARGET_DIR 指定の Worker | **常に失敗**（scripts/plan.sh が TARGET_DIR 下に存在しない） |
| worktree 内 cwd の Worker | plan.sh は存在するが queue/ が gitignore で空 → plan.sh 自体は動くが queue 読み取りが別問題 |
| 非 TARGET_DIR Worker（cwd=crewvia root） | 影響なし（現状動作している） |

---

## 2. 調査結果

### 2-1. kickoff message の実装場所（scripts/start.sh）

| 行 | 対象 | 現状のコマンド |
|---|---|---|
| **453** | Worker (TARGET_DIR あり) | `./scripts/plan.sh pull ...` × 2箇所 |
| **455** | Worker (TARGET_DIR なし) | `./scripts/plan.sh pull ...` + `./scripts/plan.sh done ...` |
| **458** | Director | `./scripts/plan.sh status` |

```bash
# start.sh line 453 (TARGET_DIR あり)
KICKOFF_MSG="ミッション開始。./scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} \
  --target-dir ${TARGET_DIR} でタスクを取得し、指示に従って作業してください。\
  完了したら ./scripts/plan.sh done で報告し、待機してください..."

# start.sh line 455 (TARGET_DIR なし)
KICKOFF_MSG="ミッション開始。./scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} \
  でタスクを取得し...完了したら ./scripts/plan.sh done で報告し、待機してください..."

# start.sh line 458 (Director)
KICKOFF_MSG="ミッション開始。./scripts/plan.sh status で状態を確認し..."
```

### 2-2. agents/worker.md 内の `./scripts/plan.sh` 出現箇所（計 7箇所）

| 行 | 内容 |
|----|------|
| 52 | `./scripts/plan.sh status` |
| 64 | `./scripts/plan.sh status --mission <slug>` |
| 189 | `TASK_JSON=$(./scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME" \` |
| 194 | `TASK_JSON=$(./scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME")` |
| 511 | `./scripts/plan.sh done "$TASK_ID" "..." --mission "$TASK_MISSION"` |
| 538 | `./scripts/plan.sh status --mission "$TASK_MISSION"` |
| 611 | `./scripts/plan.sh fail "$TASK_ID" "$HANDOFF_PATH" --mission "$TASK_MISSION"` |

### 2-3. hooks/ 内の plan.sh 参照（Option C 副作用調査）

```bash
grep -rn "plan\.sh" hooks/
# → 出力なし
```

**hooks/ は plan.sh を参照していない。修正不要。**

### 2-4. crewvia/scripts/ 内の全 .sh（Option C 名前衝突リスク評価）

| ファイル | 一般的な名前か？ | 衝突リスク |
|---------|--------------|-----------|
| `plan.sh` | ★ 高い | 他プロジェクトに同名があると上書き |
| `start.sh` | ★ 高い | 多くのプロジェクトが start.sh を持つ |
| `dispatcher.sh` | 中程度 | |
| `watchdog.sh` | 中程度 | |
| `git-helpers.sh` | 中程度 | |
| `assign-name.sh` | 低い | crewvia 固有 |
| `benchmark-ctx.sh` | 低い | crewvia 固有 |
| `log_to_obsidian.sh` | 低い | crewvia 固有 |
| `verifier-dispatcher.sh` | 低い | crewvia 固有 |
| `verify-task.sh` | 低い | crewvia 固有 |
| `test_*.sh` (7ファイル) | 低い | crewvia 固有 |
| その他 | 低〜中 | |

**Option C (`PATH` 追加) は `start.sh` / `plan.sh` 等の一般的な名前が Worker プロジェクトの  
同名スクリプトを隠蔽するリスクがあり、採用不推奨（Priya 判定と一致）。**

---

## 3. 確定オプション: Option A+B ハイブリッド（Priya 判定）

| オプション | 対象 | 変更量 | リスク |
|-----------|------|--------|--------|
| **Option A** | `scripts/start.sh` kickoff message | 小（3行修正） | 低 |
| **Option B** | `agents/worker.md` の `./scripts/plan.sh` 全7箇所 | 小（sed 1回） | 低 |
| ~~Option C~~ | PATH 追加 | 小 | **名前衝突リスク大（不採用）** |
| ~~Option D~~ | TARGET_DIR に shim 作成 | 大 | **維持コスト大（不採用）** |

---

## 4. 実装スケッチ（diff 想定）

### 4-1. scripts/start.sh の変更（Option A）

```diff
--- a/scripts/start.sh
+++ b/scripts/start.sh
@@ -450,12 +450,12 @@ if [[ -n "${TARGET_DIR:-}" ]]; then
       if [[ -n "${TARGET_DIR:-}" ]]; then
-        KICKOFF_MSG="ミッション開始。./scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} --target-dir ${TARGET_DIR} でタスクを取得し、指示に従って作業してください。完了したら ./scripts/plan.sh done で報告し、待機してください（Dispatcher が次のタスクを自動割り当てします）。"
+        KICKOFF_MSG="ミッション開始。${CREWVIA_REPO_ROOT}/scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} --target-dir ${TARGET_DIR} でタスクを取得し、指示に従って作業してください。完了したら ${CREWVIA_REPO_ROOT}/scripts/plan.sh done で報告し、待機してください（Dispatcher が次のタスクを自動割り当てします）。"
       else
-        KICKOFF_MSG="ミッション開始。./scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} でタスクを取得し、指示に従って作業してください。JSON に worktree_path が含まれる場合はそのディレクトリに cd し、.crewvia-env を source してから作業してください（例: cd <worktree_path> && source .crewvia-env）。完了したら ./scripts/plan.sh done で報告し、待機してください（Dispatcher が次のタスクを自動割り当てします）。"
+        KICKOFF_MSG="ミッション開始。${CREWVIA_REPO_ROOT}/scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS} でタスクを取得し、指示に従って作業してください。JSON に worktree_path が含まれる場合はそのディレクトリに cd し、.crewvia-env を source してから作業してください（例: cd <worktree_path> && source .crewvia-env）。完了したら ${CREWVIA_REPO_ROOT}/scripts/plan.sh done で報告し、待機してください（Dispatcher が次のタスクを自動割り当てします）。"
       fi
     else
-      KICKOFF_MSG="ミッション開始。./scripts/plan.sh status で状態を確認し、タスク分解・Worker 割り当て・全体管理を開始してください。"
+      KICKOFF_MSG="ミッション開始。${CREWVIA_REPO_ROOT}/scripts/plan.sh status で状態を確認し、タスク分解・Worker 割り当て・全体管理を開始してください。"
     fi
```

**前提**: `CREWVIA_REPO_ROOT` は start.sh の冒頭で `REPO_ROOT` として定義されており、
`CREWVIA_REPO_ROOT` としてエクスポートされている。kickoff message 内で使用可能。

```bash
# start.sh 内の CREWVIA_REPO_ROOT の設定を確認
# (ENV_EXPORTS に含まれており、Worker session に export される)
```

### 4-2. agents/worker.md の変更（Option B）

```diff
--- a/agents/worker.md
+++ b/agents/worker.md
@@ -50,10 +50,10 @@
-   ./scripts/plan.sh status
+   ${CREWVIA_REPO_ROOT}/scripts/plan.sh status

-   特定 mission の詳細を見たいときは `./scripts/plan.sh status --mission <slug>` を使う。
+   特定 mission の詳細を見たいときは `${CREWVIA_REPO_ROOT}/scripts/plan.sh status --mission <slug>` を使う。

@@ -187,8 +187,8 @@
 # Dispatcher からの assign 通知ありの場合
-TASK_JSON=$(./scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME" \
+TASK_JSON=$(${CREWVIA_REPO_ROOT}/scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME" \
   --task "$ASSIGNED_TASK_ID" --mission "$ASSIGNED_MISSION")
 PULL_RC=$?

 # 起動直後や --task なしの場合（スキルマッチで自動選択）
-TASK_JSON=$(./scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME")
+TASK_JSON=$(${CREWVIA_REPO_ROOT}/scripts/plan.sh pull --skills "$SKILLS" --agent "$AGENT_NAME")
 PULL_RC=$?

@@ -509,7 +509,7 @@
-./scripts/plan.sh done "$TASK_ID" "実行した内容と結果の要約" --mission "$TASK_MISSION"
+${CREWVIA_REPO_ROOT}/scripts/plan.sh done "$TASK_ID" "実行した内容と結果の要約" --mission "$TASK_MISSION"

@@ -536,7 +536,7 @@
-./scripts/plan.sh status --mission "$TASK_MISSION"
+${CREWVIA_REPO_ROOT}/scripts/plan.sh status --mission "$TASK_MISSION"

@@ -609,7 +609,7 @@
-./scripts/plan.sh fail "$TASK_ID" "$HANDOFF_PATH" --mission "$TASK_MISSION"
+${CREWVIA_REPO_ROOT}/scripts/plan.sh fail "$TASK_ID" "$HANDOFF_PATH" --mission "$TASK_MISSION"
```

---

## 5. Risk & Mitigation

| リスク | 影響 | 対策 |
|--------|------|------|
| `CREWVIA_REPO_ROOT` が未設定 | Worker が `$CREWVIA_REPO_ROOT/scripts/plan.sh` を呼ぼうとして `/scripts/plan.sh` になる | start.sh で必ず export していることを確認。未設定時は plan.sh 側で早期 exit + エラー出力 |
| 既存 Worker への影響（cwd=crewvia の場合） | `${CREWVIA_REPO_ROOT}/scripts/plan.sh` は絶対パスなので影響なし（動作継続） | - |
| worktree 内 Worker | `CREWVIA_REPO_ROOT` が crewvia root を指すため正しく動作する | `.crewvia-env` に `CREWVIA_REPO_ROOT` が含まれることを確認 |
| LLM が `./scripts/plan.sh` を「知識から」呼ぶ | worker.md の更新（Option B）によりモデルが新パターンを学習 | 移行期間中は kickoff message が絶対パスを指定（Option A が safety net） |
| hooks/ の plan.sh 参照 | **なし**（調査で確認済み） | 修正不要 |

### Option A+B ハイブリッドの相乗効果

```
Option A: kickoff message の絶対パス化
    → Worker が最初に受け取るメッセージが絶対パス → LLM が正しいパスを確認できる

Option B: worker.md の絶対パス統一  
    → LLM が参照する文書全体で一貫性を保つ → 推論エラーを減らす

両者を合わせることで、kickoff message と worker.md の2箇所から正しいパスが伝わる。
どちらか一方が LLM に届かなくても、もう一方が safety net になる。
```

---

## 6. 実装タスク割り当て案（t003 向け）

Priya 判定「t003 で worker.md の相対パス 7箇所（lines 52, 64, 189, 194, 511, 538, 611）を修正対象とすること」に対応する実装内容:

| Task | 対象ファイル | 変更内容 | テスト |
|------|------------|---------|--------|
| t003 (A) | `scripts/start.sh` | line 453, 455, 458 の `./scripts/plan.sh` を `${CREWVIA_REPO_ROOT}/scripts/plan.sh` に変更 | bash -n による構文確認 |
| t003 (B) | `agents/worker.md` | 7箇所の `./scripts/plan.sh` を `${CREWVIA_REPO_ROOT}/scripts/plan.sh` に変更 | diff で置換数を確認 |
| t003 (verify) | 両ファイル | `./scripts/plan.sh` が残存していないことを grep で確認 | `grep -n '\./scripts/plan\.sh' scripts/start.sh agents/worker.md` → 0件 |

---

## 7. PR タイトル案

```
docs: plan.sh の TARGET_DIR 呼び出し問題の分析 + 修正実装スケッチ
```

PR description に含める内容:
- Seo の事故事例（mission 20260827-epgget-decrypt-phase-a t008）
- Option A+B ハイブリッドの選定理由（Option C 名前衝突リスク）
- 関連: `crewvia-worktree-repo-root-pitfall` memory
