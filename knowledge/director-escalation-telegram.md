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

- **段階上げ**: 判断待ちが後続を止めていたら、**5 分で Director に再通知・10 分で Telegram でユーザーに通知**。
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
   呼び出し側は「今回は何も受けなかった」で次のサイクルに進む。
2. **間引く。** 5 秒ごとには呼ばない。`TG_POLL_INTERVAL` (既定 10 秒、config `telegram.poll_interval_seconds`) の
   スロットル。スロットルの時刻は他の通知スロットルと同じ入口 (`should_notify` 系) ではなく、
   **受信専用の状態** (§2-5 の `telegram-state.json`) に置く (通知の `NOTIFY_TTL` と混ぜない)。
3. **未設定なら 1 バイトも触らない。** `CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID` のどちらかが空なら、
   サブプロセスを起動せず、ファイルを作らず、ログも出さない (公開前提・Taskvia 非依存と同じ型。
   `CREWVIA_TASK_GRAPH=0` の「1 バイトも書かない」と同じ扱い)。
   - これは**共有規則の env 停止スイッチではない** (不変条件 5 の対象外)。受信を読むのは dispatcher だけで、
     plan.sh と答えが割れる規則が無い。「未設定 = 機能が無い」という構成の話。
   - **開かれている質問が 1 件も無いときは受信しない** (§2 の質問台帳に `open` が 0 件なら何もしない)。
     通常運用のほとんどのサイクルで外部への通信がゼロになる。代わりに、
     **bot 宛の雑多なメッセージ (`/start` 等) は拾わない・offset も進めない** (§2-6)。
