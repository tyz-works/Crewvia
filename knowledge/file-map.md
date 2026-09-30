# ディレクトリ構成の詳細（旧 root CLAUDE.md より移設）

> root の `CLAUDE.md` は毎セッション常駐するので、各ファイルの**設計の経緯・実測・「なぜそうしたか」**はここへ移した
> （2026-09-27 / t040）。root に残したのは 1 行の役割と、破ると事故になる不変条件だけ。
> 各項の末尾にある `knowledge/*.md` は、その仕組みの設計文書（こちらのほうが詳しい）。
> **ファイルを変えるときは、該当する項と設計文書を合わせて更新する**（root は骨格なので触らなくてよい）。

## `config/`

### `worker-names.yaml`

名前プール・カスタマイズ設定

### `crewvia.yaml`

システム設定（承認チャネル・WIP制限等）。`model_per_skill` は
**docs / qa / verify = `claude-sonnet-5`**（Haiku は `--permission-mode auto` を
無視して承認ダイアログで止まる。t028 / #16）。起動のたびの
`CREWVIA_WORKER_MODEL` 上書きは不要（env は従来どおり最優先で残る）。
`tests/test_model_per_skill.py::TestRealConfig` が実 config を固定する

## `hooks/`

### `pre-tool-use.sh`

PreToolUse hook（Taskvia承認）

### `post-tool-use.sh`

PostToolUse hook（ログ投稿）。role=director のときだけ、両デーモンの
heartbeat 同時 stale を検知する backstop も持つ（t008、下記参照）

## `agents/`

### `director.md`

Directorのシステムプロンプト

### `worker.md`

Workerのシステムプロンプト

## `scripts/`

### `start.sh`

マルチエージェント起動スクリプト
**Worker 起動時に 2 つを機械で残す**（どちらも「起動が成功した後」）:
(1) 渡された skills を registry の当該 Worker に**和集合で**足す
（`lib_registry.py add-skills`。足すだけで消さない・何も足さなければ書き直さない・
registry に居ない名前は作らない。t017 / #14。以前は registry の skills が古いと
「起動要求」と「仕事なし退役」が打ち消し合い、Director が registry を手で直していた）
(2) `TARGET_DIR` を `registry/workers/<Name>/target_dir.json` に記録
（`lib_worker_target.py`。下記。断られた起動では書かない）。
`Mux.spawn()` の `env=` 引数は廃止（渡すと TypeError。env は起動コマンドに
`export` として埋める）。設計: `knowledge/daemon-authority.md` §7-17、
`knowledge/assignment-routing.md` §1
**mux モードでは claude を起動する前に cwd の trust を検査して止める**（t021 / #28。
`lib_trust.py check`。下記）。信頼されていない cwd では claude の trust ダイアログ
（既定 `No, exit`）を kickoff の Enter が選んで Worker が即死し、残りの文字列がシェルに落ちた。
拒否は非 0 で終わり、端末と `logs/start-sh/refusals.log`（1 拒否 1 行。`logs/` は gitignore 済み。
#18: 以前の start.sh の拒否は端末にしか出なかった）の両方に残る。副作用
（`crewvia-worker-*.json` / `settings.local.json` / registry の更新）より前なので、拒否した起動は
何も残さない。kickoff 後の最後の網は `❯` の待機中・送信前・送信後にダイアログの文言を見て、
出ていれば kickoff を送らず（送った後なら `verified` と言わず）窓を片付けて非 0 で止まる
（`❯` はダイアログの選択カーソルでもあり、旧実装はそれを Claude の入力行と誤認して
`Kickoff message sent (verified)` と言っていた）。インラインモード（`exec claude`）と
`CREWVIA_BENCH_MODE=1` は対象外

### `plan.sh`

