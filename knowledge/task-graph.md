# herdr-task-graph 連携の運用メモ

crewvia の queue を herdr plugin `tyz-works/herdr-task-graph`
（plugin id `io.github.tyz-works.task-graph`）に描かせるための、導入・起動・制約・切り分け。
生成機構の設計は `scripts/plan.sh` の task-graph 部と `tests/test_task_graph.py`、
利用者向けの手順は README「Task graph view with herdr-task-graph」。ここには
**実測で分かったこと**と、そこから決めた方針の根拠を残す。

検証環境: herdr 0.9.0（protocol 22）、plugin 0.1.1（commit 8801fde）、2026-09-25。

---

## 1. 導入方式: config dir への symlink を推奨する

plugin が tasks.json を探す順は `--config` → `HERDR_TASKS_FILE` → `$HERDR_PLUGIN_CONFIG_DIR/tasks.json`
→ plugin 同梱の `tasks.json`（サンプル）。crewvia が正のファイルを持つので、候補は 3 つ。

| 方式 | 結果 | 判定 |
|---|---|---|
| `HERDR_TASKS_FILE=<crewvia のパス>` | **稼働中の herdr のペインには届かない**（下記） | 不採用 |
| config dir に**コピー** | 生成のたびに古くなる。二重管理 | 不採用 |
| config dir から crewvia のファイルへ **symlink** | 1 回作れば以後は何もしなくてよい。実機で動作確認 | **採用** |

### なぜ `HERDR_TASKS_FILE` が使えないか（実測）

plugin のペインは **呼び出しシェルではなく herdr server の子プロセス**として起動する
（Director / Worker のタブが server の env を継承するのと同じ性質）。
隔離した herdr で、ペインのコマンドを `env | sort` に差し替えて確認した:

- `HERDR_TASKS_FILE=... herdr plugin action invoke ...`（**呼び出し側の env にだけ**設定）
  → ペインの env に `HERDR_TASKS_FILE` は**無い**
- server を起動するときの env に置いた変数 → ペインの env に**ある**

つまり効く条件は「herdr server の起動時 env に入れておく」だけで、本番の herdr でそれを
やるには server の再起動が要り、**全タブ（Director / Worker / デーモン）が死ぬ**。
README も plugin の README も `HERDR_TASKS_FILE` を案内しているが、稼働中の herdr には
効かない前提で読むこと。

### symlink が壊れない理由（実測）

生成は `tasks.json.tmp.<pid>` に書いて `os.replace` する。symlink はパス（`registry/task-graph/tasks.json`）
を指しているので、置換後の新しい inode をそのまま読む。隔離 herdr で
`plan.sh update` → ファイルが更新 → plugin で `r` → 反映、まで通した。

### 落とし穴

- **主 checkout で実行すること。** worktree の `registry/` を指す symlink を作ると、
  Director / Worker が更新するファイルとは別物を見る（`task_graph_repo_root()` が
  `CREWVIA_REPO_ROOT` を優先するのと同じ理由）。`readlink` で symlink の先を確認する
- **先に `plan.sh task-graph` を 1 回走らせてから symlink を作る。** 未生成の状態でも
  `ln -s` は通るが、plugin は「ファイルが無い」ときに**エラーではなくサンプルを出す**（§4）
- `HERDR_PLUGIN_CONFIG_DIR` は `herdr plugin config-dir <id>` の出力と同じ場所。**このディレクトリは
  herdr の設定領域（`~/.config` 配下）なので、Worker は読み書きしない**。導入は人間（Director / ユーザー）が行う

`herdr plugin install tyz-works/herdr-task-graph`（GitHub から）自体は、この検証では
**実行していない**（本番 herdr を変更しないため、ネットワークにも出ていない）。検証は
`herdr plugin link`（ローカル clone）で行った。config dir の仕組みは install でも同じ
（`herdr plugin config-dir` が返すパスを使う）。

---

## 2. 起動: 自動では開かない

```bash
herdr plugin action invoke open-task-graph --plugin io.github.tyz-works.task-graph
```