4. **mux 非依存。** 転送は既存の `tmux_send()` (= `_mux.send`) を使う。tmux でも herdr でも同じ。
   mux が無いインラインモードでは dispatcher が動かないので、受信も動かない (§7 に「届かない」の扱い)。

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
- 質問の最大同時数・保管: `open` は最大 8 件 (超えたら `ask` が拒否 = §7)。`answered` / `withdrawn` / `expired` は
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
open ──expires_at 超過──▶ expired
```

| ケース | 扱い |
|---|---|
| **二重押し** (answered の質問のボタンをもう一度) | 転送しない。`answerCallbackQuery` で「回答済み: <ラベル>」。**先に確定した 1 つだけが有効**。状態変更は `told_lock` の下で「`open` → `answered`」を 1 回だけ行う (CAS)。違う選択肢を後から押しても上書きしない |
| **古いボタン** (期限切れ・台帳に無い・nonce 不一致) | 転送しない。`answerCallbackQuery` で「期限切れ / 不明な質問」。期限切れなら `editMessageReplyMarkup` でボタンを消す (best effort。失敗しても無視) |
| **Director が既に別の手段で答えを得た後の押下** | Director は答えを得た時点で `ask_user.sh cancel --q <id> --by screen` を呼ぶ (§4)。`withdrawn` なので押下は転送されず、「画面で回答済み」と返し、メッセージのボタンも消す。**Director が cancel を忘れた場合**は転送される — その場合も Director の規則 (§3) で、実行の直前に状態を確かめ直すので、既に済んだ操作を二重に実行しない |
| 質問に `--task` が付いていて、その task が既に `needs_director` を離れていた | 転送はする (ユーザーは押した)。行に `task_state=<status>` を添える (§3)。Director が状態を見て判断する |
| 転送 (mux send) に失敗した・Director 不在 | `answered` / `forwarded=false` のまま。**`answerCallbackQuery` は先に返す** (ボタンの待ち表示を止める)。次の受信サイクルで `forwarded=false` の answered を再送する。期限は質問の `expires_at` ではなく**答えが入った時刻から 24 時間**で諦め、Telegram に「Director に届きませんでした」と 1 回返す |
| 台帳が `Unreadable` | 転送しない・offset を**進めない**・`answerCallbackQuery` も呼ばない (ボタンの待ち表示が残る)。ログに `WARNING: telegram-questions` を 1 回/10 分。台帳を消せば復旧 (§2-1)。fail の向き: **観測できなかったことを「答えが無い」に倒さない** |

### 2-4. offset

- `telegram-state.json` に `{"offset": N, "last_poll_at": …}`。`getUpdates(offset=N, timeout=0, allowed_updates=["callback_query","message"])`。
- **offset を進めるのは、その update を処理し終えた (台帳に答えを書いた / 拒否して返信した / 無関係と確定した) 後**。
  処理途中で落ちたら同じ update をもう一度受ける (**at-least-once**)。重複しても §2-3 の CAS が 1 回に畳む。
  (`update_id` を `answer.update_id` に残し、同じ update_id の再処理は何もしない。)
- 台帳が `Unreadable` のときは offset を進めない (上表)。

### 2-5. 置き場のまとめ

| ファイル | 内容 | 消してよいか |
|---|---|---|
| `registry/daemons/telegram-questions.json` | 質問台帳 (§2-1) | 消してよい (古いボタンが不明になるだけ) |
| `registry/daemons/telegram-state.json` | offset・最後の受信時刻・エラー通知のスロットル | 消してよい (offset が 0 に戻る → 古い update を再受信するが、台帳に無い質問のボタンは拒否される。**ただし `message` の返信は reply_to で台帳と照合されるので誤転送しない**) |
| `registry/daemons/escalation-state.json` | 段階上げの台帳 (§5-4、PR-B) | 消してよい (時計が最初からになる = 通知が遅れる側。§5-4) |

3 つとも `notified-state.json` と同じ置き場・同じ入口 (`lib_daemon_state`)・同じ「消してよい」の位置づけ。
CLAUDE.md の不変条件 7 の列挙に 3 つを足す (PR-A / PR-B で、それぞれ自分のファイルの分)。

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
  `AskUserQuestion` に戻る)、**4 = 送信失敗** (ネットワーク・Bot API のエラー。同じく戻る)、**5 = open が上限**、
  1 = 使い方の誤り (`pull` の規則と同じ。exit 2 は使わない — Worker が無限リトライする前例、
  memory: `pull-exit-2-is-idle-usage-errors-must-be-1`)。**本文・token・URL は終了時のエラーに出さない**
  (解析エラーに元の行が漏れる族、memory: `parser-error-leaks-source-line-family`。固定コード + 位置だけ)。
- 順序: ① 質問を台帳に `open`・`message_id=null` で書く (ロック下) → ② `sendMessage` (inline_keyboard) →
  ③ 成功したら `message_id` を台帳に書く。② が失敗したら①のエントリを `withdrawn` にして exit 4。
  ③ が失敗 (台帳に書けない) したら、ボタンは出ているが台帳が答えを受けられない → `editMessageReplyMarkup` で消し、
  exit 4。**ボタンが出ているのに台帳に無い状態を作らない**。
- `--task` は付けると `verify` と転送行に出る。card を読んで `execution_id` を記録する (`lib_task_cards` の入口。読めなければ
  `execution_id` を空で記録して続行 — 質問は送れる。`task=` の表示だけが弱くなる)。
- **選択肢は 2〜4 個** (Telegram の 1 行に収まる。`AskUserQuestion` と同じ上限)。ラベルは 40 文字まで。
  末尾に自動で「💬 返信で答える」の注記を本文に足す (ボタンではない。返信文は §2-2 で受ける)。

### `AskUserQuestion` との併用

`AskUserQuestion` は画面を**占有する** (modal)。Telegram の答えは mux send で入力欄に入るので、
modal の最中に届いた文が取り込まれる保証がない (**未確認。本番確認で 1 回観察する**)。そのため推奨する手順:

1. 画面でも見えるよう、質問と選択肢を**通常のテキストで画面に出す** (`AskUserQuestion` は呼ばない)。
2. `ask_user.sh ask …` で Telegram に送り、`qid` を控えて**ターンを終える** (入力待ち)。
3. 画面で答えが打たれたら (ユーザーが席にいた) → `ask_user.sh cancel --q <qid> --by screen` → 実行。
   Telegram の行が来たら → §3-2 の手順 (verify → 状態の確かめ直し → 実行)。
4. exit 3 / 4 (Telegram が使えない) のときだけ、従来どおり `AskUserQuestion` を使う。

`AskUserQuestion` と Telegram を**同時に**出す案は採らない (modal が答えを取り込まない可能性と、
2 つの答えが食い違ったときの優先の規則が要る)。**ユーザーに決めてほしいこと 1** に挙げる。

## 5. 段階上げ (PR-B)

### 5-1. 「判断待ち」の定義

| 候補 | 含める? | 理由 |
|---|---|---|
| `needs_director` | **含める** | 本件の事故そのもの。`WAITS_ON_DIRECTOR_STATUSES` |
| `needs_human_review` (`verify-result needs_human_review`) | **含める** | `plan.sh` の表示は `needs_director` と同じ `blocked` / `[要判断]` (plan.sh:943-944)。人間の判断を待つ点が同じで、後続を止める点も同じ |
| `ask_user.sh` で出して未回答の質問 (`open`) | **含めない** | 質問はすでに Telegram で**ユーザーの手元に届いている**。段階上げの目的 (ユーザーに届かない) が既に満たされている。期限 (`expires_at`) が来たら `expired` になり、Director が気付く (§2-3 の転送行ではなく、`ask_user.sh list` + dispatcher の 1 回通知で十分。PR-A のスコープ外の拡張として §9 に記録) |
| `failed` の依存 (held) | **含めない** | `[held]` の経路が既にある (`plan.sh release-dep`)。保留の通知を足す話は別 (今回の事故と違う入口。§8 に残す) |

**語彙の置き場**: `scripts/lib_task_status.py` に `AWAITING_DECISION_STATUSES = WAITS_ON_DIRECTOR_STATUSES | {'needs_human_review'}` を
**1 つだけ**足す。**`WAITS_ON_DIRECTOR_STATUSES` を広げない** — あれは「assignment を撤去する / 孤児の枠を作る」の集合として
使われ (`dispatcher.sh:689` / `plan.sh:869-872`)、`needs_human_review` は assignment を**撤去しない**
(plan.sh:978-979)。広げると Kai-codex の枠の判定が変わる。`tests/test_task_status_single_definition.py` が
AST で「status の集合を別の場所に書かない」を固定しているので、新しい集合もそこに載せる。

### 5-2. 「後続を止めているか」 — `lib_dep_rules` を通す (不変条件 3)

判断待ちの card `W` (mission `M`) が後続を**止めている** ⇔ `M` の中に、`status == pending` で、
`unmet_dependencies(D.blocked_by, done_ids, task_statuses, D.released_deps)` に `W.id` を含む card `D` が 1 枚以上ある。

- 判定は `lib_dep_rules.unmet_dependencies` / `card_dependencies` を**そのまま呼ぶ**。コピーしない。
  dispatcher は既に `dependency_gate()` で同じ入力を作っている (`dispatcher.sh:892`)。新しい純粋関数
  `blocked_dependents(waiting_id, cards, …)` は、`dependency_gate` の結果を使って
  `waiting_id in verdict.unmet` の card を集めるだけ。**依存の定義を持たない**。
- **直接の後続だけ数える。** 推移的な後続 (D の後続 E) は、D が `pending` のまま unmet なので、
  直接の後続 D が 1 枚あれば止まっていると言える。直接の後続が 0 枚なら推移的な後続も無い。
- 破損カード (`[破損]`・`Unreadable`) が mission にあるとき: **観測できない = 判定しない**
  (`observed_missions` と同じ。「止めていない」に倒さず、**段階上げの時計も進めない**)。
  ただし判断待ちの card 自体が読めている限り、それは `needs_director` の通知 (既存) の対象のまま。
- **止めていない判断待ちは段階上げしない。** (後続が無い最後の task が `needs_director` のとき、
  mission は完了しないが、後続を止めてはいない。この穴は §8・**ユーザーに決めてほしいこと 2** に挙げる。)

### 5-3. 段階と時計

| 段階 | 既定の経過 | 何をする | 送り先 |
|---|---|---|---|
| 0 | 0 分 | 既存の `[needs_director]` 通知 (状態が変わるまで 1 回。`notify_state_once`) | Director |
| 1 | 5 分 | **Director に再通知**: 「後続 N 件を止めて M 分経過」 | Director (mux send) |
| 2 | 10 分 | **Telegram でユーザーに通知**: §6 の文面 | ユーザー (Telegram) |

config (`config/crewvia.yaml` の `escalation:` ブロック。コメント付き。`daemons:` ブロックの書き方に揃える):

```yaml
escalation:
  director_after_seconds: 300     # 段階 1
  telegram_after_seconds: 600     # 段階 2 (director_after_seconds より大きいこと。逆なら読み込みで拒否)
  # 0 以下 = その段階を使わない。telegram: 未設定 (env 無し) なら段階 2 は何もしない