タスクプラン管理 CLI（per-task / multi-mission）。queue を書き換える
サブコマンドの後、キューロックの外で `registry/task-graph/tasks.json`
を再生成する（herdr-task-graph 連携。`CREWVIA_TASK_GRAPH=0` で停止）
**`plan.sh fail` は `--head <sha>`（実在する commit）が必須**（t004）。
`plan.sh done` だけが証拠ゲートを持っていて、FAIL の報告が外にあったので、
QA Worker が前回の handoff（別 head 時点のもの。パスは agent 名 + task id で
固定）を 36 秒で再提出できた。免除は `--no-head "<理由>"` で、card の
`fail_head_waiver` に残る（静かに検証を飛ばす経路にしない）。`handoff_path` は
**絶対パスのみ**（dispatcher は main repo 基準・`plan.sh` は Worker の cwd 基準で
読むので、相対だと検証したファイルと通知に使うファイルが別物になる）。
done と fail は `_gate_terminal_report()` を通る（入口は 1 つ）が、**FAIL の検証で
PASS の証拠要求は流用しない**（required の checkpoint が `failed` の FAIL —
最も正当な FAIL — が報告できなくなる）。停止スイッチは無い。`update` で
FAIL を開き直すと `handoff_path` / `fail_head*` を消して古い handoff を
`.stale-<UTC>` に退避する（削除しない）。設計: `knowledge/fail-evidence.md`
**`release-dep <id> --mission <slug>`**: failed の依存で保留（HELD）された
task を Director が明示的に進める唯一の出口（t007。下の `lib_dep_rules.py`）。
**保留を案内するコマンドは必ず `--mission` 付き**（task ID は mission ごとの
採番で、付けないと別 mission の同 ID に当たる。`held_dependency_hint()` の
`slug` は必須引数）。`resolve-mission <id>` は `pull` と同じ探索順
（`mission_search_order()`）で実効 mission を返す読み取り専用コマンド
**引数は厳格**（t005 / #23。`parse_opts`）: 未知の option は usage を出して
exit 2 で何も書かない（以前は捨てるか positional に混ぜ、`init --help` が
「--help」mission を作り、`done --agent X "..."` の Result が `--agent` になった）。
`--` 以降は全部 positional。`-h` / `--help` は全サブコマンドで usage + exit 0 で
**1 バイトも書かない**。**`pull` だけ使い方の誤りが exit 1**（pull の exit 2 は
「タスクなし」で、Worker は 2 を無限に再試行する）。`--mission` 省略で task id が
複数 mission に当たるときは `resolve_ambiguous_mission()`（`CREWVIA_MISSION_SLUG` の
mission に**自分が実行中**のときだけそれを使い、他は候補と打つべきコマンドを出して拒否）。
`pull` は `--skills` → env `SKILLS` → registry の順で、どれも無ければ拒否
（`dashboard` は `parse_opts` を通らない bash 側の TUI なので、同じ規則
（`--help` は何も書かない・未知の option は拒否）を bash 側の分岐が持つ）。
設計: `knowledge/plan-sh-strict-args.md`
**`pull --task` は task の `target_dir` を Worker の実効 target
（`--target-dir` > `TARGET_DIR`）と照合し、自分が別の task を持っていれば拒否する**
（どちらも何も書かずに exit 3 = `PRECONDITION_UNMET`。`agent_busy_elsewhere()`。
**孤児の assignment・`needs_director` の card だけ**では拒否しない —
Kai-codex の codex-review が恒久に取れなくなる #13 の再発になる）。
`done <id> --pr <N>` は、その task を `blocked_by` に持つ `codex-review` /
`review` の task に `pr_number` を書き（**未設定のものだけ**。Result から推測しない）、
`blocked` の codex-review は `pending` に戻す（`propagate_pr_number()`）。
**`done` は `--pr` の付け忘れを拒否する**（t036 / PR7）: その task を `blocked_by` に
持つ未終了の codex-review に `pr_number` が無いのに `--pr` が無ければ exit 2・何も書かない
（`codex_reviews_awaiting_pr()`）。PR を作らない task は `--no-pr "<理由>"`（card の
`no_pr_waiver` + stderr に残る。`--pr` との併用・空の理由は拒否）。
`lint_plan.py` は drafting でも `status: blocked`（`blocked_reason` 必須）を受理する
ので、PR 番号待ちの task は承認前から止めて積める。
task の成果物は `deliverable: pr|file|none` で宣言し（`plan.sh add/update --deliverable`）、lint は
`config/skill-permissions.yaml` の `can_produce_deliverable` だけを見て突き合わせる（`lint_plan.py` にスキル名は書かない）。
必須化は `plan.sh init` が mission.yaml に書く `deliverable_required: true` の mission だけ。`deliverable: pr` の
task は `done` に `--pr` / `--no-pr` が要る（t013。`knowledge/assignment-routing.md` §6）。
**`pull` は Director（registry の `role: director`）を拒否する**（`ROLE` env では
判定しない — dispatcher が spawn する `kai-review.sh` が継承しうる）。
`needs-director` は呼んだ Worker の `queue/assignments/<name>` を外す
（`retire_assignment()`。`done` / `fail` と同じ。#13）。退役 marker が「前任の
後始末待ち」（phase=terminated・記録された pane_pid が `ESRCH`）のときだけ、
`pull` は最大 60 秒待ってロックを取り直し 1 回で取る（`predecessor_cleanup_pending()`。
それ以外の `retirement_reserved` は従来どおり即拒否。t021 / PR6）。
設計: `knowledge/assignment-routing.md` §3-5、`knowledge/daemon-authority.md` §7-16 / §7-18

### `dispatcher.sh`

並列モードの常駐割り当てデーモン（idle Worker への自動 assign + codex-review spawn）
**仕事の割り当ての判定者**（queue/ を読む唯一のデーモン）
**状態ベースの通知（needs_director / failed+handoff / review 拒否）は、状態が
変わるまで 1 回だけ**（t010 / backlog #10）。`NOTIFY_TTL`(300 秒）は
スロットルであって受領確認ではないので、状態が続くかぎり TTL ごとに永久に
再送され、2026-09-25 に数十通届いてユーザーがデーモンを手で止めた。
`registry/daemons/notified-state.json`（「伝えた」台帳。fingerprint = 通知内容を
決める入力）を `notify_state_once()` が見る。**台帳が読めない・壊れているときは
再送側に倒す**（欠落は再送より高くつく）。live key は `all_tasks` の 1 つの
スナップショットから集め、mission を自分で走査し直さない（観測できなかった回に
台帳を捨てると洪水が戻る）。停止スイッチは無い（rollback は台帳を消す /
PR revert + dispatcher restart）。設計: `knowledge/notify-once.md`
**codex-review の差分 300KB 超の拒否は記録して再 spawn しない**（#11）:
`kai-review.sh` が `needs-director` に倒す前に `registry/daemons/review-refusals/
<mission>__<task>.json` を書き（`lib_review_refusal.py` が唯一の定義）、
dispatcher は記録がある間 spawn しない。同じ PR は何度やっても同じ大きさなので、
再 spawn は必ず同じ結論に戻り、「spawn → 拒否 → pending → spawn」のループになっていた。
記録が**読めない・欄の値が不正なら保留**（拒否されていない、に倒さない）。
Director が再試行するには `plan.sh update <id> --pr-number <新PR>` か
`lib_review_refusal.py clear`。
**`pr_number` の無い ready な codex-review は spawn せず Director に 1 回だけ通知する**
（t036 / PR7。`notify_state_once()` の kind `no-pr-number`、メッセージ `[review-no-pr]`。
以前は log だけで pending のまま誰にも知られなかった。`blocked` の task は通知しない）
**失効した mux spawn 記録の掃除**（`sweep_stale_pane_records()`。下の
`lib_mux.py`）も毎サイクルここから呼ぶ
**割り当ては skill だけでなく TARGET_DIR でも照合する**（t009 / #21。
`worker_may_take_task()` → `lib_worker_target.worker_may_take()`。task の
target_dir が非 null で Worker の記録が**無い・読めない**ときは割り当てない
（保留）が、null の task は記録が無くても従来どおり — 起動済みの Worker が
dispatcher の再起動で全員止まらないため）。**担当できる Worker が居ないとき**の
Director 通知には、**そのまま貼れる起動コマンド**（`AGENT_NAME=$(assign-name.sh ...)
[TARGET_DIR=..] ... start.sh worker <skills>`）と、既存 Worker が担当できない理由が付く。
**送信済み・pull 待ちの Worker には別の task を送らない**（#22。通知スロットルの
TTL の内。以前は 6 秒後に別 task を送り assignment が上書きされた）。
**Rule 2（no-task / blocked-stuck の退役）の「仕事を持っている」は card で数える**
（`worker_holds_work()`。needs_director / blocked / verifying 等の card を持つ Worker は
退役させない。`needs-director` が assignment を外すようになったので、assignment では
数えられない。#13）。skill は合うが TARGET_DIR が合わない task しか残っていない Worker は
no-task と同じく退役させる (C3 / t009。記録が無い / 読めない Worker だけは待機)。**codex-review の同時 1 実行は `queue/assignments/Kai-codex` の
有無ではなく、指す task で判定**（`codex_review_slot_busy()`。終わった task・
needs_director を指す孤児は塞がない。読めない・形が違う・task が見つからないは塞ぐ側）。
設計: `knowledge/assignment-routing.md` §2、`knowledge/daemon-authority.md` §7-16

