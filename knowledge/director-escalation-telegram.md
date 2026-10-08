# Director の判断待ちの段階上げと Telegram の経路 — 設計

mission `20261005-director-escalation-telegram` / t001 (設計のみ。この PR にコードは無い)。
実装は PR-A (Telegram の経路) と PR-B (段階上げ) に分ける (§9)。

## 0. 背景と決定済みの方針

mission `20261004-watchdog-hard-idle-unknown` で t017 が `needs_director` のまま Director が閉じ忘れ
(判断の後の一手の前に中断)、後続 t006 が約 10.5 時間止まった。dispatcher は
`[blocked] ... unmet deps` を自分のログに 5 分ごとに書くだけで、Director への通知は
`needs_director` に入った最初の 1 回 (`notify_state_once`、状態ベースで 1 回だけ。`knowledge/notify-once.md`) で
終わり、ユーザーの手元には何も届かなかった。

ユーザーと合意済み (2026-10-05):

- **段階上げ**: Director の判断待ちが続いたら、**5 分で Director に再通知・10 分で Telegram でユーザーに通知** (初版は「後続を止めていたら」だったが、後続の有無を条件にしない形にユーザーが変更。§5-2)。
  どちらも config で変えられる。
- **Telegram のボタンで判断できる**: Director がユーザーに選択肢を出すとき、待たずに Telegram にボタン付きで送る。
  押された答えは Director の画面に届き、Director が進める。込み入った指示は Telegram への返信文か
  /remote-control (セッションのリンクを本文に付ける)。
- Worker のツール実行の承認 (`hooks/pre-tool-use.sh` / Taskvia) は**対象外**。
- **bot は crewvia 専用**。ai-editorial と共用すると両者の `getUpdates` が互いの更新を奪う
  (offset を進めた側だけが受け取る)。認証情報 (bot token・chat_id) は **config で選んだ 1 つの取り出し方**
  (1Password か、リポジトリ外の 0600 ファイル。§1-1・§10) からだけ解決する。`CREWVIA_TG_*` の env は**読まない**。
- 秘密を読まない・書かない。`.env` を開かない。テストは偽の Bot API サーバーで。

### 参考実装 (`~/workspace/ai-editorial/scripts/tg_send.sh` / `tg_poll.sh`) から**写さないもの**

| ai-editorial | crewvia | 理由 |
|---|---|---|
| `curl https://api.telegram.org/bot${TOKEN}/...` | Python の `urllib` をプロセス内で使う | URL に token が入るので、curl の引数だと `ps` / `/proc/*/cmdline` に token が出る |
| 状態ファイル `queue/telegram_offset.txt` | `registry/daemons/` 配下、`lib_daemon_state` の入口 | queue は mission の正本。デーモンの状態は registry/daemons (不変条件 7 の置き場) |
| 失敗は終了コード 1 で `set -e` | 失敗は**値**で返し、呼び出し側のサイクルを止めない (§7) | dispatcher が割り当てを止めたら本末転倒 |
| long-poll (`--wait` 50 秒チャンク) | `timeout=0` の 1 回 (§1) | デーモンのサイクルを塞がない |

## 1. 受信をどこで回すか

### 案

- **(a) dispatcher の 1 サイクルに `getUpdates(timeout=0)` を 1 回足す**
- (b) 専用の受信デーモン (相互監視 `lib_daemon_watch` に 3 つ目を足す)
- (c) Director の画面から `ask_user.sh wait` で long-poll する (ツール呼び出しで Director の手を塞ぐ)

### 推奨: (a)

| 観点 | (a) dispatcher | (b) 専用デーモン | (c) Director が待つ |
|---|---|---|---|
| 新しい常駐物 | 増えない | 増える。相互監視・flap ガード・restart 手順・stale しきい値が 3 者に | 増えない |
| 不変条件 4 (再起動は `lib_daemon_watch.py restart`) | 既存の経路にそのまま乗る | 3 つ目の restart / 同時死の backstop (post-tool-use.sh) を書き足す。`knowledge/daemon-authority.md` §7 の表が全部 3 者に | 関係なし |
| 受信の遅れ | サイクル間隔 (5 秒) + 受信の間引き (下記 10 秒) | ほぼ 0 | ほぼ 0 だが Director が占有される |
| Director が不在/idle のとき | 受信できる (dispatcher は常駐) | 受信できる | **受信できない** (本件は Director が止まった場面が主) |
| 失敗の波及 | dispatcher のサイクルに乗るので**対策が要る** (下記) | 受信デーモンだけが止まる | Director の画面が止まる |

(c) は「Director が止まっている」ことを検出したい本件の目的と相反する。(b) は正しく作れるが、
デーモンを 1 つ増やす費用 (相互監視の 3 者化) が、**秒単位の遅れを無くす**利益に見合わない
(人間がボタンを押す遅れの桁は秒~分)。dispatcher は既に「queue を読む唯一のデーモン」で、
Director への送信 (`tmux_send(_director_name(), ...)`) を持つので、受信した答えを転送する先と同じ場所で受ける。

### (a) の条件 — dispatcher のサイクルを守る

dispatcher のサイクルが止まると**全 mission の割り当てが止まる** (`dispatcher.sh` の
CYCLE ENTRY POINT)。ネットワーク I/O を足すので次を満たす:

1. **サブプロセスで、上限付きで呼ぶ。** サイクルの中で `urllib` を直接呼ばず、
   `python3 scripts/lib_telegram.py poll` を `timeout 8` 付きで起動する (`urlopen(timeout=5)`。
   DNS・TLS の stall もサブプロセスごと打ち切る)。戻り値は終了コードではなく**標準出力の JSON**
   (`{"forwarded": n, "undelivered": n, "error": "<種別>"}`)。サブプロセスが死んでも、
   呼び出し側は「今回は何も受けなかった」で次のサイクルに進む。`poll` は専用ロック
   (`registry/daemons/telegram-poll.lock`、非ブロッキング `flock`) を取り、取れなければ何もせず戻る
   (前のサブプロセスが残っていても 2 つ重ならない = offset の書き手は常に 1 者)。
2. **間引く。** 5 秒ごとには呼ばない。`TG_POLL_INTERVAL` (既定 10 秒、config `telegram.poll_interval_seconds`) の
   スロットル。スロットルの時刻は他の通知スロットルと同じ入口 (`should_notify` 系) ではなく、
   **受信専用の状態** (§2-5 の `telegram-offset.json` の `last_poll_at`) に置く (通知の `NOTIFY_TTL` と混ぜない)。
3. **未設定なら 1 バイトも触らない。** 認証情報 (§1-1 の `resolve_credentials()`) が解決できなければ、
   サブプロセスを起動せず、質問台帳も offset も作らず、ログも出さない (公開前提・Taskvia 非依存と同じ型。
   `CREWVIA_TASK_GRAPH=0` の「1 バイトも書かない」と同じ扱い)。**例外は 1 つだけ**: 受信側の状態
   (§1-1 の `telegram-receiver.json`) は、**既にファイルがあるとき**に限り `enabled: false` へ書き換える
   (「有効だったのに無効になった」を送信側に見せるため。**初めから未設定なら何も作らない**)。
   - これは**共有規則の env 停止スイッチではない** (不変条件 5 の対象外。認証情報の env も読まない — §1-1) — ただし「設定済みか」の答えを
     送信側 (`ask_user.sh`) と受信側 (dispatcher) が**別々に出す**と不変条件 5 と同じ型の事故になる。
     それを §1-1 で塞ぐ (設計レビュー t002 の P1-2)。
   - **受信を起動する条件**: 認証情報が解決でき、かつ**未回答の質問がある** = 「`open` かつ期限内」
     (定義は §2-3b。期限切れの掃除は `poll` の冒頭で済ませる) が 1 件以上。通常運用のほとんどのサイクルで
     外部への通信がゼロになる。**bot 宛の雑多なメッセージ (`/start` 等) は拾わない・offset も進めない** (§2-6)。
   - 掃除だけは通信を伴わないので、**台帳ファイルがあれば**サイクルごとの間引き (上の 2) で走らせる
     (期限切れの `open` を `expired` に書き、ボタンを消す `editMessageReplyMarkup` が要るものだけ通信する。§2-3b)。
4. **mux 非依存。** 転送は既存の `tmux_send()` (= `_mux.send`) を使う。tmux でも herdr でも同じ。
   mux が無いインラインモードでは dispatcher が動かないので、受信も動かない (§7 に「届かない」の扱い)。

### 1-1. 受信側の状態と認証情報 — 送信側と答えを割らない (t002 P1-2)

**問題**: `lib_daemon_watch.spawn_command` は env を allowlist (`_SPAWN_ENV_VARS`) だけ運び、秘密
(`TASKVIA_TOKEN` 等) は意図的に運ばない (`scripts/lib_daemon_watch.py` の `_SPAWN_ENV_VARS` の注記: コマンド文に
秘密を書くと `ps`・pane の scrollback・mux のログに出る)。watchdog が dispatcher を respawn すると
env で渡していた認証情報は落ち、受信と段階 2 が**黙って**止まる。一方 Director のシェルには token があり、
`ask_user.sh ask` は送れてしまう (押されたボタンを誰も受けない)。memory: `daemon-secret-env-lost-on-respawn`。

**直し方は 2 層** (どちらも要る):

1. **割れを起こさない仕組み (最低限・必須)**: dispatcher が「変化したとき + `RECEIVER_HEARTBEAT_SECONDS` (定数 30 秒) ごと」に
   `registry/daemons/telegram-receiver.json` を書く。**書き手は dispatcher だけ**。
   ```json
   {"enabled": true,  "checked_at": 1759650000.0, "reason": "ok", "bot_id": 123456789, "chat_hash": "3fa9c2…"}
   {"enabled": false, "checked_at": 1759650030.0, "reason": "no_credentials"}
   ```
   `reason` は固定コード (`ok` / `no_credentials` / `credential_command_failed` / `credential_file_permissions` …)。
   **token・chat_id・参照の文字列・ファイルのパスは書かない**。書くのは**秘密でない識別子だけ**:
   `bot_id` (bot の数値 id = token の `:` より前の部分) と `chat_hash` (chat_id の SHA-256 の先頭 12 桁。
   chat_id そのものは書かない)。`enabled: false` のときは両方とも書かない。
   - **心拍の間隔と stale のしきい値は同じ値にしない (t014 P2-1)**: 心拍の間隔は `poll_interval` と**独立の定数**
     `RECEIVER_HEARTBEAT_SECONDS = 30`、stale のしきい値は **`3 × RECEIVER_HEARTBEAT_SECONDS` = 90 秒** (心拍から導く。
     `poll_interval` を変えても動かない)。実際の書き込み間隔は 30 秒 + サイクル 1 回 (5 秒 + 処理時間) 程度なので、
     正常運転で古さが 90 秒に届くことはない。テストの境界: 「心拍の間隔ちょうど + 1 サイクル」は stale にならない・
     「しきい値ちょうど」と「+ 1 秒」で stale になる・`poll_interval` を 5 / 60 秒に変えても結果が変わらない。
   - `ask_user.sh ask` は送る前に必ずこれを読み、次の**全部**を満たさなければ**断る** (exit 4。stderr に固定コード):
     (1) `enabled == true` (`receiver_disabled`)、(2) `checked_at` が上の stale しきい値以内 (`receiver_stale`)、
     (3) ファイルがある (`receiver_unknown`)、(4) **`ask` 自身が `resolve_credentials()` で解決した bot の `bot_id` と
     `chat_hash` が、ファイルの値と一致する (`receiver_mismatch`。t014 P2-4)**。(4) は「送る側と受ける側が
     別の bot / 別の chat に解決した」を捕まえる (優先順位を 1 つに決める (下の 2) 以外の保険)。比べるのは
     秘密でない識別子だけで、token は比べない・書かない。これは初版の「dispatcher の生存確認」を置き換える — 心拍が新しければ
     生きていて、かつ受信できる状態だと言える。`ask` は断るので、押されても誰も受けないボタンは出ない。
     Director は `AskUserQuestion` に戻る。
   - **割れを Director に 1 回知らせる**: dispatcher が「前回 `enabled: true` で今回 `enabled: false`」を観測したとき、
     既存の `notify_state_once` で Director に 1 通 (key `telegram_receiver_disabled`、fp = `reason`):
     「Telegram 受信が無効になりました (理由コード)。dispatcher が起動時に認証情報を取り出せなかった可能性 (1Password のロック・`op` が PATH に無い・ファイルの権限)。
     原因を直して `lib_daemon_watch.py restart` を行うか、`telegram.credentials.source` を見直してください」。状態を離れた (`enabled: true` に戻った) ら台帳から捨てる。
   - 受信側の状態の書き方は他の台帳と同じ入口 (`lib_daemon_state`・`telegram_receiver_problem()`・原子的置換)。
     壊れていたら `ask` は断る側に倒す (**観測できなかったことを「有効」に倒さない**)。
