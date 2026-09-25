# Multi-Agent System - CLAUDE.md

公開前提のマルチエージェントシステム。
カンバン駆動でタスクを管理し、Taskvia（WebUI）と連携して承認フローを実現する。

## コンセプト

- **タスクが主役**。エージェントはカードをこなすワーカー
- **Star Trek非依存**。汎用的・ポータブルな設計
- **Taskvia連携**。WebUIでカンバン可視化・承認・ナレッジログ
- **公開前提**。tmux依存を排除し、誰でもセットアップできる

---

## ロール

- **Director**（1名）: タスク分解・Worker割り当て・進捗管理。詳細は `agents/director.md`
- **Worker**（複数）: タスク実行・改善提案。名前はスキルに紐づき歴代継承される。詳細は `agents/worker.md`
- スキルタグ一覧: `agents/director.md` §5（`qa` スキルは crewvia-qa skill で手順定義）
- 名前プール・カスタマイズ: `config/worker-names.yaml`
- 自律改善ルール: `config/autonomous-improvement.yaml`

---

## Taskvia連携

- リポジトリ: `tyz-works/taskvia` / WebUI: `taskvia.vercel.app`
- 承認チャネル（`CREWVIA_APPROVAL_CHANNEL`）: `taskvia`（デフォルト）/ `ntfy` / `both`
- 承認フロー実装: `hooks/pre-tool-use.sh`, `hooks/lib_approval_channel.sh`
- ナレッジログ投稿: `hooks/post-tool-use.sh`（type: `knowledge` / `improvement` / `work`）
- Verification Push: `scripts/taskvia-verification-sync.sh`（運用詳細は `knowledge/review.md`）

---

## herdr-task-graph 連携（任意）

- plugin: `tyz-works/herdr-task-graph`（herdr 上にタスク依存の DAG・実行中・並列実行可能を描く）
- crewvia 側は `plan.sh` が queue を書き換えるたびに `registry/task-graph/tasks.json` を再生成する
  だけ。**plugin が無くても・herdr でなくても・tmux モードでも何も起きず、エラーも出ない**
  （生成は herdr に触れない）。停止スイッチ: `CREWVIA_TASK_GRAPH=0`（1 バイトも書かない）
- **導入は crewvia の生成物を plugin の config dir に symlink する方式**。`HERDR_TASKS_FILE` は
  使わない — plugin ペインは呼び出しシェルではなく **herdr server の env を継承する**ので
  シェルの export は届かない（herdr 0.9.0 で実測）。手順は README「Task graph view」
- **`./crewvia` は plugin を自動起動しない**（必要なときに `herdr plugin action invoke
  open-task-graph --plugin io.github.tyz-works.task-graph`）。任意の付加機能に herdr 依存を
  持ち込まないため。invoke のたびに新しいタブが開く
- 制約: plugin は **`r` キーでしか再読み込みしない**（crewvia が書き換えても自動反映されない。
  実測済み）。ファイルが見つからないと plugin は**エラーを出さず同梱のサンプルを表示する**
  （画面タイトルが `crewvia / <slug>`（active mission が 1 件のとき）か `crewvia / N missions`
  （0 件・2 件以上のとき）でなければ crewvia のファイルを読めていない）。
  **herdr 0.9.0 では、エージェントが 1 つでも居ると plugin は `[offline]`（`Broken pipe`）になり、
  live なエージェント状態は来ない**（plugin が同一接続で snapshot の後に subscribe を送るのが原因。
  upstream `tyz-works/herdr-task-graph` 側の修正が要り、crewvia では直せない）。得られるのは
  crewvia が生成した依存関係と status の可視化のみ。**Worker とタスクのペイン紐付け
  （`pane_match`）も当たらない**（`pane_id` で書けば当たることは隔離環境で確認済みだが、生成器は
  まだ書かない）
- 運用メモ・切り分け・実測: `knowledge/task-graph.md`

---

## ディレクトリ構成