### `watchdog.py`

Worker 生存監視デーモン（idle 判定・pane 消滅の検知と kill）
**Worker を終了させる唯一の実行者**（t002 以降）。後始末まで担う
**timeout 終了は Director に `[timeout]` の 1 通で伝える**（t021 / PR6）: どちらの
上限か（idle×2 / max）・経過・`plan.sh update <id> --status pending --reset` の要否
（後始末が成功していれば「不要」）。notify-once 台帳に `timeout_<mission>_<task>` を
退役ごとの fingerprint で置くので、settle の再実行で 2 通目は出ない。送れなかった通知は
watchdog がメモリ上で最大 1800 秒再送する。台帳の書き手は dispatcher と watchdog の 2 人で、
書き換えはすべて `told_lock()`（`lib_daemon_state.py`）の中。`kind=timeout` は 24 時間
prune されない。設計: `knowledge/daemon-authority.md` §7-18

### `lib_daemon_watch.py`

dispatcher と watchdog の相互監視（heartbeat・respawn・自己申告・
maintenance マーカー）。CLI: spawn / spawn-cmd / beat / watch / status /
pause / resume / restart（pause→kill→spawn→resume を 1 コマンドで行う。
`scripts/lib_mux.py kill`/`spawn` を素で叩かないこと。spawn-cmd は
起動コマンド文字列だけを印字する — 両デーモン同時 restart 等で
`lib_mux.py spawn` に手渡すときに使う）
設計: knowledge/daemon-authority.md §7。両方が同時に死ぬケース
（相互監視だけでは救えない）は hooks/post-tool-use.sh の backstop
（§7-13）が Director に伝える

### `lib_retirement.py`

retirement marker プロトコル（dispatcher が判定 → watchdog が実行）
権限境界の設計は knowledge/daemon-authority.md
判定の設計は knowledge/watchdog-idle-judgment.md
沈黙の判定では **`ENOENT`（本当に無い）と、それ以外の `OSError`
（観測できなかった）を分ける**（t017）。潰すと、`registry/` の権限事故
1 回で健全な Worker が全員 `hard_idle` の terminate 対象になる
ログ: logs/watchdog/watchdog-YYYYMMDD.log（日次）

### `lib_task_cards.py`

**task カードを読むことの唯一の定義**（`list_task_cards()` /
`parse_frontmatter()` / `isolated_task()` / `CORRUPT_TASK_STATUS`）。
**plan.sh・dispatcher.sh・watchdog.py・verifier-dispatcher.sh・
taskvia-sync.sh の 5 者がここだけを読む**。識別子はファイル名、
`id` 欄の食い違いは `[破損]` として保留、読めないカードも例外では
なく `[破損]` で返す（1 枚の事故で常駐デーモンのサイクルを
落とさないため）。コピーを書き戻すと「pull は受理するのに
dispatch サイクルが KeyError で落ちる」が戻る（Codex 5 巡目 P2）。
フォールバックは持たない（読めなければ呼び出し側が落ちる）ので、
plan.sh を単体でコピーする隔離テストでは一緒に置くこと。
**空リストは「カードが 1 枚も無い」の意味だけ**を持つ（走査に失敗
したときは終端でないプレースホルダを 1 件返す）。同じ `[]` に潰すと
`all(status in TERMINAL)` がそれに True を返し、**未完了の兄弟を
残したまま mission 全体が done になる**（t016）。
**カードは通常ファイルだけ**受理する（FIFO 等は待たずに `[破損]`）。
上限の無い `open()` を残すと、書き手のいない FIFO 1 枚で全 mission の
割り当てと生存監視が同時に止まる（t016）。設計と全数調査は
`knowledge/empty-vs-unobservable.md`
**queue / registry / config のファイルを開くコードは、必ずここを
通すこと**（t017/t018）。入口は 3 つで、判定の本体は 1 つ:
`read_task_card()`（カード 1 枚。読めなければ `[破損]`、例外は出さない）/
`read_regular_text()`（中身か例外）/
`read_regular_text_or_unreadable()`（中身か `Unreadable` + 警告 1 行。
常駐デーモン用。**例外を出さない** — `except Exception` の backstop 付き）。
**読み取りの失敗を `None`/`{}`/`[]` で返さない**（t018）。`Unreadable` は
空の入れ物として振る舞わず、`bool()`/`len()`/`in`/`[]`/反復/`.get()` が
すべて `TypeError` になる。潰していたせいで、`dispatcher.load_state()` の
`{}` が `dispatch()` の `if not active_missions: shutdown_idle_workers()`
に落ち、**読めない state.yaml が idle Worker の退役を認可していた**
（Codex 9 巡目 P1 — t017 でガードを足したことで初めて到達可能になった）。
分岐は `is_unreadable()` / `is_missing()`（**ENOENT だけが「本当に無い」**。
`Path.exists()` は `EACCES` も False に潰すので分岐の材料にしない）。
表（`GUARDED_READS`）は「載せた関数」しか見ないので、t018 で向きを
逆にした: `test_no_unguarded_read_remains` が対象モジュールの
`open()`/`read_text()`/`read_bytes()` を **AST で機械的に全部拾い**、
理由付き allowlist に無ければ落とす（新しい直接読み取りは必ず赤になる）。
**例外は `plan.sh` の `load_state()` 1 つだけ**（意図的。
`knowledge/empty-vs-unobservable.md` §4 の取引）。
赤の実証は `tests/red_proof_t018.sh`、例外契約は
`tests/test_read_wrapper_exception_contract.py`
**依存欄の検証もここ**（t007）: `blocked_deps_problem()` は `blocked_by` を
「欄なし / null / [] / 空でない文字列の list」だけ受理し、`released_deps_problem()`
は task ID の list だけ受理する。それ以外は card ごと `[破損]`。
**依存の宣言は「これが済むまで進めるな」という制約なので、読み違えて落とすと制約が
消える** — 検証の `if d`（truthiness）フィルタが `blocked_by: [null]` を「依存なし」に
して pull も dispatch も開始した（#9 が潰す事故を逆向きに作り直した）。**要素を 1 つも
落とさない**（`lib_dep_rules.declared_dependencies()` が 2 枚目の網: 不正な要素は
`<不正な依存: 値>` という unmet に残す）。`released_deps` が壊れているときは
「解除なし = 保留のまま」に倒す（誤って failed の依存を解除しない）。
設計: `knowledge/failed-dependency-hold.md`
再発防止は tests/test_task_card_identity.py（両者が同じ queue から
同じ task 集合を導くことの直接 assert + コピー検出）と
tests/test_unobservable_is_not_empty.py（赤の実証は
tests/red_proof_unobservable.sh）

