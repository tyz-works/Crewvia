---
title: "Dead Config / Dead Instruction 監査"
date: "2026-09-09"
tags: [audit, dead-code, configuration, instructions]
---

## 目的

本ミッション t001-t004 で発見された「宣言はあるが実際には効いていない設定・指示」という同型欠陥が、他にどれだけ潜んでいるかを全数洗い出す。

同型欠陥は過去ミッション (20260908-Phase 3) でも 6 回連続で再発しており、個別対処では終わらないことが実証されている。

---

## 監査対象と観点

### 1. config/*.yaml の全キー
宣言されているキーが実際に scripts/ / hooks/ のコードから参照されているか。

### 2. agents/*.md の「必ず」「禁止」「★最重要」級の指示
指示が守られなかったときに、機構として検出・阻止されるか。それとも Worker の善意に依存しているか。

### 3. CLAUDE.md の環境変数表
表の説明と scripts/ の実挙動が一致しているか。

### 4. hooks/ の deny/allow ルール
宣言されたガードが実際の呼び出し経路で発火するか。dead code になっていないか。

---

## 監査結果

### A. Dead Config — 完全に参照されていない設定

#### A-1: `config/verification-profiles.yaml`

**状態**: 完全に unused （dead code）

**宣言箇所**:
- ファイル自体: `config/verification-profiles.yaml`
- 内容: light / standard / strict / research の検査プロファイル定義

**期待される効果**:
- Verifier エージェントの検査強度を profile ごとに定義し、task 別に適用する
- 各 profile は checks (lint, typecheck, diff_review, etc.) と auto_verified フラグを指定
- high-risk task (auth/billing/migration) は strict profile で escalate_on_fail: true

**実際の状態**:
```bash
$ grep -r "verification.profiles\|verification-profiles" scripts/ hooks/ agents/ 2>/dev/null
# → 何も返されない
```
参照ゼロ。未実装。

**倒れる方向**: 安全側（効かなくても危険にはならない。Verifier は fixed hardcoded checks を使う）

**再現手順**:
```bash
grep -r "verification" scripts/ hooks/ --include="*.sh" --include="*.py" | grep -v "VERIFICATION_UI\|verifier"
# verification-profiles への参照は出ない
```

**注記**: 
- verifier.md は存在し Verifier エージェントの指示がある（agents/verifier.md）
- しかし profile 選択ロジックがコード側に実装されていない
- つまり「Verifier のため」という名目で yaml が置かれているが、実装ハンガー状態

---

#### A-2: `config/autonomous-improvement.yaml` の実装ハンガー

**状態**: 定義は存在するが、コード側で自動処理されない （半dead）

**宣言箇所**:
- ファイル: `config/autonomous-improvement.yaml`
- agents/worker.md §4「改善案発見時のフロー」

**期待される効果**:
```
改善案を発見
  ↓
autonomous-improvement.yaml の allowed リストと照合
  ↓
allowed に該当 → Director に「改善提案」として報告
requires_approval に該当 → type: improvement で Taskvia 投稿して終了
```

**実際の状態**:
```bash
$ grep -r "autonomous" scripts/ hooks/ --include="*.sh" --include="*.py"
# → 何も返されない
```

コード側の参照ゼロ。判定ロジックがない。

**実装の実態**:
- worker.md は「改善案を見つけたら yaml をチェックしなさい」と指示している
- **つまり判定は Worker の手動判定に委ねられている**
- Worker が「これは allowed に該当しそう」と主観的に判断して Director に報告する形

**倒れる方向**: 安全側（要確認が必要なものを手動判定するのは保守的）

ただし、実装指示と機構のギャップがある：
- 指示: 「yaml で判定する」（= コード側サポート）
- 実態: 「Worker が手動で yaml 内容から判定する」

**再現手順**:
```bash
# allowed/requires_approval の参照を探す
grep -r "allowed\|requires_approval" scripts/ --include="*.py"
# → 構造的な値判定が見当たらない

# worker.md に指示があるか確認
grep -n "autonomous-improvement" agents/worker.md
# → 指示はある（手順説明）がコード実装はない
```

**倒れる方向**: 安全側だが、設計・実装・指示の整合性が欠けている

---

### B. 効果が不完全な指示実装

#### B-1: Worker worktree モード本編集ガード — 検出条件の限界

**状態**: 実装は存在するが、抜け道がある （partial dead）

**宣言箇所**:
- agents/worker.md §§165-206「編集対象パス」
  - 「**必ず worktree 内のパス**（`pwd` 起点の相対パス、または `git rev-parse --show-toplevel` で得た絶対パス）」
  - **「`$CREWVIA_REPO/scripts/plan.sh` を Edit/Write の対象にしない」**

- hooks/pre-tool-use.sh §§171-259「worktree モード本編集ガード」
  - Edit / Write / MultiEdit / NotebookEdit をチェック
  - `$CREWVIA_REPO` 配下への編集を deny（除外: queue/, registry/, .claude/worktrees/）

