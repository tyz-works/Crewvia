# Rex を mux バックエンドにする設計（`scripts/lib_mux.py` の `RexBackend`）

herdr に代わる mux として Rex（https://www.superlogical.com/rex/docs/）を crewvia で使えるようにするための**設計文書**
（2026-10-31 ユーザー判断: 設計だけ先に。**この PR はコードを 1 行も変えない**）。

- 対象の版: `rex 0.0.1793390926-g3b2dce`（commit `3b2dce8`・built 2026-10-30T20:08:46Z）・**API revision 9（supports 0 to 9）**・`/usr/bin/rex`
- 書いた人: Worker Mateo（mission `20261031-rex-mux-design` t001）。Director の事前実測を**自分でも隔離サーバーで再現**し、足りない所（label の一意性・send の中身・kill の挙動・再起動・env）を足した
- 凡例: 「**実測**」= 下の隔離サーバーで自分が確かめた出力。「**未確認**」= 確かめていない。推測は「見込み」と書く

## 1. 結論（先に）

1. Rex で `_Backend` の全メソッドを満たせる。**満たせない・弱くなるのは 3 点**: ① label が一意でない（§3）② kill を「検査した server に束縛」する手段が herdr の `SO_PEERCRED` 接続ほど強くない（§5）③ `state()`（agent の working / idle）に当たる情報が無い（§4）
2. 最大の設計判断は **label の一意性を Rex が保証しない**こと。名前 ↔ block_id の正本は **crewvia 側の spawn 記録（`registry/mux/<name>.json`）** にし、label は人間向けの表示 + 補助の照合に下げる（§3）
3. send は herdr と**使い分けが違う**。`rex send` は生のテキスト（`\n` はそのまま改行）、Enter は `send-key enter`（`\r`）。複数行は paste イベント（`block.event`）で bracketed paste にできる。**ctrl+u は複数行の入力欄を 1 行ずつしか消さない**（実測。herdr の「送信前に ctrl+u」はそのまま移せない。§6）
4. Rex の pane は **server の env を継承する**（実測）。herdr と同じ「server 起動時の env 掃除」が要る（§8）
5. 実装は 3 PR に割る（§11）。本番 server の起動方法はユーザー判断（§10）

## 2. 実測の環境と手順（再現できるように）

隔離の作法（既定の socket の server は起動も停止もしていない。`~/.local/state/rex/run/` は読んだだけ）:

```bash
# socket のパスは 108 バイト以内（sun_path）。長い scratchpad のパスだと bind: invalid argument になりうるので、短い symlink を介す
ln -sfn <scratchpad>/rx /tmp/rxp-t001
setsid rex server run --listen unix:///tmp/rxp-t001/s.sock --state-dir /tmp/rxp-t001/state --restore=false &
RX="rex -S unix:///tmp/rxp-t001/s.sock --autostart=false"      # 全コマンドにこの 2 つを付ける
$RX server status --json
```

- 後始末: `$RX server stop` → `pgrep` で残りが無いことを確認 → `unlink /tmp/rxp-t001`（symlink を消すだけ。`rm -rf` は使っていない）
- 入力ログ用の小さな python（raw mode で `os.read(0)` の中身を `repr` でファイルに追記し、`\x1b[?2004h` で bracketed paste を有効にする）を block の中で動かして、`rex send` が**実際に何のバイトを送るか**を見た
- Claude Code（v2.1.314）は隔離 server の block の中で実際に起動し、`capture` で入力欄を見た（§6。誤って 1 回モデルに送ってしまった事故が 1 件ある。§6.3）

## 3. 宛先名 ↔ Rex の ID

### 3.1 Rex の構造と実測

- 階層は **session → window → block**（block が端末。1 window に複数 block を split で並べられる）。ID は `session:<uuidv7>` / `window:…` / `block:…`
- `rex new <label> --cwd <dir> --json -- <cmd>` → `session_id` と `initial_windows[].{window_id, block_ids}`。**最初の block の label は `shell` 固定**（`--label` は block を作る `rex run` の option）
- `rex run -s <session> --label <l> --cwd <dir> --focus=false --json -- <cmd>` → 新しい window を作り `window_id` / `block_ids` を返す。window の label は自動で `Window <uuid の末尾 8>`。`rex window rename <window> <label> -s <session>` で変えられる（**実測**: exit 0・`window ls` に反映）
- **label は一意でない（実測）**:
  - 同じ session に `--label Mateo-worker` の block を 2 回作れた（2 回とも exit 0・別の window・別の block_id）
  - `rex new crewvia` を 2 回呼ぶと、同じ label `crewvia` の session が 2 つできた
  - 一方 **CLI の解決は曖昧を拒否する**: `-s crewvia` が 2 つに当たると `Error: session target "crewvia" is ambiguous`（候補の末尾 ID を 2 行表示）。block も `-b <label>` で引ける（実測: `capture -b envprobe` が通った）が、重複した label を引いたときの挙動は**未確認**（session と同じ曖昧エラーと見込む）