`./crewvia`（`scripts/start.sh`）は plugin を**自動起動しない**。これは任意の付加機能で、
crewvia の設計原則「mux 非依存」を崩さないため。plugin が無い・入れていない環境で
`./crewvia` が余計な失敗をしないことが、自動起動しない最大の理由。

manifest の action の説明は "Open or focus" だが、**実測では invoke のたびに新しいタブが開く**
（既存の Task Graph タブがあっても重複を避けない）。自動化に組み込むと開くたびにタブが増える。
見終わったらタブを閉じる。

---

## 3. plugin / herdr が無い環境での見え方

- 生成（`plan.sh` 側）は **herdr にも mux にも触れない**。plugin が無い・herdr が PATH に無い・
  `CREWVIA_MUX=tmux` — どれでも `plan.sh` は従来どおり動き、終了コードも変わらない
  （実測: PATH に herdr が無く tmux モードの `plan.sh update` が `rc=0`）
- `registry/task-graph/` にファイルが増えるだけ（`tasks.json` / `tasks.json.lock` /
  `tasks.json.pending.lock`。`.gitignore` 済みで `git status` に出ない）。誰も読まなければ無害
- 生成そのものが失敗したときは stderr に 1 行だけ出て、`plan.sh` の動作・終了コードは変わらない
- 生成を完全に止めるなら `CREWVIA_TASK_GRAPH=0`（何も書かず、ログも出さない）。既定は有効。
  コストは 1 回あたり +10 ms 前後（t001 / t002 の実測）

---

## 4. 制約（実測）

### 4-1. plugin は `r` キーでしか再読み込みしない

`task_graph.py` が `load_config` を呼ぶのは**起動時と `r` キーの 2 箇所だけ**（ファイル監視なし）。
隔離 herdr で確認:

```
BEFORE                          pane: 1 running · 0 ready · 4 waiting · 47 done · 1 blocked
plan.sh update t006 --status done   (生成物は即座に更新: done 48 / ready 1 / waiting 2)
4 秒後、キー操作なし             pane: 1 running · 0 ready · 4 waiting · 47 done · 1 blocked   ← 古いまま
'r' を送信                      pane: 1 running · 0 ready · 2 waiting · 48 done · 1 blocked   ← 反映
```

タスクの DAG は最後に読んだ姿のまま。「crewvia は更新しているのに画面が変わらない」は
故障ではなく仕様。`r` を押す。

（この節の初版は「生きているのは herdr が持つエージェントの状態だけ」と書いていたが、
**Worker が動いている環境では成り立たない**。次の §4-1b。）

### 4-1b. herdr 0.9.0 では、エージェントが 1 つでも居ると plugin は `[offline]` になる

**この環境（herdr 0.9.0 + plugin 0.1.1）では、エージェントが 1 つでも居ると plugin は
offline になり、live なエージェント状態は来ない。** 実運用（Worker が動いている状態）では
常に該当する。得られるのは crewvia が生成した依存関係と status の可視化だけ。

見え方: ヘッダーが `TASK DAG [offline]`、最下部に `ERROR: [Errno 32] Broken pipe`。
**故障ではない**（平常運用でこう見える）。

原因（QA t006 が隔離ソケットを直接叩いて実証。`task_graph.py:_run_session` を読んで確認）:

```
snapshot 送信 -> 応答 -> 同じ接続で 2 つ目のリクエスト (events.subscribe) -> BrokenPipeError
events.subscribe を最初のリクエストとして送る -> subscription_started (通る)
```

herdr 0.9.0 の server は、subscribe が最初のリクエストでない限り **1 接続 1 リクエスト**で閉じる。
plugin は同一接続で `session.snapshot` → `events.subscribe` の順に送るので、`agents[]` が空で
なければ必ず失敗する。**エージェント 0 件だと subscribe まで進まない**（購読対象が無いので
`_run_session` が手前で返る）ため気付かない。A/B: 0 件 → `[live]` / 1 件 → `[offline]`。

