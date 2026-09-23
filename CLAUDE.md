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
    dispatcher.sh       並列モードの常駐割り当てデーモン（idle Worker への自動 assign + codex-review spawn）
                        **仕事の割り当ての判定者**（queue/ を読む唯一のデーモン）
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
                        再発防止は tests/test_task_card_identity.py（両者が同じ queue から
                        同じ task 集合を導くことの直接 assert + コピー検出）と
                        tests/test_unobservable_is_not_empty.py（赤の実証は
                        tests/red_proof_unobservable.sh）
    lib_dep_rules.py    「依存が満たされた」の唯一の定義（`unmet_dependencies()` /
                        `DEAD_DEP_STATUSES`）。**plan.sh pull・plan.sh task-graph・
                        dispatcher.sh の 3 者がここだけを読む**。コピーを書き戻すと、
                        ズレが出るのは QA FAIL の直後だけ（= 誰も疑わない瞬間）になる。
                        フォールバックは持たない（読めなければ呼び出し側が落ちる）ので、
                        plan.sh を単体でコピーする隔離テストでは一緒に置くこと。
                        再発防止は tests/test_task_graph.py のコピー検出テスト
    kai-review.sh       Codex reviewer (Kai-codex) 起動ラッパー。詳細は `knowledge/codex-reviewer.md`
    taskvia-sync.sh     queue → Taskvia 同期
    lib_mux.py          mux 抽象化モジュール（TmuxBackend / HerdrBackend）
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
    mux/                mux バックエンドのタブ/ペイン ID キャッシュ（.gitignore 対象）
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
                        throttle マーカー（backstop-notify.throttle）を置く場所でもある
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
| `CREWVIA_TASK_GRAPH_FILE` | 生成物の書き先。既定は `$CREWVIA_REPO_ROOT/registry/task-graph/tasks.json`（未設定時のみ plan.sh の位置基準にフォールバック）。plugin 側にはこのパスを `HERDR_TASKS_FILE` で参照させる |
| `CREWVIA_MUX` | mux バックエンド選択: `tmux` / `herdr`。config `mode:` より優先 |
| `CREWVIA_MUX_ENABLED` | 並列モード有効化: `1` で並列 ON（`CREWVIA_MUX` 未設定時の tmux fallback）/ `0` でインラインモード強制。`CREWVIA_MUX` が設定済みなら不要 |
| `CREWVIA_TMUX_SESSION` | tmux backend が使うセッション名（デフォルト: `crewvia`） |
| `CREWVIA_HERDR_WORKSPACE` | herdr backend が使うワークスペース名（デフォルト: `crewvia`） |
| `CREWVIA_HERDR_SOCK` | **テスト専用**。lib_mux が ping する herdr API socket のパスを上書きする（デフォルト: `~/.config/herdr/herdr.sock`）。実 herdr はこの変数を読まないため、本番で設定すると ping 先と server の bind 先が食い違う |
| `CREWVIA_MUX_TEST_ISOLATION` | **テスト専用**。テスト中であることの印。`tests/conftest.py` が `os.environ` に置くので subprocess にも継承される。これが立っている間、既定の宛先 (`crewvia`) や接頭辞なしのペイン名を名指しする mux verb は `MuxTestIsolationError` で拒否される (2026-09-23 の本番 dispatcher 乗っ取り事故の再発防止。`knowledge/daemon-authority.md` §7-11) |
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