### `lib_dep_rules.py`

「依存が満たされた」の唯一の定義（`card_dependencies(meta, ...)` →
`DependencyVerdict(unmet, held)`。`HELD_DEP_STATUSES` は `lib_task_status` の再公開。`DEAD_DEP_STATUSES` は S1 で廃止）。
**plan.sh pull（自動・`--task`）・plan.sh task-graph・plan.sh status・
dispatcher.sh がここだけを読む**。コピーを書き戻すと、
ズレが出るのは QA FAIL の直後だけ（= 誰も疑わない瞬間）になる。
フォールバックは持たない（読めなければ呼び出し側が落ちる）ので、
plan.sh を単体でコピーする隔離テストでは一緒に置くこと。
再発防止は tests/test_task_graph.py のコピー検出テスト
**`failed` の依存は「満たされた」ではなく「保留（HELD）」**（t007 / backlog #9）。
以前は `failed` を dead 扱い（= 満たされた）にしていて、QA が FAIL した直後に
その QA に `blocked_by` した review / merge task が自動で unblock され、merge
寸前まで進んだ。fix task（進めてよい）と review / merge task（進めてはいけない）を
**規則は区別できない**ので、辺ごとの hard/soft を plan 時点で選ばせる案は採らず
（failed の理由を見る前には決められない・選び忘れが事故側に倒れる）、
Director が failed の後に `plan.sh release-dep` で解除する形にした。
`blocked_by` は消さず `released_deps` に記録する。（`cancelled` は S1 で語彙から消えた）`plan.sh status` が `🛑 HELD` と解除コマンド
を出し、task-graph は `[保留: <id> が failed]`、dispatcher は `[held]` をログに出す
（保留が「永久保留」という別の outage にならないための出口）。
**停止スイッチは無い**（長寿命の dispatcher と呼ばれるたびに読み直す plan.sh で
答えが割れ、消そうとした食い違いをスイッチが作る）。
設計と比較: `knowledge/failed-dependency-hold.md`

### `kai-review.sh`

Codex reviewer (Kai-codex) 起動ラッパー。詳細は `knowledge/codex-reviewer.md`
差分が 300KB 超なら拒否記録を書いてから `needs-director`（上の dispatcher.sh）。
`--mission` を省略した呼び出しは、**pull の前に** `plan.sh resolve-mission` で
実効 mission を 1 度だけ解決して全部に使う（記録名に mission が要るので、
空だと記録が書かれず再 spawn ループが戻る）

### `taskvia-sync.sh`

queue → Taskvia 同期

### `lib_review_refusal.py`

codex-review の拒否記録の唯一の定義（書き手 kai-review.sh・読み手
dispatcher。CLI: `record` / `show` / `clear`）。`load()` は欄の値まで検証し、
不正なら `Unreadable`（`diff_bytes: null` が通知の組み立てを落として全 mission の
dispatch が止まる、を防ぐ）

### `lib_daemon_state.py`

**デーモン側 JSON 状態ストアを読む入口**（`load_json_store(path, check=...)`）。
戻りは検証済みの値か `Unreadable`。ENOENT だけが「まだ無い」。壊れたエントリが
1 つでもあればストア全体が `Unreadable`（例外は出さない）。同じ欠陥（読めた JSON の
中身の形を確かめずに使う）が PR #214 で 3 回出たので、1 件ずつの site patch をやめて
入口を 1 つにした。`tests/test_daemon_state_reads_go_through_the_entry.py` が
`json.load(s)` を AST で全部拾い、入口の外は理由付き allowlist に無ければ落とす
（queue 側の `lib_task_cards` と同じ作法。ストアごとの「使えないときの向き」の表は
`knowledge/notify-once.md` §3）

### `lib_mux.py`

