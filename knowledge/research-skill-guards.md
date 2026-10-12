# research スキルの構造的ガード (Write の範囲 / Bash の読み取り専用)

> mission 20261012-research-skill-write-scope-and-bash-guard t001。調査の全文:
> `~/obsidian/research/20261011_research-skill-write-scope.md`

## 脅威モデル（t009。ユーザー決定）

- **守るもの**: 正直な research Worker が、習慣やうっかりで成果物（自分の `notes/<tid>*.md`）以外のファイル・git・GitHub に
  書き込むこと。例: `sed -i`、`>` へのリダイレクト、heredoc でのメモ書き、`git commit` / `git push`、`gh pr create`、`plan.sh done "…"`。
- **対象外**: 悪意をもってガードを回避しようとする Worker（シェルの細かな文法・ツールの隠れた書き込み機能・設定由来の外部プログラム
  起動を組み合わせる攻撃）。必要になれば OS レベルのサンドボックス（別ユーザー・読み取り専用 mount 等）で対処する（将来課題）。
- **判定はすべて fail closed**: 解釈できない形・列挙していない形は拒否が基本。対象外の類の穴でも、見つかったものは
  「解釈不要の形へ狭める」方向で塞ぐ（下の「既知の課題」に塞いだ形と残る経路を残す）。

## 何を止めて何を通すか

判定は `hooks/lib_research_guard.py` の 1 か所（`hooks/pre-tool-use.sh` の "Research scope guard" から呼ぶ）。
SKILLS（と task card の skills）に `research` を含むセッションだけが対象。card は env の `TASK_ID` の有無に関係なく `queue/assignments/<AGENT_NAME>` から引く（t005。`TASK_ID` が設定済みでも card の skills を見る）。**判断に迷う形は deny**（fail closed）。
どちらの判定も `TASKVIA_TOKEN` が空でも効く（native へ委ねる分岐より前）。

### 1. Write / Edit / MultiEdit / NotebookEdit

yaml の deny（`config/skill-permissions.yaml` の research）は残す。lint と `plan.sh add` の「research は
deliverable pr/file を作れない」判定がこれに依るため。hook の構造的ガードが yaml より先に評価され、範囲内だけ allow する。

| 書き先 | 判定 |
|---|---|
| `queue/missions/<自分の assignment の slug>/notes/<自分の tid>*.md` | allow（**上書きも可**） |
| 他の task の notes・他の mission の notes | deny |
| リポジトリのコード・task card・mission.yaml | deny |
| `<obsidian_research_dir>/YYYYMMDD_<名前>.md` | `research_write_scope.obsidian_research_dir` を設定したときだけ allow。**新規作成のみ**（既存は deny）。**既定は空 = 無効** |
| `..`・symlink で範囲外に出る形、既存の symlink への書き込み | deny |

slug と tid は `queue/assignments/<AGENT_NAME>`（`<slug>:<tid>`）から引く。assignment が無い・読めない・形が違えば notes は書けない。
obsidian を有効にするには `config/skill-permissions.yaml` の末尾 `research_write_scope.obsidian_research_dir` に絶対パス（`~` 可）を書く
（公開リポジトリなので既定値は空。ローカル変更として持つ）。

### research Worker が done / needs-director する手順

Bash の heredoc（`<<`）は全面的に拒否する（§2）。結果は **Write でメモに書いて、そのパスを `--result-file` に渡す**:

1. Write で `queue/missions/<slug>/notes/<tid>-result.md`（`<tid>` で始まる `.md` なら名前は自由）に Result を書く。
   リポジトリの絶対パス（主 checkout の `queue/`）で書くのが確実。調査の途中のメモも同じ dir に残してよい（上書き可）。
2. `plan.sh done <tid> --mission <slug> --execution ex-… --result-file <1 のパス>`
   （判断を仰ぐときは `plan.sh needs-director <tid> --mission <slug> --execution ex-… --result-file <パス>`。要約はファイルの先頭行に）。

`--result-file` に渡せるのは自分の assignment の `notes/<tid>*.md` だけ（`-`・他の task / mission・card は不可）。
相対パスは hook の cwd で解決する。同じコマンドの前に `cd` があるときは絶対パスで渡す。
位置引数の結果文字列（`plan.sh done t001 "…"`）は通らない。

### 2. Bash（解釈の要らない読み取り専用の形だけ。t005 で狭めた）

