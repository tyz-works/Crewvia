---
name: crewvia-qa
description: "Use when executing a QA task in the crewvia workflow — verifying that an implementation works correctly before merge. Invoke when assigned a task with skills containing \"qa\", when asked to test/verify a PR or implementation, or when checking that code meets requirements. Works for any project type: web apps, CLI tools, scripts, libraries, APIs, infrastructure code, etc."
---

# Crewvia QA スキル

QA Worker として実装を検証する。**実装者とは別の Worker が担当すること。**

---

## Step 1: 何をテストするか把握する

```bash
# PR・タスク内容を確認
gh pr view <PR番号> --json title,body,files 2>/dev/null || true
DIFF_REF="$(plan pr-base --diff-ref)" || { echo "diff の base を決められない。Director に報告して待つ" >&2; exit 1; }
case "$DIFF_REF" in
  origin/*)   # crewvia 本体の task。pull の fetch は base_branch しか取らないので、PR base をここで取る
    # 明示の src:dst で取る。素の `git fetch origin <branch>` は refspec が絞られた clone (--single-branch 等) では
    # FETCH_HEAD しか更新せず、古い origin/<branch> を読んでしまう
    git fetch origin "+refs/heads/${DIFF_REF#origin/}:refs/remotes/${DIFF_REF}" \
      && git rev-parse --verify --quiet "${DIFF_REF}^{commit}" >/dev/null \
      || { echo "${DIFF_REF} を取れない。Director に報告して待つ" >&2; exit 1; } ;;
esac          # それ以外 (TARGET_DIR の task は local の main) は何もしない
git diff "${DIFF_REF}...HEAD" --name-only
```

diff の base は `main` と決め打たない。`plan pr-base --diff-ref` が自分の QA task の mission の policy から答える
（crewvia 本体の task は `origin/<pr_base>`、TARGET_DIR の task は local の `main`）。取れなければ diff を取らず、
Director に報告して待つ（`main` に倒さない）。

確認すべき観点：
- **何が変わったか** — 新機能 / バグ修正 / リファクタリング / インフラ変更
- **影響範囲** — どのモジュール・エンドポイント・スクリプトが変わったか
- **要件との整合** — タスク description や CLAUDE.md に書かれた仕様を満たしているか
- **プロジェクト種別** — Web アプリ / CLI / ライブラリ / スクリプト / IaC 等

プロジェクト種別によってテスト手法が変わる（後述）。

---

## Step 2: テスト方針を決める

### プロジェクト種別ごとのテスト手法

| 種別 | 主な手法 |
|------|----------|
| **Web アプリ（Next.js 等）** | dev server 起動 → Playwright ブラウザテスト → API テスト |
| **REST API / サーバー** | サーバー起動 → curl / fetch でエンドポイントテスト |
| **CLI ツール / スクリプト** | コマンド実行 → 出力・終了コード・副作用を検証 |
| **ライブラリ / モジュール** | テストスイート実行（jest / pytest / go test 等） |
| **インフラ (IaC / Shell)** | dry-run / plan で差分確認、ステージング環境で動作確認 |
| **データパイプライン** | サンプルデータで実行 → 出力データを検証 |

### テスト観点（共通）

いずれの種別でも以下をカバーすること：

- **正常系（ハッピーパス）** — 主要なユースケースが期待通り動く
- **異常系** — 不正な入力 / 存在しないリソース / 権限エラー等が適切にハンドルされる
- **リグレッション** — 変更していないはずの機能が壊れていない
- **エッジケース** — 空文字・ゼロ・境界値・大量データ等（影響範囲に応じて判断）

---

## Step 3: 環境セットアップ

```bash
# 依存パッケージが揃っているか確認
# Node.js
npm install 2>/dev/null || true
# Python
pip install -r requirements.txt 2>/dev/null || true
# Go / Rust 等はビルドが通ることを確認
```

必要な環境変数・設定ファイルが揃っているか確認する（`.env.example` などを参照。`.env` 本体は読まない）。

---

## Step 4: テストを実行する

### Web アプリ

```bash
npm run dev > /tmp/qa-dev.log 2>&1 &
DEV_PID=$!
# 起動確認
for i in $(seq 15); do grep -q "Ready\|started\|running" /tmp/qa-dev.log && break; sleep 1; done
```

Playwright MCP でブラウザテスト：
```
browser_navigate → browser_snapshot → 操作 → browser_snapshot → 結果確認
```

### CLI / スクリプト

```bash
# 正常系
./script.sh --option value
echo "Exit: $?"

# 異常系
./script.sh --invalid-option 2>&1
echo "Exit: $?"
```

### ライブラリ / テストスイート

```bash
npm test          # Node.js
pytest            # Python
go test ./...     # Go
cargo test        # Rust
```

### インフラ (Terraform 等)

```bash
# dry-run / plan のみ。apply は要確認
terraform plan
```

---

## Step 5: テストデータが必要な場合

実際のデータがないと動作確認できない機能（DBレコード、承認カード、ファイル等）は、テスト用データを投入してからテストする。

