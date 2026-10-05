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
  (offset を進めた側だけが受け取る)。env は `CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID`。
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
   - これは**共有規則の env 停止スイッチではない** (不変条件 5 の対象外) — ただし「設定済みか」の答えを
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
`CREWVIA_TG_*` は落ち、受信と段階 2 が**黙って**止まる。一方 Director のシェルには token があり、
`ask_user.sh ask` は送れてしまう (押されたボタンを誰も受けない)。memory: `daemon-secret-env-lost-on-respawn`。

**直し方は 2 層** (どちらも要る):

1. **割れを起こさない仕組み (最低限・必須)**: dispatcher が毎サイクル (変化したとき + 30 秒ごとの心拍) に
   `registry/daemons/telegram-receiver.json` を書く。**書き手は dispatcher だけ**。
   ```json
   {"enabled": true,  "checked_at": 1759650000.0, "reason": "ok"}
   {"enabled": false, "checked_at": 1759650030.0, "reason": "no_credentials"}
   ```
   `reason` は固定コード (`ok` / `no_credentials` / `credential_command_failed` …)。**token・chat_id・参照の文字列は書かない**。
   - `ask_user.sh ask` は送る前に必ずこれを読み、**`enabled == true` かつ `checked_at` が 3 × `poll_interval` 以内**
     でなければ**断る** (exit 4。stderr に固定コード `receiver_disabled` / `receiver_stale` / `receiver_unknown`
     (ファイルが無い) を出す)。これは初版の「dispatcher の生存確認」を置き換える — 心拍が新しければ
     生きていて、かつ受信できる状態だと言える。`ask` は断るので、押されても誰も受けないボタンは出ない。
     Director は `AskUserQuestion` に戻る。
   - **割れを Director に 1 回知らせる**: dispatcher が「前回 `enabled: true` で今回 `enabled: false`」を観測したとき、
     既存の `notify_state_once` で Director に 1 通 (key `telegram_receiver_disabled`、fp = `reason`):
     「Telegram 受信が無効になりました (理由コード)。dispatcher が env を失った可能性 (respawn)。`lib_daemon_watch.py restart` を
     認証情報つきで行うか、§10 の選択肢を設定してください」。状態を離れた (`enabled: true` に戻った) ら台帳から捨てる。
   - 受信側の状態の書き方は他の台帳と同じ入口 (`lib_daemon_state`・`telegram_receiver_problem()`・原子的置換)。
     壊れていたら `ask` は断る側に倒す (**観測できなかったことを「有効」に倒さない**)。
2. **respawn 後も認証情報を届ける方法**: コマンド文・env allowlist には秘密を載せない (**採らない**)。
   選択肢の比較は **§10 (ユーザー判断)**。推奨は「`poll` / `ask` のたびに 1Password CLI (`opx`) で取り出す」。
   どの方式でも、**認証情報の解決は `lib_telegram.resolve_credentials()` の 1 か所**で、`ask_user.sh` と
   dispatcher の `poll` / 段階 2 の送信が同じ関数を通す (割れる余地を作らない)。

本番確認 (PR-A): 「`lib_daemon_watch.py restart` (または watchdog の自動 respawn) の後にも、ボタンが受信される
(または `ask` が `receiver_disabled` で断られ、Director に通知が 1 通来る)」ことを 1 回観察する。

### 副次: getUpdates の消費者は dispatcher の 1 者だけ

`getUpdates` は 1 つの bot に対して**消費者が 1 者**でなければならない (複数だと更新を奪い合う)。
`ask_user.sh` (Director 側) は**送信だけ**で、受信はしない。`CREWVIA_TG_*` が指す bot を
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

1. `callback_query.from.id` が `CREWVIA_TG_CHAT_ID` と一致 (個人チャットでは user id == chat id)。
   **`message.chat.id` も一致**を要求する (グループに bot が入った場合、他人の押下を排除)。
2. `callback_data` が `<qid>.<nonce>.<index>` の形に厳密に一致 (正規表現で全体マッチ。余りは拒否)。
3. `qid` が台帳にあり、**`nonce` が一致**し、**`callback_query.message.message_id` が記録の `message_id` と一致**
   (別メッセージのボタンの流用を排除)。
4. `index` が `options` の範囲内。
5. `status == "open"` かつ `now < expires_at`。

テキスト返信の場合 (「込み入った指示は返信文で」):