mux 抽象化モジュール（TmuxBackend / HerdrBackend）
`recorded_herdr_pane_id()`: spawn 記録の `pane_id` を、記録の server がまだ生きているとき
だけ返す（記録と `/proc` のみ。herdr にもロックにも触れない。task-graph 生成器が使う）
**`registry/mux/<name>.json`（spawn 記録）は kill の認可の唯一の証拠**
（§7-11-2）で、Worker の retirement は pane の pid を直接 kill して `kill()` を
通らない → 記録だけが残る（失効記録。t001 / #7）。`reap_stale_pane_records()` が
掃除する（dispatcher `sweep_stale_pane_records()` と `start.sh` の
`mux_reap_records`）。**消してよいのは mux が「その id は無い」と明確に答えた
ときだけ**（herdr は失敗を全部 `{"error"}` で返し、server 不達も同じ形。
`error.code == "pane_not_found"` 以外は「観測できなかった」で残す。旧
`_resolve_ids()` は error があれば消えたと読み、**herdr 停止中に呼ばれるだけで
記録が消えて次の kill が恒久拒否**になった）。問い合わせは名前ではなく記録の id を、
**記録の server の世代と同じ接続の上で**流す（`_herdr_pane_get_bound()`。検証と
破壊が別の接続なら別の server でありうる）。label が違う pane は消えたのではなく
`PANE_RENAMED`（同じ世代・id・tab なら記録から届く）。記録を書く `write_pane_record()` と
消す `drop_pane_record(expect=<判定した記録>)` は `registry/mux/.records.lock` の
同じ区間で直列化され（掃除が生きた pane の記録を消せた窓を塞ぐ）、記録を消す判断は
すべて「判定した記録」を渡して消す。掃除には時間予算がある（`STALE_SWEEP_BUDGET_SECONDS`。
応答しない herdr で 65 秒待つと相互監視が dispatcher を死んだと見て respawn する）。
`spawn` の終了コード: 0 起動 / 1 起動せず / **10 live プロセスが居る / 11 pane が読めず
busy 扱い**（`start.sh` が「already running」と言うのは 10 だけ）。
症状「already running と言うのにペインが無い」の**真因は特定できていない**
（記録は原因ではないと確認済み。再現できたのは起動が定着しなかった別の誤報のみ）。
停止スイッチ: `CREWVIA_MUX_RECORD_SWEEP=0`（掃除だけが止まる。記録のロックは残る）。
設計と全経路の棚卸し: `knowledge/daemon-authority.md` §7-14、
`knowledge/empty-vs-unobservable.md` §7
**`keys <name> <key>...`**（t028 / #16）: 選択ダイアログ（信頼確認・権限メニュー）を
カーソル移動で答える verb。`send` は**テキスト**を打つので、`"2"` を送ってもカーソルは
動かず、続く Enter は**先頭項目**を選ぶ。`keys` は**名前付きキーだけ**
（`up` / `down` / `left` / `right` / `enter` / `escape`(`esc`) / `tab`。大小無視）を
受け、未知の名前は何も送らずに全体を拒否する（tmux は未知の語をそのまま打つため）。
1 回の `send-keys` で送る。使う前に `capture` でカーソルの位置を見ること。
**本番宛先名（`Sora-director` 等）の拒否は `CREWVIA_MUX_TEST_ISOLATION=1` のときだけ働く**
（`_guard_test_isolation`。t025 の実測: 環境変数なしなら本番の Director に実際にキーが届く）
ので、試すときは必ず自分が起動した Worker の pane に向けること

### `lib_pane_process.py`

mux ペインのプロセス木の分類（`classify_process_tree()` → `executing` / `idle_process` /
`no_process`）の唯一の定義（B1 / #27。watchdog.py から移設）。読むのは `/proc` だけ。
**「ペインの裏で何かが走っているか」の答えはここ 1 つ**で、watchdog の idle 判定と
dispatcher の Rule 5 が共有する。分類するだけで判定しない — 「殺してよいか」「通知してよいか」は
呼び出し側が自分の fail の向きで決める。裏の shell・Monitor も前景のツールも `executing`
（claude の直下に後から `bash -c` が生える）。**t074**: 判定根拠は comm (実行ファイル名) では
なく「祖先の cmdline に Bash tool / Monitor の shell snapshot wrapper (`/shell-snapshots/
snapshot-`) が現れるか」（本番の `npm exec ...` が process.title 書き換え + `sh -c "..."` を
挟むため comm では MCP を job と誤読していた）。dispatcher の Rule 5 には
`BACKGROUND_JOB_MAX_SECONDS`（既定 30分）の上限も追加— 起動元だけでは job の中身が進んで
いるかは分からない（Ren の `pgrep -f` 自己一致ループの実例）。**t082**: cmdline が
「消滅」以外の理由で読めないノードは（マーカー無しに潰さず）`unknown` に倒す（本物の job が
idle_process に化けて watchdog が誤 terminate しうる P1）。`BACKGROUND_JOB_MAX_SECONDS` の
タイマーも「無い」と「読めない/書けない」を区別する（P2）。設計・実測・戻し方:
`knowledge/watchdog-idle-judgment.md` §7-9

### `lib_worker_target.py`

Worker が起動された `TARGET_DIR` の記録の唯一の定義（t009 / #21。書き手 `start.sh`・
読み手 dispatcher）。`registry/workers/<Name>/target_dir.json`
`{"agent","target_dir": <絶対パス|null>,"written_at"}`（`.gitignore` 対象）。
**`null` は「crewvia 本体で起動した」という事実**で「記録が無い」とは別。
`registry/mux/<Name>-worker.json`（kill の認可の証拠）には相乗りしない。
`worker_may_take()` が判定表の唯一の定義、読みは `lib_daemon_state.load_json_store`
（壊れていれば `Unreadable`）、`sweep_stale_records()` は窓も新しい heartbeat も無く
300 秒以上古い記録だけ消す。停止スイッチは無い（dispatcher と `plan.sh pull --task` の
答えが割れる）。CLI: `record <registry_dir> <agent> [<target_dir>]` / `show`。
記録が嘘・無いときは `record` で書き直す（dispatcher は毎サイクル読み直す）。
設計: `knowledge/assignment-routing.md`

### `lib_usage_limit.py`

Worker の画面から「利用枠切れ」(`⚠ Usage limit reached · continuing automatically at 6pm`) を同定する
唯一の定義 (C2 / t005)。`detect(screen)` / `observe(previous, screen)` / `excuse_deadline(entry)`。
dispatcher の Rule 5 (`handle_usage_limit`) と watchdog の idle / max (`_observe_usage_limit`) が共有する。
位置と構造に束縛 (行頭の `⚠` + 直下に入力欄の枠 + 画面末尾)、読めなければ None。免除には必ず上限
(リセット予定 + 1 時間 / 時刻不明は見え始め + 6 時間)。設計・族の掃除・戻し方:
`knowledge/watchdog-idle-judgment.md` §10。テスト: `tests/test_usage_limit.py` / `tests/red_proof_c2_usage_limit.sh`。

### `lib_trust.py`

