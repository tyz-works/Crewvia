# テストの隔離 — registry を書かない・worktree で素通りしない・fixture の lib コピー (t013 / PR4)

> 対象コード: `scripts/plan.sh` の `registry_dir()`、`tests/test_registry_isolation.py`、
> `scripts/test_registry_lock.sh` の静的検査、`tests/fixture_tree.py` / `tests/fixture_tree.sh`、
> `tests/test_fixture_tree_is_the_only_copier.py`

mission `20260926-mechanize-guards-a` の PR4。2026-09-26 に確認した本番汚染と、3 回再発した
fixture 破損を、規則ではなく**構造**で止めるための 3 つの変更をまとめる。

## 1. `plan.sh done` は queue の隣の registry だけを書く

### 何が起きていたか

`plan.sh done` の task_count 加算は `CREWVIA_REPO_ROOT`（無ければ plan.sh の位置）の
`registry/workers.yaml` を書いていた。registry の**読み取り**（`_load_workers_from_registry` /
`registered_worker`）は `dirname(QUEUE_DIR)/registry`。**読みと書きで registry が違った**ので、
`CREWVIA_QUEUE` だけを一時ディレクトリに向けたテスト（`tests/plan-assignment-identity.bats` ほか）は
queue だけ隔離したつもりで本番の task_count を加算し続けた。本番の Ren は 7 → 402 になっていた
（この PR は汚染そのものは直さない。値を戻すのは別 PR）。

### 今どうなっているか

`registry_dir()`（`scripts/plan.sh`）が registry の場所の**唯一の定義**で、`dirname(QUEUE_DIR)/registry`
を返す。読み取りと task_count 加算の両方がこれを呼ぶ。

- 本番: start.sh が `CREWVIA_QUEUE=$CREWVIA_REPO_ROOT/queue` を必ず export するので、queue の隣 =
  `$CREWVIA_REPO_ROOT/registry`。**Worker が worktree から `done` しても** `CREWVIA_QUEUE` は本番 queue を
  指すので、本番の registry に書かれる（これは正しい挙動で、テストで固定している）。
- 隔離: `CREWVIA_QUEUE` を付け替えた実行は registry も一緒に付け替わる。plan.sh の位置の registry には
  1 バイトも触れない。
- `lib_registry.py`（コード）は書き込み先ではないので、この plan.sh 自身の checkout のものを使う。
- **同じ型が他に 2 つあった**（構造テストが最初の点検で拾った）: `update --reset` が古い handoff を
  `.stale-<UTC>` に**改名する**書き込み（`_set_aside_stale_handoff`）と、pull の退役 marker の読み取り
  （`retirement_reservation` / `retirement_pid_dead` 相当）も `CREWVIA_REPO_ROOT` / plan.sh の位置の registry を
  見ていた。前者は隔離実行が別の registry の handoff を改名し、後者は本番の marker でテストの pull が
  拒否される。どちらも `registry_dir()` に揃えた。`task-graph` の生成物だけは
  `task_graph_queue_matches_root()` が「queue が root の queue のときだけ」書く別のガードを持つので触っていない。

### 構造テスト

`tests/test_registry_isolation.py`:

- plan.sh の位置の `registry/`（本番の代役）と、別の場所の queue を用意し、`CREWVIA_QUEUE` だけを
  そちらへ向けて **plan.sh の全 subcommand** を走らせ、位置の `registry/` が中身・ファイルの有無・mtime
  とも変わっていないことを assert（`CREWVIA_REPO_ROOT` を位置に向けた変種も）。
- subcommand の一覧は plan.sh 末尾の dispatch テーブルから**実装から**拾う。新しい subcommand を足したら、
  テストの `ISOLATED_INVOCATIONS` に呼び出しを足さないと赤（足し忘れが検出できる）。
- 陽性対照: `done` が queue の隣の registry を実際に加算する／worktree からの `done` が本番側に着く。

## 2. `test_registry_lock.sh` の静的検査が worktree で素通りしていた

除外判定 `".claude" in path.parts and "worktrees" in path.parts` は**絶対パス**の parts を見ていた。
root 自身が worktree（`<repo>/.claude/worktrees/<mission>/<task>/`）のとき、**全ファイルが除外に当たり**、
検査対象が 0 件のまま必ず PASS した。Worker は worktree で作業するので、迂回の検出はいちばん必要な
場所で無効だった。

修正は root からの**相対パス**（`path.relative_to(root).parts`）で判定すること（除外したいのは root の
下に入れ子になった別の worktree だけ）。あわせて「検査したファイル数」を出し、**50 件未満なら FAIL**
にした（「何も見ていない」を「迂回が見つからなかった」と読まない）。修正で検査が有効になった結果、
新規テスト `tests/test_registry_isolation.py`（使い捨てツリーに registry を組み立てる）が近接ヒューリスティクスに
当たったので、理由付きで除外リストに入れた。

## 3. plan.sh / lib の隔離コピーは helper だけ

plan.sh などを一時ディレクトリへ写す fixture が「要ると思った lib」だけを名前で列挙していると、lib を足すたびに
書き換え忘れた fixture が **CI でだけ**赤くなる（手元では主 checkout の scripts/ が見える）。3 回再発した。

- 入口は 2 つ: `tests/fixture_tree.py`（`copy_plan_tree(root)`）と `tests/fixture_tree.sh`
  （`copy_plan_tree` / `copy_scripts_libs`）。`lib_*` を **glob で**まとめて写す — 新しい lib は何もしなくても
  次のコピーから付いてくる。
- 意図して写さないもの: `git-helpers.sh`（plan.sh は「その有無」で pull 時の worktree 自動作成を決めるので、
  置くとテストの外側に worktree を作る）、`review-plan.sh`（claude を起動する）。必要なテストが自分で置く。
- 構造テスト `tests/test_fixture_tree_is_the_only_copier.py`: tests/ と scripts/test_*.sh の中で
  plan.sh / `lib_*` を**名前を指して**コピーしている箇所を Python は AST、shell / bats は論理行で拾い、
  helper を通っていなければ赤。許容リスト `ALLOWED` の各行は理由を持ち、該当が無くなったら（直したのに残った）赤。
- 新しい fixture を書くときは、自分で `cp` / `shutil.copy` を書かずに helper を呼ぶ。lib を上書きする
  スタブ（`lib_verdict.py` など）は、helper の**後**に置く。

## 戻し方

失敗すると全 mission の割り当て・生存監視が止まる種類の変更（plan.sh は dispatcher・watchdog・Worker が
毎回呼ぶ）なので、戻し手順を残す。共有規則なので env の停止スイッチは付けていない
（dispatcher と plan.sh で答えが割れる。`knowledge/failed-dependency-hold.md` と同じ理由）。

PR を revert → 主 checkout（`/home/tkadmin/workspace/crewvia`）で `git merge --ff-only origin/main`
→ `python3 scripts/lib_daemon_watch.py restart dispatcher` と `restart watchdog`（両方。片方だけ戻さない）。
主 checkout は merge 後も古いままなので、ff するまで本番の plan.sh は新しくならない
（memory: main-checkout-lags-after-pr-merge）。revert しても registry の値は戻らない（task_count の汚染は
別 PR が計算で戻す）。fixture 側だけを戻したいときは、helper を使う変更だけを revert すればよい
（plan.sh 本体と独立）。
