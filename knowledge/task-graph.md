# herdr-task-graph 連携の運用メモ

crewvia の queue を herdr plugin `tyz-works/herdr-task-graph`
（plugin id `io.github.tyz-works.task-graph`）に描かせるための、導入・起動・制約・切り分け。
生成機構の設計は `scripts/plan.sh` の task-graph 部と `tests/test_task_graph.py`、
利用者向けの手順は README「Task graph view with herdr-task-graph」。ここには
**実測で分かったこと**と、そこから決めた方針の根拠を残す。

検証環境: herdr 0.9.0（protocol 22）。§1〜§4 の実測は plugin 0.1.1（commit 8801fde、2026-09-25）。
**結合確認（t011、2026-09-26）は crewvia `f9353fa` + plugin 0.3.0（`d2b8195`）**で、結果は §0 と §8。

---

## 0. plugin の版ごとの違い（t011 の実測で確定）

必要な最低 version は **0.3.0**（label / group の表示と箱の折り返しは 0.3.0 から）。

| 症状（0.1.1） | 直った版 | 0.3.0 での実測（隔離 herdr、3 mission / 62 task、Worker 相当 pane 2 つ） |
|---|---|---|
| `r` でしか再読み込みしない（§4-1） | 0.2.0 | `r` なしで `plan.sh update` から **0.4〜0.6 秒**（0.58 / 0.36 / 0.35 / 0.36 秒。`plan.sh` 自体の 0.15 秒を含む） |
| エージェントが居ると `[offline]`・`Broken pipe`（§4-1b） | 0.2.0（snapshot と subscribe を別接続に） | ヘッダー `[live]`・ERROR 行なし。箱に `claude · idle` |
| 設定が読めないと黙ってサンプルに落ちる（§4-2） | 0.2.0（設定済みで読めないときだけ） | dangling symlink で `ERROR: cannot read …/tasks.json` を出し、最後の内容を保つ。symlink を戻すと 2 秒で回復。`--once` は exit 1 |
| 箱が重なる・入りきらない行が消える（§4-5） | 0.3.0 | 幅 80/120/200 で j を 62 回押すと全 62 task に届く（選択 box が画面外のフレーム 0）。重なりの署名（`[DO|`・`+--+--+`）0 件、陽性対照（0.1.1 の画面）では検出 |
| 箱に `tNNN`・タイトルが出ない（P-2） | 0.3.0（`label` / `group`） | 全幅で `[STATE] tNNN · <mission 末尾>` + タイトル + agent の 3 段 |

**0.3.0 でも残る**（§8 の観察）: config dir に entry が**無い**とサンプル（§4-2）/ agent 名は `claude`
（Worker 名は出ない）/ 幅 200 でタイトルが短い / `open-task-graph` がタブを増やす（§9）。

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
見終わったらタブを閉じる。0.3.0 でも同じ（t011 で確認）。今回は扱わない（§9）。

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

### 4-1. plugin は `r` キーでしか再読み込みしない（**0.1.1 のみ**。0.2.0 以降は自動。§0）

以下は 0.1.1 の実測。`task_graph.py` が `load_config` を呼ぶのは**起動時と `r` キーの 2 箇所だけ**（ファイル監視なし）。
隔離 herdr で確認:

```
BEFORE                          pane: 1 running · 0 ready · 4 waiting · 47 done · 1 blocked
plan.sh update t006 --status done   (生成物は即座に更新: done 48 / ready 1 / waiting 2)
4 秒後、キー操作なし             pane: 1 running · 0 ready · 4 waiting · 47 done · 1 blocked   ← 古いまま
'r' を送信                      pane: 1 running · 0 ready · 2 waiting · 48 done · 1 blocked   ← 反映
```

タスクの DAG は最後に読んだ姿のまま。「crewvia は更新しているのに画面が変わらない」は
故障ではなく 0.1.1 の仕様。`r` を押す（0.2.0 以降はファイルの mtime・inode・size を見て自動で
読み直す。symlink は追うので `os.replace` による置換も検知する。t011 の実測は §0）。

（この節の初版は「生きているのは herdr が持つエージェントの状態だけ」と書いていたが、
**Worker が動いている環境では成り立たない**。次の §4-1b。）

