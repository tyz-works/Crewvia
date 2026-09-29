# dispatcher.sh fix 反映には稼働 tab の restart が必要

## 2026-09-28 追記: 検知と同期が仕組み化された (t005 / B2 / backlog #26)

以下の「手順」節は今も正しいが、**検知は手作業でなくなった**。前 mission (mechanize-guards-a)
で Director が merge のたびに手で ff → restart を判断していた (9 回)。今は:

1. `dispatcher.sh` が起動時に自分の版 (HEAD sha + 対象ファイルの digest) を
   `registry/daemons/<name>.version.json` に記録する (`lib_daemon_watch.record_own_version()`。
   `watchdog.py` も起動直後に同じことをする)
2. `dispatcher.sh` が数分に 1 回 (既定 180 秒、`CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL` で調整):
   - `origin/main` が主 checkout の HEAD より進んでいないか (`git fetch` を挟む。失敗はその回
     「不明」として通知しない)
   - 稼働中の dispatcher / watchdog の記録と、ディスク上の対象ファイルがずれていないか
     (`lib_daemon_watch.restart_needed()`)
   のどちらかがずれていたら、Director に **1 回だけ** 通知する (`notify_state_once()` を再利用。
   状態が解消すれば次回また同じ状態が起きても再通知する)
3. 通知を受けた Director (または誰でも) が `scripts/sync-main-checkout.sh` を **1 本** 実行する:
   `git fetch` → `git merge --ff-only origin/main` → 変わったファイルから dispatcher /
   watchdog それぞれの restart 要否を判定 (`lib_daemon_watch.restart_needed()`、pull が
   起きたかどうかに関わらず「記録された版と今のディスク」を比較する) → 必要な方だけ
   `lib_daemon_watch.py restart <name>` → `status` で確認。`agents/start.sh` (Worker) /
   `agents/director.md` / `hooks/*.sh` (Director) の変更は「restart 推奨」と表示するだけで
   実行はしない (どちらもこのスクリプトが操作できるプロセスではないため)

```bash
scripts/sync-main-checkout.sh              # 実行 (fetch → ff → 必要な restart → status)
scripts/sync-main-checkout.sh --dry-run    # 何が起きるかだけ見る (fetch はするが何も変えない)
```

**禁止**: 開発・動作確認でこのスクリプトを本番の主 checkout (`/home/tkadmin/workspace/crewvia`)
に対して実行しない (`--dry-run` も含む)。動作確認は一時ディレクトリの bare origin + checkout で
行うこと (`tests/test_main_checkout_sync.py` がその形の実例)。

### 既知の限界（backlog、3 巡目、t007。PR#246）

- **`dispatcher.sh` が観測の一部失敗で同じ版ずれを再通知する（族A）**: 通知の指紋に「不明」を状態の
  変化として含めるため、`git fetch` 失敗 → 回復、を繰り返すたびに通知が出る。`record_told` が前の
  指紋のスロットルも消してしまうのが原因。直すならずれの原因（origin 側 / 版の記録側）ごとに独立に
  重複排除する。
- **`sync-main-checkout.sh` の `--repo-root` が相対パスだと壊れる（族B）**: スクリプト内で `cd` した
  後も相対パスのまま python 側 (`lib_daemon_watch.py`) へ渡ると checkout/checkout のような二重パスを
  読み、restart の要否が「不明」判定になって restart を skip する。既定の呼び方（引数なし、または
  絶対パス）では起きない。直すなら先頭で絶対パス化する。

### 戻し方

この機能 (版ずれ検知 + 同期スクリプト) を取り消す必要が出たら:

1. 導入 PR を revert する (`scripts/lib_daemon_watch.py` の `record_own_version` /
   `restart_needed` / `fetch_origin` / `commits_behind` / `changed_files_vs` /
   `affected_targets` / `DAEMON_RESTART_FILES` / `RESTART_ADVISORY_FILES` と、
   `dispatcher.sh` の `check_main_checkout_drift()` 呼び出し・`watchdog.py` の起動時
   `record_own_version()` 呼び出し・`scripts/sync-main-checkout.sh` 一式)
2. 主 checkout を `git merge --ff-only origin/main` で revert 後の状態に合わせる
3. `python3 scripts/lib_daemon_watch.py restart dispatcher` / `restart watchdog` で
   両方を revert 後のコードで起こし直す (このコミット自体が dispatcher.sh /
   watchdog.py を触っているので、通常の PR merge と同じ restart 手順がそのまま要る)
4. 以降は本節の下にある「手順」に戻って手作業で運用する

停止スイッチは意図的に付けていない (`CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL` は周期の
調整だけで、検知そのものを止める env は無い — memory:
no-env-killswitch-for-shared-rule。止める理由が本当にあるときは revert すること)。

---