```

環境変数での上書き (`CREWVIA_ESCALATION_DIRECTOR_AFTER_SECONDS` / `CREWVIA_ESCALATION_TELEGRAM_AFTER_SECONDS`)
は、**他のしきい値と同じ型で付ける** (config より優先)。不変条件 5 との関係: これは dispatcher だけが読む値で、
plan.sh と答えが割れる共有規則ではない (「後続を止めているか」の**規則**は `lib_dep_rules` の 1 か所で、しきい値は規則ではない)。
**不正値**は `_parse_drift_interval` と同じく WARNING を 1 回出して既定値に倒す。

**時計の起点**: card には `needs_director` に入った時刻が**無い** (`needs_director_reason` のみ。`lib_state_store.py:526`)。
card に `needs_director_at` を足す案は、serialization の golden (`tests/fixtures/state_store_serialization_golden.json`)・
Taskvia の契約・旧コードとの互換 (`rollback-compat-for-new-flag-must-merge-before-the-cutover-pr`) を全部動かす。
**採らない**。代わりに**段階上げの台帳に「初めて見た時刻」を持つ** (§5-4)。
台帳を消すと時計が最初からになる = 通知が**遅れる**側に倒れる (早まって鳴らさない)。
これは `needs_director` の既存の 1 回通知が (Director 不在でスキップしても) 戻ったらすぐ送る性質と矛盾しない
(段階 0 は時計を使わない)。

**dispatcher が止まっていた間の時間**: 時計は壁時計の差。dispatcher が 1 時間止まって戻ると、最初に見た時刻が
1 時間前なので**段階 1・2 が同じサイクルで両方発火する**。1 サイクルで 1 段階だけ進める
(段階 1 を送ったら、その同じサイクルでは段階 2 を評価しない)。次のサイクルで段階 2。
`telegram_after_seconds` を過ぎていても、`director` 段階を**飛ばさない**。

### 5-4. dedup と台帳 — **純粋関数 + 網羅テスト**から設計する

前のミッションで通知の状態の扱いが Codex に 4 回指摘された (`knowledge/watchdog-idle-judgment.md` §11-13)。
今回は「状態を書き換える手続き」ではなく、**判定を純粋関数にし、台帳の更新を戻り値で表す**。

台帳 `registry/daemons/escalation-state.json`:

```json
{"<slug>/<tid>": {"execution_id": "ex-…", "first_seen": 1759650000.0,
                  "stage_sent": 0 | 1 | 2, "stage_sent_at": 1759650300.0}}