### 4-1b. herdr 0.9.0 では、エージェントが 1 つでも居ると plugin は `[offline]` になる（**0.1.1 のみ。0.2.0 で解消**。§0）

以下は 0.1.1 の実測と原因。0.3.0 では `[live]` になることを t011 が確認した（§8）。

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
  **crewvia 側では直せない**。→ upstream 0.2.0 がこの形で直した（本番の plugin を 0.3.0 に
  入れ替える手順は README「Upgrading the plugin」と §8）
- README / CLAUDE.md の記述は、この実態に合わせてある
- plugin は失敗のたびに約 0.75 秒待って snapshot からやり直すので、offline 表示のまま
  `agents[]` は繰り返し読み込まれる（§4-4 の検証でも agents[] が箱に反映された）。ただし
  agent の状態変化が箱に追従するかどうかは**確認していない**
- crewvia の task には常に `status` が書かれ、plugin の `task_states` は `status` があれば
  agent 由来の導出より優先する（コード読み。§4-4 の検証でも `agent` の無い task が `RUN` と出た）。
  したがって **`[offline]` でも task の状態表示は crewvia の値そのもの**で、live の agent 状態が
  無いことで失うのは「箱の中の agent 名・状態の表示」だけ

### 4-2. ファイルが見つからないと、エラーではなくサンプルが出る（0.2.0 以降は「entry が無いとき」だけ）

0.1.1 の挙動。`find_config` は候補を順に `is_file()` で試し、最後の候補に**同梱のサンプル `tasks.json`**
（"Product delivery"）を置いている。symlink の先が無い（生成前・パス違い・主 checkout でなく
worktree を指した）と、**エラーなしでサンプルが描かれる**。

**0.2.0 以降**: config dir に `tasks.json` の entry があれば（**dangling symlink を含む**。
`is_symlink() or exists()`）その読み取りを試み、読めなければサンプルでなく **ERROR を出して最後に
読めた内容を保つ**（t011 で実測）。サンプルが出るのは「entry が config dir に**無い**」ときだけ
（何も設定されていない）。つまり symlink を作り忘れた・別の config dir に作った、はまだ黙って
サンプルに落ちる。だから確認点は今も要る:

→ **画面のタイトルが `crewvia / <slug>`（active mission が 1 件のとき）または
`crewvia / N missions`（0 件・2 件以上のとき）であることを毎回の確認点にする。**
どちらでもなければ crewvia のファイルは読めていない。crewvia は 1 mission だけの状態が
普通なので、`N missions` だけを確認点にすると日常的に誤判定する（実装は `scripts/plan.sh` の
`len(slugs) == 1` 分岐）。

### 4-3. 「QA FAIL 後の WAIT 表示は信用しない」は不要（crewvia 側で READY を導出している）

t001 は `pending` の READY / WAIT を plugin の導出に**委ねず**、crewvia 側で
`card_dependencies()`（`plan.sh pull` と dispatcher が使うのと同じ 1 つの規則）を使って
明示的に `ready` / `waiting` を書く。規則は t007 で変わった: `cancelled` の依存は満たされた
扱い、**`failed` の依存は保留（Director が `plan.sh release-dep` するまで pull も dispatch も
拒否）**。保留の task は `blocked` + `[保留: tXXX が failed]` の印で描かれる（`waiting` に
すると「依存が終われば勝手に進む」と読めてしまう）。画面と pull の可否は同じ規則から来るので、
**QA FAIL の直後でも、READY と出ている task は実際に pull できる**。よって「その瞬間の WAIT
は信用しない」という但し書きは要らない。逆に、plugin の導出に戻す変更を入れるなら、
この但し書きが必要になる。設計と rollback は `knowledge/failed-dependency-hold.md`。

### 4-4. `pane_match` は live の herdr では当たらない（**crewvia 側は解決: 生成器が `pane_id` を書く**。t007）

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

- ~~生成器が書いた `pane_id` を実 herdr の plugin が受けて Enter で飛べること~~ → **t011 が確認済み**
  （plugin 0.3.0 + crewvia `f9353fa`。生成物の node の `pane_id` が t011 = `w1:p2`、t012 = `w1:p3`
  で、live ペインで選択して Enter → `focused_pane_id` が `w1:p5`（plugin）→ `w1:p2` / `w1:p3` に
  移った。`registry/mux/<Name>-worker.json` の `pane_id` と一致）