```
/
  config/
    worker-names.yaml   名前プール・カスタマイズ設定
    crewvia.yaml        システム設定（承認チャネル・WIP制限等）
  hooks/
    pre-tool-use.sh     PreToolUse hook（Taskvia承認）
    post-tool-use.sh    PostToolUse hook（ログ投稿）。role=director のときだけ、両デーモンの
                        heartbeat 同時 stale を検知する backstop も持つ（t008、下記参照）
  agents/
    director.md         Directorのシステムプロンプト
    worker.md           Workerのシステムプロンプト
  scripts/
    start.sh            マルチエージェント起動スクリプト
    plan.sh             タスクプラン管理 CLI（per-task / multi-mission）。queue を書き換える
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
    dispatcher.sh       並列モードの常駐割り当てデーモン（idle Worker への自動 assign + codex-review spawn）
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
                        **失効した mux spawn 記録の掃除**（`sweep_stale_pane_records()`。下の
                        `lib_mux.py`）も毎サイクルここから呼ぶ
    watchdog.py         Worker 生存監視デーモン（idle 判定・pane 消滅の検知と kill）
                        **Worker を終了させる唯一の実行者**（t002 以降）。後始末まで担う
    lib_daemon_watch.py  dispatcher と watchdog の相互監視（heartbeat・respawn・自己申告・
                        maintenance マーカー）。CLI: spawn / spawn-cmd / beat / watch / status /
                        pause / resume / restart（pause→kill→spawn→resume を 1 コマンドで行う。
                        `scripts/lib_mux.py kill`/`spawn` を素で叩かないこと。spawn-cmd は
                        起動コマンド文字列だけを印字する — 両デーモン同時 restart 等で
                        `lib_mux.py spawn` に手渡すときに使う）
                        設計: knowledge/daemon-authority.md §7。両方が同時に死ぬケース
                        （相互監視だけでは救えない）は hooks/post-tool-use.sh の backstop
                        （§7-13）が Director に伝える
    lib_retirement.py   retirement marker プロトコル（dispatcher が判定 → watchdog が実行）
                        権限境界の設計は knowledge/daemon-authority.md
                        判定の設計は knowledge/watchdog-idle-judgment.md
                        沈黙の判定では **`ENOENT`（本当に無い）と、それ以外の `OSError`
                        （観測できなかった）を分ける**（t017）。潰すと、`registry/` の権限事故
                        1 回で健全な Worker が全員 `hard_idle` の terminate 対象になる
                        ログ: logs/watchdog/watchdog-YYYYMMDD.log（日次）
    lib_task_cards.py   **task カードを読むことの唯一の定義**（`list_task_cards()` /
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
    lib_dep_rules.py    「依存が満たされた」の唯一の定義（`card_dependencies(meta, ...)` →
                        `DependencyVerdict(unmet, held)`。`HELD_DEP_STATUSES` / `DEAD_DEP_STATUSES`）。
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
                        `blocked_by` は消さず `released_deps` に記録する。`cancelled` は Director 自身の
                        判断なので従来どおり満たされた扱い。`plan.sh status` が `🛑 HELD` と解除コマンド
                        を出し、task-graph は `[保留: <id> が failed]`、dispatcher は `[held]` をログに出す
                        （保留が「永久保留」という別の outage にならないための出口）。
                        **停止スイッチは無い**（長寿命の dispatcher と呼ばれるたびに読み直す plan.sh で
                        答えが割れ、消そうとした食い違いをスイッチが作る）。
                        設計と比較: `knowledge/failed-dependency-hold.md`
    kai-review.sh       Codex reviewer (Kai-codex) 起動ラッパー。詳細は `knowledge/codex-reviewer.md`
                        差分が 300KB 超なら拒否記録を書いてから `needs-director`（上の dispatcher.sh）。
                        `--mission` を省略した呼び出しは、**pull の前に** `plan.sh resolve-mission` で
                        実効 mission を 1 度だけ解決して全部に使う（記録名に mission が要るので、
                        空だと記録が書かれず再 spawn ループが戻る）
    taskvia-sync.sh     queue → Taskvia 同期
    lib_review_refusal.py  codex-review の拒否記録の唯一の定義（書き手 kai-review.sh・読み手
                        dispatcher。CLI: `record` / `show` / `clear`）。`load()` は欄の値まで検証し、
                        不正なら `Unreadable`（`diff_bytes: null` が通知の組み立てを落として全 mission の
                        dispatch が止まる、を防ぐ）
    lib_daemon_state.py  **デーモン側 JSON 状態ストアを読む入口**（`load_json_store(path, check=...)`）。
                        戻りは検証済みの値か `Unreadable`。ENOENT だけが「まだ無い」。壊れたエントリが
                        1 つでもあればストア全体が `Unreadable`（例外は出さない）。同じ欠陥（読めた JSON の
                        中身の形を確かめずに使う）が PR #214 で 3 回出たので、1 件ずつの site patch をやめて
                        入口を 1 つにした。`tests/test_daemon_state_reads_go_through_the_entry.py` が
                        `json.load(s)` を AST で全部拾い、入口の外は理由付き allowlist に無ければ落とす
                        （queue 側の `lib_task_cards` と同じ作法。ストアごとの「使えないときの向き」の表は
                        `knowledge/notify-once.md` §3）
    lib_mux.py          mux 抽象化モジュール（TmuxBackend / HerdrBackend）
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
    lib_mux.sh          bash 向け薄いラッパー（mux_spawn / mux_send 等）
  queue/                プラン置き場（plan.sh が管理）
    state.yaml          active mission slug + default_mission
    missions/<slug>/
      mission.yaml      title / status / next_task_id
      tasks/tNNN.md     frontmatter + Description / Result
    archive/            完了 mission の退避先
  registry/
    workers.yaml        Worker のスキル・経験値
    heartbeats/         watchdog 監視用
    mux/                mux バックエンドのタブ/ペイン ID キャッシュ（.gitignore 対象）。`<name>.json` は
                        kill の認可の証拠で、失効したものは `lib_mux.reap_stale_pane_records()` が掃除する。
                        `.records.lock` が書き手（spawn）と消す側（kill・掃除）を直列化する（消してよいのは
                        mux が「無い」と答えたときだけ。詳細は上の `lib_mux.py`）
    retirements/        Worker 終了要求と進捗（dispatcher→watchdog の引き渡し。.gitignore 対象）
    task-graph/         herdr-task-graph 用の `tasks.json`（.gitignore 対象）。queue を
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
    daemons/            dispatcher/watchdog 相互監視の heartbeat・pause マーカー・respawn 履歴
                        （.gitignore 対象）。hooks/post-tool-use.sh の同時死 backstop（t008）が
                        throttle マーカー（backstop-notify.throttle）を置く場所でもある。
                        `notified-state.json`（状態ベース通知の「伝えた」台帳）と
                        `review-refusals/<mission>__<task>.json`（codex-review の差分サイズ拒否記録）も
                        ここ。どちらも**消してよい**（無い = 再通知 / 拒否されていない）ので、通知が届かない・
                        review が動かないときの手当ては、該当ファイルを消す（dispatcher の再起動は不要。
                        `knowledge/notify-once.md`「戻し方」）
  CLAUDE.md             このファイル
  README.md             公開向けセットアップガイド
```