PR #299 の初版は bash の文法を部分的に解釈して危ない形を探していたが、Codex review（t003）で同じ型の穴が 6 件出た
（`command rm`・コメント内の heredoc・`awk -v`・出力先を位置引数 / 結合形で取るオプション・`gh api -XDELETE`・`export PATH`）。
1 件ずつ塞ぐのをやめ、**解釈が要らない形だけを通す**側へ狭めた。調査の読み取りが多少不便になるのは許容する
（ファイルを読むのは Read / Grep / Glob ツールで足りる。これらは hook の対象外で今までどおり使える）。

**文字の段階で拒否**（引用符の中も一律）: `$`（変数・コマンド置換すべて）・バッククォート・`#`・`<<`（heredoc と here-string）。

**引用符の外で拒否**: `( ) { } !`（サブシェル・グループ・ブレース展開・否定）・`&`（バックグラウンド・`&>`）。
**リダイレクトは 2 つのトークンだけ**（t007）: 引用符の外にある完全一致の `2>/dev/null` と `2>&1`（直前が空白・直後が区切りか末尾）。
それ以外で `>` か `<` が引用符の外に現れたら拒否（`>/dev/null`・`1>`・`>>`・`< file`・`2> /dev/null` も不可。読むファイルは引数で渡す）。
fd 番号を「溜まった語」から判定しない: `'2'>/dev/null` / `"2">` / `\2>` の 2 は bash では引数として残り、`uniq in '2'>/dev/null` は
cwd に `2` というファイルを作る（Codex 2 巡目の指摘 2）。glob（`* ? [`）は**先頭が `-` でない字そのもの**で始まる語だけ
（`ls queue/missions/*/notes` は可、`ls *.md` は不可 — 展開結果が `-o…` のようなオプションになりうるため）。`--include=*.md` のように
`--名前=` の後ろの glob は可。

**コマンド名**は lib の表（`SPECS` / `GIT_SPECS` / `GH_SPECS` / `PLAN_SPECS`）に完全一致するものだけ（パス指定は `plan.sh` だけ）。
**plan.sh**（t007）: コマンド語を cwd に対して解決し、字面（normpath）と realpath の両方が「本 repo（`CREWVIA_REPO_ROOT`、無ければ
hook の位置 `hooks/..`）の `scripts/plan.sh`」か「自分の worktree（`.claude/worktrees/<自分の slug>/<自分の tid>-*/`）の `scripts/plan.sh`」で
あるときだけ通す。素の `plan` / `plan.sh`（alias・PATH で解決される）・別の場所の plan.sh・`..` を含むパス・symlink 経由は不可
（plan.sh は `BASH_SOURCE` の dirname を論理パスで辿って lib を読むので、信頼する plan.sh への symlink を別の dir に置くと別の lib を
読ませられる。realpath だけでなく字面も見る理由）。**同じコマンドの中に `cd` があれば、その後ろの相対パスの plan.sh は拒否**
（絶対パスなら可）。`cd` の後の cwd は字面から確定できない: パイプの中の `cd` はサブシェルで効かず（`cd <repo> | ./scripts/plan.sh`）、
`false && cd <repo>; …`・`timeout 5 cd <repo>; …` も効かない。追跡すると「repo にいる」と判定したまま元の cwd の plan.sh が走る。
`cd` は別の Bash 呼び出しで先に済ませる（cwd は呼び出しをまたいで残り、hook はその cwd で解決する）。
`export unset alias declare set source . env command builtin exec eval xargs nohup time printf` と先頭の `X=…` は表に無いので拒否
= PATH や LD_PRELOAD を変えて許可コマンドを差し替えられない（`printf` は t009 で外した: `%n` は `-v` 無しでも変数を書く。
`printf 'abc%n' PATH` で PATH が `3` になる。代わりは `echo`）。例外は 2 つ:
- `source ./.crewvia-env`（または `. ./.crewvia-env`・絶対パス）（t009）: **コマンドの先頭の区間**で、引数はパス 1 つだけ。
  パスを cwd に対して解決し、字面と realpath の両方が「自分の worktree（`.claude/worktrees/<自分の slug>/<自分の tid>[-*]/.crewvia-env`）」か
  「本 repo の `.crewvia-env`」であること（plan.sh と同じ判定 `_is_trusted_path`。symlink・`..`・他の task / mission の worktree は不可）。
  スラッシュ無しの `source .crewvia-env` は bash が **PATH から先に探す**ので不可。先頭の区間に限るので、同じコマンドの `cd` の後ろの
  source は無い（plan.sh の「cd の後ろの相対は拒否」と同じ考え）。`cd <worktree>` は別の Bash 呼び出しで先に済ませる。