- 実際の Claude Code の Worker pane での確認（t011 も `claude` 名の `sleep` のスタンドイン）。検証は `claude` という名前のスタンドイン（`sleep`）で、
  herdr が `agent: "claude"` と認識する経路は同じだが、実 Worker では見ていない
- agent の状態変化（idle → working 等）が箱に追従するか（t011 は `claude · idle` の表示までで、
  状態の遷移は見ていない）
- pane id が herdr の再起動・復元を跨いで有効か（下記の注意点の前提）

#### 実装（t007）

生成器（`plan.sh` の `build_task_graph()`）は、`pane_match` を書く条件（status の allowlist と
`queue/assignments/<worker>` の AND）が揃った node に、`pane_id` も書く。`pane_match` は残す。

- 値は `registry/mux/<Worker>-worker.json` の `pane_id`。読むのは
  `lib_mux.recorded_herdr_pane_id()` 1 つ（記録の読み取りは既存の `read_pane_record()` =
  `load_json_store` の入口を通る）
- 書かない条件（どれも `pane_match` だけが残り、生成は落ちない）: 記録が無い / 読めない・壊れた
  JSON / object でない / `backend` が herdr でない / `pane_id` が空 / 世代の形が
  `<pid>:<starttime>` でない / **記録の server プロセスがもう居ない**
- **世代の検査は herdr に問い合わせずに行う**。herdr の世代は server プロセスの
  `<pid>:<starttime>`（`_herdr_connect_identified()`）なので、その pid が `/proc` にあり
  starttime が一致するかで「その pane id を発行した server がまだ生きているか」が言える。
  `lib_mux` の既存の世代取得（`_herdr_server_identity()`）は socket に接続するので使わない
  （生成は `retire --no-wait` から watchdog が同期で叩く経路にも乗る。herdr が固まっていると
  その分 Worker を誰も見ない時間ができる。CLAUDE.md の設計「生成は herdr に触れない」も崩れる）。
  外れた場合の害: 記録の server が生きている限り pane id は今の世代のものなので、外れが起きるのは
  「記録の server が死んだあと」だけで、その場合は書かない（plugin 側で「一致なし」になる）
- **`registry/mux/.records.lock` は取らない**。書き手（`write_pane_record()`）は `os.replace` では
  なく `write_text()`（truncate してから書く）なので、ロック無しの読み取りは空・書きかけを見うる。
  ただしそれは JSON として読めず `Unreadable` → 書かない、になるだけで、誤った `pane_id` にはならない
  （書きかけの JSON object が有効な別の値に読めることはない）。次の queue 変更で再生成される
- `label` は `tNNN`（mission slug を落とした task id）。`id` は `<slug>:tNNN` のまま。plugin 0.1.1 の
  `load_config` は未知の欄を拒否しない（`tests/test_task_graph_plugin_contract.py` で本物に読ませて確認）
- 複数 mission のとき `tNNN` だけでは同じ ID が並びうる（`t001` が mission ごとにある）。区別は
  **`group`（mission slug。t016）** が担う。plugin 0.3.0 は箱の 1 行目右端に slug の末尾を出す。
  label に slug を足すと長くて読めなくなる（P-2 の原因そのもの）ので、label とは別の欄にした。
  `group` は 0.1.1 / 0.2.0 が未知の欄として無視する（実物で確認）。**0.3.0 は `group` が文字列で
  ないとファイル全体を拒否する**が、slug は常に文字列。`[表示する task なし]` の placeholder は
  mission に属さないので `group` を書かない
- **この変更単体の戻し方**: この PR を revert する（`label` / `pane_id` が消えて従来の生成物に戻る）。
  `CREWVIA_TASK_GRAPH=0` は生成ごと止める退避路で別物

#### 直す道筋（元の見立て）

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

### 4-5. 完了済み task が多いと画面が詰まる（0.1.1。0.3.0 で折り返し・スクロールに）

0.1.1 は全 task を 1 行に並べる。完了済みが数十件あると箱が潰れて ID が読めない
（本番 queue の複製 53 task で確認）。**0.3.0 は幅を超える段を折り返し、縦にスクロールする**ので
62 task でも全件に j/k が届く（§0）が、多いほどスクロールは長くなる。描くのは **active な mission だけ**なので、
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