```

- **キーは `<slug>/<tid>`、dedup は `execution_id` 単位。** 同じ task が pending に戻って再び走り、別の試行
  (`ex-…` が変わる) で再び `needs_director` になったら**新しい事象**として最初から (`first_seen` を取り直す)。
  `execution_id` が card に無い (旧形式) ときは、`needs_director_reason` の fp (`notified-state` の `fingerprint`) で代用する。
- **解けたら台帳を消す**: その card が判断待ちでなくなった (status が `AWAITING_DECISION_STATUSES` を離れた) か、
  後続が無くなった (止めていない) とき。消すのは**観測できた mission のそれだけ** (`prune_told` と同じ
  `observed_missions` の規則。破損カードのある mission は触らない)。
  **「後続が無くなった」で台帳を消すと、A (止めている) → B (止めていない) → A で時計が戻る**のは仕様 (別の事象)。

純粋関数 (`scripts/lib_escalation.py`。副作用なし・I/O なし・時計は引数):

```python
def decide(card_view, dependents, ledger_entry, now, cfg) -> Decision
# card_view: (slug, tid, status, execution_id, observable: bool)
# dependents: 直接の後続の件数 (lib_dep_rules を通して数えたもの。観測できなければ None)
# ledger_entry: None | {execution_id, first_seen, stage_sent, stage_sent_at}
# Decision: (action, ledger_update)
#   action: none | director_renotice | telegram_notice
#   ledger_update: keep | set(entry) | delete
```

入力の全組み合わせ表 (これを **テストの母集団**にする。`tests/test_escalation_decide.py` は表の全行を
パラメータにして、さらに**ランダムな列 (長さ 1〜12) の網羅**を足す — 「網羅テストが長さ 9〜10 の並びを見ていなかった」
前例 (memory: `backlog-premise-needs-simulation`) への対策):

| status | dependents | 台帳 | 経過 | execution_id | → action / ledger |
|---|---|---|---|---|---|
| 判断待ちでない | * | あり | * | * | none / **delete** |
| 判断待ちでない | * | なし | * | * | none / keep |
| 判断待ち | `None` (観測できない) | * | * | * | none / **keep** (時計も進めない・消さない) |
| 判断待ち | 0 | あり | * | * | none / **delete** |
| 判断待ち | 0 | なし | * | * | none / keep |
| 判断待ち | ≥1 | なし | * | * | none / **set(first_seen=now, stage 0)** |
| 判断待ち | ≥1 | あり、execution_id が違う | * | 違う | none / **set(first_seen=now, stage 0)** (新しい試行) |
| 判断待ち | ≥1 | あり、同じ | < director | * | none / keep |
| 判断待ち | ≥1 | stage 0 | ≥ director | 同じ | director_renotice / set(stage 1) |
| 判断待ち | ≥1 | stage 1 | < telegram | 同じ | none / keep |
| 判断待ち | ≥1 | stage 1 | ≥ telegram | 同じ | telegram_notice / set(stage 2) |
| 判断待ち | ≥1 | stage 0 | ≥ telegram (停止明け) | 同じ | director_renotice / set(stage 1) (**飛ばさない**) |
| 判断待ち | ≥1 | stage 2 | * | 同じ | none / keep (**3 回目以降は鳴らさない**) |
| 判断待ち | ≥1 | あり、`first_seen` が未来 (時計が戻った) | * | 同じ | none / **set(first_seen=now)** (壊れた記録を信じない) |

**台帳に書くのは「送れた後」だけ**: `director_renotice` / `telegram_notice` の実行が失敗したら
(`tmux_send` が False・Director 不在・Telegram 送信失敗・§7)、`ledger_update` を**適用しない**
(= 同じ段階をもう一度試す)。`notify_state_once` と同じ向き (「送れなかった通知は記録しない — 戻ったらすぐ送る」)。
ただし Telegram 側は送信失敗を**繰り返し叩かない**ためのバックオフを持つ (§7 の「失敗の通知 1 回/10 分」と同じスロットルを使う)。

**台帳が `Unreadable`**: 純粋関数を呼ばず、段階上げ全体を**見送る** (`WARNING: escalation-state` を 1 回/10 分)。
`notify_state_once` の「台帳が読めなければ再送側に倒す」とは**向きが違う** — 再送側に倒すと、台帳が壊れている間
毎サイクル Telegram に送り続ける。段階上げは**鳴らさない側**に倒す (fail の向きは判定ごとに決まる。memory:
`fail-direction-is-per-judgment`)。段階 0 (既存の 1 回通知) は影響を受けない。

走査は `dispatcher.sh` の `needs_director` 検知ブロックの隣 (`all_tasks` を使い回す。別の走査をしない —
`handoff` ブロックと同じ t023 の規則) に 1 か所足し、`prune` も同じスナップショットで行う。

## 6. 送る文面の型

Telegram (段階 2) と、Director 宛の再通知 (段階 1) は**同じ項目**を同じ順に並べる (見た目の差は先頭のタグだけ)。

```
🛑 crewvia: Director の判断待ちが後続を止めています
mission: 20261004-watchdog-hard-idle-unknown
task: t017 (needs_director) — 止まって 10 分
後続 2 件が待機中: t006, t007
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
| Telegram 未設定 (env が空) | **何もしない**。ファイルも作らず、ログも出さない | 公開前提。Taskvia 非依存と同じ型 |
| `sendMessage` が失敗 (ネットワーク・HTTP エラー・`ok:false`) | **dispatcher のサイクルを止めない**。その通知は**記録せず**、バックオフ後に再試行 | 送れないことで割り当てを止めない。`notify_state_once` と同じ「送れなかった通知は記録しない」 |
| Telegram が長く落ちている | バックオフは指数 (30 秒 → 1 分 → 2 分 … 上限 10 分)。**ログは 10 分に 1 回** `WARNING: telegram unreachable (<種別>)` | 5 秒ごとに失敗を叩かない・ログを埋めない |
| 受信 (`getUpdates`) が失敗 | 何も受けなかった扱い。offset は進めない。サイクルは続行 | 次回に同じ update を受けるだけ (at-least-once) |
| `telegram-questions.json` が `Unreadable` | 転送しない・offset を進めない (§2-3) | 観測できなかったことを「答え無し」に倒さない |
| `escalation-state.json` が `Unreadable` | **段階上げを見送る** (§5-4) | 再送側に倒すと壊れている間ずっと送り続ける |
| Director 不在 (mux に `-director` が無い) | 転送・段階 1 は**見送って記録しない** (戻ったらすぐ送る)。**段階 2 (Telegram) は Director の有無と無関係に送る** | ユーザーに届けるのが段階 2 の目的で、Director が居ないほどユーザーに知らせる価値がある |
| mux が無い (インラインモード) | dispatcher が動かないので受信も段階上げも動かない。`ask_user.sh ask` は**送れる** (ボタンは押せるが転送されない) → `ask` は dispatcher の生存を確認し、**無ければ exit 4 の「受信側が居ない」** で断る | 押しても誰にも届かないボタンを出さない |