## 問題の説明

`scripts/dispatcher.sh`（または依存ファイル）の fix を PR で main に merge しても、
**稼働中の `dispatcher` tab は旧コードで動き続ける**。
dispatcher tab を kill → respawn しないと fix が反映されない。

**理由**: dispatcher tab は `bash scripts/dispatcher.sh` を起動時に一度読み込んで実行し続けるプロセス。
ファイルを再読み込みする仕組みはない。`git pull` で working tree の `dispatcher.sh` が更新されても、
既に起動している bash プロセスの embedded Python (`PYEOF` ブロック) はロードされない。

**過去の誤診断事例**: mission 20260905-dispatcher-idle-worker-fix で PR #154 merge 後、
dispatcher tab を restart せずに再検証すると **OBS-1 バグが再現し続ける**ように見えた。
「fix が効いていない」と誤診断するリスクがあった。

---

## 影響を受けるプロセス

| プロセス | 再起動が必要なケース |
|----------|-------------------|
| `dispatcher` tab | `scripts/dispatcher.sh` / `scripts/lib_daemon_watch.sh` / `scripts/lib_mux.py` / `scripts/lib_daemon_watch.py` / `scripts/lib_retirement.py` の変更 |
| `watchdog` tab | `scripts/watchdog.py` / `scripts/lib_daemon_watch.py` / `scripts/lib_retirement.py` / `scripts/lib_mux.py` / `scripts/lib_pane_process.py` / `scripts/lib_task_cards.py` / `scripts/lib_daemon_state.py` の変更 |
| Worker tab | `scripts/start.sh` の変更（スキル割り当て等） |
| `Sora-director` | `agents/director.md` / `hooks/*.sh` の変更（プロンプト・hook 反映） |