1. `message.chat.id` / `from.id` が `CREWVIA_TG_CHAT_ID` と一致。
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
| 質問の `message_id` がまだ `null` (ask の ③ の前) の `open` を指す押下 | **転送せず、offset を進めない** (次のサイクルで同じ update をもう一度受ける)。窓は ask の ②→③ の間だけで小さい。5 分を超えて `null` のままなら §2-3b で `withdrawn` になり、その後の押下は「不明」で拒否される (t002 P3-1) |
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

### 2-4. offset

- `telegram-offset.json` に `{"offset": N, "last_poll_at": …}` (**書き手は `poll` だけ**。§1-3 の `telegram-poll.lock` で
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
| R6 | **段階 2 が到達可能** かつ `t > 0` かつ `elapsed ≥ t` — ここで「到達可能」= `stage_sent ≥ 1` **または** `d ≤ 0` **または** `not director_live` **または** `stage1_failed_at != None` | `telegram_available`: `telegram_notice` / set(stage 2)。**でなければ** `none` / keep (見送り。使えるようになったら送る) |
| R7 | `d > 0` かつ `stage_sent == 0` かつ `elapsed ≥ d` かつ `director_live` | `director_renotice` / set(stage 1)。(R6 に当たらなかった = 段階 2 に進める条件が揃っていない、または `elapsed < t`) |
| R8 | 上のどれでもない (経過が足りない・段階 1 を送るべき Director が居ない・など) | `none` / keep |

R6 と R7 の関係が P1-1 の直し方 (t002): **Director 不在 (`director_live == False`) の間は R6 の「到達可能」が成り立つので、
`elapsed ≥ t` で段階 2 が出る** (段階 1 を待って永久に止まらない)。Director が戻っても `stage_sent == 2` なので段階 1 は出ない
(「1 回だけ」・Director が不在だった間に段階 2 が既に出ている。戻った Director には既存の `[needs_director]` が
(Director 不在では記録されず) 戻った時点で届く)。段階 1 の送信が**失敗**したとき (在席なのに `tmux_send` が False)
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
- **Director 不在で `t > 0` かつ `elapsed ≥ t` かつ `telegram_available` なら、`stage_sent < 2` の限り必ず `telegram_notice`** (P1-1 の回帰)
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
| `telegram-receiver.json` が `Unreadable` / 古い (心拍が 3 × `poll_interval` を超える) / 無い | `ask` は**断る** (§1-1) | 観測できなかったことを「有効」に倒さない |
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
- `config/crewvia.yaml`: `telegram:` ブロック (`poll_interval_seconds`・`session_link`・`question_ttl_minutes`、および §10 で
  選んだ認証情報の参照 — 秘密そのものは書かない)。
  CLAUDE.md の環境変数表に `CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID` / `CREWVIA_DIRECTOR_SESSION_URL`、不変条件 7 に新しい台帳 (5 つのうち 4 つ)。
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
  **認証情報が無い dispatcher で `enabled:false` と Director への通知 1 通 (同じ状態で 2 通目が出ない)**。
  既存の `tests/CLAUDE.md` の隔離規則 (`env -u AGENT_NAME`、`CREWVIA_MUX_TEST_ISOLATION`) に従う。
- 本番確認: 本物の bot・本物の Director で (1) `ask` → ボタン → `[telegram-answer]` が画面に届く、
  (2) §3-3 の分類器の観察、(3) §4 の modal の観察、(4) **dispatcher を `lib_daemon_watch.py restart` (または watchdog の respawn) した後も
  ボタンが受信される、または `ask` が `receiver_disabled` で断られ Director に通知が 1 通来る** (§1-1)。
  **dispatcher の restart が必要** (`merged-daemon-code-is-inert-until-restart`。`scripts/sync-main-checkout.sh`)。

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
  R6 の「Director 不在」の分岐を外して落ちること。
- 本番確認: 使い捨ての mission で `needs_director` (後続あり・なし) を作り、config の秒数を短くして 2 段階が順に届くこと。
  (使い捨て mission の `init` は `active_missions` に即露出する — memory: `disposable-mission-init-exposes-to-dispatcher-immediately`。)

## 10. ユーザー決定と、決めてほしいこと

### 決定済み (2026-10-05。この文書の反映先)

1. **`AskUserQuestion` と Telegram**: Telegram が使えるときは `AskUserQuestion` を使わず、会話に書いて `ask_user.sh ask` で送り、
   ターンを終える (§4)。
2. **後続の無い最後の task の判断待ちも段階上げする** (初版の推奨「しない」から変更)。「後続を止めているか」は条件ではなくなった (§5-2)。
3. **段階 1・段階 2 とも 1 回だけ** (§5-3)。

### 決めてほしいこと — P1-2: respawn の後も認証情報を dispatcher に届ける方法

前提: ユーザーのルールは「秘密は opx / 1Password CLI 経由、`.env` を読まない、コマンド文に秘密を書かない」。
`lib_daemon_watch` の `_SPAWN_ENV_VARS` は秘密を運ばない (§1-1)。**どの方式でも §1-1 の層 1 (受信側の状態の記録 + `ask` が断る +
割れを Director に 1 回) は入れる** — 方式は「層 2: 届け方」の選択。chat_id も token と同じ経路で運ぶ
(個人の識別子なので、公開リポジトリの `config/crewvia.yaml` には値を書かない)。

| 案 | 仕組み | 得 | 失 |
|---|---|---|---|
| **A (推奨): `poll` / `ask` のたびに 1Password CLI (`opx`) で取り出す** | config には**参照だけ** (`telegram.token_ref` / `telegram.chat_id_ref` = `op://…` 形式。秘密ではない) を置く。`resolve_credentials()` が `poll` サブプロセス・`ask_user.sh` の中で `opx` を呼び、値はプロセスのメモリにだけ置く (env にもファイルにもコマンド文にも出さない) | respawn に強い (env を運ばないので落ちない)。ユーザーのルールにそのまま合う。token の入れ替えが 1Password 側だけで済む。送信側と受信側が**同じ関数**を通すので「設定済みか」が構造的に割れにくい | `opx` が dispatcher の環境で使える必要がある (1Password のロック・デーモンの端末に認証が無いと失敗 → `enabled: false` / `credential_command_failed` で見える)。`poll` ごとに外部コマンドを呼ぶ (間引き 10 秒 + 結果を 1 サイクル内メモリのみ。呼び出しの遅れは `timeout 8` に含める)。**本番でヘッドレスの dispatcher から `opx` が通るかは未確認** (PR-A の本番確認の前に 1 回確かめる) |
| B: 権限 0600 のファイル (例 `registry/daemons/` の外のユーザー専用ファイルを 1 つ) を `resolve_credentials()` が読む | ユーザーが 1 度だけ手で作る (Claude はそのファイルを読み書きしない。`~/.claude/rules/security.md` の対象に加える) | respawn に強い。外部コマンドが要らない | **平文の秘密がディスクに残る** (「秘密は opx 経由」のルールの例外)。WSL ではファイルの権限が Windows 側から見えうる。ローテーションが手作業 |
| C: `_SPAWN_ENV_VARS` に `CREWVIA_TG_*` を足して respawn のコマンド文に載せる | `lib_daemon_watch` の allowlist を 1 行足す | 実装が最小 | **コマンド文に秘密が載る** (`ps`・pane の scrollback・mux のログ。`_SPAWN_ENV_VARS` の注記が避けている事故そのもの)・「コマンド文に秘密を書かない」に反する。**採らない** |
| D: 届け方は設計しない (層 1 だけ) | respawn で落ちたら `enabled: false` になり、Director に通知が来る。ユーザーが認証情報つきで手で dispatcher を再起動する | 秘密の扱いが増えない | respawn のたびに受信と段階 2 が止まる (watchdog の自動 respawn は設計上起きる)。最も要る場面 (長時間の無人運転) で効かなくなる |

**推奨は A**。理由: respawn に強い・ユーザーのルールに合う・平文を残さない、の 3 つを同時に満たすのは A だけ。
A が本番のヘッドレスな dispatcher から動かないと分かった場合は、B (平文ファイルを許すかをユーザーに再確認) か D にフォールバックする。
**この PR-A の実装前に、ユーザーが案を選ぶこと** (A なら `op://` の参照 2 つを教えてもらい、`opx` がデーモンから通るかを 1 回確認する)。

## 11. 検証 (設計時の実測)

- 受信の位置: `dispatcher.sh` のサイクルは 5 秒 (`sleep 5`, `dispatcher.sh:3180`)。
  `needs_director` の検知は `all_tasks` を 1 回走査して使い回す (`dispatcher.sh:2838-2886`)。
- 依存の判定: `lib_dep_rules.unmet_dependencies` / `held_dependencies`。dispatcher は `dependency_gate()` 経由
  (`dispatcher.sh:892`)。
- 通知の 1 回だけ: `notify_state_once()` (`dispatcher.sh:1133`)。台帳の入口は `lib_daemon_state.told_*`。
- `WAITS_ON_DIRECTOR_STATUSES = {'needs_director'}` (`lib_task_status.py:69`)。
- Telegram の仕様 (callback_data 64 バイト・1 秒 1 通・本文 4096) は設計時の知識で、**PR-A の実装時に
  公式の Bot API ドキュメントで再確認する** (この文書では実測していない)。
