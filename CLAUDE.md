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

- plugin `tyz-works/herdr-task-graph` が herdr 上にタスク依存の DAG を描く。crewvia 側は `plan.sh` が queue を書き換えるたびに `registry/task-graph/tasks.json` を再生成するだけ。plugin・herdr が無くても何も起きずエラーも出ない。停止スイッチ: `CREWVIA_TASK_GRAPH=0`（1 バイトも書かない）
- 導入は生成物を plugin の config dir に **symlink** する方式。`HERDR_TASKS_FILE` は使えない（plugin ペインは herdr server の env を継承し、シェルの export は届かない）。手順は README「Task graph view」
- `./crewvia` は plugin を自動起動しない（`herdr plugin action invoke open-task-graph --plugin io.github.tyz-works.task-graph`。invoke のたびに新しいタブ）
- **必要な plugin は 0.3.0 以上**。入れ替え・戻しは本番 herdr の変更なので Director がユーザーの了承を得て行う（README「Upgrading the plugin」）
- crewvia のファイルを読めているかは画面タイトル（`crewvia / <slug>` か `crewvia / N missions`）で判定する。違えば同梱サンプルが出ている
- 版ごとの挙動・`pane_match` と `pane_id`・検証手順・切り分け: `knowledge/task-graph.md`

---

## ディレクトリ構成（骨格）

各ファイルの設計の経緯・実測・理由は **`knowledge/file-map.md`**（旧本節。ファイルごとの見出し）と、そこから張った設計文書にある。
`scripts/` と `tests/` で作業するときは、それぞれの `CLAUDE.md`（入れ子。そこで作業するときだけ読み込まれる）に lib ごとの契約とテストの作法がある。

```
config/   worker-names.yaml（名前プール）/ crewvia.yaml（承認チャネル・WIP・model_per_skill）
hooks/    pre-tool-use.sh（Taskvia 承認）/ post-tool-use.sh（ログ投稿・デーモン同時死の backstop）
agents/   director.md / worker.md（システムプロンプト）
scripts/
  start.sh              起動（Worker の skills 追従・TARGET_DIR 記録）
  plan.sh               タスクプラン CLI（per-task / multi-mission、task-graph 再生成）
  dispatcher.sh         常駐の割り当て判定者（queue を読む唯一のデーモン）
  watchdog.py           Worker 生存監視・終了の唯一の実行者
  lib_daemon_watch.py   dispatcher / watchdog の相互監視・restart
  lib_retirement.py     retirement marker（dispatcher が判定 → watchdog が実行）
  lib_task_cards.py     queue / registry / config を読む唯一の入口
  lib_dep_rules.py      「依存が満たされた」の唯一の定義
  lib_daemon_state.py   デーモン側 JSON 状態ストアの入口
  lib_mux.py / .sh      mux 抽象化（tmux / herdr）
  lib_worker_target.py  Worker の TARGET_DIR 記録
  lib_registry.py       workers.yaml を書く入口
  lib_review_refusal.py codex-review の拒否記録
  kai-review.sh         Codex reviewer 起動ラッパー / taskvia-sync.sh  queue → Taskvia 同期
queue/    state.yaml / missions/<slug>/{mission.yaml,tasks/tNNN.md} / archive/
registry/ workers.yaml / workers/ / heartbeats/ / mux/ / retirements/ / task-graph/ / daemons/
```

### 不変条件（推測できず、破ると事故になるもの）

1. queue / registry / config のファイルを開くコードは、必ず `scripts/lib_task_cards.py` の入口を通す（読めない = `Unreadable`。`None` / `{}` / `[]` に潰さない）
2. task の識別子は**ファイル名**（`tNNN.md`）。frontmatter の `id` 欄ではない
3. 依存の判定は `lib_dep_rules.py` だけ。コピーしない（`failed` の依存は保留で、出口は `plan.sh release-dep`）
4. デーモンの再起動は `lib_daemon_watch.py restart`。`lib_mux.py kill` / `spawn` を素で叩かない
5. 共有規則に env 停止スイッチを付けない（dispatcher と plan.sh で答えが割れる）
6. `handoff_path` は絶対パスのみ
7. `registry/daemons/*` の通知台帳（`notified-state.json`）・拒否記録（`review-refusals/`）は消してよい。それが復旧手順

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
| `CREWVIA_WORKER_MODEL` | Worker が使用するモデルを強制指定。`config/crewvia.yaml` の `model_per_skill` による skill 別自動選択より優先（最優先）。空にするか未設定の場合は skill に応じて自動選択される。**docs / qa / verify は config で Sonnet なので、Haiku 回避のために起動のたびに設定する必要は無い**（t028）。1 回だけ別モデルを使わせたいとき用 |
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
| `CREWVIA_HERDR_SOCK` | **テスト専用**。lib_mux が ping する herdr socket の上書き。**本番で設定するな**（ping 先と server の bind 先が食い違う）。詳細: `tests/CLAUDE.md` |
| `CREWVIA_MUX_RECORD_SWEEP` | 失効した mux spawn 記録（`registry/mux/*.json`）の掃除の停止スイッチ。`0` で掃除が何も見ず何も消さない（既定は有効）。**止まるのは掃除だけ**で、記録の書き込みロックは常に働く。dispatcher は cycle ごとに新しい python なので dispatcher の env に足して再起動すれば効く。`start.sh` は env をそのまま読む。掃除は消す側の 1 者なので食い違っても危険な側には倒れない（規則を共有する `lib_dep_rules` 等に env スイッチを付けない理由と逆）。`knowledge/daemon-authority.md` §7-14 |
| `CREWVIA_MUX_TEST_ISOLATION` | **テスト専用**。テスト中の印（`tests/conftest.py` が置く）。立っている間、本番の宛先名を指す mux verb は拒否される。**本番で設定するな**。詳細: `tests/CLAUDE.md` |
| `CREWVIA_PYTEST_WORKSPACE_SWEEP` | **テスト専用**。pytest 終了時の残骸掃除の停止スイッチ（`0` で止まる）。**本番で設定するな**。詳細: `tests/CLAUDE.md` |
| `CREWVIA_MUX_PANE_PREFIX` | **テスト専用**。ペイン名の名前空間。**本番は空**（設定すると本番のペイン名が到達不能になる）。詳細: `tests/CLAUDE.md` |
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