claude の trust ダイアログを `start.sh` が踏まないための検査（t021 / #28）。CLI:
`check <dir>`（信頼済みなら無出力で 0 / 未信頼 10 / 確認不能 11。10 と 11 は端末向けの説明を stdout に出す）と
`dialog`（stdin の pane capture にダイアログの文言があれば 0 / 無ければ 1 / 読めなければ 2）。
判定は `~/.claude.json`（`CLAUDE_CONFIG_DIR` があればその下）の
`projects["<絶対パス>"].hasTrustDialogAccepted`。**cwd 自身と `/` までの祖先のどれかが `true`**
なら信頼済み（実測: 信頼済みの祖先の配下ではダイアログが出ない）。パスは論理（`cd && pwd`）と
物理（`realpath`）、それぞれ NFC の形の全部を候補にし、キー側も `normpath` で畳む（正規化は一致を
見つけやすくするためだけに使う）。`true` は `is True`（`"true"`・`1`・`null` は信頼済みにならない）。
**倒す向き**: 読めない・JSON でない・形が違う（`projects` が object でない・cwd に関わる記録が bool でない）
は **確認不能 = 止める**（「読めない」を「信頼済み」に潰さない。進めた場合の被害は Worker の即死と
シェルへの文字列漏れ、止めた場合の被害は利用者が 1 行直すこと）。ファイルが**無い**（ENOENT）のは
「何も信頼していない」が事実なので未信頼。**`~/.claude.json` は書き換えない**（trust は利用者の
判断。止めるときは `! cd <dir> && claude`（Yes を選んで `/exit`）と `jq` の 1 行を出す）。
読みは `lib_daemon_state.load_json_store`（通常ファイルか・JSON か・object か）。
最後の網の文言（`Quick safety check` / `Yes, I trust this folder` / 旧版の
`Do you trust the files in this folder` / 決定の案内と揃った `No, exit`）は claude 2.1.283 の
バンドルで確認したもので、この 1 箇所にだけある。テスト: `tests/test_trust_precheck.py`（判定の表）・
`tests/start-sh-trust-precheck.bats`（start.sh 経由）・`tests/red_proof_b6_trust_precheck.sh`。
fake tmux で start.sh を mux モードで走らせるテストは `tests/trust_fixture.sh` で信頼を宣言する
（しないと CI（`~/.claude.json` が無い）でだけ落ちる）