- `timeout <秒数> <許可コマンド>`: 後ろを同じ規則で検査する（`timeout` の入れ子・オプションは不可）。

**言語を持つツールは全部不可**: `awk python3 node perl xxd yq bash sh`（`python3 -m json.tool` も不可。整形は `jq`）。

**オプションは allowlist**（denylist ではない）: 表に列挙したオプション以外の `-`/`--` で始まる語は拒否。
結合形（`-ovictim`・`-XDELETE`・`-fa=b`）も 1 字ずつ表と照合するので、列挙外の頭文字なら拒否される。
次の語から値を取るオプションの値が `-` で始まるものも拒否（取り違えで後ろのオプションを見逃さない）。
出力先を取るもの（`sort -o`・`tee`・`split`・`tree -o`・`uniq` の 2 つ目の位置引数 ...）は表に入れていない / 列挙していない。

| 種類 | 通る形（オプションは lib の表を見る） |
|---|---|
| 読む | `cat head tail wc ls stat file realpath dirname basename diff cmp nl sort uniq cut tr rev tac column paste comm od strings readlink sha256sum md5sum du df tree` |
| 探す | `grep egrep fgrep rg ugrep jq find`（`find` は述語の allowlist。`-exec -delete -fprint* -ok*` は不可） |
| `sed` | `-n` の行指定 `p`/`=` と `s///[gIip]` だけ（`-i -f` と `w e r` は不可） |
| その他 | `echo pwd date which printenv true false sleep cd`（`printf` は不可。t009） |
| git | `log show diff status rev-parse ls-files blame rev-list merge-base describe ls-tree cat-file show-ref for-each-ref name-rev shortlog check-ignore version ls-remote`、`branch`/`tag`（位置引数があるときは `--list` 必須）、`remote [-v]`・`remote show`・`worktree list`・`stash list`・`config --get/--list`、`fetch`（下記）。**表はサブコマンドごと**（t007。同じ字でも値の取り方がサブコマンドで違う: `git grep -L` は値を取らず `git log -L` は取る）。**短いオプションは `-X` の 2 文字だけ**（`-sb`・`-vv`・`-LOecho`・`-n5` のような束ね・結合値は一律に不可。数字だけの `-10` は log / rev-list で可）。**値を取るオプションは `--name=<値>` だけ**（`--max-count 5` は不可）。pager・外部コマンドを起動しうるもの（`-O`・`--open-files-in-pager`・`--ext-diff`・`--textconv`・`-c`・`--filters`・`--upload-pack`・`--exec`）は列挙しない。`git grep` は外した（`grep -r` / `rg` で代わりになる）。**大域オプションは全部不可**（`-C`・`-c`・`--git-dir`・`--work-tree`・`--exec-path`・`--no-pager`・`-P` …。`cd` してから git を呼ぶ）。**差分を出す git は `--no-ext-diff --no-textconv` の両方が必須**（t009。下の表）: `git diff`・`git show` は常に、`git log` は差分系のオプション（`-p`・`--stat`・`--name-only`・`--follow`・`--cherry-pick`・`--word-diff` …。「差分を出さない」と列挙した `--oneline`・`--graph`・`--format=`・`--max-count=` 等以外のすべて）があるとき、`git blame` は `--no-textconv`。`git diff --no-index` は不可。`git fetch` は `git fetch` / `git fetch origin` / `git fetch origin <ref>` の 3 形だけ（オプションは一切不可 = `--prune`・`--prune-tags`・`--tags`・`--force`・`-q` も不可。ref は `:` `+` `*` `^` を含まない名前）。`ls-remote` の `<transport>::` は不可 |
| gh | `pr view/diff/checks/list/status`・`run list/view`・`issue view/list`・`repo view`・`release list/view`・`workflow list/view`・`search *`（`--web` は不可）。`gh api` は **パス 1 つ + `--jq`/`-q`/`--paginate` だけ**（`-X`/`--method`/`-f`/`-F`/`--field`/`--raw-field`/`--input`/`-H` は結合形も含め不可、`graphql` も不可） |
| plan.sh | `status pull pr-base store-check`、`done`/`needs-director` は上の `--result-file` の形だけ |

止まるものの代替:

| 止まる形 | 代わり |
|---|---|
| `grep '^# x'`（`#` を含む） | Grep ツール、または `grep -n '^. x'` |
| `echo "$AGENT_NAME"` | `printenv AGENT_NAME` |
| `find . -name *.md`（引用符の無い glob） | `find . -name '*.md'`（述語の値・始点とも引用符で囲む） |
| `awk '/a/,/b/' f` | `sed -n '/a/,/b/p' f`、または Read ツール |
| `python3 -m json.tool` / `python3 -c` で読む | `jq`、Read ツール |
| `cat > f <<'EOF'` / `cat >> f` でメモを書く | Write ツールで `notes/<tid>*.md` に書く |
| `plan.sh done t001 "$(cat <<'EOF' …)"` / `needs-director t001 "理由"` | Write でメモ → `--result-file <パス>` |
| `ls *.md` | `ls`、`ls ./*.md`、Glob ツール |
| `git fetch origin main -q` / `git fetch --prune` | `git fetch origin main 2>/dev/null`（刈り込みは不可） |
| `git -C <dir> log` | `cd <dir> && git log` |
| `git status -sb` / `git log -n5` / `git log --max-count 5` | `git status -s -b`、`git log --max-count=5`（`git log -5` も可） |
| `git grep x` | `grep -rn x .`、`rg x`、Grep ツール |
| `cmd > /dev/null` / `jq . < f` | `cmd 2>/dev/null \| head`、`jq . f` |
| `plan status`（alias） | `./scripts/plan.sh status`（主 checkout か自分の worktree の cwd で） |
| `cd X && source .crewvia-env && …` / `source .crewvia-env` | 1 回目の Bash で `cd <自分の worktree>`、2 回目で `source ./.crewvia-env && …`（または `source <自分の worktree>/.crewvia-env`） |
| `printf '%s\n' a b` | `echo a; echo b`、`echo -e 'a\nb'` |
| `git diff origin/main --stat` / `git show HEAD` / `git log -p` | `git diff --no-ext-diff --no-textconv origin/main --stat`（show / log も同じ 2 つを付ける） |
| `git blame f` | `git blame --no-textconv f` |
| `git diff --no-index a b` | `diff -u a b` |

過去の research Worker（Jiwon, mission 20260921-daemon-authority-and-mutual-watch t001）の Bash 44 件で実測（t005）:
通るのは 27 件（初版は 32 件）。t009 でも 26 件のまま（この 44 件に source・printf・git diff / show は無い）。t007 で 26 件（増えた 1 件は `git fetch origin main -q` の `-q`。`2>/dev/null` で代わりになる）。止まる 17 件のうち 13 件は意図した遮断（heredoc での書き込み・`sed -i`・`git add/commit/push`・`python3 -c`・
heredoc で Result を渡す `plan.sh done`）、残りの 4 件（`#` を含む grep・`awk`・`echo "$X"`・位置引数の `needs-director` 理由 ×2）は上の表の代替で足りる。

### git が設定から外部プログラムを起動する経路（t009 の棚卸し）

実測: textconv（`diff.<driver>.textconv`）を設定した使い捨ての repo で、フラグ無しの `git diff` / `git show` / `git log -p` /
**`git blame`**（Codex の指摘外。既定で textconv を起動する）は textconv を起動し、`--no-ext-diff --no-textconv`（blame は `--no-textconv`）
を付けると起動しない。`git log --oneline` / `--stat` / `--name-only` / `--follow` / `--cherry-pick`・`git show --stat`・`git cat-file -p`・
`git status -s` は起動しなかった（`--stat` 等も fail closed でフラグを求める）。テスト: `test_default_git_runs_textconv_and_the_required_flags_stop_it`。

| 経路 | 起動する git | 扱い |
|---|---|---|
| `GIT_EXTERNAL_DIFF` / `diff.<driver>.command`（外部 diff） | `diff`（log / show は `--ext-diff` のときだけ） | `--no-ext-diff` 必須で止まる。`--no-index` は不可 |
| `diff.<driver>.textconv` | `diff` / `show` / `log -p` / `blame` | `--no-textconv` 必須で止まる |
| `--ext-diff` / `--textconv` / `-O` / `--open-files-in-pager` / `-c` / `--exec` / `--upload-pack` | 指定したとき | 列挙しない（t007） |
| `GIT_PAGER` / `PAGER` / `core.pager` | 出力が tty のとき | Bash ツールの出力は tty ではないので pager は起動しない。env は `X=…` の前置・`export` が不可（t005）なので変えられない。`--paginate` / `-p`（大域）は不可 |
| `core.fsmonitor` | `status` / `diff`（作業ツリーを見るもの）| **残る（対象外）**。設定を書く手段が無い（`git config` は `--get` / `--list` だけ）。既存の設定に依る |
| `filter.<driver>.clean`（作業ツリーとの比較で走る） | `status` / `diff`（作業ツリー） | **残る（対象外）**。フラグで止められない。設定と `.gitattributes` が既にある repo に限る |
| `filter.<driver>.smudge` / `process` | checkout 系 | allowlist に checkout 系が無い |
| `log.showSignature` → `gpg.program` | `log` / `show` | **残る（対象外）**。既存の設定に依る |
| `core.sshCommand` / `GIT_SSH` / credential helper / `remote.<n>.uploadpack` | `fetch` / `ls-remote` | **残る（対象外）**。origin への通常の通信で使う設定そのもの。`--upload-pack` / `<transport>::` は不可 |
| `.git/hooks/reference-transaction` 等 | `fetch`（ref の更新） | **残る（対象外）**。hook を置く書き込みの手段が無い |