各 node の `label`（`tNNN`）は短い表示名、`group`（mission slug）は複数 mission を並べたときの区別（どちらも未対応の版は無視する）。Worker が就いている
task の node には `pane_id` も入る（Enter でそのペインに飛ぶための宛先。§4-4）。

依存待ち（`WAIT`）と人間待ち（`BLOCK [要判断]`）は plugin の状態そのものが違うので
取り違えない。title の `[依存不明: id]` は存在しない task への依存、
`[循環依存: id]` は循環を閉じている辺をそこで切ってある印（plugin は循環があるとファイル
全体を拒否するので、隔離して残りを描く）、`[表示する task なし]` は task が 1 件も無いとき
（最後の mission を archive した直後）の 1 ノード。

---

## 6. 切り分け

| 症状 | まず見るところ |
|---|---|
| 画面が古い | plugin の版（`herdr plugin list`）。0.1.1 なら `r` を押す（自動再読み込みは 0.2.0 以降）。0.3.0 で古いままなら開いている**タブが古いコード**のことがある（入れ替え前に開いたタブは古いまま動く。閉じて開き直す）。ファイル側は `ls -l registry/task-graph/tasks.json` の mtime で確認 |
| 画面が crewvia の内容でない（サンプルが出る） | 0.2.0 以降は config dir に entry が**無い**ときだけ。タイトルが `crewvia / <slug>`（1 mission）か `crewvia / N missions`（0・2 件以上）か。symlink の先が存在するか、主 checkout を指しているか |
| ファイルが更新されない | `CREWVIA_TASK_GRAPH=0` になっていないか。`plan.sh task-graph` を手で実行して出力を見る。`plan.sh` の stderr に生成失敗の 1 行が出ていないか |
| `plan.sh task-graph` が「queue が違う」と拒否する | `CREWVIA_QUEUE` が `<root>/queue` でない。書き先を明示するなら `CREWVIA_TASK_GRAPH_FILE` |
| ヘッダーが `[offline]`、最下部に `ERROR: [Errno 32] Broken pipe` | plugin が **0.1.1**（§4-1b。0.2.0 で直っている。入れ替えは README「Upgrading the plugin」）。crewvia の tasks.json 側を疑わない。task の状態表示は crewvia の値のまま。0.3.0 でこれが出たら別件 |
| 最下部に `ERROR: cannot read …/tasks.json` | 0.2.0 以降の正常な報告。config dir の symlink の先が無い（`plan.sh task-graph` を 1 回走らせる）・主 checkout でなく worktree を指している。最後に読めた内容は画面に残る。ファイルが戻れば自動で回復する |
| task に Worker 名が出ない・Enter で pane に飛べない | §4-4。生成物の該当 node に `pane_id` があるか（`jq` で確認）。無ければ `registry/mux/<Name>-worker.json` が無い・`backend` が herdr でない・記録の server（`server.generation` の pid）が居ない（herdr 再起動後の古い記録）のどれか。Worker を起動し直すと記録が書き直される |
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

---

## 8. 結合確認（t011）の実測記録と、本番 plugin の入れ替え・戻し

2026-09-26、隔離 herdr（HOME=`/tmp/hgt11`、server PID 3517176 の `HERDR_SOCKET_PATH` なし、socket
`/tmp/hgt11/.config/herdr/herdr.sock`）と隔離 queue/registry（`/tmp/cv11`）。検証対象 head:
crewvia `f9353fa83b09a2c8b9359e761d01ca200df5324c`、plugin `d2b819553980cb7875b962e3e6a9e2faba768e80`
（0.3.0）。対照は plugin 0.1.1（本番が今リンクしている `/tmp/herdr-task-graph`）。3 mission / 62 task
（`queue/archive` から複製した 2 本 + 進行中の 1 本）、Worker 相当 pane 2 つ（`claude` 名の `sleep`）。
本番の workspace label 集合・plugin 一覧・config dir の symlink 先・デーモン pid（dispatcher /
watchdog / herdr server）は前後で不変。