2. **respawn 後も認証情報を届ける方法 (ユーザー決定 2026-10-05。§10)**: コマンド文・env allowlist (`_SPAWN_ENV_VARS`) には
   秘密を載せない。**取り出し方は config で 1 つだけ選ぶ**: `telegram.credentials.source: op | file`。
   - **`op` (本命・A')**: config には `op://…` の**参照だけ** (`telegram.credentials.token_ref` / `chat_id_ref`。秘密ではない)。
     `dispatcher.sh` が**起動時に 1 回** (bash の外側・`while true` の前。respawn のたびに通る) 1Password CLI で取り出し、
     その bash プロセスの**シェル変数**に持つ (`export` しない。argv にも bash プロセスの env にも出ない)。サイクルごとの python
     (`poll`・段階 2 の送信・心拍) には **bash の前置代入** (`_CREWVIA_TG_RESOLVED_TOKEN="$tg_token" _CREWVIA_TG_RESOLVED_CHAT_ID="$tg_chat" python3 - <<'PYEOF'`
     のように、**`env` コマンドを付けない**) でその呼び出しにだけ渡す。**`env VAR=… python3` は使わない**: `env` は外部コマンドなので
     `VAR=…` が env の argv になり、サイクルごと (5 秒) に `ps` / `/proc/<pid>/cmdline` に token が出る (t016)。前置代入なら argv には出ず、
     子の environ にだけ載る (同一ユーザーのみ・その呼び出しの間だけ)。運搬用の変数名は **`_CREWVIA_TG_RESOLVED_*` で、
     ユーザーが設定しうる `CREWVIA_TG_*` と分ける** (下の優先順位の「env を読まない」と食い違わないため)。python のサイクルごとに
     1Password を呼ばない (t014 P2-3 — dispatcher の python はサイクルごとに新しいプロセスで、サイクル単位に呼ぶと
     1Password のロック・遅延・利用制限が毎サイクルに乗る)。呼び出しは **dispatcher の起動 (= respawn) ごとに 1 回**。
     `ask_user.sh` は呼び出しごとに同じ関数で取り出す (Director の 1 回の質問につき 1 回)。
   - **`file` (代替・B)**: リポジトリ外の権限 0600 のファイル (config `telegram.credentials.file`、例
     `~/.local/share/crewvia/telegram.env`) を `resolve_credentials()` が読む。**所有者が自分でなく、または権限に
     group / other のビットが 1 つでも付いている (= `mode & 0o077 != 0`。0600 に限らず 0400 も通す。t016 P3-2) ときは中身を開かず**「停止」(`reason: credential_file_permissions`
     を receiver.json に。§1-1 の層 1 でそのまま `enabled: false` になる)。シンボリックリンクは辿らない。
     このファイルは Claude (Director / Worker) が読み書きしない (§10 の運用)。
   - **優先順位は 1 つだけ (t014 P2-4)**: 認証情報は **`telegram.credentials.source` が指す 1 つの取り出し方からだけ**
     解決する。**`CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID` 等の env は読まない** (Director のシェルに env があって
     dispatcher には無い、で違う bot に解決する割れの根を作らない)。`source` が未設定・不正なら「未設定」(`no_credentials`)。
     `op` / `file` の**両方が設定されていても `source` が選んだ方だけ**を使う (もう一方は読まない・失敗しても無関係)。
   - **認証情報の解決は `lib_telegram.resolve_credentials(config)` の 1 か所**で、`ask_user.sh`・dispatcher の起動時・
     dispatcher の `poll` / 段階 2 の送信・心拍が同じ関数を通す。dispatcher が起動時に取り出した値を python に渡すのは
     「同じ関数が `source` どおりに取り出した結果の運搬」であって別の解決経路ではない (python 側は渡された値を
     `resolve_credentials(config, carried=True)` が `_CREWVIA_TG_RESOLVED_*` から受け、`source` と食い違う運搬は受けない。
     `carried=True` を渡すのは dispatcher が起動する python の動詞 (`poll`・段階 2 の送信・心拍) だけで、**`ask_user.sh` は運搬用の変数を読まない**
     — Director のシェルに `_CREWVIA_TG_RESOLVED_*` や `CREWVIA_TG_*` があっても `ask` の解決結果は変わらない)。
     トークンを 1Password 側で差し替えた直後は、dispatcher (起動時の値) と `ask` (新しい値) が別の bot になりうる —
     それは (4) の `receiver_mismatch` が断るので、dispatcher を restart すれば直る (`ask` が断る間は `AskUserQuestion` に戻る)。
   - **取り出せなかった間の取り直し (t016 P3-1。PR-A で決定・実装済み)**: 取り出しは起動時の 1 回だけなので、起動時に 1Password が
     ロックされていると、後で解錠しても restart まで `enabled: false` のままになる。そこで **取り出せなかった間 (`reason` が
     `no_credentials` 以外) だけ**、`dispatcher.sh` の bash ループが **`TG_RESOLVE_RETRY_SECONDS = 600` (10 分) 間隔**で取り直す
     (`_tg_maybe_retry`。サイクル単位では呼ばない・成功したら以後は呼ばない・`no_credentials` = 未設定は対象外で何も呼ばず何もログに出さない)。
     解錠は最大 10 分で反映され、1Password の呼び出しは「起動 + 失敗の間の 10 分に 1 回」に収まる。10 分の値は定数で、config・env には出さない
     (dispatcher だけが使うしきい値)。

本番確認 (PR-A): 「`lib_daemon_watch.py restart` (または watchdog の自動 respawn) の後にも、ボタンが受信される
(または `ask` が `receiver_disabled` で断られ、Director に通知が 1 通来る)」ことを 1 回観察する。

### 副次: getUpdates の消費者は dispatcher の 1 者だけ

`getUpdates` は 1 つの bot に対して**消費者が 1 者**でなければならない (複数だと更新を奪い合う)。
`ask_user.sh` (Director 側) は**送信だけ**で、受信はしない。config の `telegram.credentials` が指す bot を
他のツールが `getUpdates` で読んではいけない — これを `scripts/CLAUDE.md` と README に 1 行足す (PR-A)。

## 2. 質問と答えの対応づけ

### 2-1. 質問の記録

`registry/daemons/telegram-questions.json` (`.gitignore` 対象。`lib_daemon_state.load_json_store` の入口で
読み、`told_lock` と同じ型のロックと `write_told_atomic` と同じ原子的置換で書く。壊れたエントリが 1 つでもあればストア全体が
`Unreadable` という既存の契約に従い、**shape の検証関数 `telegram_questions_problem(data)` を
`lib_daemon_state.py` に足す**。書き手 (`ask_user.sh`) もこれを通してから書く — 読み手だけが厳しいと
書いたばかりの台帳を読めなくなる。`told_entry_problem` の docstring と同じ理由)。

```json
{
  "q-1a2b3c4d": {
    "nonce": "9f3a1c",           // 札 (6 hex。この質問のボタンだけを認める)
    "question": "…",             // 送った文面 (記録用。Director 画面の再掲にも使う)
    "options": ["A: …", "B: …"], // ラベル (callback_data には index だけを入れる)
    "slug": "20261004-…", "task": "t017",   // 任意。--task を付けたときだけ
    "execution_id": "ex-…",      // 任意。task の card から読んだ「この試行」
    "message_id": 4711,          // 送信に成功した sendMessage の message_id
    "status": "open",            // open | answered | withdrawn | expired
    "created_at": "…", "expires_at": "…",
    "answer": {"kind": "choice|text", "index": 1, "text": "…", "at": "…", "update_id": 9001},
    "forwarded": false           // Director の画面へ送り終えたか
  }
}
```

- 質問 ID は `q-` + `secrets.token_hex(4)`。**札 `nonce` は別の乱数** (`secrets.token_hex(3)`)。ID は画面にも出る
  (`[telegram-answer] q=…`) ので推測されうるが、札は callback_data にしか載らない。
- **callback_data** (Telegram の上限 64 バイト): `<qid>.<nonce>.<index>` (例 `q-1a2b3c4d.9f3a1c.1` = 20 バイト前後)。
  ラベルの文字列は入れない (入れると 64 バイトを超え、改竄にも弱い)。ラベルは**記録から** index で引く。
- 質問の最大同時数・保管: 「未回答の質問がある」(`open` かつ期限内。定義は §2-3b) は最大 8 件 (超えたら `ask` が拒否 = §7)。`answered` / `withdrawn` / `expired` は
  7 日で掃除する (消してよい台帳。不変条件 7 の仲間 — **消えても復旧手順になる**: 古いボタンは「不明」で拒否されるだけ)。

### 2-2. 受け付ける条件 (全部 AND。1 つでも欠けたら転送しない)

callback_query (ボタン) の場合:

1. `callback_query.from.id` が解決した `chat_id` (§1-1) と一致 (個人チャットでは user id == chat id)。
   **`message.chat.id` も一致**を要求する (グループに bot が入った場合、他人の押下を排除)。
2. `callback_data` が `<qid>.<nonce>.<index>` の形に厳密に一致 (正規表現で全体マッチ。余りは拒否)。
3. `qid` が台帳にあり、**`nonce` が一致**し、**`callback_query.message.message_id` が記録の `message_id` と一致**
   (別メッセージのボタンの流用を排除)。
4. `index` が `options` の範囲内。
5. `status == "open"` かつ `now < expires_at`。

テキスト返信の場合 (「込み入った指示は返信文で」):

1. `message.chat.id` / `from.id` が解決した `chat_id` と一致。
2. **`message.reply_to_message.message_id` が `open` な質問の `message_id` と一致**する場合だけ受け付ける。
   返信でない雑多なメッセージは**質問に結びつけない = 転送しない**。単独のメッセージを「最新の質問への答え」と
   推測すると、別の話題を判断として転送してしまう。
3. 本文は制御文字を除き、改行を空白にし、500 文字で切る (Director の入力欄に 1 行で入れる)。切ったら `…(truncated)`。

### 2-3. 状態遷移と各ケースの扱い

```
open ──ボタン/返信──▶ answered   (記録: answer + update_id、forwarded=false → 転送後 true)
open ──ask_user.sh cancel──▶ withdrawn  (Director が画面など別の手段で答えを得た)
open ──expires_at 超過──▶ expired       (§2-3b: poll の冒頭 / ask の冒頭が書く)
open ──message_id が null のまま 5 分──▶ withdrawn  (§2-3b: ask が途中で落ちた残骸)
```

| ケース | 扱い |
|---|---|
| **二重押し** (answered の質問のボタンをもう一度) | 転送しない。`answerCallbackQuery` で「回答済み: <ラベル>」。**先に確定した 1 つだけが有効**。状態変更は `told_lock` の下で「`open` → `answered`」を 1 回だけ行う (CAS)。違う選択肢を後から押しても上書きしない |
| **古いボタン** (台帳に無い・nonce 不一致・`answered` / `withdrawn` / `expired` の質問) | 転送しない。`answerCallbackQuery` で「期限切れ / 回答済み / 不明な質問」。ボタンは**期限・取り下げの時点で既に消してある** (§2-3b) ので、押せるのは消す前の競合だけ |
| **Director が既に別の手段で答えを得た後の押下** | Director は答えを得た時点で `ask_user.sh cancel --q <id> --by screen` を呼ぶ (§4)。`cancel` が `withdrawn` にして**ボタンも消す** (best effort)。消える前の競合で押された場合は転送されず、「画面で回答済み」と返す。**Director が cancel を忘れた場合**は転送される — その場合も Director の規則 (§3) で、実行の直前に状態を確かめ直すので、既に済んだ操作を二重に実行しない |
| 質問に `--task` が付いていて、その task が既に判断待ちを離れていた | 転送はする (ユーザーは押した)。行に `task_state=<status>` を添える (§3)。Director が状態を見て判断する |
| 転送 (mux send) に失敗した・Director 不在 | `answered` / `forwarded=false` のまま。**`answerCallbackQuery` は先に返す** (ボタンの待ち表示を止める)。次の受信サイクルで `forwarded=false` の answered を再送する。期限は質問の `expires_at` ではなく**答えが入った時刻から 24 時間**で諦め、Telegram に「Director に届きませんでした」と 1 回返す |
| 質問の `message_id` がまだ `null` (ask の ③ の前) の `open` を指す押下 (**文章の返信も同じ**: 返信先がどの質問にも一致せず、未確定の open がある間は保留。§2-3c) | **転送せず、offset を進めない** (次のサイクルで同じ update をもう一度受ける)。窓は ask の ②→③ の間だけで小さい。5 分を超えて `null` のままなら §2-3b で `withdrawn` になり、その後の押下は「不明」で拒否される (t002 P3-1)。**head-of-line**: その update より**後ろの** update (他の質問への答え) も処理はする (CAS と `update_id` で冪等)。ただし offset は先頭の保留で止まるので、後ろの update は次のサイクルでも再び届き、同じ結果に畳まれる。最大 5 分 (保留が `withdrawn` になるまで) 続く (t014 P3-3) |
| 台帳が `Unreadable` | 転送しない・offset を**進めない**・`answerCallbackQuery` も呼ばない (ボタンの待ち表示が残る)。ログに `WARNING: telegram-questions` を 1 回/10 分。台帳を消せば復旧 (§2-1)。fail の向き: **観測できなかったことを「答えが無い」に倒さない** |

### 2-3b. 期限と掃除 — 誰がいつ `expired` / `withdrawn` を書くか (t002 P2-1)

- **書き手は 2 者**: (1) `lib_telegram.py poll` の冒頭、(2) `ask_user.sh ask` の冒頭。どちらも `told_lock` の下で
  台帳を読み直し、次を**同じ関数 `sweep_questions(ledger, now)`** (純粋関数。新しい台帳を返す) で行う:
  1. `status == open` かつ `now ≥ expires_at` → `expired`
  2. `status == open` かつ `message_id == null` かつ `now - created_at ≥ 300 秒` → `withdrawn`
     (ask が ①〜③ の間で落ちた残骸。ボタンはまだ出ていないか、出ていて台帳に記録が無い。出ていた場合のために
     ②の `sendMessage` の応答を ③ より先にログへ残さない — 残骸のボタンは「不明」で拒否される)
  3. `answered` / `withdrawn` / `expired` で 7 日を過ぎたもの → 削除
- 掃除で `open` を離れた質問のうち `message_id` を持つものには、`editMessageReplyMarkup` でボタンを消す
  (best effort。失敗しても台帳は進める)。**この通信は掃除が新しく `expired` / `withdrawn` にした質問の分だけ**で、
  `open` が 0 件になった後は走らない。
- **「未回答の質問がある」の定義** (受信を起動する条件 §1-3 と `ask` の上限 8 件で**同じ関数**を使う):
  `status == open` かつ `expires_at > now` かつ (`message_id != null` または `now - created_at < 300 秒`)。
  期限切れ・残骸の `open` を数えて受信が止まらない / 8 件で `ask` が詰まる、を防ぐ。掃除は数える前に必ず走る。

### 2-3c. update の種類 × 照合の結果 — 全セルの表 (PR #281 の Codex P1)

**P1 の欠陥**: 送信 (§4 ②) は済んだが `message_id` の記録 (③) の前に届いた**文章の返信**は、`reply_to_message.message_id` で照合する
相手がまだ台帳に無く、「無関係」として捨てられ offset も進んだ → 二度と読めず、ユーザーの答えが黙って失われた。ボタンの押下は
`message_id == null` の open を指すと保留する (§2-3) のに、返信には同じ扱いが無かった。**同じ族** = 「照合できない」理由を取り違えると答えが
失われる (「待てば照合できる」を「無関係」と読む)。理由ごとに扱いを 1 つずつ決め、全セルを表駆動で押さえる
(`tests/test_telegram_pure.py::TABLE`・手書きの oracle。実装の判定を呼び直さない)。

行 = update の種類 (自分の chat・自分の発言のとき)、列 = 照合の結果。セルは「**判定 / offset / Director に届くもの**」:

| 種類 ＼ 結果 | 一致 (open・期限内) | 不一致 (台帳に無い・札違い・別メッセージ・範囲外) | **未確定** (message_id が null の open がある) | 期限切れ | 取り下げ | 回答済み |
|---|---|---|---|---|---|---|
| **callback_query** (ボタン) | answer / 進める / **転送** | reject (「不明な質問」) / 進める / 無し | **hold / 進めない** / 無し (次のサイクルで同じ update を再受信) | reject (「期限切れ」) / 進める / 無し | reject (「画面で回答済み」) / 進める / 無し | reject (「回答済み: <ラベル>」) / 進める / 無し |
| **返信** (reply_to_message あり・本文あり) | answer / 進める / **転送** | ignore / 進める / 無し (関係の無いメッセージへの返信) | **hold / 進めない** / 無し (**P1**。返信先が台帳のどのメッセージとも一致せず、猶予内の未確定な open がある間だけ) | ignore / 進める / 無し | ignore / 進める / 無し | ignore / 進める / 無し |
| **普通のメッセージ** (返信でない・`/start` 等の bot コマンドを含む) | ignore / 進める / 無し | 同左 | 同左 (**保留しない** — 返信でないものは質問に結びつけない。§2-2) | 同左 | 同左 | 同左 |
| **編集** (`edited_message`)・その他の update (`my_chat_member` 等)・テキストの無い返信 (画像等) | ignore / 進める / 無し | 同左 | 同左 | 同左 | 同左 | 同左 |
| **他 chat・他人の発言・他人の押下** (どの種類でも) | ignore / 進める / 無し | 同左 | 同左 | 同左 | 同左 | 同左 |

決めたこと:

- **保留 (hold) になるのは 2 セルだけ**: 「ボタンが未確定の質問を指す」と「返信先が台帳のどのメッセージとも一致せず、猶予内の未確定な open がある」。
  どちらも「待てば照合できるかもしれない」理由があるときだけ。**保留の理由を広げない** — 普通のメッセージ・閉じた質問への返信まで保留すると、
  offset が最大 5 分止まる (その間、他の質問の答えも毎サイクル再受信する)。
- 返信先が**すでに記録済みの別の質問**のメッセージ (期限切れ・取り下げ・回答済み) なら、それは「閉じた質問への返信」で、未確定な質問が別にあっても
  保留しない (ignore)。
- **保留の上限** = §2-3b の猶予 (`created_at` から 300 秒)。猶予を過ぎた `message_id == null` の open は sweep が `withdrawn` にする。以後その返信は
  不一致として ignore され、offset が進む。保留中も**後ろの update は処理する** (offset だけが先頭の保留で止まる。§2-3 の head-of-line)。
- 閉じた質問へのボタン押下には `answerCallbackQuery` で理由を返す (待ち表示を止める)。**閉じた質問への返信には Telegram に何も返さない**
  (返信は台帳に結びつかないので、無関係なメッセージと区別せず無言で捨てる。ユーザーへの通知を足すなら別の変更)。
- **Director に届くのは「一致」のセルだけ** (answer → `forwarded=false` → 転送)。reject / ignore / hold は何も届かない。

### 2-3d. 1 サイクルの通信の全表 — 受信は後始末に締め出されない (PR #281 の Codex P1・3 巡目)

**不変条件: どんな後始末の失敗が続いても、open な質問への押下は 1 サイクル以内に受信され、台帳に記録される。**

修正前は `poll_once` が `getUpdates` の**前**に `_unbutton_pending` を呼び、閉じた質問ごとの `editMessageReplyMarkup` を
最大 `API_TIMEOUT_SECONDS` (5 秒) まで待っていた。失敗したものは `unbutton` のまま次のサイクルで再試行されるので、
Telegram が遅い・編集が失敗し続ける (古い / 消されたメッセージ) と、dispatcher のサブプロセスの上限 (`timeout 8`) を後始末だけで使い切り、
`getUpdates` に届かないサイクルが永久に続いた。

順序は **受信 → 台帳と offset への記録 → 後始末**。後始末は `poll` 1 回の持ち時間 (`POLL_BUDGET_SECONDS` = 6.5 秒。`timeout 8` より短い) の
**残りだけ**を使う (`_Budget.timeout()`。残りが `BUDGET_MIN_REMAINING_SECONDS` = 1 秒未満なら通信せず次のサイクルへ。1 回の通信の timeout は
`min(5, 残り)`)。種類ごとに 1 サイクルの件数にも上限がある (`CLEANUP_MAX_PER_CYCLE` = 3)。

| # | 通信 | どこで | 受信との順序 | 持ち時間 | 失敗したとき |
|---|---|---|---|---|---|
| 1 | `getUpdates` | `poll` の `_receive_updates` | **最初** (ここより前に通信しない) | 5 秒 (全体の持ち時間の最初の分) | `error` を返し offset は据え置き。次のサイクルで同じ update |
| 2 | `answerCallbackQuery` | `poll_once` (台帳と offset への記録の**後**) | 受信の後 | 残り・件数 6 まで | 無視 (ボタンの待ち表示が残るだけ。答えは台帳にある) |
| 3 | `editMessageReplyMarkup` (閉じた質問のボタンを消す) | `_unbutton_pending` | 受信の後 | 残り・3 件まで・試行の少ない順 | 下の表 |
| 4 | `sendMessage` (転送を諦めた通知) | `_give_up_forwarding` | 受信の後 | 残り・3 件まで | **残りが足りなければ印 (`gave_up`) を付けない** (印 → 通知の順なので、印だけ付いて通知が出ない穴を作らない) |
| — | `sendMessage` (質問の送信) / `editMessageReplyMarkup` (ask の巻き戻し) / `cancel` の消去 | `ask_user.sh` の別プロセス | poll とは別のプロセス (poll のロックも持ち時間も共有しない) | 自分の 5 秒 | poll の受信を遅らせない。`cancel` の消去は `_Budget` 付き |
| — | getMe / 再送ループ | 無い (使っていない) | — | — | — |

`run_cycle` (dispatcher のサイクル) は通信しない (サブプロセスを起動するだけ。`timeout 8`)。したがって 1 サイクルに受信より前に走る
ネットワーク呼び出しは**無い**。表の 2〜4 は、1 つの `poll` の中で持ち時間を分け合う。

**ボタンを消す試みの諦め** (`unbutton` を外す条件。`_settle_unbutton`):

| 結果 | 扱い |
|---|---|
| 成功 | `unbutton: false` |
| Bot API の 400 / 403 / 404 (編集できないメッセージ・消えたメッセージ・bot が外された) | 待っても直らない → その場で `unbutton: false` |
| 429 (`rate_limited`) | そのサイクルの後始末を止める。回数には数えない (閉じてからの時間が諦めを担う) |
| network / 5xx / その他 (一時的) | `unbutton_tries` を 1 足す。`UNBUTTON_MAX_TRIES` (5) 回で諦める |
| 閉じてから `UNBUTTON_GIVE_UP_SECONDS` (1 時間) 経過 | 結果にかかわらず諦める |
| `message_id` が無い | 通信せず外す |

**諦めても安全な理由**: 「消えないボタン」は押されても、閉じた質問 (`expired` / `withdrawn` / `answered`) は §2-3 の照合 (台帳の status) で
拒否され、転送されない (`answerCallbackQuery` で「期限切れ / 取り下げ」)。ボタンを消すのは見た目の後始末で、答えの正しさを守っているのは台帳。
試行の少ない順に回すので、失敗し続ける 1 件が他の件を止めない。`needs_net` (`run_cycle` が poll を起動する条件) も `unbutton` が外れれば偽になり、
永久に poll を起動し続けない。`unbutton_tries` は任意の欄 (`telegram_question_entry_problem` が非負整数を検査)。旧コードは読み捨てる。

テスト: `tests/test_telegram_receive_not_starved.py` (`timeout 8` のサブプロセスで編集が遅い再現・全後始末メソッド × 失敗の種類 × 1 サイクルで受信・
順序・件数と時間の上限・諦めの表・構造 (poll の経路の `api_call` は全部 `timeout=` を取る・受信が後始末より先))。

### 2-4. offset

- `telegram-offset.json` に `{"offset": N, "last_poll_at": …, "bot_id": …, "chat_hash": …}` (識別子は §2-5b。別の bot のものは使わず捨てる) (**書き手は `poll` だけ**。§1-3 の `telegram-poll.lock` で
  2 つの `poll` が重ならない。`ask_user.sh` や段階 2 の送信は触らない — t002 P2-3)。
  `getUpdates(offset=N, timeout=0, allowed_updates=["callback_query","message"])`。
- **offset を進めるのは、その update を処理し終えた (台帳に答えを書いた / 拒否して返信した / 無関係と確定した) 後**。
  処理途中で落ちたら同じ update をもう一度受ける (**at-least-once**)。重複しても §2-3 の CAS が 1 回に畳む。
  (`update_id` を `answer.update_id` に残し、同じ update_id の再処理は何もしない。)
- 台帳が `Unreadable` のとき、および `message_id == null` の `open` を指す押下があるときは offset を進めない (§2-3)。

### 2-5. 置き場のまとめ

| ファイル | 書き手 | 内容 | 消してよいか |
|---|---|---|---|
| `registry/daemons/telegram-questions.json` | `ask_user.sh`・`poll` (どちらも `told_lock` の下) | 質問台帳 (§2-1) | 消してよい (古いボタンが不明になるだけ) |
| `registry/daemons/telegram-offset.json` | `poll` のみ (`telegram-poll.lock`) | offset・最後の受信時刻 | 消してよい (offset が 0 に戻る → 古い update を再受信するが、台帳に無い質問のボタンは拒否される。`message` の返信は `reply_to` で台帳と照合されるので誤転送しない) |
| `registry/daemons/telegram-send.json` | `ask_user.sh`・dispatcher の段階 2 送信 (**どちらも `told_lock` と同じ型のロック**の下で読み直して書く) | `last_sent_at`・バックオフ (`backoff_until`・連続失敗数)・429 の `retry_after`・失敗ログのスロットル | 消してよい (レート制限の記憶が消える = 最初の 1 通だけ制限が緩む) |
| `registry/daemons/telegram-receiver.json` | dispatcher のみ | 受信側の状態 (§1-1) | 消してよい (`ask` が `receiver_unknown` で断る側に倒れる。次の心拍で復旧) |
| `registry/daemons/escalation-state.json` | dispatcher のみ (PR-B) | 段階上げの台帳 (§5-4) | 消してよい (時計が最初からになる = 通知が遅れる側。§5-4) |

**書き手が 2 者以上のファイルは `telegram-questions.json` と `telegram-send.json` だけで、どちらもロックの下で読み直す。**
offset とレート系を別ファイルにしたのはそのため (offset は 1 者、レートは 2 者)。
5 つとも `notified-state.json` と同じ置き場・同じ入口 (`lib_daemon_state`)・同じ「消してよい」の位置づけ。
CLAUDE.md の不変条件 7 の列挙に足す (PR-A で 4 つ、PR-B で `escalation-state.json`)。

### 2-5b. 認証情報が変わったとき・旧形式のとき — 永続する状態ファイルの全表 (PR #281 の Codex P1・2 巡目)

**P1 の欠陥**: 質問台帳と offset が「どの bot・どの chat のものか」を持たなかった。認証情報を切り替える (source の `op` ↔ `file`・1Password で
トークンを差し替える・別の bot / chat にする) と、(1) **前の bot の offset を新しい bot の `getUpdates` に使う** (`update_id` の列は bot ごと。
新しい bot の update を飛ばす / 古いものを読み直す)、(2) **前の bot で出した open な質問が、新しい bot の返信・ボタンと照合されうる**
(`message_id` は chat ごとの連番で衝突する → 別の質問への答えとして Director に転送される)。**同じ族** = 永続する状態が「誰のものか」を持たず、
今の認証情報のものと取り違える。そこで永続する状態ファイルを**全部**挙げ、2 つの場合の扱いを 1 行ずつ決めた。

**識別子**: `bot_id` (token の `:` の前の数字) と `chat_hash` (chat_id の sha256 の先頭 12 hex) — **秘密でない**。`telegram-receiver.json` が既に持つものと
同じ値 (`Credentials.bot_id` / `.chat_hash`)。token・chat_id そのものは書かない。`lib_daemon_state` の検証は「両方あるか両方無いか」「bot_id が非負の整数」
「chat_hash が 12 hex」(違えばファイルが読めない扱い = 不変条件 1 のとおり黙って潰さない)。

**「束縛されている」の定義 (`lib_telegram.is_bound`)**: 状態の `bot_id` と `chat_hash` が**今の解決結果と両方一致**。識別子の無い (旧形式の) 状態は
**束縛の証拠が無い = 別物**として扱う (「無いから同じ」と読まない — 識別子導入前に別の bot で書かれた可能性を排除できない)。

| 状態ファイル | 書き手 | 認証情報が変わったとき (識別子が不一致) | 識別子が無い旧形式のとき (この PR の中で書かれたもの) | 通知 |
|---|---|---|---|---|
| `telegram-questions.json` の **open** な質問 | `ask` / `poll` / `run_cycle` | **`withdrawn`** (`closed_reason: identity_changed`・`unbutton: false`)。**ボタンを消す試みはしない** (別の bot の message_id では消せない)。照合の相手にも保留の理由にもしない (`classify_update` に identity を渡し、束縛されていない質問を台帳から除く)。`ask` の上限 (8 件) にも数えない | 同じ (別の bot のものかもしれない) → `withdrawn` | **Director に 1 回** (取り下げた qid の列。dispatcher が `notify_state_once`)。押された古いボタンは「不明な質問」で断られる (転送されない) |
| 同 **answered / expired / withdrawn** の質問 | 同 | **触らない** (閉じた質問は照合の相手にならない。`answered` で `forwarded=false` のものは台帳の中身だけで転送できるので、切り替えの前に受けた答えを失わない) | 触らない | 無し |
| `telegram-offset.json` | `poll` のみ | **捨てて最初から** (`offset=0`)。新しい bot の `getUpdates` に古い bot の `update_id` を使わない。以後は新しい identity を書く | 同じ (捨てる) | 無し (台帳が先に取り下げられているので、読み直した古い update は不明として断られるだけ) |
| `telegram-send.json` (レート・バックオフ) | `ask` と dispatcher の送信 (ロックの下) | **空から** (別の bot のバックオフ・429 の `retry_after` で新しい bot の送信を止めない)。以後は新しい identity を書く | 同じ (空から) | 無し |
| `telegram-receiver.json` | dispatcher のみ | 既存の `receiver_verdict` が `receiver_mismatch` で `ask` を断る (P1-2 / §1-1)。心拍が次のサイクルで新しい identity を書き直す | 識別子欠け = `receiver_unknown` で断る (既存) | 既存 (§1-1) |
| `escalation-state.json` (段階上げ・PR-B) | dispatcher のみ | 質問と結び付かない (時計だけ) ので束縛しない | 同左 | — |

**質問を参照する呼び出しは関所を通る** — 状態ファイルの表では呼び出しの漏れを追えない (P1・4 巡目)。§2-5c。

決めたこと:

- **offset は「最初から」(`0`) にした。「getUpdates の最新に合わせる」は選ばない**。新しい bot が未読の update を持っているなら、それは**新しい bot 宛の
  本物の答え**かもしれず、最新に飛ぶと黙って失う (P1 が直した「答えが黙って失われる」と同じ型)。最初から読んでも安全なのは、古い質問は上のとおり取り下げ済みで、
  新しい bot の update は台帳 (今の identity の質問だけ) と照合され、照合できないものは reject / ignore で捨てられるため。コストは Telegram が保持する
  未読 update (最大 24 時間・100 件/回) の読み直しだけ。
- **取り下げは poll の前・サイクルごとに安く行う** (`run_cycle` が `creds` を得た直後。`poll_once` の先頭でも同じ関数を呼ぶ — `ask` と `poll` が別のプロセスで
  動くので、どちらが先でも束縛されない質問が照合に使われない)。2 回目以降は取り下げる対象が無い (純粋関数・冪等) ので、通知は 1 回になる。
- **既知の限界**: 取り下げた qid は `run_cycle` の `summary['identity_changed']` でその 1 サイクルだけ出る。Director が不在のサイクルに当たると、
  `notify_state_once` は「記録せず見送る」が、次のサイクルには列が空なので**届かない**。取り下げ自体は台帳 (`closed_reason`) に残るので、見落としても答えを取り違えない
  (転送されないだけ)。通知を確実にするなら台帳側 (`closed_reason: identity_changed` かつ未通知) から導く案があるが、この PR では足さない (backlog)。
- **env の停止スイッチは付けない** (不変条件 5)。束縛の判定は `lib_telegram.is_bound` の 1 か所で、`ask` / `poll` / 送信が同じ答えを出す。
- 質問台帳・offset を**消してよい**(不変条件 7) ことは変わらない。識別子つきになっても、消せば「古いボタンが不明になる」だけ。

### 2-5c. 質問に結び付く Bot API 呼び出しは関所を通る (PR #281 の Codex P1・4 巡目)

**P1 の欠陥**: §2-5b は質問台帳と offset を bot_id + chat_hash に結び付けたが、**ボタンの後始末** (`editMessageReplyMarkup`) は今の認証情報の `chat_id` で、
台帳に残った前の bot / chat の質問の `message_id` を編集しにいった。`message_id` は chat ごとの連番なので、認証情報が変わった後に
(`withdrawn: identity_changed` でなく `expired` で後始末待ちだったもの等)、新しい chat の**同じ番号の無関係なメッセージ**を編集しうる。
**同じ族 (束縛の確認漏れ) の 2 回目**。§2-5b の「状態ファイルの全表」は、ファイルを挙げたが**ファイルを使う呼び出し**を挙げなかった。表で追うのをやめ、構造で押さえる。

| 質問を参照する呼び出し | 関所 | 束縛が合わないとき |
|---|---|---|
| 後始末: `editMessageReplyMarkup` (`_unbutton_pending`) | `question_api_call` | 通信せず `identity_mismatch` → その質問の `unbutton` を外す (**即「諦め」**。§2-3d の予算にも回数にも数えない) |
| 諦めの通知: `sendMessage` (`_give_up_forwarding`) | 同 | 送らない (別の chat に別の chat の質問の通知を出さない)。`forwarded: gave_up` の印は付く |
| `ask` の失敗時にボタンを消す: `editMessageReplyMarkup` (`cmd_ask`) | 同 | 同上 (今の identity で書いた直後の質問なので通常は一致する) |

**関所の仕様 (`lib_telegram.question_api_call(creds, entry, method, payload)`)**:
- `is_bound(entry, identity_of(creds))` でなければ**呼ばずに** `ApiResult(False, error='identity_mismatch')`。識別子の無い旧形式の質問も不一致 (§2-5b)。
- `chat_id` は `creds` から、`message_id` は `entry` から**関所が埋める** (呼び出し側が渡した値は上書き)。`editMessageReplyMarkup` / `editMessageText` / `deleteMessage` で
  `message_id` が無ければ `no_message_id` (通信しない)。
- 呼び出し側は `identity_mismatch` をその質問の後始末の対象から外す。押されても台帳の照合 (§2-5b) で断られるので、ボタンが残っても安全。
- 関所の外で `api_call` を直接呼べるのは、質問の `message_id` を参照しない 3 つだけ: `send_message` (質問を新しく**送る**)・`_receive_updates` (`getUpdates`)・
  `poll_once` の `answerCallbackQuery` (今の bot の getUpdates が返した `callback_query_id` への応答)。

**構造テスト** `tests/test_telegram_question_gate.py`: `lib_telegram.py` を ast で走査し、`api_call(...)` / `x.api_call(...)` の呼び出しが**関所の関数の中か、上の許可表の (関数, メソッド) にあるか**を確かめる。
メソッド名が文字列定数でない呼び出しも落とす (証明できない)。許可表の項目が消えても落ちる (表が腐らない)。陽性対照 (迂回の実際の形 4 つ) を置いてある。
**赤の実証**: ① 関所の照合を外す → 再現テスト 2 件が赤。② 関所を迂回する `api_call(..., 'deleteMessage', ...)` を足す → 構造テストが赤。③ 後始末を旧実装に戻す → 5 件赤。

### 2-5d. 状態ファイルの読み口: 無い / 読めない / 形が違う (PR #281 の Codex P2・5 巡目)

5 巡目の P2: `_receive_updates` が読めない offset を `{}` に置き換えた後 `offset_state['offset']` を引き、KeyError。ファイルが壊れている限り毎サイクル同じ所で落ち、押下が受信されなかった
(dispatcher のサイクル自体は `run_cycle` の外側の `try/except` で落ちないが、受信は永久に止まる)。**読めない値を `{}` / `None` に潰してから中身を読む**形が原因なので、`lib_telegram.py` の全読み口を表にした。
`load_json_store` は 3 つとも別の値で返す: 無い = `Unreadable(ENOENT)` (`is_missing`)・読めない / 形が違う (型違い・負の値・list・通常ファイルでない・権限) = それ以外の `Unreadable`。

| 読み口 | 無い | 読めない / 形が違う | 備考 |
|---|---|---|---|
| `read_questions` を読むだけの所 (`_unbutton_pending`・`_give_up_forwarding`・`forward_pending`・`cmd_verify`・`cmd_list`) | 何もしない / `not_found` | 何もしない (`unreadable` / `ledger_unreadable` を返す)。**中身は読まない** | 台帳は消して復旧 |
| `update_questions` | `{}` から始める | `LedgerUnreadable` を投げる (書かない) | 呼び出し側が `suppress` |
| `run_cycle` の台帳 | 何もしない | 転送せず offset も進めない (log) | |
| `read_offset` (`_receive_updates`) | 0 から (通常運用・黙る) | **0 から読み直す** (at-least-once。台帳の CAS と `forwarded` で重複は転送されない)。`offset_unreadable` に固定の語 (`EACCES` / `invalid` 等) を 1 回出し、dispatcher が Director に 1 回だけ通知。次の書き込みで正しい形に上書き | 本件。以前は `{}` にして KeyError |
| `read_offset` (`run_cycle` の `last_poll_at`) | `None` (間隔の判定を飛ばして poll) | `None` (同上。poll が offset を書き直す) | 読めない状態は poll を止めない |
| `read_receiver` / `receiver_verdict` | 断る | 断る (`ask_user.sh` は exit 4) | 安全側 |
| `write_receiver_state` | 作る / 何も作らない (未設定) | 書き直す (`exists` は「無い」でないので真) | 書き手は dispatcher だけ |
| `_update_send_state` | `{}` | `{}` で 1 回送り、書き直す。バックオフの記憶は失う (1 通だけ余計に送りうる。Telegram 側の 429 が再びバックオフを作る) | 意図。`telegram_available` は逆に「使えない」を返す (段階上げは送らない側に倒す) |
| `telegram_available` | True | False | |
| `load_telegram_config` (config) | 既定値 | 既定値 (= 未設定 = 何も送らない側) | |

読めない値を空と同じ形で読み進める所は上の `read_offset` 1 か所だけだった。残りは「読まない」か「書き直す」のどちらかを意図して選んでいる。
**テスト**: `tests/test_telegram_unreadable_offset_still_receives.py` (壊れ・型違い・負・list・通常ファイルでない・権限なしの 6 形で押下が受信され、offset が直り、重複は転送されない・送信状態の族)・
`tests/test_telegram_dispatcher_glue.py::test_an_unreadable_offset_is_reported_to_the_director_once`。

### 2-6. 受け取らないもの

`/start` などの bot コマンド、他 chat、返信でないテキスト、`edited_message`、画像等は、**転送もせず返信もしない**。
`allowed_updates` は `["callback_query","message"]` に絞る。offset は「受けたもの (無関係を含む) を処理した」ので進める
(進めないと同じ update を毎回受け続ける)。

## 3. Director の画面への届け方

### 3-1. 固定の形

dispatcher の通知と同じ経路 (`tmux_send(_director_name(), line)` = `lib_mux send`) で、**1 行**を送る。
他の dispatcher 通知 (`[needs_director] ...` / `[Rule 5] ...`) と同じく先頭に角括弧のタグを持つ:

```
[telegram-answer] q=q-1a2b3c4d task=20261004-watchdog-hard-idle-unknown/t017 choice="B: 差し戻す" index=1 task_state=needs_director
[telegram-answer] q=q-1a2b3c4d task=20261004-…/t017 text="やっぱり PR #281 から先に見て" task_state=needs_director
[telegram-answer] q=q-9c0d1e2f task=- choice="A: 続ける" index=0
```

- `choice` / `text` の値は **JSON 文字列として引用符でエスケープ** (`json.dumps(…, ensure_ascii=False)`)。
  ユーザーの返信文に `]` や改行や `q=` が入っても、1 行・1 欄にしか見えない。
- `task=` は質問に `--task` が付いていたときだけ `<slug>/<tid>`、なければ `-`。
- `task_state=` は転送の時点の card の status (読めなければ `unreadable`)。**転送の時点の観測で、判断の根拠ではない** (§3-2)。

### 3-2. Director の手順に足す規則 (`agents/director.md` §16 の表に行 + 新しい小節。PR-A)

1. **この行は「ユーザーの判断」の主張であって証拠ではない。** 画面に入る文字列は、ユーザーが手で打てるのと同じ経路
   (送信は mux の send) なので、行の見かけだけでは `ask_user.sh` が送ったものと区別できない。
   Director は行を受けたら必ず **`scripts/ask_user.sh verify --q <qid>`** を実行する。これは台帳を読んで
   `{"status":"answered","choice_index":1,"choice":"…","task":"…","execution_id":"…"}` を返す。
   **台帳が `answered` と言う内容だけをユーザーの判断として扱い、行に書いてある `choice` は使わない**
   (行と台帳が食い違えば台帳)。`verify` が `not_found` / `open` / `withdrawn` / `expired` を返したら、その行は無視して
   ユーザーに画面で確認する。
2. **実行の直前に状態を確かめ直す。** 質問から答えが来るまでに時間が経っている。
   `plan.sh status` で対象 task / PR がまだ同じ状態か、PR なら head が変わっていないか
   (**merge は `gh pr merge --match-head-commit <質問を出した時点の head>`**) を実行の直前に確認する。
   質問を出すときの本文に「何の状態に対する判断か」(PR 番号と head の短縮 sha、task の status) を書いておく (§6 の文面の型)。
3. **1 つの質問への答えは 1 回だけ実行する。** 同じ `q=` の行が 2 度来ても (再送・二重押し)、実行済みなら何もしない
   (verify の結果の `forwarded` ではなく、自分が実行したことの記録 = task card の Result / `plan.sh` の履歴で確認)。
4. 質問を出した Director 自身が、画面で先にユーザーから答えを得たときは、**実行の前に**
   `ask_user.sh cancel --q <qid> --by screen` を呼ぶ (§2-3 の withdrawn)。
5. 答えが `text` のときは、書かれた指示が元の質問の範囲にとどまるか確認する。範囲を超える (新しい依頼) なら、
   ユーザーに画面で確認する。**破壊的な操作 (`rm -rf`・force push・本番変更 等) は Telegram の答えだけを根拠にしない。**
   `~/.claude/rules/security.md` の「確認なしに実行禁止」は、Telegram のボタンを「確認」と数えない。

### 3-3. auto mode の分類器 (要望 6)

Director は `CREWVIA_DIRECTOR_PERMISSION_MODE` が未設定 (= 対話確認あり) が既定だが、auto mode で動かす構成がありうる。
auto mode の分類器が、Director の画面に**送り込まれた文**を根拠にした merge 等の実行を「ユーザーの承認」と認めるかは、
**未知**。過去の記録: レビュー Worker の merge が分類器に拒否された例が複数ある
(memory: `review-worker-merge-denied-by-classifier`・`gh-app-review-approve-blocked-by-classifier`)。
分類器は「ユーザーのメッセージ」とそうでない入力を区別するので、mux send で入った文が
ユーザーのターンとして扱われるかどうかは実機で観察するしかない。

- **認められなかった場合の手順** (Director の規則に書く): 分類器に拒否された (merge 等が `denied` で返る) ときは、
  **迂回しない** (別のコマンドで同じことをしない)。拒否の事実を Telegram の質問と同じ内容で**画面に出し**、
  その場でユーザーに確認を求める (`AskUserQuestion` で、画面にいるユーザーに。いなければ Telegram に
  「分類器が拒否したので画面で承認が要る」と `ask_user.sh ask` で 1 件送る)。
  Telegram の答えは「ユーザーの意思」の証拠として残り、画面の承認が**実行の許可**になる。
- **確かめ方** (本番確認で 1 回観察する。PR-A の本番確認の項目): 本物の Director の画面で、
  (1) 無害だが分類器が見る操作 (使い捨ての draft PR を close する) を、Telegram のボタンで承認した体で
  Director に実行させ、拒否されるかを見る。(2) 拒否された場合に上の手順が実際に動くかを見る。
  結果を `knowledge/director-escalation-telegram.md` の末尾 (§10) に追記する。**分類器が認めても、§3-2 の規則 1・2 は外さない**
  (分類器の挙動は変わりうる)。

## 4. Director が質問を送る入口 — `scripts/ask_user.sh`

薄い bash ラッパー (`parse_opts` の厳格引数の規則。`knowledge/plan-sh-strict-args.md`) が
`scripts/lib_telegram.py` の同名の動詞を呼ぶ。ロジックは Python 側 1 か所 (bash に JSON 組み立てを書かない)。

```
scripts/ask_user.sh ask --question "<本文>" --option "<ラベル>" [--option …] \
    [--task <slug>/<tid>] [--ttl-minutes 60] [--session-link <URL>]
scripts/ask_user.sh verify --q <qid>
scripts/ask_user.sh cancel --q <qid> --by <screen|other> [--note <文>]
scripts/ask_user.sh list          # open な質問 (確認用)
```

- `ask` の標準出力は `qid` 1 行だけ。終了コード: **0 = 送信成功**、**3 = Telegram 未設定** (Director は
  `AskUserQuestion` に戻る)、**4 = 送信できない** (ネットワーク・Bot API のエラー、または**受信側が無効 / 古い / 不明** = §1-1 の `receiver_disabled` / `receiver_stale` / `receiver_unknown`。stderr に固定コード。同じく戻る)、**5 = 未回答の質問が上限 (8 件)**、
  1 = 使い方の誤り (`pull` の規則と同じ。exit 2 は使わない — Worker が無限リトライする前例、
  memory: `pull-exit-2-is-idle-usage-errors-must-be-1`)。**本文・token・URL は終了時のエラーに出さない**
  (解析エラーに元の行が漏れる族、memory: `parser-error-leaks-source-line-family`。固定コード + 位置だけ)。
- 順序: ⓪ `telegram-receiver.json` を確かめ (§1-1。無効・古い・不明なら exit 4)、`sweep_questions()` を走らせる (§2-3b) → ① 質問を台帳に `open`・`message_id=null` で書く (ロック下) → ② `sendMessage` (inline_keyboard) →
  ③ 成功したら `message_id` を台帳に書く。② が失敗したら①のエントリを `withdrawn` にして exit 4。
  ③ が失敗 (台帳に書けない) したら、ボタンは出ているが台帳が答えを受けられない → `editMessageReplyMarkup` で消し、
  exit 4。**ボタンが出ているのに台帳に無い状態を作らない**。
- `--task` は付けると `verify` と転送行に出る。card を読んで `execution_id` を記録する (`lib_task_cards` の入口。読めなければ
  `execution_id` を空で記録して続行 — 質問は送れる。`task=` の表示だけが弱くなる)。
- **選択肢は 2〜4 個** (Telegram の 1 行に収まる。`AskUserQuestion` と同じ上限)。ラベルは 40 文字まで。
  末尾に自動で「💬 返信で答える」の注記を本文に足す (ボタンではない。返信文は §2-2 で受ける)。

### `AskUserQuestion` との併用 (ユーザー決定 2026-10-05: Telegram 設定済みなら使わない)

`AskUserQuestion` は画面を**占有する** (modal)。Telegram の答えは mux send で入力欄に入るので、
modal の最中に届いた文が取り込まれる保証がない。**ユーザー決定: Telegram が使えるときは `AskUserQuestion` を使わない。**
手順:

1. 質問と選択肢を**通常のテキストで会話に書く** (画面に居るユーザーはそれを読んで打ち込める)。
2. `ask_user.sh ask …` で Telegram に送り、`qid` を控えて**ターンを終える** (入力待ち)。
3. 画面で答えが打たれたら (ユーザーが席にいた) → `ask_user.sh cancel --q <qid> --by screen` → 実行。
   Telegram の行が来たら → §3-2 の手順 (verify → 状態の確かめ直し → 実行)。
4. `ask` が exit 3 (未設定) / exit 4 (送信失敗・受信側が無効 = §1-1) のときだけ、従来どおり `AskUserQuestion` を使う。

`AskUserQuestion` と Telegram を**同時に**出す案は採らない (modal が答えを取り込まない可能性と、
2 つの答えが食い違ったときの優先の規則が要る)。modal の挙動の観察は PR-A の本番確認に残すが、結論は決定を変えない。

## 5. 段階上げ (PR-B)

### 5-1. 「判断待ち」の定義

| 候補 | 含める? | 理由 |
|---|---|---|
| `needs_director` | **含める** | 本件の事故そのもの。`WAITS_ON_DIRECTOR_STATUSES` |
| `needs_human_review` (`verify-result needs_human_review`) | **含める** | `plan.sh` の表示は `needs_director` と同じ `blocked` / `[要判断]` (plan.sh:943-944)。人間の判断を待つ点が同じ |
| `ask_user.sh` で出して未回答の質問 (`open`) | **含めない** | 質問はすでに Telegram で**ユーザーの手元に届いている**。段階上げの目的 (ユーザーに届かない) が既に満たされている。期限 (`expires_at`) が来たら `expired` になり、Director が気付く (§2-3 の転送行ではなく、`ask_user.sh list` + dispatcher の 1 回通知で十分。PR-A のスコープ外の拡張として §9 に記録) |
| `failed` の依存 (held) | **含めない** | `[held]` の経路が既にある (`plan.sh release-dep`)。保留の通知を足す話は別 (今回の事故と違う入口。§8 に残す) |

**語彙の置き場**: `scripts/lib_task_status.py` に `AWAITING_DECISION_STATUSES = WAITS_ON_DIRECTOR_STATUSES | {'needs_human_review'}` を
**1 つだけ**足す。**`WAITS_ON_DIRECTOR_STATUSES` を広げない** — あれは「assignment を撤去する / 孤児の枠を作る」の集合として
使われ (`dispatcher.sh:689` / `plan.sh:869-872`)、`needs_human_review` は assignment を**撤去しない**
(plan.sh:978-979)。広げると Kai-codex の枠の判定が変わる。`tests/test_task_status_single_definition.py` が
AST で「status の集合を別の場所に書かない」を固定しているので、新しい集合もそこに載せる。

### 5-2. 「後続」は条件ではなく文面の材料 (ユーザー決定 2026-10-05 で変更)

初版の設計は「判断待ちの card が**後続を止めているとき**だけ段階上げする」だった (後続の無い最後の task は対象外)。
**ユーザー決定で変更**: **後続の無い最後の task の判断待ちも段階上げする**。「後続を止めているか」は
段階上げの**条件ではなくなった**。

- 判断待ち (§5-1 の `AWAITING_DECISION_STATUSES`) の card は、**後続の有無にかかわらず**時計が回る。
  理由: 最後の task が `needs_director` のまま放置されると mission が完了せず `全ミッション完了` も出ない
  (誰にも気付かれない停止)。後続の有無で通知を切ると、止まっている事実の一部しか拾えない。
- 後続の数 (`dependents`) は**文面の材料としてだけ**使う (§6 の「後続 N 件が待機中」。0 件なら行ごと省く)。
  数え方は初版のまま `lib_dep_rules` を通す (不変条件 3): mission の中に `status == pending` で
  `unmet_dependencies(D.blocked_by, done_ids, task_statuses, D.released_deps)` に `W.id` を含む card `D`。
  dispatcher は既に `dependency_gate()` (`dispatcher.sh:892`) で同じ入力を作る。
  **依存の定義を持たない** (`blocked_dependents()` は `dependency_gate` の結果から `waiting_id in verdict.unmet` を集めるだけ)。
- 数えられない (破損カード・`Unreadable`) ときは `dependents = None` として**文面から行を省くだけ**で、段階上げは止めない
  (card 自体が読めている限り判断待ちは判定できる)。card 自体が読めないときは §5-4 の「観測できない」。
- 副作用の確認: 「後続を止めているか」の条件を外したので、`decide()` の入力から `dependents` を**判定に使う欄が無くなる**
  (文面用に渡すだけ。判定の表に出てこない)。判定に使う入力が減る分、表は小さくなる。

### 5-3. 段階と時計

| 段階 | 既定の経過 | 何をする | 送り先 |
|---|---|---|---|
| 0 | 0 分 | 既存の `[needs_director]` 通知 (状態が変わるまで 1 回。`notify_state_once`) | Director |
| 1 | 5 分 | **Director に再通知**: 「M 分経過」 | Director (mux send) |
| 2 | 10 分 | **Telegram でユーザーに通知**: §6 の文面 | ユーザー (Telegram) |

**段階 1・段階 2 とも 1 回だけ** (ユーザー決定。3 回目以降も、同じ段階の繰り返しも無い)。

config (`config/crewvia.yaml` の `escalation:` ブロック。コメント付き。`daemons:` ブロックの書き方に揃える):

```yaml
escalation:
  director_after_seconds: 300     # 段階 1。0 以下 = 段階 1 を使わない
  telegram_after_seconds: 600     # 段階 2。0 以下 = 段階 2 を使わない
```

**cfg の意味 (t002 P2-2)** — 2 つの値は独立に「0 以下 = その段階を使わない」で、**使う段階どうしの順序だけ**を検証する:

| `director_after` | `telegram_after` | 意味 | 検証 |
|---|---|---|---|
| > 0 | > 0 | 段階 1 → 段階 2 | `telegram_after > director_after` でなければ**拒否** (WARNING を 1 回 + 既定値 300 / 600 に倒す) |
| ≤ 0 | > 0 | 段階 1 は無い。経過が `telegram_after` を超えたら段階 2 | 順序の検証は**かけない** (比べる相手が無い) |
| > 0 | ≤ 0 | 段階 1 だけ | — |
| ≤ 0 | ≤ 0 | 何も鳴らさない (段階 0 の既存通知だけ) | — |

数値でない値・NaN・inf も WARNING + 既定値 (`_parse_drift_interval` と同じ型)。環境変数での上書き
(`CREWVIA_ESCALATION_DIRECTOR_AFTER_SECONDS` / `CREWVIA_ESCALATION_TELEGRAM_AFTER_SECONDS`) は他のしきい値と同じ型で付ける
(config より優先)。不変条件 5 との関係: dispatcher だけが読む**しきい値**で、plan.sh と答えが割れる共有規則ではない。

**「飛ばさない」規則の範囲 (P2-2)**: 段階 1 が**使える**とき (`director_after > 0` かつ Director が在席かつ段階 1 の送信が
失敗していない) だけ、段階 2 の前に段階 1 を必ず経る。段階 1 が使えない (cfg で無効・Director 不在・送信失敗) ときは、
段階 2 は段階 1 を待たない (§5-4 の `decide()` の規則 R5/R6)。

**時計の起点**: card には判断待ちに入った時刻が**無い** (`needs_director_reason` のみ。`lib_state_store.py:526`)。
card に `needs_director_at` を足す案は、serialization の golden (`tests/fixtures/state_store_serialization_golden.json`)・
Taskvia の契約・旧コードとの互換 (`rollback-compat-for-new-flag-must-merge-before-the-cutover-pr`) を全部動かす。
**採らない**。代わりに**段階上げの台帳に「初めて見た時刻」を持つ** (§5-4)。
台帳を消すと時計が最初からになる = 通知が**遅れる**側に倒れる (早まって鳴らさない)。

**dispatcher が止まっていた間の時間**: 時計は壁時計の差。dispatcher が 1 時間止まって戻ると、最初に見た時刻が
1 時間前なので段階 1・2 が同じサイクルで両方発火しうる。**1 サイクルで 1 段階だけ**進める
(段階 1 を送ったら、その同じサイクルでは段階 2 を評価しない)。次のサイクルで段階 2。

**観測できなかった間 (t002 P3-2)**: `first_seen` は壁時計の差なので、「観測できない間は時計を止める」は実現できない
(台帳を `keep` しても経過は伸びる)。**書き直した規則**: 観測できない間は**鳴らさない** (`none` / `keep`)。
観測が戻った最初のサイクルで、`first_seen` からの経過どおりに評価する (戻った直後に段階 1・2 が連続で出うるが、
1 サイクル 1 段階なので 2 サイクルに分かれる)。「遅れる側」であって「欠落」ではない。

### 5-4. dedup と台帳 — **純粋関数 + 網羅テスト**から設計する

前のミッションで通知の状態の扱いが Codex に 4 回指摘された (`knowledge/watchdog-idle-judgment.md` §11-13)。
今回は「状態を書き換える手続き」ではなく、**判定を純粋関数にし、台帳の更新を戻り値で表す**。

台帳 `registry/daemons/escalation-state.json`:

```json
{"<slug>/<tid>": {"execution_id": "ex-…", "first_seen": 1759650000.0,
                  "stage_sent": 0 | 1 | 2, "stage_sent_at": 1759650300.0,
                  "stage1_failed_at": null | 1759650400.0}}
```

- **キーは `<slug>/<tid>`、dedup は `execution_id` 単位。** 同じ task が pending に戻って再び走り、別の試行
  (`ex-…` が変わる) で再び判断待ちになったら**新しい事象**として最初から (`first_seen` を取り直す)。
  `execution_id` が card に無い (旧形式) ときは、`needs_director_reason` の fp (`notified-state` の `fingerprint`) で代用する。
- **解けたら台帳を消す**: その card が判断待ちでなくなった (status が `AWAITING_DECISION_STATUSES` を離れた) とき。
  消すのは**観測できた mission のそれだけ** (`prune_told` と同じ `observed_missions` の規則。破損カードのある mission は触らない)。
  (初版の「後続が無くなったとき消す」は、後続の条件を外したので無くなった。)

純粋関数 (`scripts/lib_escalation.py`。副作用なし・I/O なし・時計は引数):

```python
def decide(card_view, ledger_entry, now, cfg, director_live, telegram_available) -> Decision
# card_view: (slug, tid, status, execution_id, observable: bool)
# ledger_entry: None | {execution_id, first_seen, stage_sent, stage_sent_at, stage1_failed_at}
# cfg: (director_after, telegram_after)  — ≤0 = その段階を使わない。順序の検証は読み込み側 (§5-3)
# director_live: mux に Director が居るか (mux list の結果。呼び出し側が遅延評価で 1 回だけ)
# telegram_available: 認証情報が解決でき、受信側が enabled (§1-1) で、送信のバックオフ中でないか
# Decision: (action, ledger_update)
#   action: none | director_renotice | telegram_notice
#   ledger_update: keep | set(entry) | delete

def apply_failure(entry, action, now) -> entry
# 送信が失敗したときの台帳の更新 (純粋関数)。director_renotice の失敗 → stage1_failed_at を (未設定なら) now に。
# telegram_notice の失敗 → 変更なし (Telegram 側のバックオフ §7 が再試行の間隔を持つ)
```

**判定の規則** (`elapsed = now - first_seen`、`d` = `director_after`、`t` = `telegram_after`。上から順に最初に当たった 1 つ):

| # | 条件 | → action / ledger |
|---|---|---|
| R1 | status が判断待ちでない | 台帳あり: `none` / **delete**。なし: `none` / keep |
| R2 | 判断待ち、`observable == False` | `none` / keep (§5-3「観測できなかった間」) |
| R3 | 判断待ち、台帳なし | `none` / **set(first_seen=now, stage_sent=0, stage1_failed_at=None)** |
| R4 | 台帳あり、`execution_id` が違う (新しい試行) | `none` / **set(first_seen=now, stage_sent=0, …)** |
| R4b | 台帳あり、`first_seen > now` (時計が戻った・壊れた記録) | `none` / **set(first_seen=now)** (他の欄は保つ) |
| R5 | `stage_sent == 2` | `none` / keep (**3 回目以降は鳴らさない**。段階 2 は 1 試行に 1 回) |
| R6 | **段階 2 が到達可能** かつ `t > 0` かつ `elapsed ≥ t` かつ **`telegram_available`** — ここで「到達可能」= `stage_sent ≥ 1` **または** `d ≤ 0` **または** `not director_live` **または** `stage1_failed_at != None` | `telegram_notice` / set(stage 2)。**`telegram_available == False` のときは R6 に当たらなかった扱い**で、`none` で終わらせず**次の R7 以降へ落ちる** (t014 P2-2。見送りは R8 の `none` / keep。使えるようになったら次のサイクルで R6 が当たる) |
| R7 | `d > 0` かつ `stage_sent == 0` かつ `elapsed ≥ d` かつ `director_live` | `director_renotice` / set(stage 1)。(R6 に当たらなかった = 段階 2 に進める条件が揃っていない・`elapsed < t`・または Telegram が使えない) |
| R8 | 上のどれでもない (経過が足りない・段階 1 を送るべき Director が居ない・など) | `none` / keep |

R6 と R7 の関係が P1-1 の直し方 (t002): **Director 不在 (`director_live == False`) の間は R6 の「到達可能」が成り立つので、
`elapsed ≥ t` かつ Telegram が使えれば段階 2 が出る** (段階 1 を待って永久に止まらない)。Director が戻っても `stage_sent == 2` なので段階 1 は出ない
(「1 回だけ」・Director が不在だった間に段階 2 が既に出ている)。戻った Director に何が届くかは条件付き:
既存の `[needs_director]` (段階 0) が Director の離席**後**に出ていた (= Director 不在で記録されなかった) ときは、
戻った時点で届く。段階 0 が離席**前**に届いていたときは、戻っても Director には何も来ない (ユーザーには段階 2 が届いているので害は無い)。

**Telegram 未設定 (`telegram_available == False`) でも段階 1 は出る (t014 P2-2)**: R6 は Telegram が使えないと当たらず R7 に落ちるので、
`d > 0`・Director 在席・`stage_sent == 0`・`elapsed ≥ d` なら、`elapsed ≥ t` を過ぎていても (段階 1 の送信が失敗し続けた後に
`stage1_failed_at` が付いていても) `director_renotice` が出る。段階 1 は成功すれば `stage_sent == 1` になり、
Telegram が使えるようになった次のサイクルで R6 が当たる。段階 1 の送信が**失敗**したとき (在席なのに `tmux_send` が False)
は、`apply_failure` が `stage1_failed_at` を書き、次のサイクルから R6 の「到達可能」が成り立つ。
段階 1 の再試行は `elapsed < t` の間だけ意味があり、間隔は既存の `should_notify(<key>#<execution_id>)` スロットル (`NOTIFY_TTL`) に任せる
(5 秒ごとに失敗を叩かない)。

**台帳に書くのは「送れた後」だけ**: `director_renotice` / `telegram_notice` の実行が成功したときだけ `ledger_update` を適用する。
失敗したら適用せず (`telegram_notice` は何も書かず、`director_renotice` は `apply_failure`)、同じ段階をもう一度試す。
`notify_state_once` と同じ向き (「送れなかった通知は記録しない — 戻ったらすぐ送る」)。

**テストの母集団 (全直積)** — `tests/test_escalation_decide.py` は次の軸の**全直積**を `decide()` に流す (純粋関数なので
全部で数千通りでも一瞬):

| 軸 | 値 |
|---|---|
| status | 判断待ちの 2 種 (`needs_director` / `needs_human_review`)・判断待ちでない |
| observable | True / False |
| 台帳 | なし / 同じ試行 / 別の試行 / `first_seen` が未来 |
| `stage_sent` (台帳あり) | 0 / 1 / 2 |
| `stage1_failed_at` | None / あり |
| 経過 | `< min(d,t の正のもの)` / `d ≤ elapsed < t` / `elapsed ≥ t` (と、`d` / `t` ちょうどの境界) |
| cfg | (d>0, t>0) / (d≤0, t>0) / (d>0, t≤0) / (d≤0, t≤0) — さらに d・t が 0・負・極端に小さい値 |
| director_live | True / False |
| telegram_available | True / False |

各行について (1) 上の規則表を**テスト内で再実装しない** (期待値は表の行そのものを手で書いた oracle のテーブル。
`regression-test-must-prove-red`)、(2) 次の**不変条件**を全行に対して検査する:

- `stage_sent` は単調増加 (`ledger_update` で下がらない。`set(new)` は R3/R4 の新しい事象のときだけ 0 に戻る)
- 同じ `(execution_id, stage)` に対して `director_renotice` / `telegram_notice` が 2 回出ない (`decide` の戻り値を適用して畳み込み、列の中で数える)
- 判断待ちでなくなったら必ず `delete` (台帳なしなら keep)
- `telegram_notice` は `t > 0` かつ `telegram_available` かつ `elapsed ≥ t` のときだけ。`director_renotice` は `d > 0` かつ `director_live` かつ `stage_sent == 0` のときだけ
- (前提: 以下の 2 つは **「判断待ち・`observable`・同じ試行の台帳あり・`first_seen ≤ now`」= R1〜R4b を通過した行**だけに課す。
  R1 (判断待ちでない)・R2 (観測できない)・R3/R4 (台帳なし / 別の試行)・R4b (時計が戻った) の行では `none` / `set` / `delete` が
  正しいので成り立たない — 全直積に前提を付けずに書くとそのまま赤になる。t014 P3-1)
- **Director 不在で `t > 0` かつ `elapsed ≥ t` かつ `telegram_available` なら、`stage_sent < 2` の限り必ず `telegram_notice`** (P1-1 の回帰。上の前提つき)
- **`telegram_available == False`・`d > 0`・`director_live`・`stage_sent == 0`・`elapsed ≥ d` なら必ず `director_renotice`** (t014 P2-2 の回帰。上の前提つき。`stage1_failed_at` の有無・`elapsed` が `t` の前か後かを問わない)
- 1 回の `decide` は高々 1 つの action (1 サイクル 1 段階)

さらに**ランダムな入力の列** (長さ 1〜12。`backlog-premise-needs-simulation` の「網羅テストが長さ 9〜10 の並びを見ていなかった」
への対策) を `decide` → 成功/失敗を確率で決めて `apply_failure` or `ledger_update` を適用、を繰り返す畳み込みで流し、
Director の在・不在と送信失敗 (段階 1・段階 2 とも) を列の中で混ぜて、上の不変条件を検査する。

**台帳が `Unreadable`**: 純粋関数を呼ばず、段階上げ全体を**見送る** (`WARNING: escalation-state` を 1 回/10 分)。
`notify_state_once` の「台帳が読めなければ再送側に倒す」とは**向きが違う** — 再送側に倒すと、台帳が壊れている間
毎サイクル Telegram に送り続ける。段階上げは**鳴らさない側**に倒す (fail の向きは判定ごとに決まる。memory:
`fail-direction-is-per-judgment`)。段階 0 (既存の 1 回通知) は影響を受けない。

走査は `dispatcher.sh` の `needs_director` 検知ブロックの隣 (`all_tasks` を使い回す。別の走査をしない —
`handoff` ブロックと同じ t023 の規則) に 1 か所足し、`prune` も同じスナップショットで行う。

## 6. 送る文面の型

Telegram (段階 2) と、Director 宛の再通知 (段階 1) は**同じ項目**を同じ順に並べる (見た目の差は先頭のタグだけ)。

```
🛑 crewvia: Director の判断待ちが続いています
mission: 20261004-watchdog-hard-idle-unknown
task: t017 (needs_director) — 止まって 10 分
後続 2 件が待機中: t006, t007        ← 後続が 0 件・数えられないときは行ごと省く (§5-2)
理由: <needs_director_reason の 1 行目 (200 文字まで)>
👉 Director の画面で `plan.sh status` を見て対処してください
セッション: <session_link>        ← 設定されているときだけ
```

- 質問 (`ask_user.sh ask`) の文面:
  ```
  ❓ crewvia Director からの質問
  <本文>
  対象: <slug>/<tid>[ (PR #<n> @ <head 7 桁>)]   ← --task / 本文に PR を書いたとき
  [A: …] [B: …]   (ボタン)
  💬 込み入った指示はこのメッセージに返信してください
  セッション: <session_link>
  期限: <HH:MM> まで有効
  ```
- **セッションのリンク**: crewvia は Director のセッション URL を知らない。`/remote-control` が出す URL を
  Director が `--session-link` に渡す (質問)。段階上げ (dispatcher が送る) には env
  `CREWVIA_DIRECTOR_SESSION_URL` か config `telegram.session_link` を使う。**無ければ行ごと省く** (空欄を送らない)。
- **長さ**: Telegram の本文上限は 4096 文字。**2000 文字で切る** (UTF-8 の 4096 UTF-16 コード単位に余裕を持たせる)。
  `reason` は 200 文字・質問本文は 1200 文字までとし、超えたら `…`。
- **エスケープ**: `parse_mode` を**使わない** (プレーンテキスト)。Markdown / HTML の特殊文字の事故を避ける
  (task の reason は Worker が書いた任意の文字列)。
- **漏らさないもの**: token・URL・env・card の本文 (Result) は文面に入れない。reason の 1 行目だけ。
  既存の needs_director 通知が `全文: <card のパス>` を出している (Director 宛) のは、パスをユーザーの
  Telegram には出さない (WSL のパスは無意味)。

## 7. 落ちている / 未設定 / レート制限

### fail の向き (判定ごと)

| 判定 | 向き | 理由 |
|---|---|---|
| Telegram 未設定 (認証情報が解決できない) | **何もしない**。ファイルも作らず、ログも出さない。ただし**受信側の状態ファイルが既にあれば `enabled: false` に書き換える** (§1-3・§1-1) | 公開前提。Taskvia 非依存と同じ型。送信側 (`ask_user.sh`) との答えを割らない |
| **dispatcher が respawn で認証情報を失った** (§1-1) | `telegram-receiver.json` が `enabled: false` に変わり、`ask` は断り (exit 4)、Director に 1 通 | 黙って止まらない。割れた状態でボタンを出さない |
| `sendMessage` が失敗 (ネットワーク・HTTP エラー・`ok:false`) | **dispatcher のサイクルを止めない**。その通知は**記録せず**、バックオフ後に再試行 | 送れないことで割り当てを止めない。`notify_state_once` と同じ「送れなかった通知は記録しない」 |
| Telegram が長く落ちている | バックオフは指数 (30 秒 → 1 分 → 2 分 … 上限 10 分。`telegram-send.json`)。**ログは 10 分に 1 回** `WARNING: telegram unreachable (<種別>)`。段階 2 の `decide()` には `telegram_available = False` で渡る | 5 秒ごとに失敗を叩かない・ログを埋めない |
| 受信 (`getUpdates`) が失敗 | 何も受けなかった扱い。offset は進めない。サイクルは続行 | 次回に同じ update を受けるだけ (at-least-once) |
| `telegram-questions.json` が `Unreadable` | 転送しない・offset を進めない (§2-3) | 観測できなかったことを「答え無し」に倒さない |
| `telegram-receiver.json` が `Unreadable` / 古い (心拍が 3 × `RECEIVER_HEARTBEAT_SECONDS` = 90 秒を超える) / 無い / `bot_id`・`chat_hash` が `ask` の解決結果と違う | `ask` は**断る** (§1-1。`receiver_stale` / `receiver_unknown` / `receiver_mismatch`) | 観測できなかったことを「有効」に倒さない |
| `escalation-state.json` が `Unreadable` | **段階上げを見送る** (§5-4) | 再送側に倒すと壊れている間ずっと送り続ける |
| Director 不在 (mux に `-director` が無い) | 転送・段階 1 は**見送って記録しない** (戻ったらすぐ送る)。**段階 2 (Telegram) は Director の有無と無関係に送る** — `decide()` の `director_live = False` が R6 の「到達可能」を満たす (§5-4。t002 P1-1 で整合させた) | ユーザーに届けるのが段階 2 の目的で、Director が居ないほどユーザーに知らせる価値がある |
| mux が無い (インラインモード) | dispatcher が動かないので受信も段階上げも動かない。`ask` は `telegram-receiver.json` の心拍が無い・古いので**断る** (`receiver_unknown` / `receiver_stale`)。「dispatcher の生存確認」を別に持たない (心拍がそれを兼ねる) | 押しても誰にも届かないボタンを出さない |

### レート制限

- 送信は**全体で 1 秒に 1 通**まで (Telegram の個人チャットの目安は 1 通/秒、1 分に 20 通)。`telegram-send.json` の `last_sent_at` で数える
  (書き手は `ask_user.sh` と dispatcher の段階 2 送信の 2 者 — どちらもロックの下で読み直す。§2-5)。
- 段階上げは 1 task ごとに最大 1 通 (段階 2 は 1 試行につき 1 回)。**1 サイクルで送る段階上げの通知は最大 3 通**
  (多数の task が同時に 10 分を超えたとき。残りは次のサイクル)。
- 質問 (`ask_user.sh ask`) は「未回答の質問がある」(§2-3b の定義) が 8 件を超えたら拒否 (exit 5)。
- Bot API が 429 (`retry_after`) を返したら、その秒数だけ送信を止める (§7 のバックオフと同じ状態に載せる)。

## 8. このミッションでやらないこと (記録)

- Worker のツール実行の承認 (pre-tool-use / Taskvia)。
- `failed` + held の依存の通知を Telegram に載せること (別の入口。`[held]` ログが既にある)。
- `ask_user.sh` の `open` 質問が期限切れになったときの Director への通知 (期限切れはボタンが消えるだけ。§2-3b)。
- 複数ユーザー / 複数 chat。chat_id は 1 つだけ。
- Webhook (`setWebhook`)。公開サーバーが要る (公開前提で誰でも立てられる、に反する)。
- 段階 1・段階 2 の繰り返し通知 (ユーザー決定: 1 回だけ)。
- (初版にあった「後続が無い最後の task の判断待ちを段階上げしない」は、ユーザー決定で**やる**に変えた。§5-2)

## 9. 実装の分割

### PR-A: Telegram の経路

- `scripts/lib_telegram.py` (新): Bot API のクライアント (urllib、token は引数にもログにも出さない、timeout 付き、
  失敗は例外ではなく戻り値)・**`resolve_credentials()` (認証情報の解決の唯一の入口。§1-1・§10)**・
  callback_data の生成と照合 (純粋関数)・`sweep_questions()` (純粋関数。§2-3b)・質問台帳の読み書き (`lib_daemon_state` 経由)・
  `poll` / `send` / `ask` / `verify` / `cancel` / `list` の動詞。
- `scripts/ask_user.sh` (新): 厳格引数のラッパー。`ask` は送る前に `telegram-receiver.json` を確かめて断る (§1-1)。
- `scripts/lib_daemon_state.py`: `telegram_questions_problem()` / `telegram_offset_problem()` / `telegram_send_problem()` /
  `telegram_receiver_problem()` を追加 (書き手も通す)。
- `scripts/dispatcher.sh`: サイクルに受信を 1 か所足す (§1 の条件。サブプロセス・間引き・未設定なら無し・未回答の質問が無ければ通信無し)。
  転送の再送 (`forwarded=false`) もここ。**`telegram-receiver.json` の心拍の書き込みと、`enabled: true → false` の
  Director への 1 回通知 (`notify_state_once`、key `telegram_receiver_disabled`)** もここ。
- `agents/director.md`: §16 の表に `[telegram-answer]` の行・§3-2 の規則・§3-3 の分類器の手順・§4 の手順
  (**ユーザー決定: Telegram が使えるときは `AskUserQuestion` を使わず、会話に書いて `ask_user.sh ask` で送りターンを終える**)。
- `config/crewvia.yaml`: `telegram:` ブロック (`poll_interval_seconds`・`session_link`・`question_ttl_minutes`・
  `credentials: {source: op|file, token_ref, chat_id_ref, file}` — **参照とパスだけで秘密そのものは書かない**。§1-1・§10)。
  CLAUDE.md の環境変数表: **`CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID` は載せない** (採った案では env を読まない。t014 P3-4)。
  載せるのは `CREWVIA_DIRECTOR_SESSION_URL` だけ。認証情報は表ではなく「Telegram 連携」の節に config の `telegram.credentials` として
  1 行 (取り出し方は `op` / `file` のどちらか 1 つ・env は読まない・詳細は本文書 §1-1)。不変条件 7 に新しい台帳 (5 つのうち 4 つ)。
  (将来 §10 の選択を変えて env を使う案にしたときは、その時点で表に足す。)
- `scripts/dispatcher.sh`: **起動時 (ループの前) に認証情報を 1 回取り出す** (`lib_telegram.py resolve` を呼び、結果をシェル変数に。
  `export` しない・python には bash の前置代入 (`env` を付けない) で `_CREWVIA_TG_RESOLVED_*` をその呼び出しにだけ渡す。§1-1)。取り出せなければ `reason` を心拍に書いて `enabled: false`。
  `lib_daemon_watch.py` の `_SPAWN_ENV_VARS` には**足さない** (コマンド文に秘密を載せない。§1-1)。
- `.gitignore`: `registry/daemons/telegram-*.json` と `telegram-poll.lock`。
- テスト (**偽の Bot API サーバー** = `http.server` をテスト内で立て、**lib の引数 `api_base`** で向ける。
  env の `TEST` 専用スイッチを本番コードに足さない): callback_data の照合 (§2-2 の 5 条件を 1 つずつ欠かした表)・二重押し・
  古いボタン・withdrawn 後の押下・chat_id 違い・message_id 違い・nonce 違い・返信でないメッセージ・offset の at-least-once・
  **`message_id == null` の open への押下で offset が進まない**・台帳 `Unreadable`・未設定で 1 バイトも書かない
  (ファイル一覧の前後比較。**ただし受信側の状態ファイルが既にあれば `enabled:false` に書き換わる**)・token が argv / ログ / 例外文に出ない
  (token を偽の値で入れ、全出力を grep)・サブプロセスの timeout (応答しないサーバー)・
  **`sweep_questions()` の表 (期限切れ・`message_id == null` の残骸・7 日の削除・「未回答の質問がある」の定義)**・
  **`telegram-send.json` の 2 者同時書き込み (ロックの下で `last_sent_at` を数え損ねない)**・
  **`telegram-receiver.json` が `disabled` / 古い / 無い / 壊れているとき `ask` が断る (exit 4 と固定コード)**・
  **認証情報が無い dispatcher で `enabled:false` と Director への通知 1 通 (同じ状態で 2 通目が出ない)**・
  **心拍の間隔 (定数 30 秒) と stale (90 秒) の境界 (「間隔 + 1 サイクル」で stale にならない・「90 秒ちょうど / +1 秒」・`poll_interval` を変えても動かない。t014 P2-1)**・
  **`receiver_mismatch` (`bot_id` 違い・`chat_hash` 違いで `ask` が exit 4。ファイルに token・chat_id が出ない)**・
  **`resolve_credentials()` の優先順位 (`source=op` のとき `file` と env の `CREWVIA_TG_*` を読まない・逆も。env だけ設定された Director のシェルで `no_credentials` になる。`ask` は `_CREWVIA_TG_RESOLVED_*` が環境にあっても読まない・`carried=True` の動詞だけが読む)**・
  **dispatcher の 1 サイクルの間、全プロセスの `/proc/*/cmdline` に偽 token が出ない (サイクルを回しながら `/proc` を繰り返し走査し、偽 token と偽 chat_id の文字列を探す。陽性対照として `env VAR=<偽 token> sleep` の形を 1 回走らせ、検出器が拾うことを先に確かめる。t016)**・
  **`file` の権限 (0600 以外・所有者違い・シンボリックリンクで中身を開かず `credential_file_permissions`)**・
  **`op` の呼び出し回数 (偽の `op` スクリプトで dispatcher の複数サイクルを回して 1 回だけ。python のサイクルごとに呼ばない。t014 P2-3)**・
  `op` が PATH に無い / 失敗 / 空 → `credential_command_failed` (値・参照・stderr の中身が receiver.json とログに出ない)。
  既存の `tests/CLAUDE.md` の隔離規則 (`env -u AGENT_NAME`、`CREWVIA_MUX_TEST_ISOLATION`) に従う。
- 本番確認: 本物の bot・本物の Director で (1) `ask` → ボタン → `[telegram-answer]` が画面に届く、
  (2) §3-3 の分類器の観察、(3) §4 の modal の観察、(4) **dispatcher を `lib_daemon_watch.py restart` (または watchdog の respawn) した後も
  ボタンが受信される、または `ask` が `receiver_disabled` で断られ Director に通知が 1 通来る** (§1-1)。
  **dispatcher の restart が必要** (`merged-daemon-code-is-inert-until-restart`。`scripts/sync-main-checkout.sh`)。
  (5) **ヘッドレスな dispatcher から 1Password が通るか** — **ユーザー立ち会いで**確かめる (§10)。PATH に `op` が無い・ロック中・
  プロンプトが要る場合に、`receiver.json` が `enabled: false` / `reason: credential_command_failed` を出し、Director に通知が 1 通来ることを見る。
  通らなければ `source: file` に切り替えて (4) をやり直す。**`~/.config` 配下を読まず、1Password の中身 (token・chat_id) を
  取り出して表示しない** — 確認は「`reason: ok` と `bot_id` が出たか」だけで行う。

### PR-B: 段階上げ (PR-A の後)

- `scripts/lib_task_status.py`: `AWAITING_DECISION_STATUSES` (§5-1)。`tests/test_task_status_single_definition.py` に載せる。
- `scripts/lib_escalation.py` (新): `decide()`・`apply_failure()` (純粋関数)・`blocked_dependents()` (文面用)・
  台帳の shape (`lib_daemon_state` に `escalation_state_problem()`)・config の読み込みと検証 (§5-3 の cfg 表)。
- `scripts/dispatcher.sh`: `needs_director` ブロックの隣に段階上げを 1 か所 (`all_tasks` を使い回す)。段階 1 は既存の
  `tmux_send`、段階 2 は PR-A の送信 (認証情報が無い・受信側が無効なら `telegram_available = False`)。
  **段階 1 は Telegram 未設定でも動く** — これだけで今回の事故 (Director への再通知が無い) は塞がる。
- `config/crewvia.yaml`: `escalation:` ブロック。
- `agents/director.md`: §16 の表に段階 1 の再通知の行。
- テスト: §5-4 の**全直積** + 手書きの oracle 表 + 不変条件 + ランダムな列 (長さ 1〜12)・
  cfg の検証 (逆順・0・負・NaN → WARNING + 既定値)・dispatcher の 1 サイクル harness
  (`dispatcher-real-code-namespace-harness`) で、`needs_director` (後続あり / **後続なし**) → 5 分後に Director へ・
  10 分後に偽の Bot API へ・**Director 不在でも 10 分後に Telegram へ**・解けたら台帳が消える・台帳が `Unreadable` で鳴らさない・
  破損カードで鳴らさない (戻ったら経過どおり)。**赤の実証** (`regression-test-must-prove-red`): 段階 2 の dedup を外して落ちること、
  R6 の「Director 不在」の分岐を外して落ちること、**R6 の `telegram_available == False` を `none` / keep で終わらせる版 (R7 に落とさない) で P2-2 の不変条件が落ちること**。
- 本番確認: 使い捨ての mission で `needs_director` (後続あり・なし) を作り、config の秒数を短くして 2 段階が順に届くこと。
  (使い捨て mission の `init` は `active_missions` に即露出する — memory: `disposable-mission-init-exposes-to-dispatcher-immediately`。)

## 10. ユーザー決定と、決めてほしいこと

### 決定済み (2026-10-05。この文書の反映先)

1. **`AskUserQuestion` と Telegram**: Telegram が使えるときは `AskUserQuestion` を使わず、会話に書いて `ask_user.sh ask` で送り、
   ターンを終える (§4)。
2. **後続の無い最後の task の判断待ちも段階上げする** (初版の推奨「しない」から変更)。「後続を止めているか」は条件ではなくなった (§5-2)。
3. **段階 1・段階 2 とも 1 回だけ** (§5-3)。

### 決定済み (2026-10-05, 2 回目) — P1-2: respawn の後も認証情報を dispatcher に届ける方法

**1Password を本命 (A')・ダメならリポジトリ外の 0600 ファイル (B)**。取り出し方は config (`telegram.credentials.source: op | file`) で
**1 つだけ選ぶ**。送る側 (`ask_user.sh`) と受ける側 (dispatcher) は同じ設定・同じ `resolve_credentials()` を通る。
env の `CREWVIA_TG_*` は読まない (§1-1 に優先順位の全体)。**どの方式でも §1-1 の層 1 (受信側の状態の記録 + `ask` が断る +
割れを Director に 1 回 + `bot_id` / `chat_hash` の照合) は入れる**。chat_id も token と同じ経路で運ぶ
(個人の識別子なので、公開リポジトリの `config/crewvia.yaml` には値を書かない)。

| 案 | 仕組み | 得 | 失 |
|---|---|---|---|
| **A' (本命): dispatcher の起動時に 1 回 1Password CLI で取り出す** | config には**参照だけ** (`op://…`)。`dispatcher.sh` が bash の外側・ループの前 (respawn のたびに通る) で 1 回取り出し、シェル変数に持つ (`export` しない)。python のサイクルには bash の前置代入 (`env` を付けない) でその呼び出しにだけ渡す。`ask_user.sh` は呼び出しごとに同じ関数で取り出す | respawn に強い (起動のたびに取り直す)。ユーザーのルール (秘密は 1Password 経由) に合う。コマンド文・argv・ファイルに秘密が出ない (python の子プロセスの env に呼び出しの間だけ載る — 同一ユーザーのみ・短時間)。1Password の呼び出しは **dispatcher の起動ごとに 1 回 + 質問ごとに 1 回**で、サイクル単位に乗らない。トークンの入れ替えは 1Password 側 + dispatcher の restart | `op` が dispatcher の環境 (herdr 配下のヘッドレスなペイン) で通る必要がある (ロック中・認証プロンプト・PATH に `op` 無し → `credential_command_failed`)。**このマシンの非対話シェルでは `command -v opx` が見つからなかった (t014)** ので、通るかは**未確認で一段強く疑わしい** → 本番確認 (§9 (5)) でユーザー立ち会いのもとで確かめる。dispatcher の bash プロセスの存続中、値がそのプロセスのメモリに残る (同一ユーザーの `/proc/<pid>/environ` には出ない — `export` しないため) |
| **B (代替): リポジトリ外の 0600 ファイル** | `telegram.credentials.file` (例 `~/.local/share/crewvia/telegram.env`) を `resolve_credentials()` が読む。所有者が自分でなく / 権限が 0600 でなければ**中身を開かず**停止 (`credential_file_permissions`) | respawn に強い。外部コマンド・1Password のロックに依存しない (デーモンから確実に使える) | **平文の秘密がディスクに残る** (「秘密は opx / 1Password 経由」のルールの**例外**。下の理由)。WSL では権限が Windows 側から見えうる。ローテーションが手作業 |
| (C: 採らない) `_SPAWN_ENV_VARS` に足して respawn のコマンド文に載せる | allowlist に 1 行 | 実装が最小 | **コマンド文に秘密が載る** (`ps`・pane の scrollback・mux のログ。`_SPAWN_ENV_VARS` の注記が避けている事故そのもの) |
| (D: 届け方を設計しない) | respawn で落ちたら `enabled: false` + Director に通知。ユーザーが手で再起動 | 秘密の扱いが増えない | respawn のたびに受信と段階 2 が止まる (watchdog の自動 respawn は設計上起きる)。長時間の無人運転で効かない |

**選び方 (config)**: 既定は `source` 未設定 = Telegram 未設定 (公開前提。何も起きない)。`op` を使うか `file` を使うかはユーザーが
config に書く。**`op` で本番確認 (§9 (5)) が通らなければ `file` に切り替える** — その切り替えはユーザーの判断 (ルールの例外を使うため)。

**B がルールの例外である理由 (§10 に残す)**: ユーザーのルールは「秘密は opx / 1Password CLI 経由、コマンド文に秘密を書かない」。
B は平文をディスクに置くので例外になる。それでも選択肢に残すのは、**デーモンから確実に使える秘密の置き場がこのマシンに他に無い**ため:

- **systemd の credential (systemd-creds)**: `--user` に対応しておらず、root が要る。`sudo` は原則禁止。
- **gpg**: gpg-agent の期限が切れるとデーモンが復号できない (無人運転で止まる)。
- **Windows の資格情報マネージャー**: herdr 配下では WSL interop が無く (`herdr-env-lacks-wsl-interop`)、デーモンから呼べない。

**bot token が漏れた場合の影響範囲** (B を許す判断の材料): chat_id の照合があるので**押下は偽造できない** (§2-2 の 1)。
できるのは (1) その bot として任意のメッセージをユーザーに送り付ける、(2) `getUpdates` を奪って受信を妨害する、の 2 つ。
**BotFather で token を無効化 (revoke) すれば止まる**。ボタンの答えは §3-2 の規則 (台帳の `verify`・実行直前の確認・破壊的操作は
Telegram だけを根拠にしない) で守られているので、漏洩しても承認の偽造にはならない。B の運用: ファイルは**ユーザーが 1 度だけ手で作る**
(Claude はそのファイルを読み書きしない。`~/.claude/rules/security.md` の対象に `~/.local/share/crewvia/*.env` を加える)。

**未確認 (本番確認でユーザー立ち会い)**: ヘッドレスの dispatcher から 1Password が通るか。**`~/.config` 配下を読まない・1Password の中身を
取り出さない** (ユーザーのルール)。確認は §9 (5) の「`reason: ok` と `bot_id` が出たか」だけで行う。

## 11. 検証 (設計時の実測)

- 受信の位置: `dispatcher.sh` のサイクルは 5 秒 (`sleep 5`, `dispatcher.sh:3180`)。
  `needs_director` の検知は `all_tasks` を 1 回走査して使い回す (`dispatcher.sh:2838-2886`)。
- 依存の判定: `lib_dep_rules.unmet_dependencies` / `held_dependencies`。dispatcher は `dependency_gate()` 経由
  (`dispatcher.sh:892`)。
- 通知の 1 回だけ: `notify_state_once()` (`dispatcher.sh:1133`)。台帳の入口は `lib_daemon_state.told_*`。
- `WAITS_ON_DIRECTOR_STATUSES = {'needs_director'}` (`lib_task_status.py:69`)。
- Telegram の仕様 (callback_data 64 バイト・1 秒 1 通・本文 4096) は設計時の知識で、**PR-A の実装時に
  公式の Bot API ドキュメントで再確認する** (この文書では実測していない)。

## 11. PR-B の実装メモ (t007)

- `scripts/lib_escalation.py`: `decide()` / `apply_failure()` (純粋関数。規則表 R1〜R8 は §5-4 のまま)・`run_cycle()` (台帳の読み書きと送信の順序)・
  `load_cfg()` / `parse_cfg()` (§5-3)・`escalation_state_problem()` (台帳の形。`lib_daemon_state` ではなくこの lib に置いた — 書き手も読み手もここだけ)。
  通信はしない (送信は dispatcher が関数で渡す)。`blocked_dependents()` は `lib_dep_rules.card_dependencies` を通す (依存の定義を持たない)。
- `dispatcher.sh`: `run_escalation_cycle()` を `needs_director` / handoff の後・`prune_told` の前に 1 か所 (`all_tasks` を使い回す)。
  段階 2 は `lib_telegram.send_message` (プロセス内。token は `_TG_RUNTIME` に持ち env に出さない・`timeout` は 5 秒)。`telegram_available` は
  `lib_telegram.telegram_available`。1 サイクルの Telegram は最大 3 通。
- **問い合わせは鳴らしうるときだけ**: mux (`director_live`) と Telegram の可否は、経過が最小のしきい値に届いた card があるときにだけ 1 回評価する
  (`_could_act`)。待機中の task が増えても毎サイクル `mux list` を叩かない (`test_a_cycle_with_only_already_told_states_does_not_ask_the_mux`)。
- 台帳は「送れた後だけ」書く。書けなければその周期の残りの送信を止める (記録できないまま送り続けない)。台帳が `Unreadable` なら段階上げ全体を見送る (§5-4)。
- Director 宛 (段階 1) は 1 行 (` / ` 区切り。mux send は改行で複数送信になるため)、Telegram 宛は同じ項目の複数行。
- テスト: `tests/test_escalation_decide.py` (手書き oracle 表 25 行・全直積の不変条件・ランダム列 400 本・cfg・台帳の形・文面) /
  `tests/test_escalation_dispatcher_cycle.py` (本物の dispatcher 1 サイクル + 偽の Bot API。t017 → t006 の事故の再現)。
  赤の実証 (欠陥を戻して落ちること): R5 の dedup 除去 / R6 の「Director 不在」分岐除去 / R6 で Telegram 不可のとき R7 に落とさない版 /
  dispatcher の呼び出し除去 (13 本赤)。
- 本番確認: 実施済み (§12)。

### 11-1. 記録してから送る (PR #283 Codex P2 / t029)

初版は「送ってから台帳に書く」で、**台帳が読めるが書けない**状態 (ディレクトリの権限・ロックが取れ続けない) だと、
次のサイクルが古い段階を読んで同じ通知を 5 秒ごとに送った。直し:

1. 段階 N を送る**前**に、台帳へ `sending_stage=N` / `sending_at` を書く。**書けなければ送らない** (そのサイクルは見送り)。
2. 送れたら `stage_sent=N` に進めて sending を外す。失敗したら sending を外し (段階 1 は `stage1_failed_at`) 次のサイクルで再試行。
3. 結果の書き込みが失敗しても sending が残る → `decide()` の **R5b** (期限内の sending は `none` / keep) が同じ段階を止める。
4. sending が `SENDING_TIMEOUT_SECONDS` (600 秒) を過ぎたら「失敗」として読む (段階 1 なら `stage1_failed_at`)。送り直せるのは、**その前に送る前の書き込みが成功した**ときだけ
   — 保存が壊れている間は何度でも見送りで、連投にならない。倒れる向き = 保存が壊れている間は通知が欠ける側。
5. 見送りは Director に知らせる (`[escalation] escalation-state に書けない…`)。間引きは台帳ではなく notify cache (`should_notify`。別ファイル・プロセスをまたぐ・NOTIFY_TTL に 1 回)。
   Director 不在・送信失敗なら log だけ。notify cache も書けないと毎サイクル届きうるが、dispatcher の他の通知と同じ前提 (台帳に依存させないことを優先した)。

テスト: `tests/test_escalation_ledger_failures.py` (書き込み失敗を出来事とする長さ 1〜40 のランダム列 300 本 + 個別の再現)・`test_escalation_decide.py` の R5b 行。
赤の実証: 送る前の書き込みを外す → 112 本赤 / R5b を外す → 31 本赤。

## 12. 本番確認 (t011, 2026-10-07)

PR-A (#281)・PR-B (#283) の merge と dispatcher の restart の後に本番で確かめた結果。認証情報は §10 の A' (`source: op` + `crewvia-op`)。
判定は `registry/daemons/telegram-receiver.json` の `enabled` / `reason` / `bot_id` だけで行い、`~/.local/share/crewvia/*` は開いていない。

### 12-1. 走っているコードの版

- `dispatcher.version.json`: `head = a60fdff3942dd34ba0684a6dc05e27315d28117b` (= `origin/main`)・`files_digest = 051ffb53…caaad0b`。
- dispatcher の起動は 22:48:27 (ps の lstart)。receiver は `enabled: true / reason: ok / bot_id: 8958114524`。
- **`files_digest` が覆う範囲に注意**: 覆うのは `dispatcher.sh`・`lib_daemon_watch.*`・`lib_mux.py`・`lib_retirement.py` だけで、`lib_escalation.py` / `lib_telegram.py` は入っていない。この 2 つが a60fdff の版であることは、`head = a60fdff` かつ `scripts/` に未コミットの変更が無いこと (dispatcher はサイクルごとに新しい python で import し直す) で証明している。

### 12-2. Telegram の往復 — 合格

- Director が `ask_user.sh ask` で試験の質問 `q-d9708959` を送信 (exit 0・22:49:14)。ユーザーが「届いた」を押した (22:50:53)。
- Director の画面に `[telegram-answer] q=q-d9708959 task=…/t011 choice="届いた" index=0 task_state=in_progress` が届いた。
- 台帳: `status=answered` / `forwarded=true` / `closed_at` あり / `execution_id` は t011 の実行 ID。`ask_user.sh verify` も同じ値。
- **札の再利用不可は、台帳が `answered` で閉じていることまでの確認**。もう一度押してもらう実測はしていない (残す)。

### 12-3. 段階上げ — 合格

使い捨て mission (`esc-obs-t011`) の t001 を `needs_director`、t002 を `blocked_by t001` にし、`active_missions` に足して dispatcher に見せた。

| 事象 | 時刻 | `first_seen` (22:49:55) からの経過 |
|---|---|---|
| 段階 1 (`stage_sent=1`) | 22:55:01 | 301.8 秒 (設定 300) |
| 段階 2 (`stage_sent=2`) | 23:00:11 | 605.4 秒 (設定 600) |

- 段階 1・2 とも **1 回だけ**。23:04 まで台帳は `stage_sent=2` のまま追加の送信なし。
- Telegram の送信記録 (`telegram-send.json`): `last_sent_at` は段階 2 の時刻と一致・`consecutive_failures=0`。
  受信の目視: 段階 1 は Director の画面に 1 回届いた。段階 2 はユーザーの Telegram に **1 通だけ**届いた (2026-10-07、ユーザーが確認)。
  段階 1 の文面に出た「理由: (理由未記載)」は、`esc-obs-t011` の card に `needs_director_reason` が無かったため (使い捨て mission の作り方の都合)。通常の needs_director では理由が入る。§6 の文面の確認としては、この欄は読まない。
  PR-B merge 直後 (Telegram 未設定の時点) にも段階 1 が 1 回届いている (段階 2 は無し)。
- 解除: t001 を done にすると 20 秒以内に `escalation-state.json` が `{}` に戻り、以後送信なし。
- 後始末: `state.yaml` を元に戻し (`active_missions` から外した)。t002 は pending のまま残り mission は非 active。
- 観察の型: 台帳を 15 秒ごとに記録し、遷移の行だけ抜き出す。経過は `first_seen` との差で出す。

### 12-4. 分類器の観察 — probe は通った (確かめとしては弱い)

- Telegram の答えを根拠に Director が無害な操作 (`plan.sh update t002 --mission esc-obs-t011 --description "classifier probe"`) を実行した。
  card の description が `classifier probe` に変わっていて、**実行は拒否されなかった**。
- **留保**: この probe (description の update) は、普段の Director の操作でも止められない種類なので、分類器の確かめとしては弱い。**merge など外部に影響する操作を、Telegram の答えだけを根拠に分類器が通すかは未確認**。
- merge は試していない。§3-2 のとおり、破壊的・外向きの操作は Telegram の答えだけを根拠にしない。§6 の「認められなければ画面で確認を求める」手順はそのまま残す。

### 12-5. 残り

- 札の再押下の実測 (12-2)。
- 外部に影響する操作 (merge 等) を Telegram の答えだけで分類器が通すかの確認 (12-4)。

## 13. P3 4 件の解消 (PR #281 の merge 時に残したもの)

1. **未設定の `cancel` が 0 byte の lock を作る**: 台帳が無ければロックを取る前に `not_found` (exit 6) で抜ける。`list` / `verify` は読むだけでロックを取らない (確認済み)。
2. **閉じた質問のボタンの再押下**: `watched_buttons()` = ボタンを消す途中 (`unbutton`) か、閉じて `CLOSED_WATCH_SECONDS` (600 秒) 以内の質問。`run_cycle` の `needs_net` と `_receive_updates` の「通信しない」条件の両方に足した。
   質問 0 件・閉じて 10 分を過ぎたものだけなら今までどおり getUpdates を叩かない。10 分を過ぎたボタンの再押下は応答されない (台帳の照合で断られる側。Telegram 側の待ち表示は数秒で止まる)。
3. **`telegram_offset_unreadable` の台帳**: poll が `offset_ok` (本体が読めた / 無い) を返したら dispatcher が `clear_told_key` する。offset を読まなかった poll (台帳が使えない・質問なし) は `offset_ok` を返さない = 畳まない。
4. **offset の本体が書けない** (パスがディレクトリ等): 本体が書けなければ `telegram-offset-fallback.json` に書く。本体が**読めない** (無いではない) ときだけ退避先を読むので、(a) 同じ update を読み直さず (b) `last_poll_at` の間引きも効く。
   poll は `offset_unwritable` を返し、dispatcher が Director に 1 回だけ知らせる (`telegram_offset_unwritable`。このときは「読めない」の通知を重ねない)。本体に書けた poll は `offset_write_ok` を返し、退避先を消して台帳を畳む。
   dispatcher の受信サイクルの変更なので merge 後に **dispatcher の restart が要る**。