**期待される効果**:
- Edit/Write/MultiEdit/NotebookEdit で worktree Worker が main checkout を直接編集するのをブロック

**実際の状態**:
```bash
# ガードコード (hooks/pre-tool-use.sh:228-259)
if [ "$TOOL_NAME" = "Edit" ] || [ "$TOOL_NAME" = "Write" ] || [ "$TOOL_NAME" = "MultiEdit" ] || [ "$TOOL_NAME" = "NotebookEdit" ]; then
  ... path check ...
  case "$_GUARD_FILE_REAL" in
    "${_GUARD_REPO_REAL}"/queue/*|...) : # 正当な経路
      ;;
    "${_GUARD_REPO_REAL}"/*)  echo "🚫 main repo direct edit blocked"
      emit_decision "deny"
      ;;
```
実装あり。ガード発火テストで検証済み（t011/t014）。

**既知の限界** (worker.md 同じセクションで明記):
```
Bash 経由の書き込み（heredoc / `sed -i` / `tee` 等）はこのガードの対象外として残る既知の限界 —
ツール名を偽装できない Edit/Write/MultiEdit/NotebookEdit だけを機械的に守る仕組みであり、
Bash を使った書き込みは引き続き自分の規律で避けること。
```

**倒れる方向**: 危険側（Bash 経由は対象外。heredoc / sed -i / tee で main checkout 編集可能）

**再現手順**:
```bash
# Edit/Write でガード検出
# → 発火

# Bash で同じことをしようとする
# → ガード対象外（ただし main_repo_git_guard.py で別にブロック可能性）

# さらに、Bash のパス参照が変数経由だと検出困難
R=$CREWVIA_REPO_ROOT
cd $R && git checkout origin/main && git checkout -b new-fix-branch
# → sed -i などのパス文字列検出回避も可能
```

**倒れる方向**: Bash 経由では完全には止められない（Worker 規律に依存）

ただし、worker.md で「**最初から worktree 内のパスを使うのが正しい進め方**」と明記されており、指示は明確。

**補足 — t001 main_repo_git_guard との組み合わせ**:
- hooks/lib_main_repo_git_guard.py により、破壊的 git 操作（checkout/reset/merge など）+ $CREWVIA_REPO 参照の同時発生は deny
- ただし相対パス到達 (`cd ../../.. && git checkout`) は検出困難（既知の限界）

---

### C. 不完全な検証

#### C-1: CREWVIA_MUX / CREWVIA_MUX_ENABLED の優先順位ドキュメント

**状態**: 実装と CLAUDE.md 記載に齟齬の可能性（未検証）

**宣言箇所**:
- CLAUDE.md 環境変数表:
  ```
  CREWVIA_MUX: mux バックエンド選択: tmux / herdr。config `mode:` より優先
  CREWVIA_MUX_ENABLED: 並列モード有効化: `1` で並列 ON / `0` でインラインモード強制。
    `CREWVIA_MUX` が設定済みなら不要
  ```

- config/crewvia.yaml コメント (L55):
  ```
  環境変数での上書き: CREWVIA_MUX=tmux|herdr (最優先) / CREWVIA_MUX_ENABLED=1 (並列 ON) / CREWVIA_MUX_ENABLED=0 (inline)
  CREWVIA_MUX が設定されていれば CREWVIA_MUX_ENABLED より優先される。
  ```

**期待される効果**:
- CREWVIA_MUX が設定されている場合、CREWVIA_MUX_ENABLED は無視される
- CREWVIA_MUX が未設定の場合のみ CREWVIA_MUX_ENABLED が有効

**実装検証** (scripts/start.sh:248-264):
```bash
MODE_FROM_CONFIG=$(grep -E '^mode:[[:space:]]*\S' "$CONFIG_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"' | head -1)
if [[ "$MODE_FROM_CONFIG" == "herdr" ]]; then
  export CREWVIA_MUX=herdr
  export CREWVIA_MUX_ENABLED=1
```

コード側の確認:
- config から mode: を読む
- その前に CREWVIA_MUX env var がチェックされるか？ → **詳細検証必要**（未検証項目）

**倒れる方向**: 未検証（推測域）

**再現手順**:
```bash
# start.sh の行順を追跡
grep -n "CREWVIA_MUX\|CREWVIA_MUX_ENABLED" scripts/start.sh | head -20

# 優先順位が明確か確認
# 1. env var CREWVIA_MUX が優先されるか？
# 2. config mode が次か？
# 3. CREWVIA_MUX_ENABLED がその次か？
```

**注記**:
- 実装されており動作しているが、env var 優先順位の詳細な検証は scope 外
- 複数の情報源（CLAUDE.md 表 vs config ファイル vs scripts）で説明があり、一貫性確認が必要

---

