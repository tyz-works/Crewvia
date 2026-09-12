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

**【訂正・追記】** 上記4観点には `.github/workflows/`（CI 設定）が含まれていなかった。そのため「テストファイルが存在する」ことだけを確認し、CI で実際に実行されているかを検証していなかった。この scope 漏れにより tests/*.py (pytest 8本) が CI で一度も実行されていない事実を見落としていた（詳細: [E. Scope からの漏れ](#e-scope-からの漏れ---ciがテストを一度も実行していなかった)）。

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

### C. 実欠陥（Worker 経路は修正済み、Director 経路は宣言と実装の食い違いが未解決）

#### C-1: CREWVIA_MUX / CREWVIA_MUX_ENABLED の優先順位 — Worker 経路の実欠陥2箇所は修正済み。Director 経路は別の食い違いが未解決 (open)

**状態（再訂正、t014）**: 本監査 (2026-09-09) 時点では「未検証・危険にはならない・LOW」と結論していたが誤りだった。t012 (mission 20260912-verdict-ci-launcher) の詳細検証で **Worker が並列モードに入る経路**（start.sh のゲート判定 + `./crewvia` ランチャの export）に危険側の実欠陥が2箇所見つかり、いずれも本ミッション内の別 task で修正済み（下記 実欠陥1・2）。ただし t012 は「両方とも修正済み」と C-1 全体を解消扱いにしていたが、これは Worker 経路のみを指す表現で不正確だった。**Director 経路**（`scripts/start.sh` の `ROLE=director` ブロック）には別の食い違いが残っており、こちらはコード修正が未実施の open 残件（下記 実欠陥3。本 task = t014 で発見・訂正）。

**宣言箇所**（監査時点から変更なし）:
- CLAUDE.md 環境変数表:
  ```
  CREWVIA_MUX: mux バックエンド選択: tmux / herdr。config `mode:` より優先
  CREWVIA_MUX_ENABLED: 並列モード有効化: `1` で並列 ON / `0` でインラインモード強制。
    `CREWVIA_MUX` が設定済みなら不要
  ```

- config/crewvia.yaml コメント (L55-56):
  ```
  環境変数での上書き: CREWVIA_MUX=tmux|herdr (最優先) / CREWVIA_MUX_ENABLED=1 (並列 ON) / CREWVIA_MUX_ENABLED=0 (inline)
  CREWVIA_MUX が設定されていれば CREWVIA_MUX_ENABLED より優先される。
  ```

**期待される効果（宣言どおりに動くなら）**:
- CREWVIA_MUX が設定されている場合、CREWVIA_MUX_ENABLED は無視される
- CREWVIA_MUX が未設定の場合のみ CREWVIA_MUX_ENABLED が有効
- **この「期待される効果」は Worker 経路の実装 (start.sh:597-601) とは逆**。実装の優先順位1位は `CREWVIA_MUX_ENABLED` の明示（`0` も含む）であり、`CREWVIA_MUX` はそれより優先されない（PR #196, t002 の設計判断 A: 「実装を docs に合わせる」ではなく「二重ゲートを実効値解決で統合する」を採用）。config コメントはこの修正後も訂正されておらず、Director 経路（実欠陥3）の open 残件としてここに記載する

**実欠陥 1: scripts/start.sh の並列モードゲートが ROLE=worker で機能していなかった（修正済み: PR #196, merge commit `1581d11`）**

- 並列モードを実際にゲートしていたのは `CREWVIA_MUX_ENABLED == 1` 判定 1 箇所のみだったが、`CREWVIA_MUX_ENABLED` を設定する処理は「ROLE=director かつ未設定」の対話選択ブロック内にしか無かった
- そのため ROLE=worker では `CREWVIA_MUX=herdr/tmux` を明示していても `CREWVIA_MUX_ENABLED` が設定されず、ゲート判定でインラインモードに転落していた（docs が謳う「CREWVIA_MUX 設定済みなら不要」が worker 経路では成立していなかった）
- 修正: ゲート判定の直前で実効値を解決 — `CREWVIA_MUX_ENABLED` の明示があれば最優先、未設定なら `CREWVIA_MUX` の有無にフォールバック
- 回帰テスト: `tests/start-sh-mux-gate.bats`（ROLE=worker + CREWVIA_MUX=tmux + CREWVIA_MUX_ENABLED 未設定で並列モードに入ることを含む3ケース）

**実欠陥 2: 呼び出し元の `./crewvia` ランチャが mode: tmux で `CREWVIA_MUX` を export していなかった（修正済み: PR #201, merge commit `a26b60a`）**

- `./crewvia` は改名済みで誰も読まない旧変数 `CREWVIA_TMUX`（mission 20260907-tmux-name-cleanup で `CREWVIA_MUX_ENABLED` に改名済み）だけを export しており、`mode: tmux` / 不明値のケースで `CREWVIA_MUX` を export していなかった
- `scripts/start.sh` の worker 起動ゲートは `CREWVIA_MUX` の有無で並列モードを決めるため、config を `mode: tmux` に戻すと `./crewvia worker` はインラインモードに落ちていた（PR #196 が start.sh 側を直しても、呼び出し元がそもそも `CREWVIA_MUX` を渡していなければ同じ症状が再発する構造）
- 修正: `mode: tmux` / 不明値 → `CREWVIA_MUX=tmux` を export（herdr は従来通り）。`mode: inline` は `CREWVIA_MUX` を export しない。旧 `CREWVIA_TMUX` は値を参照せず、設定されていれば無視される旨の WARNING のみ出す
- 回帰テスト: `tests/crewvia-launcher-mux-mode.bats`（mode 4通り × CREWVIA_MUX env 有無 × 旧 CREWVIA_TMUX env 有無。修正前は 10 件中 5 件が red、修正後は全 green）

**実欠陥 3 (open, コード修正は未実施 — Director backlog): Director 経路では env の `CREWVIA_MUX` よりも config の `mode:` が優先されてしまう**

- `scripts/start.sh:232` `[[ "${ROLE}" == "director" ]] && [[ -z "${CREWVIA_MUX_ENABLED:-}" ]]` のブロックは `CREWVIA_MUX_ENABLED` が未設定なら実行され、env の `CREWVIA_MUX` の値に関係なく config `mode:` で上書きする（origin/main 実物 (`git show origin/main:scripts/start.sh`) で確認済み、行番号付き抜粋）:
  - `:249-257` `mode: herdr` → `export CREWVIA_MUX=herdr`（env が tmux でも上書き）
  - `:258-261` `mode: tmux` → `export CREWVIA_MUX=tmux`（env が herdr でも上書き）
  - `:262-263` `mode: inline` → `export CREWVIA_MUX_ENABLED=0`（env に CREWVIA_MUX があってもインライン）
- `scripts/start.sh:1-231` に `CREWVIA_MUX` / `CREWVIA_MUX_ENABLED` への代入は無い（`grep -n` で確認、0 件）。呼び出し元 `crewvia` ランチャ (origin/main:106) は env の `CREWVIA_MUX` が既に設定されていればそのまま（`export` し直さず）`exec bash scripts/start.sh director` するだけで、`CREWVIA_MUX_ENABLED` は一度も export しない（`crewvia` に代入なし、grep 確認）。つまり `./crewvia` 経由の Director でも `CREWVIA_MUX_ENABLED` は未設定のまま L232 に到達し、config が env の `CREWVIA_MUX` に勝つ
- 宣言側 `CLAUDE.md:94`「`CREWVIA_MUX` … config `mode:` より優先」と食い違う。`config/crewvia.yaml:55-56` のコメントも同じ食い違いを繰り返している（上記「宣言箇所」「期待される効果」参照。t013 レビュー指摘の F2 相当）
- 具体的に困る場面: herdr pane は `CREWVIA_MUX=herdr` を継承するが `CREWVIA_MUX_ENABLED` は継承しない。この状態で config を `mode: tmux` に変えて `./crewvia` を実行すると、Director は tmux (`:258-261`) になり、同じ pane からの `./crewvia worker` は Worker 経路のフォールバック (`start.sh:597-601`。env の `CREWVIA_MUX=herdr` を検出して `CREWVIA_MUX_ENABLED` を 1 にフォールバック) で herdr のままになるため、Director と Worker が別 mux に分散しうる（`start.sh:231` のコメントが防ぎたいとしている事故そのもの）
- 根拠: 静的読解 + origin/main 実物への `git show` / `grep` による直接確認（2026-09-12, 本 task t014 で実施）。実行してのハーネス検証は未実施
- **コード修正は未実施**。修正方針（config を尊重する／env を常に優先する／二重ゲートを統合する、等）の検討と実装は次ミッションで Director が扱う

**倒れる方向**:
- Worker 経路 (実欠陥1・2): 修正前は危険側（設定したのに並列モードに入らずインラインへ転落）。両修正の適用後は解消
- Director 経路 (実欠陥3): 未解決の open 残件。危険側（Director と Worker が異なる mux に分散する事故）だが、発生には「herdr pane から config を tmux/inline に変えて `./crewvia` する」等の特定条件が必要なため、Worker 経路の実欠陥よりは影響範囲が限定的

**再現手順**:
```bash
# 1. Worker 経路のゲート判定 (修正後の実体: start.sh:578-596 のコメント + 597-601 のコード。
#    1581d11 のコミットメッセージ 24-29 行はこの修正の説明文であり、コード実体ではない)
git show 1581d11 -- scripts/start.sh

# 2. ./crewvia ランチャの export 分岐 (修正後)
git show a26b60a -- crewvia

# 3. Director 経路 (未修正, open): config が env を上書きするブロックの実体
git show origin/main:scripts/start.sh | sed -n '232,263p'
```

優先順位（実装で確認済み）:
- **Worker 経路**（両修正適用後）: 1. env の `CREWVIA_MUX_ENABLED` 明示（`0` も含め最優先。start.sh:597-601） → 2. env の `CREWVIA_MUX` 明示（config より優先。crewvia ランチャ / start.sh フォールバック双方） → 3. config/crewvia.yaml の `mode:`（crewvia ランチャが `CREWVIA_MUX` に変換）
- **Director 経路**（open）: 1. config/crewvia.yaml の `mode:`（`CREWVIA_MUX_ENABLED` 未設定なら env の `CREWVIA_MUX` を無視して上書き。start.sh:232-263） → 2. env の `CREWVIA_MUX_ENABLED` 明示（設定済みならブロック自体をスキップするので事実上勝つが、Director 起動元の `crewvia` ランチャはこれを一度も export しないため実運用では到達しない）

**注記**:
- 本節は Director の要約ではなく、`git show 1581d11` / `git show a26b60a` の実 diff・コミットメッセージ、および origin/main 実物 (`scripts/start.sh`, `crewvia`, `config/crewvia.yaml`, `CLAUDE.md`) への直接確認に基づく
- 監査時点の「未検証・低・LOW」という結論、および t012 の「両方とも修正済み」という結論（Director 経路を見落とした点）は、いずれも本監査の同型欠陥（宣言と実装の食い違いを十分に検証せずに楽観側で評価した）に該当する

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

### E. Scope からの漏れ — CI がテストを一度も実行していなかった

**状態**: 本監査の scope（config/*.yaml, agents/*.md, CLAUDE.md, hooks/ の4観点。「監査対象と観点」節参照）に `.github/workflows/` が含まれておらず、CI が実際に何を実行しているかを見ないまま「テストはある」で済ませていた。これも「宣言（テストファイルが存在する）≠ 実装（CI で実行される）」という本監査と同型の欠陥。

#### E-1: `tests/*.py` が CI で一度も実行されていなかった（修正済み: PR #200 / t006, merge commit `abcd434`）

**宣言箇所（時点により件数が異なる。t014 で `git worktree add --detach <tmp> 8df15a3` + `python3 -m pytest tests/ --collect-only -q` を実行し実測、混在を訂正）**:
- 監査時点 (8df15a3): `tests/test_*.py` 7ファイル・149テストケース（`test_orphan_daemon_guard.py` はまだ存在しない）
- 現時点 (origin/main。#197 = `1e8be20` で `test_orphan_daemon_guard.py` が追加された後): `tests/test_*.py` 8ファイル・163テストケース

**実際の状態（監査時点）**: `.github/workflows/ci.yml` には bats (`tests/*.bats`) と `scripts/test_handoff_path.sh` / `scripts/test_registry_lock.sh` を実行するジョブしかなく、pytest を実行するジョブが無かった。監査時点で書かれていた149件のテストコード（7ファイル）は CI では一度も走っていなかった（ローカルで手動実行しない限り regression を検知できない状態）。

**修正**: `.github/workflows/ci.yml` に `pytest` ジョブを追加（`actions/setup-python@v5` (3.12) → `pip install pytest pyyaml` → `python3 -m pytest tests/ -v`）。`continue-on-error` 等は使っておらず、テスト失敗はそのまま job 失敗になる。PR #200 は #197 (`test_orphan_daemon_guard.py` 追加) の後に作られたため、163件 collect / 163件 pass を確認済み（PR #200 本文）。

**倒れる方向**: 危険側（regression が CI で検知されずに merge される）。修正後は解消。

#### E-2: `scripts/test_*.sh` は 21本中 2本しか CI で実行されていない（未対応・open）

**宣言箇所**: `scripts/test_*.sh` 21本（bash による回帰テスト群）

**実際の状態**: main `1e8be200e61d952884b294d75d7c93798e246e28` 時点（PR #200 棚卸し時点。#199 の merge で本数が変わり得るため commit hash を明記する）で CI 実行中なのは `test_handoff_path.sh` / `test_registry_lock.sh` の2本のみ。残り19本は静的調査（各ファイルの grep + 抜粋読み。実行しての検証はしていない）では概ね外部バイナリ不要に見えるが、CI には追加されていない。PR #200 の棚卸し表（t008 レビューで #9 `test_phase_e.sh` の外部依存記述を Upstash Redis REST に訂正済み）を引用する:

| # | ファイル | CI実行中 | CI実行可能性(簡易調査) | 外部依存 |
|---|---|---|---|---|
| 1 | test_blocked_by_guard.sh | – | 可能 | なし (plan.sh ロジックのみ) |
| 2 | test_dispatcher_notify.sh | – | 可能 | なし (tmux はコメント中のみ、実spawnなし) |
| 3 | test_handoff_path.sh | ✅ | (実行中) | git worktree (実行、CI で問題なし) |
| 4 | test_hooks.sh | – | 可能 | curl は接続失敗ケースのテストのみ (127.0.0.1:19999 へ) |
| 5 | test_kai_review.sh | – | 可能 | gh/codex は FAKE_BIN_DIR でスタブ化済み、実バイナリ不要 |
| 6 | test_main_repo_git_guard.sh | – | 可能 | `git worktree add` は hook 判定用の入力文字列 (実際には作らない) |
| 7 | test_normalize_plan_review_verdict.sh | – | 可能 | なし |
| 8 | test_phase_c.sh | – | **不可** | 実ネットワーク (`https://ntfy.elni.net`) + localhost dev server 必須 |
| 9 | test_phase_e.sh | – | **不可** | Upstash Redis REST (`UPSTASH_REDIS_REST_URL`/`TOKEN` 必須) + localhost:3000 dev server 必須 (ntfy は不使用。t008 レビューで訂正) |
| 10 | test_plan_reason_frontmatter.sh | – | 可能 | なし |
| 11 | test_plan_review_cycle_refund.sh | – | 可能 | なし |
| 12 | test_plan_review_write_guard.sh | – | 可能 | なし (codex はコメント中のみ) |
| 13 | test_pr181_review_fixes.sh | – | 可能 | ローカル python ソケットサーバーのみ (外部ネットワーク不要) |
| 14 | test_registry_lock.sh | ✅ | (実行中) | なし |
| 15 | test_review_plan_director_identity.sh | – | 可能 | claude CLI は FAKE_BIN_DIR でスタブ化、herdr は到達不能 socket で意図的 fallback |
| 16 | test_review_plan_model.sh | – | 可能 | 同上 (claude スタブ化) |
| 17 | test_review_plan_pane_leak.sh | – | 可能 | claude CLI 起動せず (ファイル自身の注記あり) |
| 18 | test_task_file_write_guard.sh | – | 可能 | gh/codex は hook 判定用の入力文字列のみ |
| 19 | test_wait_for_plan_review.sh | – | 可能 | claude CLI 起動せず (ファイル自身の注記あり) |
| 20 | test_watchdog_config_mode.sh | – | 可能 | herdr/tmux は mode 文字列パースのテストのみ、実 spawn なし |
| 21 | test_worktree_edit_guard.sh | – | 可能 | git worktree は実際には作らない方針 (ファイル自身の注記あり) |

CI実行不可と判定: 2本 (`test_phase_c.sh`, `test_phase_e.sh` — 実ネットワーク + localhost dev server 必須)。残り19本は CI 追加の候補だが未着手（本 task の scope 外。CI 追加は Director が別途判断）。

**倒れる方向**: 危険側（19本の回帰テストが regression を検知できない状態のまま残っている）。**未対応 — この audit doc 上は open のまま**。

**再現手順**:
```bash
ls scripts/test_*.sh | wc -l   # 21
grep -n "test_handoff_path.sh\|test_registry_lock.sh\|pytest" .github/workflows/ci.yml
```

**原因（scope 漏れ）**: 本監査の「監査対象と観点」節（1. config/*.yaml、2. agents/*.md、3. CLAUDE.md、4. hooks/）に CI 設定 (`.github/workflows/`) が含まれていなかったため、テストファイルの存在確認だけで「テストはある」と判断し、CI で実際に実行されているかを検証していなかった。

---

## 優先度マトリクス

| ID | 項目 | 倒れる方向 | 影響度 | 優先度 |
|---|---|---|---|---|
| A-1 | verification-profiles.yaml | 安全側 | 低（未実装機能） | LOW |
| A-2 | autonomous-improvement.yaml | 安全側 | 中（指示と実装ギャップ） | MEDIUM |
| B-1 | worktree edit ガード Bash抜け道 | 危険側 | 高（t009/t011 過去事故） | HIGH |
| C-1 | CREWVIA_MUX 優先順位 | 危険側（Worker 経路は修正済み／Director 経路は open） | 高（並列モードが無効化していた。Director 経路は mux 分散事故が残存） | Worker 経路: 修正済み（PR #196, #201）／Director 経路: 未対応（open, Director backlog） |
| D-1 | NTFY 認証情報設定 | 安全側 | 低（設計OK） | - |
| E-1 | pytest が CI 未実行 | 危険側（修正済み） | 高（監査時点149件・修正時点163件が regression 検知不能だった） | 修正済み（PR #200） |
| E-2 | scripts/test_*.sh 19本が CI 未実行 | 危険側 | 中〜高（未検証） | 未対応（open） |

---

## 後続検証候補（Scope 外）

以下は audit scope 外だが、Director の次ミッション判断時に検討すること：

1. **C-1 優先順位検証**: CREWVIA_MUX 環境変数と config mode の正確な優先順位確認
   - **部分完了**: Worker 経路の実欠陥2箇所は mission 20260912-verdict-ci-launcher で検証・修正済み（PR #196 merge commit `1581d11`, PR #201 merge commit `a26b60a`）。**Director 経路** (`scripts/start.sh` の `ROLE=director` ブロック) は env の `CREWVIA_MUX` より config `mode:` が優先されてしまう食い違いが未解決 (open) — 次ミッションで Director が修正方針を検討すること。詳細は上記 C-1 参照

2. **A-2 autonomous-improvement.yaml 実装検討**:
   - Option 1: Worker が手動判定する現状を保持し、ドキュメント確認
   - Option 2: コード側で自動判定ロジックを追加（hooks でチェック + Taskvia auto-log）
   - **推奨**: Option 1 確認（指示は十分か確認し、不足分だけ足す）

3. **A-1 verification-profiles.yaml の実装**:
   - verifier.md の指示とコード実装が不在
   - 実装予定があるか確認し、無いなら削除 or タスク化

4. **E-2 scripts/test_*.sh 19本の CI 追加**: 静的調査上は外部バイナリ不要に見える19本を CI に追加するか判断。実行しての検証はしていないため、追加時に想定外の失敗が出る可能性はある

---

## まとめ

### 発見件数
- **Dead config**: 2 件（verification-profiles.yaml, autonomous-improvement.yaml 実装部）
- **Partial dead / 限界あり**: 1 件（worktree edit ガード Bash 抜け道）
- **修正済み**: 1 件（pytest CI 未実行 [E-1] — PR #200）
- **部分修正済み（一部 open）**: 1 件（CREWVIA_MUX 優先順位 [C-1] — Worker 経路は PR #196/#201 で修正済み、Director 経路は未解決）
- **未対応 (open)**: 2 件（CREWVIA_MUX 優先順位 Director 経路 [C-1 実欠陥3]、scripts/test_*.sh 19本の CI 未実行 [E-2]）
- **健全**: 1 件（NTFY 認証情報 env 優先）
- **scope 漏れの訂正**: 監査対象の4観点に `.github/workflows/` が含まれておらず、CI 未実行の finding (E-1/E-2) を当初見落としていた

### t001-t004 との共通パターン

1. **宣言 ≠ 実装**: verification-profiles.yaml, autonomous-improvement.yaml
   - t001-t004 と同型 (宣言されているが機構で保護されていない)

2. **実装の限界が指示に不完全に反映**: worktree edit ガード
   - t001-t004 と異なり、ガード**は存在するが Bash 抜け道がある**
   - 指示で「Bash は対象外、手動規律に依存」と明記されているため、健全性評価は「安全側」

3. **設計健全（env > config）だが指示ドキュメント曖昧**: NTFY 認証情報
   - 実装は正しいが、CLAUDE.md で「config に書かない」と明記されていない

4. **「未検証」の楽観評価も同型欠陥になり得る（訂正それ自体でも再発した）**: C-1 は監査時点で「未検証・低・LOW」と結論したが、実際には危険側の実欠陥が2箇所（Worker 経路）あった。「効いているように見える／確認していないが低リスクだろう」という評価そのものが t001-t004 と同じ「検証せずに安全側だと仮定する」パターンに該当する。さらに、この訂正 (t012) 自体も Worker 経路の2箇所を直しただけで「C-1 は解消」と結論し、別に残っていた Director 経路の食い違いを見落としていた（t014 で発見・訂正）。楽観評価パターンは1回訂正すれば終わりではなく、訂正のたびに再検証が必要

5. **scope 設計に監査対象を検証する経路 (CI) が抜けていた**: E-1/E-2 はテストファイルの存在確認だけで「テストはある」と判断し、CI で実行されているかを見ていなかった。宣言（テストコード）と実装（CI 実行）のギャップという点で A-1/A-2 と同型

### 次アクション

- **HIGH**: B-1 worktree edit ガード Bash 抜け道を次ミッションで検討（PR #180 検討対象か）
- **MEDIUM**: A-2 autonomous-improvement.yaml 実装 OR 指示確認・修正
- **MEDIUM**: E-2 scripts/test_*.sh 19本の CI 追加を次ミッションで検討
- **LOW**: A-1 verification-profiles.yaml の方針確認（実装か削除か）
- ~~**LOW**: C-1 CREWVIA_MUX 優先順位を詳細検証~~ → **Worker 経路は完了**（PR #196, #201 で修正済み）
- **MEDIUM**: C-1 Director 経路 (`scripts/start.sh:232-263` の `ROLE=director` ブロック) の env/config 優先順位食い違いを次ミッションで修正検討（open, Director backlog。t014 で発見）

---

**監査者**: Haiku 4.5  
**監査日**: 2026-09-09  
**対象リポジトリ**: crewvia (main 8df15a3)

---

**訂正・追記者**: Mei（Sonnet 5）  
**訂正日**: 2026-09-12（mission 20260912-verdict-ci-launcher, t012）  
**訂正内容**: C-1 の結論を「未検証・低・LOW」から「実欠陥2箇所・修正済み」に訂正（PR #196 merge commit `1581d11`, PR #201 merge commit `a26b60a`）。CI がテストを実行していない finding (E-1 pytest / E-2 scripts/test_*.sh) を追加。監査対象の4観点に `.github/workflows/` が含まれていなかった scope 漏れを明記。

---

**再訂正・追記者**: Mei（Sonnet 5）  
**訂正日**: 2026-09-12（mission 20260912-verdict-ci-launcher, t014。PR #198 の t013 (Seo) fact-check レビュー F1-F5 対応）  
**訂正内容**:
- **F1/F2**: t012 の「C-1 は両実欠陥とも修正済みで解消」という結論は Worker 経路のみを指すもので不正確だった。Director 経路 (`scripts/start.sh:232-263` の `ROLE=director` ブロック) では env の `CREWVIA_MUX` に関係なく config `mode:` が優先されてしまう食い違いが別に存在し、未修正 (open, Director backlog)。origin/main の `scripts/start.sh` / `crewvia` / `config/crewvia.yaml` / `CLAUDE.md` を実物で確認し、C-1 を「Worker 経路: 修正済み」「Director 経路: open」に書き分けた（サマリ表・後続検証候補・まとめ件数・次アクションの全 `grep -n 'C-1'` 箇所を統一）
- **F3**: 再現手順の `git show 1581d11 -- scripts/start.sh (修正後: 24-29行のコメント参照)` は誤り（24-29行はコミットメッセージ）。実体は `start.sh:578-596`（コメント）+ `597-601`（コード）と訂正
- **F4**: E-1「監査時点で163件」の混在を訂正。監査対象 `8df15a3` 時点の `tests/*.py` は7ファイル・149テストケース（`git worktree add --detach` で当時のツリーを再現し `pytest --collect-only` で実測）。163件（8ファイル）は `test_orphan_daemon_guard.py` (#197 = `1e8be20`) 追加後の現在の件数
- **F5**: 本 task では PR #198 のタイトル・本文を本ドキュメントの最終状態に合わせて更新（`gh pr edit 198`）