投入方法はプロジェクトによって異なる：
- API エンドポイント経由（認証が必要なら token を env から読む。`.env` は直接読まない）
- シードスクリプト / フィクスチャ
- サンプルファイルの配置

テスト終了後はテストデータを削除するか、そのまま残してよいか判断する。

### 観察用の mission（本番の dispatcher を見る QA）

plan.sh / dispatcher の挙動を本番で観察するために使い捨ての mission を作るときは、**必ず `--inactive` を付ける**:

```bash
./scripts/plan.sh init "probe" --mission <probe-slug> --inactive
./scripts/plan.sh add "..." --mission <probe-slug> --skills code --deliverable none
```

`--inactive` なしの `init` は `active_missions` に足し `default_mission` も書き換える。dispatcher が probe の task を
本物として配り（Director に Worker 起動要求が漏れる）、default_mission が probe のまま残る（2026-09-28/29 の
t037 / t041）。`--inactive` の mission は `state.yaml` を 1 バイトも変えず、dispatcher に見えない。以降の
`add` / `update` / `lint` / `done` / `pull --task` は `--mission <probe-slug>` を付けて呼ぶ。
dispatcher が実際にその task を配るか観察したい QA だけは、`--inactive` なしで作り、終わったら
`state.yaml` の `active_missions` / `default_mission` を元に戻すこと（`knowledge/plan-input-guards.md`）。

---

## Step 6: リグレッション確認

```bash
# 変更されたファイルから影響範囲を推定（DIFF_REF は Step 1 で取ったもの。別の shell なら Step 1 の手順をやり直す）
git diff "${DIFF_REF:?Step 1 の手順で DIFF_REF を取り直す}...HEAD" --name-only
```

変更に関係しない主要な機能を 1〜2 個ピックアップしてスモークテストする。

---

## Step 7: クリーンアップ

```bash
# dev server / バックグラウンドプロセスを停止
kill $DEV_PID 2>/dev/null || true
```

---

## Step 8: 結果レポート

`plan.sh done` に渡す結果サマリーのフォーマット（`--result-file <path>` か `--result-file -` + クォート付きヒアドキュメントで渡す。二重引用符の位置引数はバッククォート / `$(...)` がシェルに実行される）：

**今の試行を名乗る**: `plan.sh done` / `plan.sh needs-director` には、pull の JSON の `execution_id` を `--execution` で渡す（`${EXECUTION_ID:+--execution "$EXECUTION_ID"}` の形。`agents/worker.md`「完了・失敗・差し戻しの報告には、この `execution_id` を名乗る」）。Bash は呼び出しごとに env が消えるので、報告のたびに同じ呼び出しの中で ID を取り直す（`source .crewvia-env` するか pull の JSON の値を書く）。名乗りなしだと plan.sh が stderr に警告を出し、将来は拒否される。違う試行・取り直された後の古い試行からの報告は exit 3 で拒否され、何も書かれない（打ち直さず Director に報告する）。

```
QA結果: [対象: <タスクタイトル>]

正常系:
  ✅ <機能A>: <確認内容>
  ✅ <機能B>: <確認内容>

異常系:
  ✅ <ケース>: 期待通りのエラーが返る
  ⚠️ <ケース>: テストデータ不足のため未実施

リグレッション:
  ✅ <機能>: 正常

問題: なし / あり → <内容>
```

問題が見つかった場合は実装担当 Worker に差し戻し、Orchestrator に報告する。

---

## Step 8a: QA Gate セクションの記述（`qa_checkpoints` 宣言タスクの場合）

タスクの frontmatter に `qa_checkpoints` が宣言されている場合、`plan.sh done` の result に
`## QA Gate` セクションを含めることが **必須**。欠落すると `plan.sh done` が FAIL を返す。

### 記述形式

```
## QA Gate

checkpoint: <チェックポイント名> | required: yes | result: observed
checkpoint: <チェックポイント名> | required: yes | result: not_run | note: <理由>
checkpoint: <チェックポイント名> | required: no  | result: not_run | note: <理由>
checkpoint: <チェックポイント名> | required: yes | result: failed   | note: <詳細>
```

### result 値の意味

| result | 意味 |
|--------|------|
| `observed` | 検証実施・確認済み |
| `not_run` | 実施しなかった（理由を note に記載） |
| `failed` | 検証実施したが FAIL（詳細を note に記載） |

### PASS/FAIL ルール

- `required: yes` かつ `result: observed` → PASS に寄与
- `required: yes` かつ `result: not_run` または `failed` → **FAIL（done 拒否）**
- `required: no` → PASS/FAIL に影響しない（証跡として記録のみ）

### 詰まったとき

チェックポイントを完遂できない場合は `plan.sh done` を呼ばず、
`plan.sh needs-director` で Director に差し戻すこと:

```bash
${CREWVIA_REPO_ROOT}/scripts/plan.sh needs-director "$TASK_ID" --mission "$TASK_MISSION" ${EXECUTION_ID:+--execution "$EXECUTION_ID"} --result-file - <<'RESULT_EOF'
詰まった理由
RESULT_EOF
```

これは「代替検証して done を無理やり呼ぶ」よりも **常に正しく、安い選択肢** である。