- **直す場所は upstream（`tyz-works/herdr-task-graph`）**: snapshot 用と subscribe 用で接続を分ける。
  **crewvia 側では直せない**。upstream は t023 では変更していない（Director が別途判断）
- README / CLAUDE.md の記述は、この実態に合わせてある
- plugin は失敗のたびに約 0.75 秒待って snapshot からやり直すので、offline 表示のまま
  `agents[]` は繰り返し読み込まれる（§4-4 の検証でも agents[] が箱に反映された）。ただし
  agent の状態変化が箱に追従するかどうかは**確認していない**
- crewvia の task には常に `status` が書かれ、plugin の `task_states` は `status` があれば
  agent 由来の導出より優先する（コード読み。§4-4 の検証でも `agent` の無い task が `RUN` と出た）。
  したがって **`[offline]` でも task の状態表示は crewvia の値そのもの**で、live の agent 状態が
  無いことで失うのは「箱の中の agent 名・状態の表示」だけ

### 4-2. ファイルが見つからないと、エラーではなくサンプルが出る

`find_config` は候補を順に `is_file()` で試し、最後の候補に**同梱のサンプル `tasks.json`**
（"Product delivery"）を置いている。symlink の先が無い（生成前・パス違い・主 checkout でなく
worktree を指した）と、**エラーなしでサンプルが描かれる**。

→ **画面のタイトルが `crewvia / <slug>`（active mission が 1 件のとき）または
`crewvia / N missions`（0 件・2 件以上のとき）であることを毎回の確認点にする。**
どちらでもなければ crewvia のファイルは読めていない。crewvia は 1 mission だけの状態が
普通なので、`N missions` だけを確認点にすると日常的に誤判定する（実装は `scripts/plan.sh` の
`len(slugs) == 1` 分岐）。

### 4-3. 「QA FAIL 後の WAIT 表示は信用しない」は不要（crewvia 側で READY を導出している）

t001 は `pending` の READY / WAIT を plugin の導出に**委ねず**、crewvia 側で
`unmet_dependencies()`（`plan.sh pull` と dispatcher が使うのと同じ 1 つの規則）を使って
明示的に `ready` / `waiting` を書く。`failed` / `cancelled` の依存を満たされた扱いにする
規則が画面にも反映されるので、**QA FAIL の直後でも、dispatch される task は READY と出る**。
よって「その瞬間の WAIT は信用しない」という但し書きは要らない。逆に、plugin の導出に
戻す変更を入れるなら、この但し書きが必要になる。

### 4-4. `pane_match` は live の herdr では当たらない（**未解決・要フォローアップ**。`pane_id` なら当たることを隔離環境で確認済み）

**現状で起きること**: task の箱に Worker（agent）の表示が出ず、Enter を押しても何も起きない。
§4-1b の offline とは独立した問題（offline でも、直っても、`pane_match` は当たらない）。

生成物は `worker` が就いている task に `pane_match: "<Name>-worker"` を書く。
plugin は `pane_match` を **`session.snapshot` の `agents[]` の
`pane_id / name / title / display_agent / agent / terminal_title`** に対して部分一致で探す。

実測（`herdr api snapshot` = plugin が読むのと同じ snapshot）:

- 隔離 herdr で pane に label `Ren-worker` を付け、`claude` 名のプロセスを走らせた:
  `agents[]` の項目は `agent: "claude"`, `pane_id`, `cwd`, `agent_status` … で、
  **`label` も `name` も `title` も無い**。plugin の画面にも紐付けは出なかった
- 本番の snapshot（読み取りのみ）: Worker の項目は `agent: "claude"` と
  `terminal_title: "◑ Mission workflow setup"`（Claude Code のセッション名。毎回変わる）。
  **`Wei-worker` という文字列は照合対象のどのフィールドにも出ない**

crewvia のペイン名 `<Name>-worker` は herdr の pane の **`label`** に入っているが、plugin は
`label` を見ない。結果:

- 影響**なし**: status / 依存 / READY・WAIT の区別 / 人間待ちの見分け
- 影響**あり**: task の箱に「どの Worker がやっているか」が出ない、Enter でその pane に飛べない