- 構造の読み出し: `rex ls --json`（session 一覧）/ `rex window ls -s <s> --json` / `rex block ls -s <s> --json`（block_id・label・window_id）。`rex block ls -A --json` は**エラー**（`requires one session. Pass -s SESSION`。`-A` を付けたときの JSON は session 指定が要る。`-A` の表形式は動く）。Director の事前メモの `block ls -A --json` はこの版では使えない

### 3.2 対応づけの案

| crewvia | Rex |
|---|---|
| 宛先名（`Mateo-worker` / `dispatcher` / `Sora-director` …。`_pane_name()` = `CREWVIA_MUX_PANE_PREFIX` + 名前） | **block の label**（同じ文字列を window にも rename で付ける） |
| herdr の workspace（`CREWVIA_HERDR_WORKSPACE`、既定 `crewvia`） | **session**（`CREWVIA_REX_SESSION`、既定 `crewvia`。§8） |
| herdr の tab ≡ tmux window | **window**（Worker ごとに 1 window。`rex run` の既定） |
| `registry/mux/<name>.json` の `handle`（herdr は tab_id） | **`window_id`**（kill の単位）。`pane_id` に **`block_id`**（send / capture / process の単位）。両方を記録 |

**画面構成の比較**:

| 案 | 長所 | 短所 |
|---|---|---|
| A. `crewvia` session に Worker ごとの window（**推奨**） | herdr の tab / tmux の window と同じ形。`list()` は 1 session の window を引けば足りる。ユーザーが 1 つの session に attach して window を切り替えられる。session を 1 つだけ作ればよく、session label の重複を作る場面が起動時の 1 回に絞れる | session label の重複を防ぐのは crewvia の責任（§3.3） |
| B. Worker ごとに session | session 単位の `rex kill` で後始末が単純。session 間は完全に独立 | session が 64 個（`--max-sessions` 既定 64）まで・attach の切り替えが重い・session label の重複を作る場面が spawn のたびに来る |

推奨は A。window の上限は session あたり 64（`--max-windows` 既定 64）、block は全体で 1024（`--max-blocks`）— Worker 数の想定（数十）に対して余裕はあるが、**本番 server の起動で上限を明示するか既定のままにするかを決める**（§10 の決めること）。

### 3.3 一意性は crewvia が作る

Rex が label の重複を防がないので、`RexBackend` が次を守る（herdr の `_label_lookup` / `_resolve_ids` と同じ位置づけ）:

1. **spawn は `registry/mux/.records.lock`（`_pane_record_lock`）の中で「同名の記録 + 同名 label の block が session に無い」を確かめてから作る**。ロックを取れなければ spawn しない（重複を作るより失敗を選ぶ）。ロックは `write_pane_record` と同じ区間に伸ばす
2. **解決は記録の `block_id` を正本にする**。`send` / `capture` / `pid` / `kill` は名前 → 記録 → `block_id` で引き、label は「記録の block の label が名前と一致するか」の照合にだけ使う（一致しなければ `pane not found` 側。他人が同じ名前を付けた別 block に送らない）
3. **label で引いた結果が 2 つ以上なら「曖昧」**: `list()` は重複を 1 つに潰さず警告つきで返す・`spawn` は拒否（`SPAWN_UNREADABLE` 側）・`kill` は拒否（`--force` だけが出口）。重複を片方に倒さない
4. 記録が無い（`./crewvia` が作っていない）block は、名前が一致しても kill の対象にしない（`DAEMON_PANE_NAMES` = dispatcher / watchdog は既存の `may_destroy_pane` が止める。Worker 窓は §5 の本人確認）

## 4. `_Backend` の全メソッドの対応表

HerdrBackend の契約の要点（`scripts/lib_mux.py`）:
spawn は記録（`write_pane_record`）を server の `(endpoint, generation)` つきで書く・kill は `may_destroy_pane`（記録 AND 占有者 / 空 / foreign の veto）を通し、`_herdr_close_tab_bound` で**検査した server と同じ接続**に close を流す・
テスト隔離（`_guard_test_isolation` — `CREWVIA_MUX_TEST_ISOLATION` + `CREWVIA_MUX_PANE_PREFIX` + 既定以外の宛先）・記録の刈り取り（`reap_stale_pane_records` ← `record_existence`）。