### レート制限

- 送信は**全体で 1 秒に 1 通**まで (Telegram の個人チャットの目安は 1 通/秒、1 分に 20 通)。`telegram-state.json` の `last_sent_at` で数える。
- 段階上げは 1 task ごとに最大 1 通 (段階 2 は 1 試行につき 1 回)。**1 サイクルで送る段階上げの通知は最大 3 通**
  (多数の task が同時に 10 分を超えたとき。残りは次のサイクル)。
- 質問 (`ask_user.sh ask`) は `open` が 8 件を超えたら拒否 (exit 5)。
- Bot API が 429 (`retry_after`) を返したら、その秒数だけ送信を止める (§7 のバックオフと同じ状態に載せる)。

## 8. このミッションでやらないこと (記録)

- Worker のツール実行の承認 (pre-tool-use / Taskvia)。
- `failed` + held の依存の通知を Telegram に載せること (別の入口。`[held]` ログが既にある)。
- **後続が無い最後の task が `needs_director` のときの段階上げ** (§5-2)。
- `ask_user.sh` の `open` 質問が期限切れになったときの Director への通知。
- 複数ユーザー / 複数 chat。`CREWVIA_TG_CHAT_ID` は 1 つだけ。
- Webhook (`setWebhook`)。公開サーバーが要る (公開前提で誰でも立てられる、に反する)。