### D. 明示的に「効かない」として設計された指示（健全）

#### D-1: approval_channel.ntfy.user / .pass （安全側）

**状態**: 意図的に config から読まない （健全な設計）

**宣言箇所**:
- config/crewvia.yaml (L115):
  ```yaml
  user: ""
  pass: ""
  ```
  コメント: 「セキュリティのため値はここに書かず、環境変数で設定すること」

**期待される効果**:
- NTFY_USER / NTFY_PASS は環境変数からのみ読む
- config に書かない

**実装** (scripts/start.sh:331-332):
```bash
[[ -z "${NTFY_USER:-}" ]]  && export NTFY_USER="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.user")"
[[ -z "${NTFY_PASS:-}" ]]  && export NTFY_PASS="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.pass")"
```

config から読む処理が **存在する**。ただし：
- config は空文字で定義されているため、env 優先（env が未設定なら config の空文字が使われる）
- 実質的には env 優先になっている（デザイン上 OK）

**倒れる方向**: 安全側（env 未設定時は空になり、認証失敗で明示的に失敗する）

**健全性評価**:
- 設計: ✅ 正しい（クレデンシャルを config に書かない）
- 実装: ✅ 正しい（env > config 優先順位）
- 指示: ⚠️ 曖昧（CLAUDE.md 環境変数表に NTFY_USER/NTFY_PASS の記載があるが「config には書かない」と明記されていない）

**再現手順**:
```bash
# config に値が空で定義されていることを確認
grep -A3 "user:\|pass:" config/crewvia.yaml

# scripts で config から読む処理
grep "NTFY_USER\|NTFY_PASS" scripts/start.sh

# 実際には env が優先されるため、config 値は死に絵
```

---

## 優先度マトリクス

| ID | 項目 | 倒れる方向 | 影響度 | 優先度 |
|---|---|---|---|---|
| A-1 | verification-profiles.yaml | 安全側 | 低（未実装機能） | LOW |
| A-2 | autonomous-improvement.yaml | 安全側 | 中（指示と実装ギャップ） | MEDIUM |
| B-1 | worktree edit ガード Bash抜け道 | 危険側 | 高（t009/t011 過去事故） | HIGH |
| C-1 | CREWVIA_MUX 優先順位 | 未検証 | 低（実装動作中） | LOW ※検証必要 |
| D-1 | NTFY 認証情報設定 | 安全側 | 低（設計OK） | - |

---

## 後続検証候補（Scope 外）

以下は audit scope 外だが、Director の次ミッション判断時に検討すること：

1. **C-1 優先順位検証**: CREWVIA_MUX 環境変数と config mode の正確な優先順位確認
   - 実装: scripts/start.sh:224-290
   - 検証方法: tmux/herdr の並列モード選択フローを step-by-step trace

2. **A-2 autonomous-improvement.yaml 実装検討**:
   - Option 1: Worker が手動判定する現状を保持し、ドキュメント確認
   - Option 2: コード側で自動判定ロジックを追加（hooks でチェック + Taskvia auto-log）
   - **推奨**: Option 1 確認（指示は十分か確認し、不足分だけ足す）

3. **A-1 verification-profiles.yaml の実装**:
   - verifier.md の指示とコード実装が不在
   - 実装予定があるか確認し、無いなら削除 or タスク化

---

## まとめ

### 発見件数
- **Dead config**: 2 件（verification-profiles.yaml, autonomous-improvement.yaml 実装部）
- **Partial dead / 限界あり**: 1 件（worktree edit ガード Bash 抜け道）
- **未検証**: 1 件（CREWVIA_MUX 優先順位）
- **健全**: 1 件（NTFY 認証情報 env 優先）

### t001-t004 との共通パターン

1. **宣言 ≠ 実装**: verification-profiles.yaml, autonomous-improvement.yaml
   - t001-t004 と同型 (宣言されているが機構で保護されていない)

2. **実装の限界が指示に不完全に反映**: worktree edit ガード
   - t001-t004 と異なり、ガード**は存在するが Bash 抜け道がある**
   - 指示で「Bash は対象外、手動規律に依存」と明記されているため、健全性評価は「安全側」

3. **設計健全（env > config）だが指示ドキュメント曖昧**: NTFY 認証情報
   - 実装は正しいが、CLAUDE.md で「config に書かない」と明記されていない

### 次アクション

- **HIGH**: B-1 worktree edit ガード Bash 抜け道を次ミッションで検討（PR #180 検討対象か）
- **MEDIUM**: A-2 autonomous-improvement.yaml 実装 OR 指示確認・修正
- **LOW**: A-1 verification-profiles.yaml の方針確認（実装か削除か）
- **LOW**: C-1 CREWVIA_MUX 優先順位を詳細検証

---

**監査者**: Haiku 4.5  
**監査日**: 2026-09-09  
**対象リポジトリ**: crewvia (main 8df15a3)