| メソッド | herdr の実装 | Rex での満たし方 | 満たせるか |
|---|---|---|---|
| `server_identity()` | 接続の `SO_PEERCRED` で server pid → `(sock, "<pid>:<starttime>")` | `rex server status --json` の `endpoint` と **`instance_id`**（server の生成ごとに変わる UUID。**実測**: 再起動で `c7b02a5c…` → `23fc24c8…` → `cd9abb25…`）。generation は `"<instance_id>:<pid>"`（pid も `status` が返す）。`SO_PEERCRED` は CLI が自前の接続を開くので使えない（§5） | 満たせる。ただし「同じ接続で束縛」は弱い（§5） |
| `_inspect_pane` / `_inspect_pane_full` | tab_id と shell pid を 1 query で | `rex block call process '{}' -b <block>` → `child.pid` / `foreground.pid` / `cwd` / `last_exit`。window_id は記録から。server は直前に `server status` | 満たせる（query は 2 本になる。§5） |
| `may_destroy(name)` | `may_destroy_pane(...)` | 変更なし（`BACKEND_NAME = "rex"` を渡すだけ） | 満たせる |
| `record_existence(name, record, timeout)` | `pane.get` を**記録の server の世代と同じ接続**で流し、`pane_not_found` だけ `PANE_GONE` | `rex block inspect <block_id>` 相当。**実測**: 存在しない block は `Error: block target "…" not found`・**exit 3**。server が居ない（socket 無し）は **exit 4**（`no Rex server is running …`）。→ exit 3 かつ stderr が `not found` のときだけ `PANE_GONE`、他は `PANE_UNOBSERVED`。server の世代が記録と違えば（`instance_id` 不一致）問い合わせず `PANE_UNOBSERVED` | 満たせる。**exit code と文言の両方を見る**（exit 3 は「target が見つからない」全般に使われている可能性があり、server 自体の `not found` と区別できるかは未確認） |
| `spawn(name, cmd, cwd)` | tab create → rename → `pane run` → 記録 | §3.3 の確認 → `rex run -s <session> --label <name> --cwd <cwd> --focus=false --json -- <cmd>` → `window rename` → 記録（`window_id` / `block_id` / `instance_id` / `child.pid` と `/proc` の starttime）。**cmd は shell 経由か直か**: `rex run` は既定で login shell の環境を通す（`--shell=none` で直接）。crewvia の LAUNCH_CMD は `export …; cd … && claude …` の**シェル文字列**なので `--shell` の既定を使い、`-- bash -c '<LAUNCH_CMD>'`（または start.sh が既に作る形）にする | 満たせる |
| `last_spawn_refusal`（`SPAWN_OCCUPIED` / `SPAWN_UNREADABLE`） | 同名 pane が居て idle shell でなければ OCCUPIED | 同名の記録 + block が居て `process` の child が idle shell でなければ OCCUPIED。**block は command が終わると消える**（実測、§4.1）ので、「idle shell の husk」は `--keep-open` で作った block か、server 復元後の block（§4.2）にしか生じない | 満たせる（husk の経路は herdr と違う形で残る） |
| `send(name, text)` | `❯` を待つ → ctrl+u → `pane run` → capture で Enter 保険 | §6 | **弱い**: ctrl+u が複数行を消さない |
| `keys(name, keys)` | `pane send-keys`（`translate_keys` の herdr 列） | `rex send-key -b <block> <KEY>…`。`_KEY_NAMES` に Rex の列を足す: `up` / `down` / `left` / `right` / `enter` / `escape` / `tab`（Rex の key 構文。**実測**: `enter` → `\r`・`ctrl+c` → `\x03`・`escape` → `\x1b`・`up` → `\x1b[A`・`ctrl+u` → `\x15`） | 満たせる |
| `capture(name)` | `pane read --source visible` | `rex capture -b <block> --trim`。**実測**: 画面 + scrollback（Director の測定では約 64MiB。自分では再現していない）。生きている間と `--keep-open` の終了後は読める。**block が閉じた後は読めない**（exit 3） | 満たせる |
| `list(suffix)` | `pane list` を live で | `rex block ls -s <session> --json` の `label`（`_caller_name` で prefix を外す）。重複は §3.3-3 | 満たせる |
| `kill(name, allow_foreign)` | `may_destroy_pane` → `tab.close` を検査した接続に束縛 | §5 | 満たせる（束縛は弱い） |
| `pid(name)` | shell pid（`process-info`） | `process` の **`child.pid`**（block の子プロセス。**実測**: `ppid` = server の pid・自身が session leader） | 満たせる |
| `attach(name)` / `attach_cmd(name)` | `tab focus` / `["herdr"]` | `rex window focus <window>`（**未確認**: 実行していない）/ `["rex", "-S", <endpoint>]`（引数なしの `rex` が TUI client を開くかは**未確認**） | 未確認 |
| `available()` | herdr の CLI と socket の ping | `rex` が PATH にある ∧ `rex -S <endpoint> --autostart=false server status --json` が exit 0 ∧ `api_revision` が想定内（§7） | 満たせる |
| `server_running()` | socket の ping | 同上（exit 0）。**server が居ない = exit 4 / `server status` は exit 1**（実測。どちらも非 0 = 居ない） | 満たせる |
| `state(name)` | `pane get` の `agent_status`（working / idle …） | Rex に agent の状態の API は**見つけていない**（`com.superlogical.terminal.*` は title / pwd / process / modes / input_mode / clipboard_read）。常に `"unknown"`（契約の fail-safe: 呼び出し側は通知しない側に倒れる） | **満たせない**（unknown 固定。Rule 5 / hard_idle への影響は PR-2 で棚卸し） |

### 4.1 block は command の終了で消える（実測）