## 9. 実装の分割

### PR-A: Telegram の経路

- `scripts/lib_telegram.py` (新): Bot API のクライアント (urllib、token は引数にもログにも出さない、timeout 付き、
  失敗は例外ではなく戻り値)・callback_data の生成と照合 (純粋関数)・質問台帳の読み書き (`lib_daemon_state` 経由)・
  `poll` / `send` / `ask` / `verify` / `cancel` / `list` の動詞。
- `scripts/ask_user.sh` (新): 厳格引数のラッパー。
- `scripts/lib_daemon_state.py`: `telegram_questions_problem()` / `telegram_state_problem()` を追加 (書き手も通す)。
- `scripts/dispatcher.sh`: サイクルに受信を 1 か所足す (§1 の条件。サブプロセス・間引き・未設定なら無し・`open` が 0 件なら無し)。
  転送の再送 (`forwarded=false`) もここ。
- `agents/director.md`: §16 の表に `[telegram-answer]` の行・§3-2 の規則・§3-3 の分類器の手順・§4 の併用手順。
- `config/crewvia.yaml`: `telegram:` ブロック (`poll_interval_seconds`・`session_link`・`question_ttl_minutes`)。
  CLAUDE.md の環境変数表に `CREWVIA_TG_BOT_TOKEN` / `CREWVIA_TG_CHAT_ID` / `CREWVIA_DIRECTOR_SESSION_URL`、不変条件 7 に新しい台帳。
- `.gitignore`: `registry/daemons/telegram-*.json`。
- テスト (**偽の Bot API サーバー** = `http.server` をテスト内で立て、`CREWVIA_TG_API_BASE` ではなく
  **lib の引数 `api_base`** で向ける。env の `TEST` 専用スイッチを本番コードに足さない):
  callback_data の照合 (§2-2 の 5 条件を 1 つずつ欠かした表)・二重押し・古いボタン・withdrawn 後の押下・
  chat_id 違い・message_id 違い・nonce 違い・返信でないメッセージ・offset の at-least-once・
  台帳 `Unreadable`・未設定で 1 バイトも書かない (ファイル一覧の前後比較)・token が argv / ログ / 例外文に出ない
  (token を偽の値で入れ、全出力を grep)・サブプロセスの timeout (応答しないサーバー)。
  既存の `tests/CLAUDE.md` の隔離規則 (`env -u AGENT_NAME`、`CREWVIA_MUX_TEST_ISOLATION`) に従う。
