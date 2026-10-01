# Git Policy (vNext 01b) — 設計 (ADR)

vNext 01b の実装 PR (G1〜G4) はこの設計に従う。**コードの根拠はすべて main `48ca12e` の行番号**
(01a の記録 t025 / PR #262 が入った版)。

- 入力:
  - 原案 `~/obsidian/proposals/crewvia/crewvia-vnext-mission-01-control-plane-foundation.md`
    (§3.2 Phase 1 / §7.3 Git Policy Resolver / §8.3 GIT-01〜05 / §10.4 / §11 AC-02 / §12.3 / §14)
  - 改訂案 `~/obsidian/proposals/crewvia/20260930_crewvia-vnext-mission-01-revision.md`
    (§1 表の 3・5・6・7 行 / §2-B「worktree 作成の失敗」/ §2-F / §4-R3 の 01b 行 / §4-R4「GIT（01b）」/ §7)
  - 01a の設計と実績 `knowledge/state-store.md` (§0 の 01b 行 / §2 回復 / §5.2 / §6 構造ガード / §10.3「01b へ」)
- ユーザー決定 (改訂案 §7): **R2** — 呼び出し元ゼロの lib は通常どおり merge する。
  **呼び出し側を移す PR は merge 前にユーザーの承認を取る**。`plan.sh` は主 checkout から直接実行されるので、
  merge がそのまま cutover になる。env の停止スイッチで新旧を切り替えない (不変条件 5)。
- PR と段の対応:

  | 段 | task | 内容 |
  |---|---|---|
  | G1 | t004 | GIT-05。cutover |
  | G2 | t008 | Resolver。呼び出し元ゼロ |
  | G3 | t012 | 委譲・`.crewvia-env`・構造ガード。cutover |
  | G4 | t016 | 文書の書き換え。cutover |
  | 本番確認 | t019 | |
  | 記録 | t020 | |

- 順序: G1 は G2 より先に入る (t004 の blocked_by は t003 だけ)。そのため **G1 は Resolver なしで書ける形**にする (§1.7)。
  G3 は G1 と G2 の両方の後に入る。

## 0. このミッションでやらないこと

| 項目 | 扱い |
|---|---|
| `target_dir` の task の branch / base / PR base | **範囲外**。今の挙動を保つ。worktree を作らず `worktree_path: null` を返し (plan.sh:3496)、Worker は TARGET_DIR の checkout で作業する (worker.md:182)。文書の `--base main` は TARGET_DIR の Worker には既定値 `main` のまま届く (§5) |
| mode `direct` 以外 (`integration` 等) | **実装しない**。指定されたら黙って direct に倒さず停止する (§2) |
| Execution ID・attempt・Task Controller・呼び出し元の照合・pull の冪等化 | **01c**。G1 の後始末は「pull 自身が今作った予約か」を card の `worker` / `started_at` で確かめるだけで (§1.2)、新しい ID は作らない |
| worktree の削除・`git worktree prune` | しない。片付けは今どおり `worktree_gc.py` の手動運用 (`knowledge/worktree-gc.md`)。`crewvia_remove_worktree` (git-helpers.sh:92-114) は呼び出し元が無いまま残す |
| 主 checkout の同期 (`sync-main-checkout.sh` / drift 検出) | Resolver に寄せない (§4)。task の policy ではなく、本番 crewvia の更新手順 |
| stacked PR の base / review の diff base を PR の実 base にする | 範囲外 (§4 の kai-review 行・§10)。Policy の `pr_base` に揃えるところまでがこのミッション |

---

## 1. GIT-05 — worktree を作れないとき (G1 / t004)

### 1.1 実測: `worktree_path` が null / 空になる経路 (main 48ca12e)

pull の流れ。

1. ロックの中で card を `in_progress` にする (plan.sh:3414-3417)。
2. 同じロックの中で `.identity` と assignment を公開する (:3423-3424)。
3. ロックの外で Taskvia に同期する (:3478)。
4. `crewvia_create_worktree` を呼ぶ (:3495-3505)。
5. `.crewvia-env` を書く (:3506-3522)。
6. JSON を出す (:3529-3532)。

4 以降の失敗は、すでに 1 と 2 が確定した後に起きる。

| # | 経路 | 根拠 | 今の結果 |
|---|---|---|---|
| N1 | task に `target_dir` がある | plan.sh:3496 | `worktree_path: null`。**意図どおり** (範囲外) |
| N2 | `scripts/git-helpers.sh` が無い | plan.sh:3495-3496 `os.path.exists(git_helpers)` | null。stderr にも何も出ない |
| N3 | worktree の path がもうある | git-helpers.sh:67-70 `path already exists` → return 1 | plan.sh:3523-3527 が `WARNING: worktree creation skipped` を出して**成功扱い**。null |
| N4 | `git worktree add` が失敗する (branch が別の worktree で checkout 済み / prunable な登録が残っている / base の `main` も無い / ディスク) | git-helpers.sh:82-86 (`set -euo pipefail` :2) | N3 と同じ (WARNING・成功・null) |
| N5 | 引数が空 | git-helpers.sh:57-60 | N3 と同じ。`_slugify` (plan.sh:3482-3487) が task_id に fallback するので、今は起きない |
| N6 | `crewvia_create_worktree` が rc=0 で stdout が空 | plan.sh:3507 `wt.stdout.strip()` | `worktree_path: ""`。Worker の `jq -r '.worktree_path // empty'` (worker.md:295) は空文字を空と読むので、cd しない |
| N7 | `.crewvia-env` が書けない (耐久性だけの失敗を除く) | plan.sh:3518-3520 `die(...)` | exit 1・JSON なし。card は `in_progress` で assignment もあり、worktree もできている。**同じ task の再 pull は `already in_progress` で拒否される** (:3238-3243)。Worker は動けず、card は誰も片付けない |
| N8 | pull のプロセスが 2 と 6 の間で死ぬ | — | N7 と同じ形 (JSON なし・card は in_progress) |

Worker 側の読み方。

- Worker は `WORKTREE_PATH` が空なら cd しない (worker.md:294-301)。
- 起動した場所は `WORK_DIR="$REPO_ROOT"` (start.sh:403)、つまり**主 checkout** になる。
- hook の編集ガード (pre-tool-use.sh:245-251) は Edit/Write を止める。しかしその前提のコメント
  (pre-tool-use.sh:205-207「TARGET_DIR 未設定なら plan.sh pull は必ず worktree を作り worktree_path を返す」) は
  **N2〜N4 で破れている**。Bash 経由の書き込みは止まらない (worker.md の「既知の限界」)。

**正当な再 pull が N3 を必ず踏む**。

- worktree の path は `mission_slug` / `task_id` / title 由来の `task_slug` から決定的に決まる (git-helpers.sh:62・:65)。
- 片付ける者もいない (§0)。
- だから、一度 pull された task をもう一度 pull すると、**毎回** path が既にあって N3 になる。
  例: `update --reset` → 再割り当て、`needs-director` → Director が pending に戻す、
  S4 の回復 (R-2) の後の `pull --task`、Kai-codex の codex-review の再実行。
- `.crewvia-env` が書き直されない問題 (state-store.md §0 の 01b 行・§5.2) も、根は同じ N3。

### 1.2 決定: 失敗の出口

| 案 | 内容 | 判定 | 理由 |
|---|---|---|---|
| A. 予約を解く | 2 つ目のロックで card を `pending` に戻し (`worker` / `started_at` を消す)、assignment を撤去する | **捨てる** | N3〜N4 のほとんどは**決定的**で、もう一度やっても同じく失敗する (path が残っている・branch が別 worktree に握られている)。pending に戻すと dispatcher はすぐ同じ task を配る。skill と TARGET_DIR が合うのが同じ Worker なら、同じ Worker に配り直して**失敗し続ける無限ループ**になる。回数を数えて止めるには、card に新しい欄 (attempt) が要る。それは 01c の Execution の仕事で、ここで先取りすると二重定義になる |
| B. **`needs_director` に送る** | 2 つ目のロックで、card を `needs-director` と同じ遷移で `needs_director` にする。理由は `needs_director_reason` に書く。assignment は撤去する | **採る** | `needs_director` は pull の候補にならない (`_TASK_STATUS.accepts('pull', ...)` plan.sh:3316 は pending だけ)。dispatcher も配らないので、**ループが構造的に起きない**。dispatcher は needs_director を状態ベースで **1 回だけ** Director に通知する (`notify_state_once`、scripts/CLAUDE.md「通知・記録」)。出口は既存のもの。Director が原因 (残った dir・握っている worktree) を片付け、`plan.sh update <id> --status pending --reset` で戻す。Worker は card の `worker` が残るので「判断待ち」と読まれ、退役されない (dispatcher.sh:651 `worker_waits_on_director` / :2518) |
| C. `failed` にする | `plan.sh fail` と同じ遷移 | 捨てる | `failed` は依存先を**保留**にし (`release-dep` が要る。不変条件 3)、`--head` も要る (scripts/CLAUDE.md「plan.sh」)。作業は 1 行も始まっていないので、失敗の記録として重すぎる |
| D. 1 回だけ自動で再試行してから B | git の一時的な失敗 (`index.lock` の競合) を吸収する | 捨てる | 再試行で直る形は実測で見つかっていない。再試行の前後で「何が変わったら成功するか」を言えないものは、待ち時間だけが増える。B の 1 回の Director 対応で足りる |

B の細部。G1 はこのとおり実装する。

- **2 つ目のロック**。worktree の作成はロックの外 (原案 §14-15・plan.sh:3475-3477)。失敗が分かったら、もう一度 `with_lock` を取って次の順で動く。
  1. 先に `recover_before(cards=[(slug, tid)])` を呼ぶ (01a の規則。scripts/CLAUDE.md「回復」)。
  2. card を読み直し、**`status == in_progress` かつ `worker == <この pull の agent>` かつ `started_at == <この pull が書いた世代>`**
     のときだけ書く。この pull 自身が作った予約かどうかを確かめる compare-and-set で、01c の呼び出し元照合ではない。
  3. 合わなければ何も書かない。その間に Director が reset した・別の Worker が取った、のどちらかなので、stderr に 1 行出して exit 1 にする。
- **遷移は `cmd_needs_director` (plan.sh:3832) と同じ関数を通す**。今の `_do` の本体
  (status・`needs_director_reason`・長文の退避・`retire_assignment`) を 1 つの関数に切り出し、needs-director と pull の失敗の両方から呼ぶ。
  コピーしない (原案 §14-7)。撤去する assignment の名前は、env の `AGENT_NAME` ではなく **pull の `agent`** (`--agent` 優先。plan.sh:3129) を使う。
- **理由の文言** (`needs_director_reason`) は `worktree を作れませんでした (<分類>): <git-helpers の stderr の最後の 1 行>` にする。
  全文は今の `split_long_freeform` の規則で本文の `## Needs-Director 詳細` に入る。分類は §1.3 の表の W 番号。
- **exit code は 1**。exit 2 は「task が無い = idle」なので使わない (memory `pull-exit-2-is-idle-usage-errors-must-be-1`)。
  exit 3 (PRECONDITION_UNMET plan.sh:1961) は「1 バイトも書いていない」の約束なので使わない (B は card を書く)。
  stdout には **JSON を出さない**。Worker には「取れなかった」以外の読み方をさせない。
- `agent` が空の pull (`--agent` なし・`AGENT_NAME` なし) でも同じ。card の `worker` は空のまま比べ、撤去する枠は無い。

### 1.3 再 pull と既存の worktree の判定

置き場所は `crewvia_create_worktree` (git-helpers.sh:52)。G3 の後も、git の**観測と副作用**はここに残す。Resolver は値を決めるだけ (§3)。
plan.sh には git の知識を増やさない。

判定は `git worktree list --porcelain -z` (git 2.36 以降。手元は 2.43.0。`worktree_gc.py:246-273` が同じ形を Python で読んでいる)。
この出力を NUL 区切りで読み、**期待する path と完全一致する `worktree <path>` の段落**を探す。比べる前に、両側の path を
`realpath` (bash では `cd -P` + `pwd -P`) で正規化する。正規化できなければ「一致しない」側に倒す (W5 になる。安全側)。

| # | 観測 | 動作 | 結果 |
|---|---|---|---|
| W0 | path が無い・branch が無い | `git worktree add -b <branch> <path> <base>` (今の :85) | 作成 |
| W1 | path が無い・branch がある・どの worktree にも checkout されていない | `git worktree add <path> <branch>` (今の :83。**base は無視**して既存の branch を使う。GIT-04 の現状維持) | 作成 |
| W2 | path がある・**その path が登録済みで `branch refs/heads/<この task の branch>`**・`prunable` でない・dir である | 何もしない。path を返す | **再利用**。`.crewvia-env` は pull が毎回原子的に書き直す (§1.4) |
| W3 | path がある・登録済みだが branch が違う / detached | 失敗 | B (needs_director) |
| W4 | path がある・未登録 (残骸の dir) | 失敗。**dir を消さない** | B |
| W5 | path が無い・`git worktree add` が失敗 (branch が別の path の worktree で checkout 済み・prunable な登録・base が無い・ディスク) | 失敗 | B |
| W6 | `git fetch origin` が失敗 | 警告して**続行** (今の :72。GIT-04)。警告は stderr に出し続ける (§1.5) | W0〜W5 のどれか |
| W7 | `origin/main` が無い | local `main` に fallback して警告 (今の :74-78。GIT-04) | W0〜W5 のどれか |

- **W2 で中身を確かめない理由**。未 commit の変更・前の試行の commit が残っていても再利用する。今の W1 (既存の branch を base 無視で使う)
  と同じ方針で、「同じ task の branch = 同じ task の作業」と読む。前の試行を捨てたいなら、Director が reset の前に worktree を片付ける
  (`worktree_gc.py` の隔離か手作業)。消す判断を pull に持たせない (memory `destruction-needs-provenance-not-classification`)。
- **W3・W4 を自動で直さない理由**。どちらも「その path に誰かの作業がある」可能性を否定できない。登録の付け替え・dir の削除は破壊的な
  操作なので、出口は Director の手作業にする。
- **title を変えると path と branch が変わる**。`task_slug` が title 由来だから (plan.sh:3491)。title を変えた task を再 pull すると、
  旧 branch の commit を見ずに新しい branch を作る (W0)。今もそうで、01b では変えない (§10 の backlog)。
- stdout は今どおり path 1 行だけ。再利用したかどうかは stderr に `crewvia_create_worktree: reusing registered worktree <path> (<branch>)`
  の 1 行で出す。**呼び出し元の契約 (stdout = path) を変えない**。`scripts/test_handoff_path.sh:67` が stdout を path として読んでいる。

### 1.4 null の経路を本番で 0 にする

| # | G1 の後 |
|---|---|
| N1 | 変えない (範囲外。意図どおり) |
| N2 | **テストの継ぎ目として残す**。理由は下 |
| N3 | W2 なら再利用して path を返す。W3・W4 なら B (needs_director・exit 1・JSON なし) |
| N4 | B |
| N5 | B (起きないが、同じ出口に入れる) |
| N6 | rc=0 で stdout が空、または path が dir でないとき B に入れる。**`worktree_path` に空文字を出さない** |
| N7 | B に入れる (`die` で in_progress のまま止めない)。耐久性だけの失敗 (`_committed_durability_failure` plan.sh:2635) は今どおり警告して続行する (01a S5 の規則。memory `committed-but-not-durable-must-finish`) |
| N8 | 01c の範囲 (pull の冪等化)。G1 では JSON が出ないので、Worker は主 checkout で作業を始めない (§1.6)。card は in_progress のまま残り、出口は Director の `update --reset` (今と同じ) |

**N2 を失敗にしない理由**。`git-helpers.sh` が無いことは、テストの隔離が使っている継ぎ目になっている。

- `tests/fixture_tree.sh:15` と `tests/fixture_tree.py:38` は、意図して git-helpers.sh を写さない。
- `scripts/test_kai_review.sh:160-168` は「置くとテストの外側に worktree を作る」と書いている。
- `copy_plan_tree` を使うテストは 37 ファイルある (`grep -rl 'copy_plan_tree\|fixture_tree' tests scripts`)。

これを失敗にすると、crewvia-local の task を pull するテスト fixture が全部 needs_director に倒れる。
本番の主 checkout では git-helpers.sh は git で追跡されているので、無いのは checkout が壊れたときだけ。G1 は次の 2 つを入れる。

1. N2 の経路でも stderr に 1 行出す (`[plan.sh pull] scripts/git-helpers.sh が無いので worktree を作りません`)。黙らせない。
2. 「`scripts/git-helpers.sh` が git で追跡されている」を CI のテストで固定する (`git ls-files --error-unmatch`)。

env のスイッチで継ぎ目を作り直すことはしない (不変条件 5)。

### 1.5 記録

| 何を | どこに | 理由 |
|---|---|---|
| 失敗の遷移 (in_progress → needs_director) | 監査ログ。今の `with_lock` が `op=pull` (SUBCOMMAND。plan.sh:1901) で 1 行出す。`from=in_progress to=needs_director` で、成功の pull (`to=in_progress`) と区別できる | 新しい op 名を作らない。理由の本文は監査ログに出さない (state-store.md §4 の規則) |
| 失敗の理由・分類 | card の `needs_director_reason` と本文の `## Needs-Director 詳細` | Director が読むのは card |
| git の stderr (警告を含む) | **成功時も** pull の stderr にそのまま流す。今は失敗時しか出していない (plan.sh:3523-3527)。fetch 失敗 (W6)・local main fallback (W7)・再利用 (W2) が黙って消えている | 古い base で branch を切ったことを後から追えない |
| Director への通知 | 新しく作らない。needs_director の状態ベースの通知 (dispatcher、1 回だけ) に乗る | 通知の経路を増やさない |

01a の backlog 1 (失敗した操作は行を出さない・`result` は常に `ok`) は、この設計では塞がない。B は「失敗した操作」ではなく、
**needs_director への成功した遷移**として記録される。拒否 (exit 1/2/3) の行は 01c に送る。

### 1.6 01a S4 の回復 (R-1〜R-4) との関係

worktree の作成はロックの外。落ちる点ごとに、次のロック取得の回復と食い違わないことを確かめる。

| 落ちる点 | 正本 (card) | projection | 次のロックで | 出口 |
|---|---|---|---|---|
| 1 つ目のロックの中 | 01a §2.2 の表のとおり | 同左 | 同左 | 同左 |
| ロックの外 (Taskvia 同期・worktree 作成・`.crewvia-env`) で死ぬ | `in_progress`・worker=A・G | 枠と `.identity` あり (整合) | 何もしない (食い違いが無い) | N8。Director の `update --reset`。半端な worktree が残っていれば、次の pull で W2 (登録済みで branch 一致) か W4/W5 になる |
| 2 つ目のロックで card を needs_director にした後、assignment 撤去の前 | `needs_director` | 枠が残る | **R-2** が消す (`needs_director` は R-2 の対象。state-store.md §2.3) | 今の `needs-director` が途中で落ちたときと同じ |
| 2 つ目のロックの CAS が外れた | 他者が書いた状態 | 他者の状態 | 回復は他者の状態を正本として扱う | この pull は何も書かない |

- B は**正本 (card) を先に書き、projection (assignment) を後で消す**。done の D3 → D4 (scripts/CLAUDE.md「回復」) と同じ向き。
  逆順にすると「in_progress なのに枠が無い」が生まれ、R-1 が枠を作り直してしまう。
- 2 つ目のロックの冒頭で `recover_before` を呼ぶのは、ロックを取る全コマンドと同じ規則に揃えるため。回復は拒否を足さない (01a §2.5)。

### 1.7 G1 の変更範囲 (Resolver より前に入る)

- `scripts/git-helpers.sh`: §1.3 の W2〜W5 の判定と、stderr の再利用行。branch / path / base の式は**今のまま** (G3 で Resolver に移す)。
- `scripts/plan.sh` `cmd_pull`:
  - §1.2 の 2 つ目のロック。
  - `cmd_needs_director` と共有する遷移関数。
  - N6・N7 を B に入れる。
  - N2 の stderr 行。
  - 成功時に git-helpers の stderr を流す。
- `hooks/pre-tool-use.sh:205-207` のコメントを事実に合わせる (「pull は worktree を返すか、失敗で exit 1 して JSON を出さない」)。
- `agents/worker.md`:
  - pull が exit 1 のときの手当て。cwd で作業を始めない。`plan` の stderr を Director に見せる。待つ。
  - JSON の `worktree_path` が null になるのは `target_dir` の task だけ、と書く。
  - 01a の backlog 5 (exit 3 の対処) も同じ段落で 1 行足す。
- 構造ガードは足さない (G3 の範囲)。テストは §9。

---

## 2. Policy の schema (GIT-01 / GIT-02)

### 2.1 形

mission.yaml のトップレベルに `git:` を置く。1 段の mapping で、`lib_task_cards.parse_yaml` (lib_task_cards.py:161) が読める形にする。

```yaml
git:
  mode: direct
  base_branch: main
  pr_base: main
  task_branch_pattern: "task/{mission_slug}/{task_id}-{task_slug}"
  worktree_root: .claude/worktrees
```

| 欄 | 既定 (欄が無いとき) | 受け付ける値 | 拒否 |
|---|---|---|---|
| `mode` | `direct` | `direct` だけ | それ以外は `unsupported_mode` で停止。`integration` も同じ (原案 GIT-02) |
| `base_branch` | `main` | branch 名 (§2.3) | 空・不正な文字 |
| `pr_base` | `main` | branch 名 (§2.3) | 同上 |
| `task_branch_pattern` | `task/{mission_slug}/{task_id}-{task_slug}` | 置換子は `{mission_slug}` `{task_id}` `{task_slug}` だけ。**`{mission_slug}` と `{task_id}` を必ず含む**。置換後が §2.3 を満たす | 未知の置換子 (`{agent}` 等)・必須の置換子が無い |
| `worktree_root` | `.claude/worktrees` | **既定値だけ** (下の理由) | それ以外は `unsupported_value` |

- **`git:` が無い mission (今ある全部) は、上の既定値になる** (GIT-01)。既定値から作る branch と path は、git-helpers.sh:62・:65 と
  バイト単位で同じでなければならない (G2 の単体テストで固定)。
- **`task_branch_pattern` に `{mission_slug}` と `{task_id}` を必須にする理由**:
  - task ごとに branch が 1 本、を pattern の側で保証する。
  - `{agent}` (Agent 単位の branch。原案 §14-10) も、試行ごとの branch (§14-11) も書けない。
  - task_id はファイル名から来るので一意 (不変条件 2)。
- **`worktree_root` を既定値に固定する理由**。`.claude/worktrees` は Resolver の外の 3 か所が**自分で**知っている。
  - `hooks/pre-tool-use.sh:245` (編集ガードの除外)
  - `hooks/lib_main_repo_git_guard.py:185` (主 repo の git ガードの除外)
  - `scripts/worktree_gc.py:324・:682・:754` (片付けの根)

  別の値を許すと、hook が自分の worktree への編集を拒否し、gc が worktree を見落とす。3 か所を Resolver 経由にする
  (hook は tool 呼び出しのたびに mission を読むことになる) のは 01b の範囲を超える。だから欄は schema に置いて
  **既定値以外を拒否**する。広げるのは 3 か所を寄せる PR と同時にする。「未実装は黙って fallback せず停止」と同じ扱い。
- `base_branch` と `pr_base` に別の値を書けるのは G3 以降。G3 の cutover を確認するまで、Director は mission.yaml に `git:` を
  書かない (§7)。G3 より前の plan.sh は `git:` を未知のキーとして黙って読み飛ばす (parse_yaml は未知のキーを拒否しない) ので、
  書いても効かない。

### 2.2 検証の規則 (fail closed)

| 入力 | 動作 | 理由 |
|---|---|---|
| `git:` キーが無い | 既定値 | 既存互換 |
| `git:` キーがあって値が mapping でない (`git: null`・`git:` だけ・スカラー・リスト) | **拒否** (`malformed`) | parse_yaml は `git:` の下の行が 4 字下げだと**黙って読み飛ばし**、値を `None` にする (lib_task_cards.py:178-182 の「Deeply nested / orphaned indented line — skip silently」・:211-213)。`None` を「欄なし」に倒すと、`mode: integration` が黙って direct になる。`deliverable_required` で踏んだ「キーはあるが値が空」と同じ族 (lint_plan.py:480-484) |
| `git:` の下の字下げ行の数と、読めた欄の数が合わない | **拒否** (`malformed`) | 上と同じ読み飛ばしが、欄の一部だけに起きる形 (`  mode: direct` の次に 4 字下げの `pr_base`) を拾う。Resolver は mission.yaml の生の文字列も受け取って数える |
| 未知の欄 (`pr-base`・`baseBranch` 等) | **拒否** (`unknown_key`) | 綴り間違いを黙って既定値にしない |
| 値が文字列でない (`_scalar` が int / bool / None にした) | 拒否 (`type`) | `pr_base: 123` を `"123"` に読み替えない |
| 空文字・前後の空白 | 拒否 | 空の branch 名 (原案 GIT-02) |
| 制御文字 (`\x00-\x1f`・`\x7f`) | 拒否 | 原案 GIT-02 |
| 絶対パス・`..` の成分・`~` で始まる (worktree_root) | 拒否 | 原案 GIT-02。そもそも既定値以外を拒否するが、規則は先に書いておく (広げるときに要る) |
| mission.yaml が読めない (`Unreadable`) | **拒否**。既定値に倒さない | 不変条件 1 (読めない ≠ 無い)。ENOENT (mission が無い) は今の `load_mission` (plan.sh:725-733) と同じく呼び出し元のエラー |

拒否はすべて `GitPolicyError(code, field, detail)` (§3) で、pull はこれを §1.2 の B に入れる。
欄の値は理由の文言に入れてよいが、mission.yaml の他の欄は出さない。

### 2.3 branch 名の規則

git の `check-ref-format` を**写さない**。より狭い許可集合を決めて、その中だけを通す。
狭い集合は git の規則の部分集合なので、通したものが git に拒否されることは無い。

- 成分は `/` で区切る。各成分は `[A-Za-z0-9._-]+` で、`.` で始まらず、`.lock` で終わらない。
- `..` と `@{` を含まない。先頭・末尾の `/` と、連続した `/` を含まない。全体で 200 文字以内。
- 置換に入る値も同じ規則で検査する。今の値はすべて許可集合に入る。
  - `{task_slug}` は `_slugify` で `[a-z0-9-]`、40 文字以内 (plan.sh:3482-3487)。
  - `{task_id}` は `tNNN`。
  - `{mission_slug}` は日付 + kebab。

### 2.4 読み口と lint

- **読むのは Resolver だけ**。`load_git_policy(slug)` は mission.yaml を `lib_task_cards.read_regular_text_or_unreadable` で読み、
  `parse_yaml` で解く (不変条件 1。plan.sh の `load_mission` と同じ読み手)。
- plan.sh / git-helpers.sh / lint は欄を自分で読まない (原案 §14-7 の二重実装の禁止)。
- **lint (`lint_plan.py lint_mission` :793)** は 2 つのことをする。
  1. Resolver の `policy_from_text()` を呼び、`GitPolicyError` を FAIL にする。lint が `hooks/lib_skill_perms.check_permission()` を
     そのまま呼んでいるのと同じ作法 (lint_plan.py の「判定を書き写さない」の段)。
  2. lint はもう 1 つ、本物の YAML パーサの読み口 (`_load_yaml_document` lint_plan.py:369) を持っている。これで `git:` の subtree も
     読み、**Resolver の読みと食い違えば FAIL** にする。parse_yaml の黙った読み飛ばしを、2 つの読み手の突き合わせで拾う
     (§2.2 の行数検査と二重の網)。
- `save_mission` (plan.sh:736) は `git:` を今の `dump_yaml` で書き戻す。mapping は `_dump_kv` の dict の枝、`{` を含む値は
  `_NEEDS_QUOTE` で引用符付きになる (lib_state_store.py:534-537・:547)。**G2 の単体テストに「`git:` 付きの mission.yaml を
  `write_mission` で書き戻してもバイトが変わらない」を入れる** (原案 §14-17 の unknown field の削除の禁止)。

---

## 3. Resolver の API (GIT-03 / G2 / t008)

モジュール: `scripts/lib_git_policy.py`。

- **判断だけ**を持ち、git も gh も呼ばない (subprocess なし)。
- 観測 (`git show-ref` / `git worktree list`) と副作用 (fetch / worktree add) は呼び出し元が持つ。
- 例外は 1 つ、CLI の層が観測を 1 つだけする (下)。

```python
DEFAULT_MODE = "direct"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_PR_BASE = "main"
DEFAULT_TASK_BRANCH_PATTERN = "task/{mission_slug}/{task_id}-{task_slug}"
DEFAULT_WORKTREE_ROOT = ".claude/worktrees"
REMOTE = "origin"            # remote 名は policy にしない (§4: CI・skill-permissions・sync が origin 前提)

@dataclass(frozen=True)
class GitPolicy:
    mode: str
    base_branch: str
    pr_base: str
    task_branch_pattern: str
    worktree_root: str
    source: str              # "default" | "mission"

class GitPolicyError(Exception):  # .code ∈ {malformed, unknown_key, type, invalid_value,
    ...                           #           unsupported_mode, unsupported_value, unreadable}

def policy_from_text(text: str, source: str) -> GitPolicy           # 純関数。lint と load の共通の芯
def load_git_policy(slug: str, *, queue_dir: str) -> GitPolicy      # read_regular_text_or_unreadable → policy_from_text
def task_branch(policy, *, mission_slug, task_id, task_slug) -> str
def task_worktree_path(policy, *, repo_root, mission_slug, task_id, task_slug) -> str   # 絶対パス。repo_root の外に出ないことを検査
def task_base(policy, *, remote_tracking_exists: bool) -> TaskBase  # TaskBase(ref="origin/main", fallback=False) / ("main", True)
def pr_base(policy) -> str
```

- 原案の `get_task_base(policy, repository_state)` は、`repository_state` を `remote_tracking_exists` の 1 つの bool にした。
  今の判断が使っている観測は `refs/remotes/origin/<base_branch>` があるかどうかだけ (git-helpers.sh:75)。
  受け取る観測を必要な分だけにすると、テストで観測を偽装しやすい。
- **CLI** (`python3 scripts/lib_git_policy.py <verb> ...`。bash の呼び出し元向け。出力は 1 行の JSON):

  | verb | 引数 | 出力 |
  |---|---|---|
  | `resolve-task` | `--queue <dir> --mission <slug> --task <tid> --task-slug <s> --repo-root <dir>` | `{"branch", "worktree_path", "base_remote": "origin/main", "base_local": "main", "pr_base"}` |
  | `pr-base` | `--queue <dir> --mission <slug>` | `{"pr_base"}` |

  - 拒否は exit 2 で、stderr に `code` と `detail` を出す。
  - `remote_tracking_exists` の観測は CLI でもしない。両方の候補 (`base_remote` / `base_local`) を返し、選ぶのは呼び出し元の
    `git show-ref` (今の :75 の位置)。選ぶ規則 (remote があれば remote) は `task_base()` の docstring と単体テストで固定する。
    bash 側の 1 行の `if` が規則のコピーになるので、構造ガード (§6) はこの 1 か所だけを理由付きで許可する。
- **fetch の失敗 (GIT-04)**: task の base は**今どおり警告して続行**する (W6)。改訂案 §4-R4 の「どちらに倒すか 1 つ決める」への答え:
  - task branch の base が少し古いことの害は小さい。PR は GitHub 上で base と突き合わされ、CI もそこで走る。
    止めると、ネットワークが落ちている間は 1 つも pull できない。
  - `sync-main-checkout.sh` の fetch 失敗が致命 (:100-102) なのは、別の判断として正しい。あのスクリプトの仕事は
    「主 checkout を最新にした」と言うことで、fetch できないのに成功を返すと嘘になる。
  - だから 2 つを 1 つの規則に揃えない。sync は Resolver の対象外 (§4)。
- **bash からの呼び方 (G3)**: `crewvia_create_worktree` は 3 引数の署名を保つ (`test_handoff_path.sh:67` と plan.sh:3499 が呼ぶ)。
  関数の中で `resolve-task` を 1 回呼び、branch / path / base の候補を受け取る。:62・:65・:74-78 のリテラルを消す。
  §1.3 の判定はそのまま bash に残る。
- plan.sh は Python なので CLI を通さず `import lib_git_policy` する (lib は普通に import する。scripts/CLAUDE.md「書き込みの入口」の注意)。
  pull は `pr_base(policy)` を `.crewvia-env` に書く (§5)。
- **G2 は呼び出し元ゼロ**で merge する (R2)。G2 の時点で `grep -rn lib_git_policy scripts hooks` に出るのは lib 自身とテストだけ。

---

## 4. Git の判断の全一覧 (main 48ca12e。G3 / G4 の作業表)

列の意味:
- **寄せる** — 判断を Resolver の出力に置き換える (段を書く)。
- **寄せない** — 理由つきで今のまま。構造ガードでは allowlist に載る。

### 4.1 コード

| 場所 | 決めていること | 扱い | 理由 / 段 |
|---|---|---|---|
| `scripts/git-helpers.sh:62` | task branch 名 | **寄せる (G3)** | Resolver の `task_branch` |
| `scripts/git-helpers.sh:65` | worktree path | **寄せる (G3)** | `task_worktree_path` |
| `scripts/git-helpers.sh:67-70` | path がある時の扱い | G1 で W2〜W4 に置き換える | 観測なので bash に残る (§1.3) |
| `scripts/git-helpers.sh:72` | fetch の失敗を許す | 寄せない (動作として残す) | GIT-04。fetch は副作用で Resolver の外 (§3) |
| `scripts/git-helpers.sh:74-78` | base = `origin/main`、無ければ `main` | **寄せる (G3)** | 候補は `resolve-task`、選ぶ 1 行だけ残す (§3) |
| `scripts/git-helpers.sh:82-86` | 既存の branch は base 無視で再利用 | 寄せない (動作として残す) | GIT-04 の現状維持。判断ではなく観測に従う分岐 |
| `scripts/git-helpers.sh:106` | remove の path (同じ式) | **寄せる (G3)** | 呼び出し元ゼロだが、式のコピーなので Resolver から取る |
| `scripts/git-helpers.sh:117-135` `crewvia_create_pr` (`--base main` :131) | PR base | **寄せる (G3)** | 呼び出し元ゼロ。消さずに `pr-base` を使う。消す案は API を変えるので範囲外 |
| `scripts/plan.sh:3482-3491` `_slugify` | branch / path の成分 | 寄せない | task_slug は task の属性で policy ではない。pattern の `{task_slug}` に入る値として Resolver が検査する (§2.3) |
| `scripts/plan.sh:3495-3527` | worktree を作るか (`target_dir` / git-helpers の有無)・失敗の扱い | G1 で §1.2 / §1.4 | `target_dir` で分ける部分は範囲外の境界なので残す |
| `scripts/plan.sh:3511-3515` `.crewvia-env` | Worker に渡す値 | **G3 で `CREWVIA_PR_BASE` を足す** (§5) | |
| `scripts/sync-main-checkout.sh:99-154` | 主 checkout を `origin/main` に ff | **寄せない** | 本番 crewvia を更新する手順で、task の policy ではない。crewvia の本番 branch は mission ごとに変わらない |
| `scripts/lib_daemon_watch.py:393` `fetch_origin(branch="main")`・`:410` `commits_behind(ref="origin/main")`・`:426` `changed_files_vs` | 主 checkout の drift | **寄せない** | 同上 (dispatcher.sh:3048-3130 `check_main_checkout_drift` が使う) |
| `scripts/kai-review.sh:307-313` diff base (`fetch origin main` → `...HEAD`) | review の diff の base | **寄せる (G3)**: `lib_git_policy.py pr-base --mission "$MISSION_SLUG"` の値を fetch する | kai-review は mission を知っている (:82・:130-140 で pull の前に解決して保持)。PR の実 base (`gh pr view --json baseRefName`) を使う案は、stacked PR のレビューの意味を変えるので範囲外 (§10) |
| `scripts/kai-review.sh:290-293` review 用 worktree (`mktemp -d`・detached) | 一時 worktree の path | 寄せない | task の worktree ではない。使い捨てで、PR head を detached で見るだけ |
| `scripts/worktree_gc.py:324・:682・:754` `.claude/worktrees` | 片付けの根 | **G3 で `lib_git_policy.DEFAULT_WORKTREE_ROOT` を import** | 値を 1 か所に置く。worktree_root は既定値に固定なので、定数の共有で足りる (§2.1) |
| `scripts/worktree_gc.py:163` `.quarantine` | 隔離先 | 寄せない | gc 自身の規則 |
| `hooks/pre-tool-use.sh:245`・`hooks/lib_main_repo_git_guard.py:185` `.claude/worktrees` | 編集ガード・git ガードの除外 | **寄せない (G3 は理由つきで allowlist)** | hook は tool 呼び出しのたびに走る。worktree_root を既定値に固定している (§2.1) 間は値が一致する。広げるときに寄せる |
| `config/skill-permissions.yaml:49-50` `git push origin main*` / `master*` の deny | 保護 branch | **寄せない** | 安全側の deny で、policy が何であっても main への push は止める。`base_branch` を別の値にした mission の保護は範囲外 (§10) |
| `.github/workflows/ci.yml:5・:7` `branches: [main]` | CI の対象 | 寄せない | リポジトリの設定。mission ごとに変えない |
| `scripts/start.sh:403-409` `WORK_DIR` | Worker の起動場所 | 寄せない | 起動時点では task が無い。cwd の切り替えは pull の JSON (G1) |
| `scripts/start.sh:948` kickoff 文言 | worktree への cd の指示 | G1 で文言を確認 (exit 1 の手当て) | |
| `scripts/dispatcher.sh` | branch / worktree を作らない | 対象外 | |
| `scripts/lint_plan.py:524-528` `_PUSH_TOOL_SIG` (`git push origin task-branch`) | push の権限を試す signature | 寄せない | 権限の検査で、branch を決めていない |

### 4.2 エージェント向けの文書 (G4 / t016)

| 場所 | 書いてあること | 扱い |
|---|---|---|
| `agents/worker.md:584`・`:922` | `gh pr create ... --base main` | **書き換える** → `--base "${CREWVIA_PR_BASE:-main}"` (§5) |
| `agents/worker.md:937-945` | stacked PR の判定「base が main / master 以外」・`gh pr edit {子PR} --base main` | **書き換える** → `${CREWVIA_PR_BASE:-main}` |
| `agents/worker.md:148-152`・`:865-879` | worktree path と branch の形 | **文言を変える**: 「既定では」と添え、決めるのは pull で、JSON の `worktree_path` と `git branch --show-current` を見る、と書く。形の説明は残す |
| `agents/worker.md:294-301` | `worktree_path` が空なら cd しない | G1 で「空になるのは target_dir の task だけ」を書く |
| `agents/director.md:884-900` | branch / worktree の命名 | 同上 (既定値として書き、mission.yaml の `git:` で変わると足す。§2 の G3 以降の注意も) |
| `agents/director.md:921-935` | stacked PR の `gh pr edit --base main` | **書き換える** |
| `agents/director.md:962-1002` | 主 checkout の同期 | 寄せない (§4.1 sync と同じ) |
| `agents/director.md:1156-1163` | `git log origin/task/...` | 寄せない (調査の例) |
| `agents/worker-codex.md:122-125` | 「origin/main との diff」 | **書き換える** → 「mission の PR base (`lib_git_policy.py pr-base`) との diff」(kai-review.sh の G3 の変更に合わせる) |
| `skills/crewvia-qa/SKILL.md:17`・`:148` | `git diff main...HEAD` | **書き換える** → `git diff "origin/${CREWVIA_PR_BASE:-main}...HEAD"`。local `main` は主 checkout と ref を共有していて古くなる (kai-review.sh:296-306 が実測した理由と同じ)。`origin/<base>` は pull の fetch (git-helpers.sh:72) で更新される |
| `skills/crewvia-plan-review/SKILL.md:127-129` | PR head の checkout / push | 寄せない (head は PR の属性) |
| `agents/verifier.md:64` | push の禁止 | 対象外 |
| `knowledge/*.md` の `origin/main` | 戻し方 (`git merge --ff-only origin/main`)・経緯・実測 | **書き換えない**。主 checkout の同期 (寄せない側) か、記録。構造ガードの対象外 (§6) |

---

## 5. PR base を Worker に渡す方法 (G3 / G4)

| 決定 | 内容 |
|---|---|
| 渡す変数 | `.crewvia-env` に `export CREWVIA_PR_BASE=<pr_base>` を 1 行足す (G3)。`CREWVIA_BASE_BRANCH` は出さない: Worker が base を使う場面は無い (branch は pull が作る)。必要になった時点で足す |
| 書くとき | pull が毎回原子的に書く (`_STORE.atomic_write_text`)。**W2 の再利用でも書き直す** (§1.3)。再 pull で古い値が残らない |
| 文書の書き方 | Worker の Bash は**呼び出しごとに env が消える** (Claude Code の Bash tool は cwd だけを持ち越す)。だから「最初に 1 回 source」では効かない。PR を作るコードブロックの先頭で、その都度読む: `[ -f .crewvia-env ] && . ./.crewvia-env` → `gh pr create ... --base "${CREWVIA_PR_BASE:-main}"` |
| `-f` で確かめる理由 | `source .crewvia-env && gh pr create ...` の形は、TARGET_DIR の Worker (`.crewvia-env` が無い) で PR を作らずに止まる |
| 既定値 `:-main` | env が無い 3 つの場合に、今と同じ `main` になる: ① TARGET_DIR の Worker (範囲外。今も main)、② G3 より前に作られた worktree の `.crewvia-env` (この行が無い)、③ cwd が主 checkout に戻ってしまった shell (`.crewvia-env` は gitignore 済み .gitignore:34 で、主 checkout には無い)。②③ で既定値に落ちても、G3 の cutover を確認するまで `git:` を書かない (§2.1) ので、そのときの policy は必ず `main` になり、食い違わない |
| `:?` (無ければ止める) を採らない理由 | ① の TARGET_DIR の Worker を止めてしまう。範囲外の挙動を変えることになる |
| 書き換える範囲 | §4.2 の「書き換える」行だけ。knowledge/ は対象外 |
| G4 の QA (t022) | 3 つの shell で、書き換えた全コードブロックを実際に評価する: env あり (custom `pr_base: develop` の隔離 mission) / env なし (G3 前の形の `.crewvia-env`) / TARGET_DIR (`.crewvia-env` なし)。`gh` は stub にして、`--base` に渡った値を記録する |

---

## 6. 構造ガード (改訂案 §4-R6。G3 / t012)

「Resolver の外に branch / base / worktree root の判断が増えたら CI が赤」。

- 置き場所: `tests/test_git_decisions_go_through_policy.py`。
- 形は 01a の `tests/test_queue_writes_go_through_the_store.py` と同じ (state-store.md §6)。

| 項目 | 決定 |
|---|---|
| 向き | **allowlist**。表は `(ファイル, 関数) → (件数, 理由)`。鍵に断片の文字列を使わない (memory `write-guard-allowlist-key-is-function-and-count`) |
| 対象 (コード) | glob で決める: `scripts/*.py`・`scripts/*.sh`・`scripts/bin/*`・`hooks/*.sh`・`hooks/*.py`・トップの `crewvia`・`crewvia-stop`。除外は理由つきで列挙する (テスト・`lib_git_policy.py` 自身) |
| 対象 (文書) | `agents/*.md`・`skills/*/SKILL.md` の **fenced code block の中だけ**。`knowledge/` と本文の地の文は対象外。地の文まで見ると、ガードを説明する文がガードに掛かる (memory `red-proof-for-a-text-pattern-guard-trips-itself`)。knowledge/ は主 checkout の同期の手順と記録が大半を占める (§4.2) |
| Python で拾う形 | AST の文字列定数 (`ast.Constant` と `JoinedStr` の各片) で、`origin/main`・`refs/remotes/origin/main`・`refs/heads/main`・`.claude/worktrees` (と `'.claude', 'worktrees'` の連続した 2 定数)・`task/` で始まり `{` を含むもの、を含むもの。**関数の既定値** (`ref: str = "origin/main"` lib_daemon_watch.py:410 の形) と、**`"main"` に完全一致する定数** (`branch="main"` :393 の形) も拾う。docstring (関数・モジュールの先頭の `Expr`) は除く |
| bash で拾う形 | 01a の字句分割器 `tests/queue_write_scan.py` (t035 で、コメント中の `<<X` で残りを読み飛ばす盲点を直した版) で、コメントと heredoc の本文を分ける。コードの行で `origin/main`・`--base[ =]+main\b`・`\bmain\.\.\.`・`refs/(heads\|remotes/origin)/main`・`\.claude/worktrees`・`task/\$` を拾う。heredoc の本文が Python (`<<'PYEOF'`) なら、Python の規則で全ブロックを見る |
| 実際の呼び出し形で陽性対照 | 本物のコードから切り出した形を合成ソースに入れ、各形を拾うことをパラメタライズで固定する (memory `structural-guard-must-match-real-call-shape`): `local base="origin/main"` (git-helpers.sh:74)・複数行の `gh pr create \` の末尾の `  --base main` (git-helpers.sh:131)・`"main:${BASE_FETCH_LOCAL_REF}"` (kai-review.sh:309 — `main:` の形)・`def fetch_origin(repo_root, *, remote: str = "origin", branch: str = "main",` (lib_daemon_watch.py:393)・`os.path.join(self.repo, '.claude', 'worktrees')` (worktree_gc.py:324)・`"${_GUARD_REPO_REAL}"/.claude/worktrees/*)` (pre-tool-use.sh:245 — case パターン)・文書の `git diff main...HEAD --name-only`。さらに `# <<EOF` を含むコメントの**後**にあるリテラルも拾うこと (t035 の盲点の再発防止) |
| 初期の allowlist | §4.1 の「寄せない」行: sync-main-checkout.sh・lib_daemon_watch.py・dispatcher.sh `check_main_checkout_drift`・hooks の 2 か所・kai-review.sh の mktemp の行・git-helpers.sh の base を選ぶ 1 行 (§3)。§4.2 の「寄せない」行の code block |
| 件数と空虚な PASS | (1) 走査したファイル数・code block 数・拾った件数を出し、**下限を assert する**。下限は G3 が実測した値に置く。(2) allowlist の死んだ行を落とすテスト。(3) worktree で走らせても対象が 0 件にならない |
| 件数の可視化 | 01a backlog 2 (成功したテストの print が CI ログに出ない) をここで一緒に直す。`tests/conftest.py` の `pytest_terminal_summary` に、2 つのガード (書き込み・Git) の件数を出す。成功しても CI ログに残る |
| 赤の実証 | G3 の PR 説明に 2 つの実行結果を載せる (memory `regression-test-must-prove-red`): `git-helpers.sh` に `--base main` を 1 行戻すと赤、`worker.md` の code block に `--base main` を戻すと赤 |

---

## 7. cutover と rollback (R2)

| PR | 本番で変わること | merge 前 | merge 後に Director が本番で確かめること | 戻し方 |
|---|---|---|---|---|
| G1 (t004) | worktree を作れない pull が exit 1 + needs_director になる (今は成功・主 checkout)。登録済みで branch が一致する path は再利用する。再 pull で `.crewvia-env` が書き直される。成功時も git の警告が stderr に出る | **ユーザー承認** (t007) | **実際の Worker の pull で worktree と `.crewvia-env` ができる** (通常の割り当て 1 件。JSON の `worktree_path` が非 null・dir がある・`.crewvia-env` を source でき 3 変数が出る・`git -C <wt> branch --show-current` が task branch)。再利用の経路を 1 件観察する (`update --reset` で戻した task の再 pull。observation 用 mission は `init --inactive`。memory `disposable-mission-init-exposes-to-dispatcher-immediately`)。監査ログに `op=pull to=needs_director` が**出ていない** (出ていれば 1 件ずつ理由を読む) | PR revert → `scripts/sync-main-checkout.sh`。G1 は新しい status も欄も書かない (`needs_director` と `needs_director_reason` は既存のもの)。戻しても旧コードがそのまま読める。needs_director になった card は Director が今の出口で戻す |
| G2 (t008) | **なし** (呼び出し元ゼロ) | 通常 merge | `grep -rn lib_git_policy scripts hooks` が lib 自身とテストだけ | revert |
| G3 (t012) | branch / path / base の式が Resolver から来る (既定値では同じバイト)。`.crewvia-env` に `CREWVIA_PR_BASE` が増える。kai-review の diff base が `pr-base` 経由 (既定値では `main`)。lint が `git:` を検査する。CI に構造ガード | **ユーザー承認** (t015) | 実際の Worker の pull で、作られた branch 名と path が G3 前の式の値と同じ (同じ mission の G3 前の worktree と並べる)。`.crewvia-env` に `CREWVIA_PR_BASE=main`。次の codex-review が通常どおり diff を取る (kai-review のログの `Computing diff`)。active mission 全部の `plan.sh lint` が rc=0 (`git:` を書いた mission は 0 件のはず) | revert → sync-main-checkout。`.crewvia-env` の余分な行は旧 Worker 文書が読まないので害が無い。**`git:` を書いた mission があるなら revert の前に消す** (旧 plan.sh は黙って無視するので、custom の base が効かなくなる。§2.1) |
| G4 (t016) | エージェント向け文書の `--base` と diff base が env 参照になる (既定値では同じ `main` / `origin/main`) | **ユーザー承認** (t018) | 次に PR を作った Worker の PR の base が `main` (`gh pr view --json baseRefName`)。QA Worker の diff が `origin/main...HEAD` で取れている | revert。文書なので restart は不要。**動いている Worker のセッションには、起動時に読み込んだ版が残る** (次の起動から入れ替わる) |

共通:
- **merge 後に主 checkout を ff するまで、本番は旧コードのまま** (memory `main-checkout-lags-after-pr-merge`)。Director の確認は、
  `git -C <主 checkout> log -1` が merge commit であることから始める。
- G1 と G3 が触るのは plan.sh / git-helpers.sh / kai-review.sh / worktree_gc.py / lint_plan.py で、どれもデーモンではない。
  plan.sh は呼び出しごとに新しいプロセスになる。kai-review は spawn ごとに読む。
  dispatcher が import するもの (lib_daemon_watch 等) は触らない (§4.1 で寄せない)。**デーモンの restart は要らない**。
  ただし G3 で dispatcher.sh / watchdog.py が lib_git_policy を import するように変えるなら、`DAEMON_RESTART_FILES` に足して
  sync-main-checkout の restart 判定に乗せる (01a backlog 10 と同じ注意)。
- **custom の `git:` を本番の mission に書くのは、G3 の本番確認 (t019) の後**。それまでは既定値の互換だけを観察する。
- env の停止スイッチは付けない (不変条件 5)。戻すときは常に revert。

---

## 8. 禁止事項・不変条件・01a の規則との整合 (確認表)

| 原案 §14 / 不変条件 / 01a | この設計 |
|---|---|
| §14-1 主 checkout での実装 | 全 PR は worktree で作る。本番確認 (t019) は通常運用の観察と読み取りだけ |
| §14-2 稼働中の queue へのテスト書き込み | G1 のテストは `CREWVIA_QUEUE` を付け替えた隔離 queue と、使い捨ての git repo (clone) の上で行う。本番の `.claude/worktrees` に作らない (§9) |
| §14-3・4 hot reload / 自動 cutover | merge + ユーザー承認 + sync-main-checkout (§7)。デーモンの restart は無し |
| §14-6 Dispatcher に遷移の authority | dispatcher は書かないまま。needs_director は pull が書く |
| §14-7 同じ規則の二重実装 | branch / path / base の式は lib_git_policy の 1 か所。lint も Resolver を呼ぶ。needs_director の遷移は cmd_needs_director と共有する。コピーは構造ガードで落とす (§6) |
| §14-10・11 Agent 単位 / 試行ごとの branch | pattern に `{mission_slug}` と `{task_id}` を必須にし、`{agent}` を置換子に持たない (§2.1) |
| §14-12 Git 状態から Task 状態を推測して確定 | しない。W2 の再利用は「同じ task の branch」を見るだけで、card は書かない。B の needs_director は**pull が自分で起こした失敗**の記録で、git の状態から完了・失敗を推測していない |
| §14-13 worktree の失敗で主 checkout に fallback | G1 で塞ぐ (§1.4)。残る null は N1 (target_dir。原案 GIT-05 の例外) と N2 (テストの継ぎ目。本番では git の追跡を CI で固定) |
| §14-15 lock 内の network / subprocess | worktree 作成・fetch はロックの外のまま。2 つ目のロックは card の読み直しと書き込みだけ |
| §14-16 silent 修復 / silent fallback | 未知の mode・未知の欄・読めない mission.yaml・既定値以外の worktree_root を拒否する (§2.2)。fetch の失敗と local main への fallback は GIT-04 で**残す**動作だが、成功時も stderr に出す (§1.5) |
| §14-17 unknown field の削除 | `git:` は `dump_yaml` で往復してバイトが変わらないことをテストで固定する (§2.4) |
| §14-20 実 remote への push / PR | テストは `origin` を一時 bare repo に向ける。`gh` は stub |
| 不変条件 1 (読み取りは lib_task_cards) | Resolver は `read_regular_text_or_unreadable` + `parse_yaml`。`Unreadable` は拒否 |
| 不変条件 2 (識別子はファイル名) | `{task_id}` には card のファイル名から来る id を入れる (pull の `meta['id']` は `normalize_card` がファイル名と照合済み) |
| 不変条件 3 (依存は lib_dep_rules) | 触らない。B は `failed` を書かないので、依存の保留も起きない |
| 不変条件 4 (デーモンの再起動) | restart 不要 (§7)。必要になれば sync-main-checkout 経由 |
| 不変条件 5 (env の停止スイッチなし) | 付けない。N2 の継ぎ目は「ファイルが無い」で、env ではない |
| 不変条件 6・7 | 触らない |
| 01a: plan.sh は `with_lock` の中だけで queue を書く | B の書き込みは 2 つ目の `with_lock` の中 |
| 01a: ロック取得の直後に `recover_before` | B の 2 つ目のロックも呼ぶ (§1.2) |
| 01a: 正本を先に書き、projection を後で消す | B は card → assignment の順 (§1.6) |
| 01a: 「済んだが耐久性だけ失敗」は続行 | `.crewvia-env` の耐久性だけの失敗は警告して続行。それ以外は B (§1.4 N7) |
| 01a: 監査ログに本文・理由を出さない | 理由は card にだけ書く (§1.5) |

---

## 9. テスト観点 (QA task への申し送り)

- **G1 (t005)**:
  - 使い捨ての git repo (bare の `origin` + clone) と隔離 queue で、§1.3 の W0〜W7 を 1 行ずつ作る。
    - W2 で `worktree_path` が同じ path になり、`.crewvia-env` が書き直される (mtime と中身)。
    - W3・W4・W5 で exit 1・stdout が空・card が needs_director・assignment なし。
    - W6・W7 で成功し、stderr に警告がある。
  - N6 (stub の git-helpers が rc=0 で stdout を空にする) と N7 (`.crewvia-env` の親を書けなくする) が B になる。
  - **無限ループにならない**: W4 の状態のまま dispatcher を 1 サイクル回し、同じ task が送られない
    (memory `qa-real-dispatcher-cycle-harness-and-flake` の harness)。
  - 2 つ目のロックの CAS: worktree 作成の間に `update --reset` を差し込んで、pull が何も書かない。
  - §1.6 の「needs_director を書いた後・枠の撤去の前」で落とし、次のロックで R-2 が枠を消す。
  - 欠陥版 (B を元の WARNING に戻す) で、主 checkout に残る形が赤になる。
  - N2 の継ぎ目の既存テストが緑のまま。
- **G2 (t009)**:
  - 原案 §10.4 の各行を単体テストにする。
  - 既定値の branch / path が git-helpers.sh の式と同じ (G2 の時点の main の式と照合)。
  - §2.2 の拒否の各行 (4 字下げ・未知の欄・型・空・制御文字・`..`・絶対パス・`integration`・`Unreadable`)。
  - `dump_yaml` で往復してもバイトが変わらない。
  - 呼び出し元がゼロ。
- **G3 (t013)**:
  - 既存の mission (`git:` なし) で、G3 の前と後の branch と path がバイト単位で一致する。
  - custom の `base_branch` / `pr_base` を書いた隔離 mission で、base と `.crewvia-env` に反映される。
  - 構造ガードの陽性対照・件数の下限・赤の実証・死んだ allowlist 行。
  - lint が parse_yaml と本物の YAML パーサの食い違いを FAIL にする。
- **G4 (t022)**: §5 の 3 つの shell。書き換えていない決め打ちが文書に残っていない (構造ガードの文書側が緑で、件数が 0 でない)。

---

## 10. 範囲外・backlog (01b の完了を止めない)

1. **title を変えると task branch が変わる** (§1.3)。task_slug を pull ごとに title から作り直さず、card に固定する案がある。
   card の欄を足すので、01c の Execution の議論と一緒に扱う。
2. **pull の冪等化** (N8: pull が途中で死ぬと、同じ Worker は同じ task を取り直せない)。01c。
3. **stacked PR の review の diff base**。kai-review は G3 で `pr_base` を使う。しかし stacked PR の実 base は親の branch なので、
   diff に親の変更が混ざる (今と同じ)。PR の `baseRefName` を使うかどうかは、review の意味を決める話なので別に扱う。
4. **`base_branch` を `main` 以外にした mission の保護** (`config/skill-permissions.yaml` の deny は main / master だけ)。
   custom の base を本番で使い始める前に決める。
5. **worktree_root を既定値以外に広げる**: hooks の 2 か所と worktree_gc を Resolver 経由にしてから。
6. **fetch のタイムアウト**: git-helpers.sh:72 の `git fetch origin` に上限が無い。認証のプロンプトで止まると pull が戻らない
   (ロックの外なので他は止まらない)。`GIT_TERMINAL_PROMPT=0` と上限を付けるかを、実測してから決める。
7. **TARGET_DIR の task の branch / base**: 原案どおり後続。