- `rex run -- bash -c 'echo hi; sleep 1; exit 7'` は 1 秒後に**block ごと消える**（`block ls` から消え、`process` / `capture` は exit 3）
- `--keep-open` を付けると残る: `process` が `{"last_exit":{"exit_code":7,"error":"exit status 7"}}` を返し、`capture` で最後の出力（`last-words`）が読める。`rex wait <target>` は終了コードを返す（`wait` は `-b` を取らず、位置引数）
- → herdr / tmux の「shell が残り、中身だけ死ぬ husk」は **既定では起きない**。起きるのは `--keep-open` の block と §4.2
- **crewvia が欲しい挙動**: Worker が落ちたとき watchdog / dispatcher が「窓が消えた（`worker-vanish`）」と「窓は残っているが中身が無い」を区別している（`knowledge/worker-vanish-detection.md`）。Rex の既定では前者になる。`--keep-open` にすると退役の checkpoint（PR #323）の pane capture が**死後も取れる**利点がある一方、window が残り続けるので掃除（`kill`）が要る。**どちらにするかは決めること（§10）**。推奨は `--keep-open` なし（herdr と同じ「kill の前に capture」の既存手順を変えない）

### 4.2 server の再起動（実測）

`--state-dir` つきで `--restore`（既定 true）にした server を `kill` して同じ state-dir で起動し直した:

- `instance_id` と `pid` と `started_at` は**変わる**
- session / block の **ID と label は同じまま復元**される（`block:01a18fa8-2f47-7375-…` が再起動前後で同一）
- 復元された block の中身は**元の command ではなく login shell**（`capture` が `Welcome to Ubuntu …` を表示・`process` の `child.script` は無い）。元のプロセスは `got 1`（SIGHUP）で終わっていた
- → herdr の husk pane と同じ形が server 再起動で生じる。`spawn()` の「同名の記録 + idle shell なら中で再実行」は Rex でも要る（再実行は `rex send` で command を打つ形になる＝**Rex には `pane run` 相当の「既存 block で command を走らせる」API があるか未確認**。無ければ block を閉じて作り直す）
- **ID が復元で保たれる**ので、record の `block_id` は再起動後も有効。herdr は ID が振り直されるので「古い ID が別の pane を指す」危険があったが、Rex は**古い ID が別 block を指すことは無い**（UUIDv7。見込み。再利用の可能性は未確認）。代わりに「同じ ID の中身が別物（login shell）になった」が来るので、**kill の認可は「記録 AND 占有者」の二重の証明のまま**（`may_destroy_pane` が既にそうしている）で足りる
- `--restore=false`（Director の隔離）は「保存した session を使わず、何も保存しない」。本番で `--restore` をどうするかは決めること（§10）

## 5. kill の本人確認（watchdog が唯一の実行者）

`knowledge/daemon-authority.md`: Worker の終了は watchdog だけが実行する。Rex でもこの前提は変えない。`RexBackend.kill()` が通す関門:

1. **記録の `block_id` / `window_id`** を正本にする（名前で再解決しない。label は誰でも付けられる＝§3.1）
2. **server の世代**: `server status --json` の `instance_id` が記録のそれと一致（§4.2 の通り ID は保たれるので「世代が違う」だけでは拒否にしないが、**一致しない場合は占有者の証明を必須にする**）
3. **プロセスの本人確認**: `process` の `child.pid` が (a) 記録した `child.pid` と同じ ∧ (b) `/proc/<pid>/stat` の starttime（22 番目の欄）が記録と同じ ∧ (c) ppid が `server status` の `pid` と同じ。
   **実測**: `child.pid 1453685` の `ppid` = server pid `1447392`・starttime は `14963234`。pid の再利用は starttime で弾ける（herdr の `<pid>:<starttime>` と同じ考え）
4. **占有者**: `pid()` の結果を `daemon_pane_owner` / `may_destroy_pane` にそのまま渡す（dispatcher / watchdog の foreign 判定は `/proc` 側の仕事で backend 非依存）
5. 全部通ったら `rex block close <block>`（block を閉じると window も閉じる。window に他の block を足さない設計なので 1 block = 1 window）

**kill の挙動（実測）**: SIGHUP を無視する python（`sigrec.py`。SIGHUP / SIGTERM のハンドラで `sig.txt` に書く）を block で動かして `block close` した。
`block close` は約 1.0 秒で返り、プロセスは **SIGHUP を 1 回受け**（`got 1`。SIGTERM は受けていない）、**3 秒後には居なかった**。つまり HUP を握りつぶしても消える（強制終了に切り替わる。正確な猶予と最終シグナルは**未確認**だが SIGKILL と見込む）。
herdr の「tab close で子が SIGHUP を受けて 2 秒以内に終わる」より強い（無視しても残らない）。ただし claude が自分の子を残す場合は `/proc` の後始末（`lib_daemon_watch` の descendants）が要る点は herdr と同じ。

