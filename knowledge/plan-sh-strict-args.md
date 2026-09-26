# plan.sh の引数は厳格 (t005 / backlog #23)

## 何が起きていたか

同じ根から 4 種の事故が出た。根は「**解釈できなかった引数を黙って捨てる / positional に混ぜる**」。

| 事故 | 機序 |
|---|---|
| `plan.sh init --help` が「--help」mission を作り default_mission を奪った (2026-09-25) | `--help` は未知の option なので positional の title になった |
| `plan.sh pull --help` が本物の pull を実行した | 未知の option は捨てられ、残りの引数で pull が走った |
| `done t007 --agent X "..."` で Result が `--agent` になった / `add` の title に引数が紛れた (5 task 連続) | `--agent` は positional に入り、`positional[1]` (= Result) になった |
| `done` が「multiple missions」で拒否され、数千字の Result を打ち直した | task id は mission ごとの採番で、複数 mission に同じ `t001` がある。`--mission` を打ち忘れると拒否 |

## 直し方 (`scripts/plan.sh` の `parse_opts`)

* **未知の option は拒否** (`-x` / `--xxx` / `--xxx=値` の形の語)。usage を stderr に出して exit 2。何も書かない。
  空白を含む語 (`- 修正した` / `-3 件`) は option の形ではないので positional として通る。
  option の値の位置に別の option (`--mission --skills`) が来たら「値の打ち忘れ」として拒否
* **`--` 以降は全部 positional**。`-` で始まる (かつ option の形の) title / Result の逃げ道
* **サブコマンドごとに positional の数を宣言する** (`POSITIONAL_ARITY`)。余剰も不足も exit 2
* **`-h` / `--help` は全サブコマンドで usage を出して exit 0。queue にも registry にも 1 バイトも書かない**。
  これは 3 か所を潰して初めて成り立つ:
  1. `plan.sh --help` (サブコマンドの位置) は bash 側で受ける。以前は「--help というサブコマンド」として
     queue の骨組み (`mkdir`) を作ってから python に渡していた
  2. queue の骨組みは、引数を検証し終えた `parse_opts` の末尾 (`_ensure_queue_dirs()`) が作る
  3. 末尾の dispatch は `SystemExit` のあとで task-graph を再生成する (途中まで書いて `die()` した実行の
     あとでも DAG を最新にするため)。`--help` / 使い方の誤りは `UsageExit` (SystemExit の子) で終わり、
     再生成を飛ばす。そうしないと `registry/task-graph/tasks.json` が書かれる
* **`pull` だけ使い方の誤りが exit 1**。`pull` の exit 2 は「タスクなし (idle)」で、Worker は 2 を受けると
  30 秒待って再試行する (`agents/worker.md`)。引数の誤りを 2 で返すと、壊れた呼び出しが「ただのアイドル」として
  無限に再試行される。worker.md は「1 = 実エラー (不正引数を含む)」と明記している

## mission の曖昧さ

`done` / `fail` / `needs-director` / `update` / `ready-for-verification` / `verify-result` は、`--mission` が
無く task id が複数の active mission に当たるとき `resolve_ambiguous_mission()` (唯一の定義) で決める。

* **`CREWVIA_MISSION_SLUG` の mission に、自分が実行中のとき**だけ、それを使う (stderr に 1 行)。3 つ全部が要る:
  (1) その mission が候補に居る (2) card の `worker` が `AGENT_NAME` と一致 (3) card の `status` が
  `in_progress` / `verifying`。env だけを信じると、前の task の `.crewvia-env` を source したままのシェルが
  無関係な mission の同じ `tNNN` に届く。card が読めなければ「確かめられなかった」なので使わない (拒否側)
* それ以外は拒否。**候補の mission と、打つべきコマンド** (`plan.sh done t001 --mission <slug> ...`) を出す
* `update` は以前、`--mission` 省略時に default_mission へ黙って当てていた (複数に当たっても拒否しなかった)。
  複数に当たるときは同じ規則で決める。1 つにしか当たらなければ従来どおり (default_mission)
* `retire` は対象外: watchdog が呼ぶので `AGENT_NAME` は退役対象の Worker ではない。退役は
  `--agent` / `--started-at` で束縛されている
* `pull --task <id>` が複数 mission に当たって `--mission` が無いときは拒否。以前は最初に当たった mission で
  `die` するか、優先度順で先頭を取っていた (別 mission の同じ tNNN を in_progress にしうる)

## pull

* **skills**: `--skills` → 環境変数 `SKILLS` (start.sh が Worker に export する) → registry の Worker の skills。
  どれも無ければ拒否。空集合を「絞り込みなし」にしない (skill の絞り込みを丸ごと無効にしていた)
* **Director は pull しない**: `--agent` / `AGENT_NAME` の registry 上の `role` が `director` なら拒否。
  **`ROLE` 環境変数では判定しない** — dispatcher が spawn する `kai-review.sh` は Director の env を継承しうるので、
  env で判定すると Kai-codex が Director として拒否される。registry に居ない名前は Director ではない
* registry が読めない (または空) ときは警告を出して判定を通す。registry の事故で全 Worker の pull を
  止めない (Director の判定が効かないことは stderr に見える)

## 検証

* `tests/test_plan_strict_args.py`: 全サブコマンドで `--help` が木全体 (mtime を含む) を変えない (陽性対照つき) /
  未知 option・余剰 positional の拒否 / 曖昧さの解決の全分岐 / pull の 3 拒否 / 表とサブコマンドの 1 対 1
* 赤の実証: `tests/red_proof_t005.sh` (修正前の plan.sh に差し替えて上のテストが赤くなる)

## 戻し方

共有規則 (呼び出し元すべてが同じ plan.sh を通る) なので、**env の停止スイッチは付けていない**
(`CREWVIA_*` で食い違わせると、dispatcher と Worker で答えが割れる)。戻すときは
**PR revert → 主 checkout を `git merge --ff-only origin/main` → `lib_daemon_watch.py restart`**
(dispatcher / watchdog は cycle ごとに plan.sh を呼び直すが、常駐 python が古い規則を持ち続けないよう
両方を restart する)。一部だけ戻したいとき:

* Worker の `pull` が skills 不明で拒否される → その Worker の env に `SKILLS` を足すか、registry の skills を直す
  (拒否は `plan.sh pull requires the worker's skills` で始まる)
* 呼び出しが `unknown option` で拒否される → 呼び出し元の打ち間違い。`plan.sh <sub> --help` で usage を見る