| 項目 | 実測 |
|---|---|
| 接続 | 0.1.1: `[offline]` + `ERROR: [Errno 32] Broken pipe`（箱も重なる）/ 0.3.0: `[live]`・ERROR なし |
| 反映 | `r` なしで 0.58 / 0.36 / 0.35 / 0.36 秒（pane を 0.2 秒間隔で読んで測定。`plan.sh` の 0.14〜0.15 秒を含む）。**引用は「1 秒未満」** |
| 幅 80 / 120 / 200 | 全 62 task に届く・選択 box が画面外のフレーム 0・重なり署名 0（陽性対照で検出器が効くことを確認）。幅 120 は live ペインでも同じ |
| 箱 | 3 段（`[STATE] tNNN · <group 末尾>` / タイトル / meta）。タイトルは約 34 桁で切れる。幅 200 は右に AGENTS パネル（約 30 桁）が付いて箱が 29 桁に縮み、上端で connector の断片が欠けて見える箇所がある（読めなくはない） |
| Enter | `pane_id` で `focused_pane_id` が動く（§4-4） |
| 読めないとき | dangling symlink → `ERROR: cannot read …` を出して内容を保持 / 2 秒で回復。`--once --socket <存在しないパス>` は exit 1・サンプルなし |

### 観察（PASS を妨げない）

- **O-1 agent 名は `claude`。** 箱と AGENTS パネルに Worker 名（`Arjun-worker`）は出ない。plugin が pane の
  label を読まないため（upstream の変更）。Enter は `pane_id` で正しく飛ぶので機能は成立
- **O-2 幅 200 は箱が狭い**（AGENTS パネルの分。見た目のみ）
- **O-3 ヘッダーの状態語は `[live]`**（`[connected]` ではない）。確認点は「`[offline]` でない・ERROR 行が無い」
- **O-4 本番が今リンクしている 0.1.1 の実体は `/tmp/herdr-task-graph`**（`/tmp` の複製で、再起動で消える）。
  戻し方が通るのは残っている間だけ。**入れ替えの前に消えない場所へ複製する**（例
  `cp -r /tmp/herdr-task-graph ~/htg-0.1.1`）
- **O-5 入れ替えても、開いたままの古い plugin ペインは古いコードで動き続ける。** 入れ替え前に閉じ、
  後で開き直す

### 入れ替え（隔離で実行した記録。手順の本体は README「Upgrading the plugin」）

`herdr plugin pane close` → `unlink` → `link ~/workspace/herdr-task-graph` → `action invoke
open-task-graph`。link の応答の version が 0.3.0、画面タイトルが `crewvia / 3 missions`・`[live]`・
ERROR なし。**`unlink` / `link` しても config dir の `tasks.json` symlink は残る**（`ls -l` で同じ先）ので
作り直しは要らず、入れ替えで crewvia のファイルが読めなくなることは無い。所要は数秒。
事前に `git -C ~/workspace/herdr-task-graph rev-parse HEAD origin/main` で意図した commit か確かめる。

### 戻し方（隔離で実行済み）

同じく pane を閉じて `unlink` → `link <0.1.1 の clone>` → `invoke`。link 応答の version が 0.1.1 になり、
画面は入れ替え前と同じ（`[offline]` + `Broken pipe` + 箱の重なり）に戻った。タイトルは
`crewvia / 3 missions` のまま、config dir の symlink は変わらない。**戻し先の clone が消えていると link が
失敗する**（O-4）。

**本番の入れ替えそのものは t011・t012 では実行していない**（本番 herdr の変更。Director がユーザーの
了承を得て行う: t014）。

---

## 9. 今回扱わないこと（次のミッションの種）

- **`open-task-graph` は invoke のたびに新しいタブを開く。** manifest の説明は "Open or focus" だが、
  既存の Task Graph タブがあっても重複を避けない（0.1.1 で実測し、0.3.0 でも変わらない）。開くたびに
  タブが増えるので、自動化（`./crewvia` からの自動起動・hook・cron）には載せていない。直すなら
  upstream（既存タブへフォーカスを移す）か、crewvia 側で開く前に `herdr api snapshot` の label
  `Task Graph` を見て既にあればフォーカスするラッパー。どちらも今回は着手しない
- **箱に Worker 名を出す**（O-1）: plugin が pane の label を読む必要があり、upstream の変更
- **幅 200 の AGENTS パネルによる箱の縮み**（O-2）: 見た目のみ。upstream
- **agent の状態遷移（idle → working）が箱に追従するか**: t011 は `claude · idle` の表示までで
  遷移は見ていない（§4-4）