**束縛の弱さ（満たせない点の②）**: herdr は `tab.close` を、**検査したのと同じ接続**（kernel が `SO_PEERCRED` で名指した peer）に流す。`rex` の CLI は 1 コマンド = 1 接続で、検査（`process`）と破壊（`block close`）が別の接続になる。
Rex の制御 API は unix socket 上の WebSocket（`/api/control`。エラー文に `failed to WebSocket dial … Get "http://rex/api/control"` と出る）なので、herdr と同じ作りに**できなくはない**が、WebSocket の握手を python で実装する必要があり、API revision への依存も増える。選択肢:

| 案 | 内容 | 評価 |
|---|---|---|
| K1（**推奨・PR-1**） | CLI 2 本（`process` → `block close`）。直前と直後に `instance_id` を読み、**一致しなければ close しない/結果を信用しない**。close は block_id 指定なので、server が入れ替わっても UUID は別 block に当たらない（§4.2） | herdr の「ID が振り直されて別 pane に当たる」穴は Rex では ID の性質で塞がる（見込み）。残る窓は「検査した後に同じ ID の中身が入れ替わる」だけで、それは再起動直後の login shell（空）に限られる |
| K2（後続） | WebSocket 制御 API を直接叩き、検査と close を 1 接続に束縛 | 強い。ただし実装・テストが大きい（herdr の束縛は Codex 6〜8 巡目の産物）。PR-1 では入れない |

**退役の checkpoint（PR #323）との関係**: `lib_retirement._start` が通知の前に `mux.capture` で pane を撮る手順はそのまま使える（`capture` は生きている間だけ読める＝§4.1 なので **kill の前に撮る順序は Rex でも必須**。`--keep-open` にすれば死後も撮れるが、既定は使わない）。
`meta.json` の `files.pane.source` の固定コード（`before_kill` / `live` / `capture_error` / `capture_empty` / `window_gone`）は変えない。`window_gone` は Rex では「block が消えた（exit 3）」で出る。

## 6. send の実測

`rex send -b <block> '<text>'` は**生のテキスト**。Enter は付かない。`rex send-key -b <block> enter` が `\r`。

### 6.1 byte 列（入力ログ用 python。bracketed paste を有効にした状態）

| 操作 | 受け取った byte |
|---|---|
| `send 'hello'` | `b'hello'` |
| `send $'line1\nline2\nline3'` | `b'line1\nline2\nline3'`（**bracketed paste の囲みは付かない**。`\n` のまま） |
| `send 'こんにちは、日本語のテキスト🙂'` | UTF-8 のまま 1 チャンクで到達（絵文字を含む。欠落なし） |
| `send-key enter` | `b'\r'` |
| `send-key ctrl+c escape up` | `b'\x03'` / `b'\x1b'` / `b'\x1b[A'` |
| `send-key ctrl+u` | `b'\x15'` |
| paste イベント（下記） | `b'\x1b[200~para1\npara2 日本語\n\x1b[201~'`（**囲みが付く**） |
| 20,000 文字の `send`（stdin から） | 全量到達（pty の 4096 バイト単位に分割されて届く） |
| 60,000 文字の日本語（約 180KB）の `send` | **`Error: control message too large: encoded request is 180223 bytes, limit is 65536`**・exit 1（黙って欠けない） |

- 空の `-b ''` を渡すと `rex send` は**宛先なしと見て stdin を読みに行き、止まる**（実測。最初の試行で 120 秒ハングした）。`RexBackend` は block_id が空なら呼ばない・全 rex 呼び出しに `stdin=DEVNULL` と timeout を付ける
- paste イベント: `rex api call block.event '{"session_id":"…","block_id":"…","event":{"type":"paste","payload":{"text":"…"}}}'`（`session_id` が要る）。`api describe block.event` に例がある

### 6.2 Claude Code（v2.1.314）の入力欄での実測（隔離 server の block で起動・`capture` で観察）

- 入力欄の行は herdr の時と同じ `❯ `（`❯` の行頭一致で `_text_in_input_line` が使える）。`❯ Try "edit …"` のようなヒント文が出るので、**空の入力欄の判定は `❯` の後ろが空とは限らない**（herdr でも同じ）
- `send` の複数行（`$'行1 日本語\n行2\n行3'`）: 入力欄に 3 行で入り、**送信されない**（`\n` は改行として入る）
- paste イベントの複数行: 入力欄に入る（短い文は `[Pasted text]` に畳まれなかった。長い文での畳みは未確認）
- **`send-key ctrl+u` は 1 回で「今の行」だけ消す**（複数行の入力欄では 3 行 → 2 行 → …と 1 行ずつ）。herdr の `send()` が送信前に打つ ctrl+u（過去の残りを消す。t007 F3）は **1 回では足りない**

### 6.3 実測中の事故（隠さず書く）

「ctrl+u を 6 回打てば消える」と考えて 6 回打ったが、最初の行が残っていた（日本語の 1 行目）。そのまま `send '/help'` + `send-key enter` を打ったので、
**`行1 日本語\n/help` がモデルへの入力として送信された**（画面に `* Razzle-dazzling…`）。隔離 server の使い捨て block の claude で、1 ターンの短い入力だったため実害は無いが、
**「入力欄の掃除が不完全なまま Enter を打つと、残りと合わさって送られる」**ことの実例。→ RexBackend の send では:

1. 送る**前**に入力欄が空であることを `capture` で確かめる（`❯` の行が 1 行で、後ろにユーザー文字が無い）。空でなければ **Enter を送らず** `False` を返す（残りの種類によっては ctrl+u を繰り返す「掃除」を試してよいが、**掃除の完了は capture で確認**する）
2. 複数行の掃除に効くキーは**未確認**（ctrl+c 1 回は入力を消すが、2 回で終了するので試していない。`escape` の連打も未確認）。**PR-1 の QA 項目**にする（§11）

### 6.4 Enter の別送は要るか

- herdr の過去の問題: `pane run` が `❯` の出る前だと Enter が落ちる（Phase 0）→ `send()` が `❯` を最大 5 秒待ち、送信後に capture して 30 文字の prefix が入力行に残っていれば Enter を足す。`verify_sent()` は kickoff の再送用
- Rex では**テキストと Enter が最初から別 API**。`❯` が出ている入力欄に `send` + `send-key enter` を続けて打つと（実測）、`/help` は送信された。起動直後（`❯` がまだ出ていない）の挙動は**未確認**
- → `send()` = 「`❯` を待つ → 空を確認 → `send`（または paste）→ `send-key enter` → capture で prefix が残っていればもう 1 回 Enter」。herdr の待ち・保険の構造はそのまま使い、`pane run` を `send` + `send-key enter` に置き換える。`verify_sent()` は backend 非依存
- 複数行・長文は paste イベント → 少し待つ → `send-key enter`。**paste の直後の Enter が Claude Code に食われないか**は**未確認**（QA 項目）
- 65536 バイトの上限（encoded request）は、paste イベントなら JSON 化後の大きさ。kickoff の文は数 KB なので通常は収まるが、`RexBackend.send()` は**上限を超える文を黙って切らず `False`** にする

### 6.5 env（send の話ではないが同じ実測）

`SRV_ONLY=from_server_env rex server run …` の server に、別の env（`CLIENT_ONLY`）を export したシェルから `rex run` した。block の中の `env`:

- `SRV_ONLY=from_server_env` は**ある**・`CLIENT_ONLY` は**無い**・`CLAUDECODE=1` が**ある**（server を起動したシェルの env）
- 加えて `REX_SESSION` / `REX_BLOCK` / `REX_BIN_DIR` / `TERM=xterm-rex` が入る
- → **herdr と同じ「server の起動時の env が全 pane に流れ込む」**。`_herdr_server_env()` の denylist（`CLAUDE_*` / `CODEX_*` / `CREWVIA_*` / `TASK_*` / `TASKVIA_TOKEN` / `NTFY_*` / `AGENT_NAME` …）を `_rex_server_env()` として共通化する。`./crewvia` が server を起動する場合はこの env で起動する（§10 の案 2・3）。ユーザーが手で起動した server の env は crewvia が制御できない（案 1 の弱点）
- `TERM=xterm-rex` の terminfo は Claude Code に問題を起こさなかった（TUI は正常に描画）

## 7. 版の検査（fail-closed）

- 起動時（`available()` / start.sh の mux 選択時）に `rex server status --json` の **`api_revision`** と `rex version` の `api: revision 9 (supports 0 to 9)` を読む
- 想定する版は定数 `_REX_API_REVISIONS = {9}`（実測した版だけ）。**範囲外・読めない・JSON の形が違う → mux を使わない**（`available()` = False、`spawn` は `SPAWN_UNREADABLE` 側で拒否し、理由を stderr に 1 行）。tmux / herdr に黙って倒さない（`CREWVIA_MUX=rex` を明示したのに別 backend で動くのは、kill の宛先を取り違える）
- 版を上げるときは revision を足す PR で、§4 の対応表の各コマンド（`process` / `block close` / `send` / `capture` / `window rename`）を隔離 server で再実測してから
- `rex` の CLI の出力形（`--json`）に依存するのは `server status` / `ls` / `block ls -s` / `run --json` / `new --json` / `block call process`。**欄が無い・型が違う → 読めなかったとして扱う**（`None` / 空に倒さない。既存の `Unreadable` の作法）

## 8. 設定

- 選択: `CREWVIA_MUX=rex`（env）> `config/crewvia.yaml` の `mode: rex` > auto。`_config_mode()` の許可値と `_select_backend()` に `rex` を足す。`mode:` の読み取りは既に trailing comment を落とす実装なのでそのまま
- 宛先: `CREWVIA_REX_SESSION`（既定 `crewvia`。`CREWVIA_HERDR_WORKSPACE` / `CREWVIA_TMUX_SESSION` に当たる）。`_DEFAULT_SESSION` と同じ定数を本番の既定とみなし、**テスト隔離は宛先が `crewvia` なら拒否**（`_guard_test_isolation` の `destination`）
- テスト用の socket 上書き: `CREWVIA_REX_SOCK`（herdr の `CREWVIA_HERDR_SOCK` に当たる）。`rex` には全コマンドで `-S unix://<sock> --autostart=false` を渡す。
  **本番で設定させない仕組み**: herdr と同じ「テスト専用・本番で設定するな」の文書化に加え、**`CREWVIA_MUX_TEST_ISOLATION` が立っていないときに `CREWVIA_REX_SOCK` が設定されていたら `available()` を False にして理由を出す**（herdr の「ping 先と server の bind 先が食い違う」事故を、Rex では起動時に拒否に変える）。本番の endpoint は config の `rex.endpoint`（既定は Rex の既定 socket。**crewvia は既定 socket の server を測定やテストで起動・停止しない**）