> `lib_daemon_watch.py` / `lib_retirement.py` は dispatcher・watchdog 双方が起動時に
> import する共有モジュール。ここを直した PR は **両方** restart すること
> (`agents/director.md` §12 と揃えてある)。
>
> **2026-09-28 (t112 / PR#246 Codex 1 巡目 P1) 追記**: 上の表は
> `scripts/lib_daemon_watch.py` の `DAEMON_RESTART_FILES` の写しであり、そちらが
> 唯一の実装。dispatcher は毎サイクル新しい python3 を起動し直すので、その
> heredoc が import する `lib_*.py` (`lib_dep_rules.py` / `lib_task_cards.py` /
> `lib_review_refusal.py` / `lib_worker_target.py` 等) は次のサイクルで disk から
> 読み直され、restart は要らない — 表に載っていないのはそのため。dispatcher で
> restart が要るのは、常駐する bash 自身が **起動時に 1 回だけ**読む
> `dispatcher.sh` 自身と、それが `source` する `lib_daemon_watch.sh` だけ。
> watchdog は逆に単一の長寿命インタプリタなので、起動時に import した
> `lib_*.py` は最後まで disk の変更を拾わない — 直接 import はもちろん、
> それが更に import する先 (推移的) まで表に要る
> (`tests/test_daemon_restart_files_match_load_model.py` が実際の import /
> source 文と突き合わせて固定する)。

---

## restart 手順

> **⚠️ 素の `lib_mux.py kill` → `spawn` は使わないこと (t005 以降)。**
> dispatcher と watchdog は互いの生存を見張るようになった
> (`knowledge/daemon-authority.md` §7)。kill と spawn の**隙間でデーモンは本当に
> 死んでいる**ので、そこを見た相手が正しく respawn し、その直後に手で打った spawn が
> 上に乗って**二重起動**になる。dispatcher が 2 つになると同じ task が 2 人の Worker に
> 渡るので、これは restart で起こしうる一番重い事故である。
>
> 停止マーカーを挟む `restart` サブコマンドを使えば、この隙間は開かない。

### dispatcher の restart 例

```bash
# 1. main に fix を pull (worktree 外の crewvia repo root で実行)
cd /path/to/crewvia
git fetch origin main && git pull --ff-only origin main

# 2. restart (pause → kill → spawn → resume を 1 コマンドで)
python3 scripts/lib_daemon_watch.py restart dispatcher

# 3. 起動確認 (ログの最終行が "Starting dispatcher (PID ...)" になっていること)
tail -3 logs/dispatcher/dispatcher-$(date +%Y%m%d).log

# 4. 相互監視から見た状態確認
#    双方が running=[<pid>] で、PAUSED が付いていないこと。
#    running は heartbeat ではなく /proc の実走査なので、heartbeat がまだ
#    書かれていない起動直後でも正しく答える。
python3 scripts/lib_daemon_watch.py status
```

### restart が「このペインは自分のものではない」と断ったとき (t037)

```
[daemon-watch] restart dispatcher: refused — the pane is not this checkout's
to end (foreign: pid 12345 runs /other/crewvia/scripts/dispatcher.sh,
not /path/to/crewvia/scripts/dispatcher.sh). Nothing was killed.
Pass --force to override.
```

kill する前に、**そのペインで走っているのが自分の checkout のデーモンか**を
`/proc` で確かめている (`daemon-authority.md` §7-11)。断られる理由は 2 つ:

- `foreign` — 別 checkout の同じデーモンが入っている。**まず自分がどこに居るか
  を疑うこと**。worktree から叩いていないか、herdr の古い env を引きずって
  いないかを見る (`CREWVIA_HERDR_WORKSPACE` / `CREWVIA_TMUX_SESSION`)。本当に
  そのペインを引き取りたいなら `--force`。
- `unknown` — ペインの pid か `/proc` が読めない。再実行で直ることが多い。
  直らなければ `--force`。

```bash
python3 scripts/lib_daemon_watch.py restart dispatcher --force
```

`--force` はこの確認だけを飛ばす。pause マーカーもロックも従来どおり効く。

### watchdog の restart 例

```bash
python3 scripts/lib_daemon_watch.py restart watchdog
```

### 両方を同時に restart する場合

`CREWVIA_KILL_AUTHORITY` の切り替えなど、**両デーモンを同時に入れ替える**必要が
あるとき (`daemon-authority.md` §5-3) は、**先に両方 pause してから** kill する。
片方だけ pause して kill すると、生きている側が死んだ側を起こしてしまう。

```bash
TOK_D=$(python3 scripts/lib_daemon_watch.py pause dispatcher --reason "同時 restart")
TOK_W=$(python3 scripts/lib_daemon_watch.py pause watchdog   --reason "同時 restart")

python3 scripts/lib_mux.py kill dispatcher
python3 scripts/lib_mux.py kill watchdog

python3 scripts/lib_mux.py spawn dispatcher \
  "$(python3 scripts/lib_daemon_watch.py spawn-cmd dispatcher)" "$PWD"
python3 scripts/lib_mux.py spawn watchdog \
  "$(python3 scripts/lib_daemon_watch.py spawn-cmd watchdog)" "$PWD"

python3 scripts/lib_daemon_watch.py resume dispatcher --token "$TOK_D"
python3 scripts/lib_daemon_watch.py resume watchdog   --token "$TOK_W"
```

起動コマンドを手で書かず `spawn-cmd` から取るのは、`Mux.spawn()` が env を運べない
(`env=` 引数は両 backend とも無視されていたため t017 で廃止した。渡すと TypeError) ので、
`CREWVIA_MUX` をコマンド文字列に埋め込む必要があるからである。手書きすると、起こし直したデーモンだけ別の backend を向く。

> pause したまま resume を忘れると、そのデーモンは相互監視の対象外になる
> (相手が死んでも誰も起こさない)。30 分でその旨が Director に 1 度通知されるが、
> `status` に `PAUSED` が出ていないことを確認しておくとよい。

### restart 後の status は新しい世代の heartbeat を待ってから (C3 / t009)

restart 直後は記録上の heartbeat がまだ旧 pid のままで、status を出すと `recorded_instance_alive=False` に
見える (2026-09-28 21:11。15 秒後は新 pid で正常)。`sync-main-checkout.sh` は restart した daemon ごとに、
restart 前の `heartbeat-id` (`<pid> <generation>`) と時刻を控え、`lib_daemon_watch.py wait-heartbeat` で
**別の世代・restart 以降の時刻・生きている instance** の heartbeat が記録されるまで待つ (`--restart-wait-seconds`、
既定 90 秒)。上限までに記録されなければ `note_failure()` に積んで非 0 で終わる (成功に潰さない)。
`--restart-wait-seconds` は待ち時間の上限だけを変え、待つかどうかの切り替えではない。
**戻し方**: PR を revert → `scripts/sync-main-checkout.sh`。

---

## 教訓

> **「fix が効いていない」と誤診断する前に restart を確認すること。**

dispatcher / watchdog の挙動がおかしいと感じたとき、まず以下を確認する:

1. 直近で関連する PR を merge したか？
2. merge 後に該当 tab を restart したか？
3. restart していなければ restart してから再検証する

restart は **fix 効果検証の前提** であるため、mission 完了後のクリーンアップ手順にも組み込むこと。

---

## 関連

- MEMORY: `dispatcher-restart-after-merge` — この運用上の落とし穴の背景
- `scripts/lib_mux.py` — kill / spawn の実装
- `scripts/lib_daemon_watch.py` — `restart` / `pause` / `resume` / `status` / `spawn-cmd`
- `knowledge/daemon-authority.md` §7 — 相互監視の設計 (なぜ pause を挟むのか)
- `knowledge/ops.md` — その他の運用手順