「残る」ものは、どれも **既に repo か git の設定に仕込まれていること**が前提（research Worker 自身はそれを書けない）。
脅威モデルの対象外（悪意ある設定の仕込み）として扱う。

### 既知の課題（Codex 1〜3 巡目で塞いだ形と残る経路）

塞いだ形（どれも脅威モデルでは対象外の類だが、解釈不要の形へ狭めて塞いだ）:

| 巡 | 形 | 塞ぎ方 |
|---|---|---|
| 1 (t005) | `command rm`・`export PATH=…`・`LD_PRELOAD` | 状態を変える builtin・ラッパーを表から外す |
| 1 | コメント内・本物の heredoc（`# <<'EOF'`） | `#` と `<<` を文字の段階で拒否 |
| 1 | `awk -v` の `system()`・`python3 -m json.tool in out`・`xxd -r`・`sort -ovictim` | 言語を持つツールを外す・オプションを allowlist に |
| 1 | `gh api -XDELETE` / `-f` / `-F` | `gh api` はパス + `--jq` / `--paginate` だけ |
| 2 (t007) | `git grep -LOecho`（pager として echo を起動） | git の表をサブコマンドごと・束ね不可・`git grep` を外す |
| 2 | `uniq in '2'>/dev/null`（引用符つきの fd 番号で書き込み） | リダイレクトは完全一致の 2 トークンだけ |
| 2 | `/tmp/plan.sh status`（basename だけの判定） | 字面と realpath の両方で本 repo / 自分の worktree の plan.sh |
| 2 | `git fetch --prune --prune-tags` | fetch は 3 形だけ・オプション無し |
| 3 (t009) | `cd /other` → `source .crewvia-env`・PATH 探索・symlink | 自分の worktree / repo root の `.crewvia-env` をパスで指す形だけ |
| 3 | `printf 'abc%n' PATH` | `printf` を外す |
| 3 | `git diff --no-index /dev/null x`（GIT_EXTERNAL_DIFF）・diff / show / log -p の textconv | `--no-ext-diff --no-textconv` 必須・`--no-index` 不可・blame も `--no-textconv` |

残る経路: 上の「git が設定から外部プログラムを起動する経路」の **残る（対象外）** の行。ほかに、許可したコマンドの実装そのものの
脆弱性（`jq` / `rg` 等）と、`cd` で入れる場所の制限は無い（読むことは許可している）。必要になれば OS レベルのサンドボックスで対処する。

## hook を変えたときの効き方

hook は Worker のツール呼び出しごとに起動されるので、merge すると**本番の全 Worker の次のツール呼び出しから効く（再起動不要）**。

## テスト

`tests/test_research_skill_guards.py`。Bash は表（`all_specs()`）から「列挙外のオプションの分離形・結合形・許可文字の後ろへの結合」を
全組み合わせで回す（表に行を足すとテストも増える）。GNU getopt と git は長いオプションの前方一致を受け付けるので、表の名前が危ない名前の
前方一致になっていないことも確かめる。git の表（strict）は、列挙済みの字の束ね（`-pp`・`-pOecho`）と、値を取る長いオプションの分離形
（`--max-count 5`）も全組み合わせで拒否されることを見る。Codex の指摘の再現は `CODEX_REPRODUCTIONS`（1 巡目 12 形 + 2 巡目 4 形 + 3 巡目 6 形）。
赤の実証: `python3 tests/red_proof_research_bash_guard.py`（t005 の項目 1〜5・t007 の項目 6〜9・t009 の項目 10〜12 を 1 つずつ戻した複製で同じテストを走らせる）。