- **`--autostart=false` を必ず付ける**（既定 true だと、CLI が server を勝手に起動する＝ユーザーの本番になりうる server を crewvia が作る）。server の起動は §10 の選択肢で crewvia が**明示的に**行う
- ペイン名の名前空間 `CREWVIA_MUX_PANE_PREFIX` はそのまま label の前置きに使う（`_pane_name()`）
- socket のパスは 108 バイト以内。config の `rex.endpoint` / `CREWVIA_REX_SOCK` が超えていたら `available()` を False（bind できない server を作らない）
- CLAUDE.md の環境変数の表に足すのは実装 PR（PR-3）。**この PR では README / CLAUDE.md を触らない**

## 9. 範囲外（見込みだけ）

- **herdr-task-graph plugin は herdr 専用**（`plan.sh` が書く `registry/task-graph/tasks.json` を herdr の plugin が読む）。Rex では動かない。生成物（`tasks.json`）の生成は mux 非依存なので止めなくてよい
- Rex には **block plugin**（`rex server run --block-dir DIR`・`--block-plugin-reload`・`block types`）があり、タスクの依存を描く block を作れる見込み。**tasks.json を読んで描く plugin が書けるか・どんな API かは未確認**。別 mission で扱う

## 10. 決めること（ユーザー判断）

### 10.1 本番 server の起動方法

| 案 | 内容 | 長所 | 短所 |
|---|---|---|---|
| 1. ユーザーが手で起動 | `rex server start` 等を自分で | crewvia が server の寿命に責任を持たない | **server の env を crewvia が掃除できない**（§6.5）。起動し忘れで `./crewvia` が失敗 |
| 2. `start.sh` が起動（**推奨**） | 無ければ `rex server start`（または `setsid rex server run …`）を**掃除した env**（§6.5）で起動し、`api_revision` を検査してから使う | 1 コマンドで動く・env を制御できる・herdr の `_herdr_start_server()` と同じ作り | server の寿命が `./crewvia` の起動に紐づく。本番 socket を crewvia が作る＝**起動する前にユーザーの了承**が要る |
| 3. systemd user service（`rex.service`、既定は無効） | systemd が常駐・自動再起動 | 再起動に強い | env は unit の `Environment=` で固定（掃除は unit に書く）。WSL で systemd が動く前提 |

推奨は **2**。ただし「既に server が居るなら使う（env は信用できないので `ps` で env を読まず、掃除済みかは判定できない＝警告だけ）」を仕様にする。

### 10.2 その他

1. **`--keep-open` を使うか**（§4.1。推奨: 使わない）
2. **`--restore` の既定**（server 再起動で session / block を復元する。復元された block は login shell の husk になる＝§4.2。推奨: 既定のまま `true`＋spawn が husk を再利用。`false` だと再起動で window が全部消え、ユーザーの目には「何も無い」）
3. **上限**（`--max-sessions` / `--max-blocks` / `--max-windows` / `--max-panes`）を明示するか（既定: 64 / 1024 / 64 / 64。Worker 数の想定では既定で足りる。推奨: 既定のまま）
4. **PR-2 で `state()` が unknown 固定でよいか**（Rule 5 / hard_idle の入力に `state` を使っている箇所を棚卸しして、使っていなければ問題なし。使っていれば capture ベースの代替を設計する）
5. **K2（束縛を WebSocket API で強化）を後続にするか**（§5。推奨: K1 で出して様子を見る）

## 11. 実装の PR 分割案と各 PR の QA 項目

### PR-1: `RexBackend` + 隔離テスト（`scripts/lib_mux.py` / `tests/`）

