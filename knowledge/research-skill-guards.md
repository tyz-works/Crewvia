# research スキルの構造的ガード (Write の範囲 / Bash の読み取り専用)

> mission 20261012-research-skill-write-scope-and-bash-guard t001。調査の全文:
> `~/obsidian/research/20261011_research-skill-write-scope.md`

## 何を止めて何を通すか

判定は `hooks/lib_research_guard.py` の 1 か所（`hooks/pre-tool-use.sh` の "Research scope guard" から呼ぶ）。
SKILLS（と task card の skills）に `research` を含むセッションだけが対象。**判断に迷う形は deny**（fail closed）。
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

使い方: research の Worker は調査メモを `queue/missions/<slug>/notes/<tid>-<topic>.md` に Write で残し、`plan.sh done` の Result にそのパスを書く。

### 2. Bash（読み取り専用の allowlist）

通る形:
`cat head tail wc ls stat file realpath diff sort uniq cut tr jq grep rg ugrep find`（`-exec -delete -fprint*` 以外）・`sed`（`-n` の行指定 `p` と `s///[gIp]` だけ。`-i -f` と `w e r` は不可）・
`awk`（`> | system getline close` を含まないプログラムだけ）・`echo printf pwd date which test sleep`・`cd`・`source .crewvia-env`・`timeout/time <許可コマンド>`・
`git log|show|diff|status|rev-parse|ls-files|grep|blame|rev-list|cat-file|ls-tree|merge-base|describe|fetch|branch -a|remote -v|worktree list`（`-c`・`--output`・`--ext-diff` は不可）・
`gh pr view|diff|checks|list|status`・`gh run list|view`・`gh issue view|list`・`gh api`（GET のみ。`-f -F --input` と `graphql` は不可）・
`plan.sh status|pull|done|needs-director|pr-base|store-check`（`--result-file -` を `<<'EOF'` の引用符つき heredoc で渡す形も可）・`python3 -m json.tool`。
パイプ・`&&`・`||`・`;` でつないだ各区間がすべて通ったときだけ通る。`2>&1`・`2>/dev/null` は可。

通らない形: `>` `>>`（`/dev/null` 宛て以外）・`tee`・`sed -i`・`cp mv rm mkdir touch chmod`・`xargs`・`$(…)`・バッククォート・プロセス置換・`&`・
引用符なしの heredoc・先頭の `VAR=value`・`bash -c`・**`python3`（`-m json.tool` 以外）**・`curl wget`・`git add|commit|push|checkout|reset|merge`。

**python3 / node / perl は一律不可**にした（`open(..,'w')` を静的に見分けられないため）。読むだけの用途は grep / sed -n / jq / yq で代える。
**`git fetch` は通す**（.git の remote ref を更新するだけで作業ツリーは変えない。過去の research Worker が毎回使っていた）。

過去の research Worker（Jiwon, mission 20260921-daemon-authority-and-mutual-watch t001）の Bash 実績 40 件で実測:
読み取りの調査（sed -n / grep / wc / ls / git log / gh run list / plan.sh pull|needs-director / awk の読み）は止まらない。
止まったのは `git add|commit|push`・`sed -i`・heredoc への `cat >>`・`python3 -c`（読むだけ）・`awk` の一部だけ
（前の 4 つは意図した遮断、`python3 -c` は上の代替で足りる）。

## hook を変えたときの効き方

hook は Worker のツール呼び出しごとに起動されるので、merge すると**本番の全 Worker の次のツール呼び出しから効く（再起動不要）**。

## テスト

`tests/test_research_skill_guards.py`。赤の実証: ガードの塊を外す（または allowlist に `python3` を足す）と該当テストが赤になる。