t001 が未確認としていた点（label か terminal title か）の答えは「どちらでもない」。

#### 検証済み: task に `pane_id` を書けば当たる（offline のままでも）

隔離 herdr（別 HOME、`env -i` で起動した server。本番の `crewvia` workspace には触れていない）で、
label `Ren-worker` の pane に `claude` 名のプロセスを走らせ、次の 2 task を持つ tasks.json を
plugin に読ませた（2026-09-25、t023）:

```
{"id": "qa-f1:t001", "status": "running", "pane_match": "Ren-worker"}   ← label で照合
{"id": "qa-f1:t002", "status": "running", "pane_id": "w1:p9"}           ← pane_id で照合
```

| 観測 | `pane_match: "Ren-worker"` | `pane_id: "w1:p9"` |
|---|---|---|
| 箱の agent 表示 | **無し** | `claude · idle` が出た |
| Enter（選択して押す） | フォーカス不変（`w1:pA` のまま） | **`w1:p9`（Worker の pane）にフォーカスが移った** |

この間ヘッダーは `[offline]`（`Broken pipe`）のまま。つまり **offline 中でも、snapshot 1 回分の
`agents[]` は読み込まれ、`pane_id` の完全一致は効く**（§4-1b のとおり plugin は再接続のたびに
snapshot を取り直す）。t006 QA が「未検証」としていた点は、これで確認できた。

**確認できていないこと**（「できる」とは書かない）:

- **crewvia の生成器が `pane_id` を書く実装はまだ無い**。上の検証は tasks.json を手で書いた。
  今の crewvia の画面は「Worker 表示なし・Enter 不可」のまま
- 実際の Claude Code の Worker pane での確認。検証は `claude` という名前のスタンドイン（`sleep`）で、
  herdr が `agent: "claude"` と認識する経路は同じだが、実 Worker では見ていない
- agent の状態変化（idle → working 等）が offline 中の箱に追従するか
- pane id が herdr の再起動・復元を跨いで有効か（下記の注意点の前提）

#### 直す道筋（crewvia 側で完結できる可能性がある。実装は別 task）

crewvia は Worker 起動時に `registry/mux/<Name>-worker.json` へ `pane_id`
（例 `wP:p80`）を記録している。生成器がここから `pane_id` を引いて書けば、upstream を
触らずに紐付けられる見込み（上の検証が根拠）。注意点:

- pane id は herdr の再起動・復元で変わりうる。`server.generation` が現在の server と
  一致しない記録は使わない（古い記録は実在する。`Arjun-worker.json` は 2026-09-23 のもの）
- 消えた Worker の記録が残るので、`queue/assignments/<worker>` との AND を今の `pane_match`
  と同様に取る
- upstream の `label` 対応（plugin 側で `label` も検索する）でも直るが、別リポジトリの変更

これは生成器の変更なので、ドキュメントの task（t005 / t023）では実装していない。
task 化は Director の判断。

### 4-5. 完了済み task が多いと画面が詰まる

plugin は全 task を並べる。完了済みが数十件あると箱が潰れて ID が読めない
（本番 queue の複製 53 task で確認）。描くのは **active な mission だけ**なので、
終わった mission は `plan.sh archive <slug>` で退避すれば画面から消える。

---

## 5. 画面の読み方

| 見え方 | 意味 |
|---|---|
| `READY` | `pending` で依存が満たされている。今 pull されうる（並列に走れる） |
| `WAIT` | `pending` で依存待ち |
| `RUN` | `in_progress` / `verifying` |
| `DONE` | `done` / `verified` / `skipped`（title に `[skip]`） |
| `FAIL` | `failed` / `verification_failed`（`[検証NG]`）/ `cancelled`（`[中止]`）/ 読めないカード（`[破損]`） |
| `BLOCK [停止]` | `blocked`（`blocked_reason` 付きで明示的に止めてある） |
| `BLOCK [要判断]` | **人間の判断待ち**: `needs_director` / `needs_human_review` / `ready_for_verification` |
| `BLOCK [status不明]` | 対応表に無い status。done にも ready にも倒さず止めて見せる |