- 範囲: `RexBackend`（§4 の全メソッド）・`_rex_run`（`-S` / `--autostart=false` / timeout / `stdin=DEVNULL` / 出力の検査）・`_select_backend` / `_config_mode` に `rex`・`_KEY_NAMES` に Rex の列・テスト隔離（`_guard_test_isolation`）・spawn 記録の拡張（`block_id` / `window_id` / `instance_id` / `child.pid` / starttime）
- テスト: **本物の `rex` を隔離 server（`--listen` の短い socket・`--restore=false`）で動かす**＋偽の `rex`（PATH 上の stub）で server が居ない / 想定外の JSON / exit code の分類。`CREWVIA_MUX_TEST_ISOLATION` 下で本番の宛先名が拒否されること
- QA 項目:
  1. label の重複（同名 block を 2 つ作った状態）で `spawn` / `kill` / `list` が曖昧として拒否すること（§3.3）
  2. 空の block_id で `rex send` を呼ばない（stdin を読んで止まらない）・全呼び出しに timeout
  3. `kill`: 記録の `child.pid` / starttime / ppid の不一致で拒否。pid の再利用（同じ pid で starttime 違い）の模擬
  4. `record_existence`: exit 3 + `not found` だけが `PANE_GONE`。server が居ない（exit 4）は `PANE_UNOBSERVED`
  5. server 再起動後（§4.2）に husk の再利用が動くこと（`--restore` ありの隔離 server で）
  6. `send` の複数行・日本語・長文・65536 超（`False`）・paste イベント。**Claude Code の入力欄に残った複数行を掃除する方法を実測で決める**（§6.3）。決まるまで「空でなければ Enter を打たない」
  7. `api_revision` が想定外・`server status` が読めない → `available()` False（§7）
  8. 赤の実証（既存の作法）: 本人確認の検査を外すと落ちるテスト・label を信用すると落ちるテスト
- 戻し方: PR revert（`rex` を選ばなければ動作は変わらない）

### PR-2: `start.sh` / dispatcher / watchdog の配線

- 範囲: `start.sh` の server 起動（§10.1 の決定どおり）・env 掃除・`lib_daemon_watch.py` の `spawn_command()` / restart が Rex でも通ること・dispatcher / watchdog の `mux_list` / `capture` / `kill` の経路・退役の checkpoint の pane capture（§5）・`state()` が unknown の場合の棚卸し
- QA 項目: 隔離 server で `./crewvia` 相当を一周（Director 窓 + Worker 窓 + dispatcher + watchdog）・Worker の退役（watchdog が kill し、checkpoint に pane が入る）・server を落として respawn・二重 kill（同名の別 Worker を殺さない）・`CREWVIA_KILL_AUTHORITY` の切り替え・**本番の既定 socket に触っていないこと**を QA 自身が確認
- 戻し方: `CREWVIA_MUX` / `mode:` を戻す

### PR-3: docs / README

- 範囲: README の mux の節・CLAUDE.md の環境変数の表（`CREWVIA_REX_SESSION` / `CREWVIA_REX_SOCK` / `CREWVIA_MUX` の値に `rex`）・`knowledge/file-map.md`・この文書の「実測」を実装後の実績に更新・未確認の項目の結果
- QA 項目: 文書の記載どおりに別環境で設定できること（新規ユーザーの手順）

## 12. 未確認（実装前に確かめる一覧）

1. 重複した label を `-b <label>` で引いたときの挙動（§3.1）
2. 複数行の入力欄を空にするキー（§6.3）・paste の直後の Enter・起動直後（`❯` 前）の Enter（§6.4）・長文の paste の畳み（§6.2）
3. `rex window focus` / 引数なしの `rex` の attach（§4）
4. `block close` の SIGHUP から強制終了までの猶予と最終シグナル（§5）
5. 既存 block で command を走らせる API（`pane run` 相当）の有無（§4.2）
6. exit 3 が「target が見つからない」以外にも使われるか（§4 の `record_existence`）
7. scrollback の上限（Director の測定 64MiB を自分では再現していない）
8. UUID の再利用が無いこと（§4.2。見込み）
9. `rex events` の `child_exited` を使った死活の検知（`wait` / `events` は help のみ。dispatcher / watchdog が polling で足りるなら不要）

## 13. 実測の記録（要点）

| 項目 | 結果 |
|---|---|
| `rex version` | `rex 0.0.1793390926-g3b2dce` / commit `3b2dce8…` / built `2026-10-30T20:08:46Z` / `api: revision 9 (supports 0 to 9)` |
| `server status --json` の欄 | `endpoint` / `pid` / `instance_id` / `host_id` / `started_at` / `rex_version` / `api_revision` / `session_count` / `you.{uid,gid,pid}` |
| server が居ない | `rex ls`: exit 4（`no Rex server is running at <sock>, and autostart is disabled`）/ `server status`: exit 1（`rex server is not running`） |
| target が無い | exit 3（`block target "…" not found`） |
| 同 label の block | 2 つ作れた（label は一意でない） |
| 同 label の session | 2 つ作れた・`-s crewvia` は `ambiguous` で拒否 |
| 子プロセス | `child.pid` の ppid = server の pid・session leader |
| `block close` | 約 1.0 秒・SIGHUP を 1 回・3 秒後に消滅（HUP を無視するプロセスでも） |
| command の終了 | block ごと消える（`--keep-open` なら `last_exit` と capture が残る） |
| 再起動（`--restore`） | `instance_id` / pid 変更・ID と label は保持・中身は login shell |
| pane の env | server の env を継承（`CLAUDECODE=1` も）・client の env は渡らない |
| 後始末 | 隔離 server 3 本（初回・restore 用・env 用）をすべて `server stop` / `kill`・`pgrep` で残り 0・symlink 2 本を `unlink` |