- 本番確認: 本物の bot・本物の Director で (1) `ask` → ボタン → `[telegram-answer]` が画面に届く、
  (2) §3-3 の分類器の観察、(3) §4 の modal の観察 (`AskUserQuestion` の最中に届くか)。
  **dispatcher の restart が必要** (`merged-daemon-code-is-inert-until-restart`。`scripts/sync-main-checkout.sh`)。

### PR-B: 段階上げ (PR-A の後)

- `scripts/lib_task_status.py`: `AWAITING_DECISION_STATUSES` (§5-1)。`tests/test_task_status_single_definition.py` に載せる。
- `scripts/lib_escalation.py` (新): `decide()` (純粋関数)・`blocked_dependents()`・台帳の shape (`lib_daemon_state` に
  `escalation_state_problem()`)。
- `scripts/dispatcher.sh`: `needs_director` ブロックの隣に段階上げを 1 か所 (`all_tasks` を使い回す)。段階 1 は既存の
  `tmux_send`、段階 2 は PR-A の送信 (Telegram 未設定なら段階 2 は何もしない。**段階 1 は未設定でも動く** — これだけで
  今回の事故 (Director への再通知が無い) は塞がる)。
- `config/crewvia.yaml`: `escalation:` ブロック。
- `agents/director.md`: §16 の表に段階 1 の再通知の行。
- テスト: §5-4 の表の全行 + ランダムな列の網羅 (長さ 1〜12・`decide` を畳み込んで台帳の不変条件 =
  「stage は単調増加・同じ (execution_id, stage) で 2 回鳴らない・解けたら必ず delete」を検査)・
  dispatcher の 1 サイクル harness (`dispatcher-real-code-namespace-harness`) で、`needs_director` + pending の後続 →
  5 分後に Director へ・10 分後に偽の Bot API へ・解けたら台帳が消える・台帳が `Unreadable` で鳴らない・
  破損カードで時計が進まない。**赤の実証** (`regression-test-must-prove-red`): 段階 2 の dedup を外して落ちること。
- 本番確認: 使い捨ての mission で `needs_director` + 後続を作り、config の秒数を短くして 2 段階が順に届くこと。
  (使い捨て mission の `init` は `active_missions` に即露出する — memory: `disposable-mission-init-exposes-to-dispatcher-immediately`。)

## 10. ユーザーに決めてほしいこと

1. **`ask_user.sh` と `AskUserQuestion` の併用**: 推奨は「Telegram を設定済みのときは `AskUserQuestion` を使わず、
   通常のテキストで画面に出して `ask_user.sh ask`、ターンを終える」(modal が mux send を取り込む保証が無いため)。
   画面に居るときも打ち込めば答えられる。同時に出したい (画面のボタンも欲しい) 場合は、PR-A の本番確認で
   modal の挙動を観察してから決める。
2. **後続の無い最後の task が `needs_director` のとき**に段階上げするか。推奨は「しない (今回は後続を止めた場合に絞る)」。
   する場合は「同 mission に進められる task が他に無い」を `lib_dep_rules` とは別の規則で足すことになり、
   `mission 完了を待っている` という状態の定義が新しく要る。
3. **段階 1 の既定 5 分は、段階 0 の通知からの経過ではなく「判断待ちを最初に見てからの経過」**
   (§5-3)。Director がずっと席に居ない間、5 分ごとに Director に再通知する (繰り返す) 案は採らず
   **1 回だけ** (3 回目以降は鳴らさない)。繰り返したいなら間隔を足す。

## 11. 検証 (設計時の実測)

- 受信の位置: `dispatcher.sh` のサイクルは 5 秒 (`sleep 5`, `dispatcher.sh:3180`)。
  `needs_director` の検知は `all_tasks` を 1 回走査して使い回す (`dispatcher.sh:2838-2886`)。
- 依存の判定: `lib_dep_rules.unmet_dependencies` / `held_dependencies`。dispatcher は `dependency_gate()` 経由
  (`dispatcher.sh:892`)。
- 通知の 1 回だけ: `notify_state_once()` (`dispatcher.sh:1133`)。台帳の入口は `lib_daemon_state.told_*`。
- `WAITS_ON_DIRECTOR_STATUSES = {'needs_director'}` (`lib_task_status.py:69`)。
- Telegram の仕様 (callback_data 64 バイト・1 秒 1 通・本文 4096) は設計時の知識で、**PR-A の実装時に
  公式の Bot API ドキュメントで再確認する** (この文書では実測していない)。