依存待ち（`WAIT`）と人間待ち（`BLOCK [要判断]`）は plugin の状態そのものが違うので
取り違えない。title の `[依存不明: id]` は存在しない task への依存、
`[循環依存: id]` は循環を閉じている辺をそこで切ってある印（plugin は循環があるとファイル
全体を拒否するので、隔離して残りを描く）、`[表示する task なし]` は task が 1 件も無いとき
（最後の mission を archive した直後）の 1 ノード。

---

## 6. 切り分け

| 症状 | まず見るところ |
|---|---|
| 画面が古い | `r` を押したか。ファイル側は `ls -l registry/task-graph/tasks.json` の mtime で確認 |
| 画面が crewvia の内容でない（サンプルが出る） | タイトルが `crewvia / <slug>`（1 mission）か `crewvia / N missions`（0・2 件以上）か。symlink の先が存在するか、主 checkout を指しているか |
| ファイルが更新されない | `CREWVIA_TASK_GRAPH=0` になっていないか。`plan.sh task-graph` を手で実行して出力を見る。`plan.sh` の stderr に生成失敗の 1 行が出ていないか |
| `plan.sh task-graph` が「queue が違う」と拒否する | `CREWVIA_QUEUE` が `<root>/queue` でない。書き先を明示するなら `CREWVIA_TASK_GRAPH_FILE` |
| ヘッダーが `[offline]`、最下部に `ERROR: [Errno 32] Broken pipe` | Worker が動いている間は**平常**（§4-1b。herdr 0.9.0 の plugin の欠陥で、upstream 側の修正待ち）。crewvia の tasks.json 側を疑わない。task の状態表示は crewvia の値のまま |
| task に Worker 名が出ない・Enter で pane に飛べない | §4-4（現状は仕様上出ない。生成器が `pane_id` を書いていない） |
| plugin が読み込みに失敗する | plugin は空の `tasks` / id の空・重複 / 解決できない `depends_on` / 循環でファイル全体を拒否する。生成器は 4 つとも潰してあるので、出たらバグ（`enforce_task_graph_contract()` を見る） |

---

## 7. herdr 連携を検証するときの隔離のしかた（再現手順）

**本番の herdr（workspace `crewvia`）には触れない。** 別 server を別 HOME で立てる:

- `HOME=/tmp/<短いパス>` を付けて起動する。herdr は `$HOME/.config/herdr/herdr.sock` に
  bind するので、本番のソケットとは別になる。**パスが長いと `sun_path` の上限（108 文字）で
  失敗する**ので、scratchpad ではなく `/tmp/hgt` のような短いパスを使う（実測で当たった）
- 自分の pane の env に `HERDR_SOCKET_PATH`（本番のソケット）が入っている。`env -i HOME=... PATH=... herdr ...`
  のように**環境を空から組む**こと。継承すると本番に向く
- server の起動は stdout/stderr を `DEVNULL` にして `start_new_session=True` の Popen
  （出力をリダイレクトした素の `herdr server` は fd を掴んで返らない。memory
  `herdr-server-autostart-blocking-bug`）
- 隔離を確認してから触る: 同じ env で `herdr status server` が「running」を返す＝隔離側、
  別 HOME で本番のソケットに繋がろうとして `sun_path` エラーになる＝分離できている
- 止めるときは **`herdr server stop` ではなく PID を指定して `kill`**（起動時に出した PID。
  `/proc/<pid>/environ` で `HOME` が隔離側であることを確認してから）。本番の PID
  （`ps` で PPID=1 の `herdr server` が複数出る）と取り違えない
- plugin の照合を見るなら `herdr api snapshot`（plugin が読むのと同じ）を読む。
  画面を `herdr pane read <pane_id>` で取るのは補助
- 後始末: このリポジトリのルールは `rm -rf` を禁じている。`/tmp/hgt` は残る（`/tmp` なので
  再起動で消える）