---

## 環境変数

| 変数名 | 用途 |
|---|---|
| `CREWVIA_TASKVIA` | Taskvia 連携モード: `enabled` / `disabled` / `ask`（最優先。config・フラグより上） |
| `TASKVIA_URL` | Taskvia WebUIのURL |
| `TASKVIA_TOKEN` | Taskvia API認証トークン（`disabled` 時は不要） |
| `AGENT_NAME` | 起動時に設定されるエージェント名 |
| `TASK_TITLE` | 現在担当中のタスクタイトル |
| `TASK_ID` | 現在担当中のカードID |
| `CREWVIA_REPO_ROOT` | crewvia リポジトリの絶対パス。worktree 内からでも crewvia tools を参照できる |
| `CREWVIA_QUEUE` | キューディレクトリの絶対パス（通常 `$CREWVIA_REPO_ROOT/queue`） |
| `CREWVIA_MISSION_SLUG` | 担当中ミッションの slug（plan.sh pull 後に `.crewvia-env` 経由で設定） |
| `CREWVIA_TASK_ID` | 担当中タスクの ID（plan.sh pull 後に設定） |
| `CREWVIA_TASK_SLUG` | タスクタイトルを kebab-case 化した slug（worktree パス末尾に使用） |
| `CREWVIA_PROJECT` | Taskvia に送るプロジェクト識別子。デフォルト: `crewvia` |
| `CREWVIA_APPROVAL_CHANNEL` | 承認通知チャネル: `taskvia` / `ntfy` / `both`（config `approval_channel.mode` より優先） |
| `CREWVIA_DIRECTOR_MODEL` | Director が使用するモデル。`config/crewvia.yaml` の `director_model` より優先。空の場合は claude CLI のデフォルト |
| `CREWVIA_WORKER_MODEL` | Worker が使用するモデルを強制指定。`config/crewvia.yaml` の `model_per_skill` による skill 別自動選択より優先（最優先）。空にするか未設定の場合は skill に応じて自動選択される |
| `CREWVIA_WORKER_PERMISSION_MODE` | Worker 起動時の `claude --permission-mode` 値。デフォルト: `auto`（対話プロンプトなし。実質的な承認ゲートは hooks/pre-tool-use.sh + Taskvia が別途担う）。空にすると CLI 既定（対話確認あり）にフォールバック |
| `CREWVIA_DIRECTOR_PERMISSION_MODE` | Director 起動時の `claude --permission-mode` 値。デフォルト: 未設定（CLI 既定 = 対話確認あり）。Director は Taskvia 承認 hook を role 判定でスキップするため、対話確認が唯一の安全弁 |
| `CREWVIA_KILL_AUTHORITY` | Worker を終了させる主体: `watchdog`（デフォルト）/ `dispatcher`（ロールバック）。**両デーモンで同じ値にし、同時に再起動すること** — 片方だけ戻すと「誰も窓を閉じない」か「二重 kill で同名の別 Worker を殺す」のどちらかが必ず起きる（`knowledge/daemon-authority.md` §5） |
| `CREWVIA_DAEMON_MUTUAL_WATCH` | dispatcher/watchdog の相互監視の ON/OFF（`0` で無効）。`config/crewvia.yaml` の `daemons.mutual_watch`（既定 `true`）より優先。切ると片方が死んでも誰も respawn しない（`knowledge/daemon-authority.md` §7） |
| `CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS` / `CREWVIA_DAEMON_WATCHDOG_STALE_SECONDS` | 相互監視の stale 判定しきい値（既定 60 秒 / 240 秒）。`config/crewvia.yaml` の `daemons.dispatcher_stale_seconds` / `daemons.watchdog_stale_seconds` より優先。`hooks/post-tool-use.sh` の同時死 backstop（t008）も同じ変数名・同じ既定値を読む（`knowledge/daemon-authority.md` §7-13） |
| `CREWVIA_DAEMON_FLAP_WINDOW_SECONDS` / `CREWVIA_DAEMON_FLAP_THRESHOLD` | flap ガード: この秒数の窓（既定 900）でこの回数（既定 3）respawn したら自動 respawn を止め Director に報告する。`config/crewvia.yaml` の `daemons.flap_window_seconds` / `daemons.flap_threshold` より優先 |
| `CREWVIA_DAEMON_RESPAWN_GRACE_SECONDS` / `CREWVIA_DAEMON_PAUSE_REPORT_AFTER_SECONDS` / `CREWVIA_DAEMON_HOLD_REPORT_AFTER_SECONDS` / `CREWVIA_DAEMON_WATCH_LOCK_TIMEOUT_SECONDS` / `CREWVIA_DAEMON_MAINTENANCE_LOCK_TIMEOUT_SECONDS` | 相互監視の残りのしきい値（既定 120 / 1800 / 1800 / 2 / 60 秒）。`config/crewvia.yaml` の `daemons:` ブロック（コメント付き）より優先。詳細: `knowledge/daemon-authority.md` §7-8 |
| `CREWVIA_TASK_GRAPH` | herdr-task-graph 用 `tasks.json` の生成 ON/OFF。既定は有効、`0` で完全に無効（1 バイトも書かず、ログも出さない）。生成は queue を書き換える plan.sh サブコマンドすべてに乗るので、重い・壊れたときの退避路として残してある |
| `CREWVIA_TASK_GRAPH_FILE` | 生成物の書き先。既定は `$CREWVIA_REPO_ROOT/registry/task-graph/tasks.json`（未設定時のみ plan.sh の位置基準にフォールバック）。plugin にはこのパスを **plugin の config dir への symlink** で参照させる（`HERDR_TASKS_FILE` は稼働中の herdr のペインに届かないので使わない。`knowledge/task-graph.md`） |
| `CREWVIA_MUX` | mux バックエンド選択: `tmux` / `herdr`。config `mode:` より優先 |
| `CREWVIA_MUX_ENABLED` | 並列モード有効化: `1` で並列 ON（`CREWVIA_MUX` 未設定時の tmux fallback）/ `0` でインラインモード強制。`CREWVIA_MUX` が設定済みなら不要 |
| `CREWVIA_TMUX_SESSION` | tmux backend が使うセッション名（デフォルト: `crewvia`） |
| `CREWVIA_HERDR_WORKSPACE` | herdr backend が使うワークスペース名（デフォルト: `crewvia`） |
| `CREWVIA_HERDR_SOCK` | **テスト専用**。lib_mux が ping する herdr API socket のパスを上書きする（デフォルト: `~/.config/herdr/herdr.sock`）。実 herdr はこの変数を読まないため、本番で設定すると ping 先と server の bind 先が食い違う |
| `CREWVIA_MUX_RECORD_SWEEP` | 失効した mux spawn 記録（`registry/mux/*.json`）の掃除の停止スイッチ。`0` で掃除が何も見ず何も消さない（既定は有効）。**止まるのは掃除だけ**で、記録の書き込みロックは常に働く。dispatcher は cycle ごとに新しい python なので dispatcher の env に足して再起動すれば効く。`start.sh` は env をそのまま読む。掃除は消す側の 1 者なので食い違っても危険な側には倒れない（規則を共有する `lib_dep_rules` 等に env スイッチを付けない理由と逆）。`knowledge/daemon-authority.md` §7-14 |
| `CREWVIA_MUX_TEST_ISOLATION` | **テスト専用**。テスト中であることの印。`tests/conftest.py` が `os.environ` に置くので subprocess にも継承される。これが立っている間、既定の宛先 (`crewvia`) や接頭辞なしのペイン名を名指しする mux verb は `MuxTestIsolationError` で拒否される (2026-09-23 の本番 dispatcher 乗っ取り事故の再発防止。`knowledge/daemon-authority.md` §7-11)。**セッションが作った宛先 (`crewvia-pytest-<pid>-<hex>`) は pytest の終了時に片付けられる**（`tests/pytest_workspace_sweep.py`。以前は herdr に空の workspace が 1 回ごとに溜まっていた） |
| `CREWVIA_PYTEST_WORKSPACE_SWEEP` | **テスト専用**。pytest 終了時の**残骸掃除**の停止スイッチ。`0` のときだけ、死んだ pid の `crewvia-pytest-<pid>-<hex>` を何も見ず何も消さない（既定は有効）。**止まるのは残骸掃除だけ**で、セッション自身の宛先の後始末は止まらない（自分の label だけを消すので危険が無く、止めると元の漏れに戻る）。消すのは「形が合う・pid が `ESRCH`・live な pane が無い」の AND を満たす宛先だけ。pytest を起動するシェルの env に足すだけで効く（デーモンの再起動は不要）。`knowledge/daemon-authority.md` §7-15 |
| `CREWVIA_MUX_PANE_PREFIX` | **テスト専用**。ペイン名の名前空間。**本番は空 (no-op)**。設定すると `spawn("dispatcher")` が `<prefix>dispatcher` に解決され、本番のペイン名そのものが到達不能になる |
| `NTFY_URL` | ntfy サーバーの URL。`approval_channel.ntfy.url` より優先 |
| `NTFY_TOPIC` | ntfy 通知トピック名。**必須** — 空のまま運用すると通知が silent skip される |
| `NTFY_USER` | ntfy Basic 認証ユーザー名。`auth-default-access: deny-all` サーバーでは必須 |
| `NTFY_PASS` | ntfy Basic 認証パスワード |
| `APPROVAL_TOKEN_TTL_SECONDS` | ntfy ワンタイムトークンの有効期限（秒）。デフォルト: 900 |
| `CREWVIA_VERIFICATION_UI` | Taskvia 側の verification UI 表示制御。**Taskvia の Vercel env に設定** |

---

## 設計原則

1. **mux 非依存** - mux（tmux / herdr）がなくても動く。どちらもオプション（並列モード用）
2. **Taskvia非依存** - Taskvia未接続でもスタンドアロンで動作可能
3. **名前はポジション** - 同スキルWorkerは同名前を引き継ぐ
4. **公開前提** - ドメイン固有設定を外に出し、設定ファイルで全カスタマイズ可能

---

## 今後の拡張余地

- [x] ~~Planner ロール~~ → `planning` スキルでプランレビュー Worker (Priya) として実装済み。crewvia-plan-review skill 参照。
- [ ] `plan.sh status` の JSON 出力サポート（WIP 計測の grep を置き換える）
- [ ] task frontmatter にブランチ名を持たせて Worker に伝達
- [ ] mission 間の優先度設定（現状は default_mission 優先のみ）