**t051 (B6 fix / PR#237 の Codex findings) で直した 3 件**:
- **P1 (設定の不伝播)**: precheck は `start.sh` プロセスの ambient `CLAUDE_CONFIG_DIR` を読むが、
  spawn 先のペインは mux server が保持し続ける起動時点の env（`herdr-server-stale-env-inheritance`）
  を引き継ぐため、precheck が見た設定と実際に起動する claude が読む設定が食い違いうる。`CLAUDE_CONFIG_DIR`
  が設定されていれば `ENV_EXPORTS` に含めて spawn 先へも伝え、未設定なら spawn 先の起動コマンドで
  明示的に `unset CLAUDE_CONFIG_DIR`（server 側に残っているかもしれない古い値を消す）。
- **P2 (読めない画面を「無い」に倒す)**: `capture()` は「pane を読めなかった」ときも「画面が本当に空」
  なときも同じ `""` を返し区別できない（`Mux.verify_sent` の docstring と同じ理由）。空を「ダイアログ
  なし」に倒すと、まさに読めなかった側で kickoff の Enter を送ってしまう（この網が本来防ぐはずだった
  失敗そのもの）。空画面は「観測できない」として扱い、kickoff 送信直前・送信後の網
  （`_require_no_trust_dialog`）は画面の検査が成功する（rc=0 か rc=1）まで有限回 (3 回) 再試行し、
  それでも駄目なら拒否する。プロンプト待ちループ側の緩い網（`_trust_dialog_check`）は観測できない間
  中止せず、上位の 30 回ポーリングに判定を委ねる（起動直後の「まだ何も描画していない」正常系まで
  拾わないため）。
- **P2x2 (bench mode の除外漏れ)**: プロンプト待ちループの `_trust_dialog_check` は precheck と同じ
  `CREWVIA_BENCH_MODE` 除外条件を持たず、bench mode の未信頼 pane まで kill していた。関数の先頭に
  `[[ "${CREWVIA_BENCH_MODE:-0}" == "1" ]] && return 0` を追加（kickoff 送信直前/後は元から
  `if BENCH_MODE != 1` ブロックの内側なので対象外だった）。

**t070 (B6 fix 2巡目: Codex review 2巡目の findings) で直した 2 件 + 族 D**:
- **P1 (シェルインジェクション)**: t051 が追加した `CLAUDE_CONFIG_DIR='${CLAUDE_CONFIG_DIR}'` / `HOME='${HOME}'`
  を含め、LAUNCH_CMD 全体が生の `'$var'` 埋め込みで組み立てられていた。値に `'` が入るだけで壊れ、
  `'; cmd; #` のような値ならペインが LAUNCH_CMD を評価したときに追加のコマンドが実行される
  (AGENT_NAME・TARGET_DIR・WORK_DIR は外部由来で入力しうる)。`_shq()`（自前でクォートを
  組み立てない）を導入し、ENV_EXPORTS の全変数・`--model` / `--settings` / `--permission-mode` の
  CLI 引数・`cd`・advisory メッセージ（`_abort_on_trust_dialog` 等）まで、埋め込み箇所を**全部**
  これに通した（族 D の掃除）。**文字列の形を見るだけのテストは評価時の挙動を保証しない**
  (Codex 指摘) ので、`tests/start-sh-trust-precheck.bats` は fake tmux に送られた LAUNCH_CMD の
  実テキストを取り出し、claude の代わりに引数と cwd を書き出すスタブを使って**隔離した bash で
  実際に評価する**テストを持つ。

**t089 (B6 fix 4巡目: PR#237 Codex 4巡目 P2×2) で直した 2 件**:
- **P2-1 (trust ダイアログ検出の族B再発)**: t078 は「文言」と「ダイアログの操作構造」を要求したが、
  画面の**どこからでも独立に**拾っていたため、無関係なパスの文言 (`/tmp/quick safety check` 等) と、
  無関係な別の権限確認メニューが同じ画面に乗ると誤検出した。`screen_shows_trust_dialog()` は
  「カーソルが選択肢 1 を指し、その選択肢自身が `Yes, I trust this folder` / `Yes, proceed` である」
  ことを 1 つの正規表現 (`_TRUST_ACCEPT_OPTION_RE`) でまとめて要求するように直した — 文言と選択肢を
  独立に探すのではなく、最初から「同じダイアログの枠の中にある」ことを保証する形にした。
- **P2-2 (`_shq` が bash 専用)**: `_shq()` は `printf '%q'` (bash 組み込み) を使っていたが、
  LAUNCH_CMD を実際に評価するのは pane の**設定済みシェル**であり、それが bash である保証は無い。
  `%q` は改行・非 ASCII を `$'...'` (ANSI-C quoting、bash 専用) で出力するが、dash はそれを構文
  エラーにする。シングルクォート方式 (`'...'`、中の `'` は `'\''`) に変えた — bash / dash のどちらでも
  同じ意味になる (常にクォート付きになる点が観測できる違い)。あわせて LAUNCH_CMD の
  `cd $(_shq "$WORK_DIR")` と `claude...` の間を `;` から `&&` にした (族 A): `cd` が失敗しても
  `;` は後続を実行してしまい、**別のディレクトリで Claude が起動する**。
  `tests/start-sh-trust-precheck.bats` は `_shq` を start.sh のソースから直接取り出し bash と dash の
  両方で評価するテストと、`cd` の行き先を送信後に消してから評価し claude が起動しないことを確かめる
  テストを持つ。
- **P2 (相対パスの CLAUDE_CONFIG_DIR / HOME)**: precheck は start.sh 自身の cwd を基準に相対パスを
  開くが、LAUNCH_CMD は WORK_DIR に `cd` してから claude を起動する。相対な `CLAUDE_CONFIG_DIR` /
  `HOME` を「同じ文字列」のまま渡しても、cd の前後で基準が変わり「同じ解決済みの対象」にはならない
  (t051 P1 の続き)。`_EFFECTIVE_MUX_ENABLED` 判定の直後、precheck より前 (bench mode でも実行 —
  ENV_EXPORTS への伝播は bench mode でも起きるため) で一度だけ絶対パスへ解決し、export で上書きする。
  以降のコード (precheck 本体・ENV_EXPORTS への伝播) は同じ変数を読むだけなので、「同じ解決済みの
  対象」であることが構造的に保証される (2 箇所で別々に解決して食い違う余地を作らない)。解決できない
  (存在しない dir 等) ときは元の値のまま precheck に委ねる (安全側: lib_trust.py が untrusted として
  止める)。
- **族D の掃除で見送った関連事項**: `crewvia-worker-${AGENT_NAME}.json` のファイル名も `AGENT_NAME` を
  埋め込むが、これは python heredoc への argv 渡し (シェル文字列への埋め込みではない) なので族D の
  定義には当たらない。ただし `AGENT_NAME` に `/` や `..` が入ると意図しないパスに書き込みうる、族D に
  隣接する懸念として見つけたが、AGENT_NAME の形式検証は別スコープ (名前プール / `--name` の入力検証)
  であり本タスクでは直していない。KICKOFF_MSG は claude 自身の REPL (エージェントの判断 + 承認 hook)
  へ渡るテキストであり、シェルが自動評価する対象ではないため族D の対象外と判断した。

**既知の限界（backlog、6 巡目、t023、2026-09-28。族B: 対象の同定）**: 空白を潰して比べる代替の判定
経路が、`No, exit` / `Enter to confirm` の文言を**実際のダイアログのまとまりに束縛せず**照合する
（t089 は主経路を直したが、代替経路が残った）。`/tmp/❯ 1. Yes, proceed` のようにメニューの文言
そのものを名前に含むディレクトリでしか起きない作為的な入力で、結果は起動直後の Worker 1 人の
kill（データ消失なし、起動し直せる）。この族は Director 判断で「起動判定 (B6) の本題」として
backlog へ送らず、6 巡目まで毎巡直してきたが、より作為的な入力でしか再現しなくなってきたため
Director 判断で打ち切った。直すなら経路ごとの手当てにせず、`lib_trust.py` の判定経路を全部列挙し
すべてが同じ「ダイアログのまとまり」の同定を通ることを構造で保証させる（`knowledge/task-graph.md`
と同じ「族ごとの掃除」の要領。横断ガード (§`test_observation_authority_does_not_fail_open.py`)
の候補）。

**戻し方**（誤判定すると Worker / Director の起動が止まる種類の変更。**env の停止スイッチは付けない**:
trust は利用者の判断で、迂回口を作ると「信頼していない dir で起動して即死」に戻る）:
(1) 個別に通す — 止められた dir は、表示されたコマンド（`! cd <dir> && claude` で Yes を選ぶか、`jq`
の 1 行）で信頼を記録すれば通る。claude 自身が出さない dir（git worktree など未知の継承規則）で
誤って止められた場合も、記録しておけば害は無い（claude が自分で書く値と同じ）。
(2) 全体を戻す — 該当 PR を revert → 主 checkout を `git merge --ff-only origin/main`。
`start.sh` は起動のたびに読まれる（dispatcher が pane で起動するときも毎回新しい bash）ので、
デーモンの再起動（`lib_daemon_watch.py restart`）は要らない。既に走っている Worker には影響しない。
拒否の記録は `logs/start-sh/refusals.log`（消してよい）

### `lib_registry.py`

`registry/workers.yaml` を書く入口（`registry/.workers.lock` の中で parse → 変更 → write）。
CLI に `add-skills PATH NAME SKILL...`（`start.sh` が使う。和集合・flow list に書き戻せない
tag は警告して飛ばす）。t017 / PR5a

### `lib_mux.sh`

bash 向け薄いラッパー（mux_spawn / mux_send 等）

### `worktree_gc.py`

古い Worker worktree を片付ける道具（t033 / #33）。**既定は dry-run**（何も消さない）で、`--apply` で
remove と判定したものは**削除ではなく隔離する**（`git worktree move` で `.claude/worktrees/.quarantine/
<timestamp>/<元の相対パス>` へ移し `git worktree lock` を付ける。`git worktree remove` / `git branch -d`
はどちらも呼ばない。t071 / PR#239 3巡目）。remove は「管理下 (`.claude/worktrees/<slug>/<name>`)・
mission が active でなく archive 済み・Worker が使っていない（TARGET_DIR 記録・プロセスの cwd）・
clean・コミットがすべて origin にある」の全部を満たすものだけ。観測できなかったものはすべて keep。
隔離済みの一覧は `--list-quarantine`、元に戻すのは `--restore <隔離先 or 元のパス>`
（lock の成否には依存しない。t080 P2-1）。デーモンは読まない（無効化は PR revert だけ。稼働中の
隔離済み worktree を戻したいときは `--restore`）。設計・理由コード・限界: `knowledge/worktree-gc.md`

## `queue/`

プラン置き場（plan.sh が管理）

### `state.yaml`

active mission slug + default_mission

### `missions/<slug>/`

- mission.yaml      title / status / next_task_id
- tasks/tNNN.md     frontmatter + Description / Result

### `archive/`

完了 mission の退避先

## `registry/`

### `workers.yaml`

Worker のスキル・経験値（`start.sh` が起動のたびに skills を和集合で追従させる）

### `workers/<Name>/target_dir.json`

起動時の `TARGET_DIR` の記録（`lib_worker_target.py`。.gitignore 対象。
消してよい — 無い = 「target_dir 付きの task は割り当てない」側に倒れるだけ）

### `heartbeats/`

watchdog 監視用

### `mux/`

mux バックエンドのタブ/ペイン ID キャッシュ（.gitignore 対象）。`<name>.json` は
kill の認可の証拠で、失効したものは `lib_mux.reap_stale_pane_records()` が掃除する。
`.records.lock` が書き手（spawn）と消す側（kill・掃除）を直列化する（消してよいのは
mux が「無い」と答えたときだけ。詳細は上の `lib_mux.py`）

### `retirements/`

Worker 終了要求と進捗（dispatcher→watchdog の引き渡し。.gitignore 対象）

### `task-graph/`

herdr-task-graph 用の `tasks.json`（.gitignore 対象）。queue を
書き換える plan.sh サブコマンドの後、キューロックの外で再生成される。
再生成は `tasks.json.lock` で直列化される（古い読み取りが新しい姿を
巻き戻さないため。キューロックの保持時間は伸びない）。待ち切れずに
引き返した実行は `tasks.json.pending` に読み直しの要求を置く。その
要求は必ず拾われる（t011）: 要求を置いたあと本ロックを 1 回取りに
行き、取れれば自分で publish、取れなければ保持者が読み直す。
「要求が無いことの確認」と「本ロックの解放」は
`tasks.json.pending.lock` の一区間にまとめてあり、その隙間に置かれた
要求を誰も拾わない、という取りこぼしが起きない。
**両方のロックの待ちは有限**（本ロック 10 秒 / 印のロック 2 秒、t012）。
区間に入れなかった実行は**印に一切触れずに**手を引き 1 行報告する
（置かれた要求は消えないので、次の queue 変更が拾う）。可視化は
付加機能なので、倒す先は「グラフが少し古くなる」であって
「plan.sh が待たされる」ではない — とくに `retire --no-wait` は
watchdog が同期で叩くため、ここが詰まると Worker の生死を誰も
見ていない時間ができる
手動で書き出すなら `plan.sh task-graph`（`CREWVIA_QUEUE` が
`<root>/queue` でなければ拒否。書き先を明示すれば通る）。
plugin は 4 つの理由で**ファイル全体**を拒否する（空の tasks /
id が空・重複 / 解決できない depends_on / 依存の循環）。1 枚の
カードの事情で全 mission の DAG が消えるので、**publish の直前に
置いた唯一のゲート `enforce_task_graph_contract()` で 4 つとも
潰す**（t013。別々の場所に書くと次の 1 件で片方だけ直して穴が開く）。
潰し方はどれも隔離であって削除ではない: task が 0 件なら
`[表示する task なし]` の node を 1 件だけ置き（最後の mission を
archive した直後に必ず通る状態なので、画面が空にも古いままにも
ならないようにするため）、循環は後退辺だけを落として
`[循環依存: <id>]`、解決できない辺は `[依存不明: <id>]`、
id の衝突は `[id重複: <id>]` を title に残す（t010 / t013）

### `daemons/`

dispatcher/watchdog 相互監視の heartbeat・pause マーカー・respawn 履歴
（.gitignore 対象）。hooks/post-tool-use.sh の同時死 backstop（t008）が
throttle マーカー（backstop-notify.throttle）を置く場所でもある。
`notified-state.json`（状態ベース通知の「伝えた」台帳）と
`review-refusals/<mission>__<task>.json`（codex-review の差分サイズ拒否記録）も
ここ。どちらも**消してよい**（無い = 再通知 / 拒否されていない）ので、通知が届かない・
review が動かないときの手当ては、該当ファイルを消す（dispatcher の再起動は不要。
`knowledge/notify-once.md`「戻し方」）

## queue/ の識別子の規則（task カードの読み方）

※ **task の識別子はファイル名**（`tNNN.md`）であって frontmatter の `id` 欄ではない。
`id` 欄がファイル名と食い違うカードは `[破損]` として保留され、pull も dispatch も
拾わない（`plan.sh status` に理由と直し方が出る）。突き合わせないと、tNNN.md を
コピーして id 行を直し忘れただけで DAG が全滅し、さらに `pull` の書き戻しが
**別のカードを上書きして消す**（t013）。
この規則は `scripts/lib_task_cards.py` に 1 つだけ置いてある（t014）。
**queue のカードを読むコードを新しく書くときは、必ずこのモジュールを呼ぶこと** —
t013 で plan.sh にだけ入れた結果、`id` 行の無いカードで dispatch サイクルが
`KeyError` で落ち、**全 mission の割り当てが止まる**経路ができていた（pull は
受理し `plan.sh status` にも ready と出るので、queue を見るかぎり何も壊れて
いないように見える）
