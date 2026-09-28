# watchdog の idle 判定 — 到達しない欠陥とその修正

> 作成: 2026-09-21
> タスク: 20260921-daemon-authority-and-mutual-watch/t016
> 関連: `knowledge/daemon-authority.md` (責務境界)、`knowledge/worker-shutdown-rules.md`
> 対象コード: `scripts/watchdog.py`、`tests/test_watchdog_idle.py`、`tests/watchdog-idle-e2e.sh`

## 1. 欠陥

`check()` の判定順がこうなっていた:

```python
if now - self.started_at > self.max_threshold: return "terminate"  # 絶対上限
target = self._tmux_window_target()
if target is None: return "kill"
if self._has_child_processes(): return "alive"    # ← ここで必ず返る
idle_seconds = now - self._last_activity_mtime()  # ← 到達しない
```

`_has_child_processes()` は `pgrep -P <pane_pid>` が 1 件でも返せば True。
Worker のペインは常に `bash → claude → (MCP サーバー)` の木を持つので、
**生きている Worker は必ず alive と判定される**。

帰結として、task frontmatter の `timeout.idle` は**書いても効かず**、実際に
発火しうるのは絶対上限 (`max`) だけだった。watchdog が持つ 4 つの判定
(`knowledge/daemon-authority.md` §2-2 の W1〜W4) のうち、W1 (warn) と
W2 の idle 経路が丸ごと死んでいたことになる。

### 実測 (2026-09-21)

Worker Ren が PreToolUse hook で 16 分以上ハングし heartbeat が 2.5 時間
更新されない状態でも、watchdog は何も検知しなかった。
`registry/watchdog-observations.jsonl` にはその間ずっとこう残っている:

```json
{"agent":"Ren","task_id":"t002","idle_seconds":9098.3,
 "idle_threshold":1800,"check_result":"alive"}
```

しきい値 1800s に対し無音 9098s。判定材料は正しく集まっていて、
**判定に使われていなかった**だけである。

## 2. 本番のプロセス木 (判定材料の実測)

`Ren-worker` の pane_pid 3850583 を実測 (2026-09-21):

```
/bin/bash                                    et=119s   ← pane_pid
  claude --model claude-opus-5               et=119s   ← セッション。常駐
    npm exec @playwright/mcp                 et=117s   ← MCP。claude の 2 秒後
    npm exec chrome-devtools-mcp             et=117s   ← MCP
    /bin/bash -c source ...shell-snapshot    et=0s     ← Bash tool 実行中だけ現れる
```

`pgrep -P <pane_pid>` が返すのは claude 1 件だけで、これは Worker が生きている
限り常に存在する。**子プロセスの存在は「働いている」の証拠にならない。**
claude 本体も MCP サーバーも、ハング中・入力待ち・承認待ちのあいだ生き続ける。

一方で「いつ生えたか」には情報がある。MCP はセッション起動の 1-2 秒後に立ち、
tool 実行の子プロセスはそれよりずっと後に生える。

## 3. 修正後の判定

```
1. now - started_at > max          → terminate      (絶対上限。従来どおり)
2. mux 窓が無い                     → kill
3. idle = now - max(floor 以降の activity/heartbeat/notification mtime, floor) ← 常に評価する
     idle <= idle_threshold        → alive
     idle <= idle_threshold * 2    → warn
     それ以上:
       プロセス層が executing      → warn   (tool が実行中)
       プロセス層が unknown        → warn   (pane pid が引けない = 判断不能)
       未解除の Notification あり  → warn   (人間の承認/入力待ち)
       それ以外                    → terminate
```

要点は **プロセス層を「生存の証明」から「terminate の抑制材料」に降格した**こと。
idle 秒数だけが「働いていない」の根拠で、常に評価される。

### floor — 誰の沈黙かを決める (t044)

3 層のうち **2 層は agent 単位** である: `registry/heartbeats/<agent>` と
`registry/notifications/<agent>/` は Worker 名で引かれ、task では引かれない。
crewvia は同じスキルの Worker に同じ名前を継承させる設計なので、これらは
**前の task の残骸として次の Worker を出迎える**。

t044 以前はそれを今の task のシグナルとして読んでいた。pull した直後
(`<task_id>.activity` がまだ無い) の Worker が、前日 18 時間前の heartbeat で
`idle=64877s` と判定され、`elapsed=0s` で terminate された (2026-09-23 本番)。
`started_at` は「候補が 1 つも無いとき」の fallback にしか使われておらず、
その「1 つも無い」は起きなかった — 古い heartbeat が必ず 1 つあったからである。

そこで **floor = この監視対象が始まった時刻** を導入し、floor より古いシグナルは
候補から落とし、floor 自身を `max()` に入れる。floor は

    min(task frontmatter の started_at,  WorkerMonitor の生成時刻)

で、片方だけでは両方向に壊れる:

- **生成時刻だけ** — watchdog を再起動するたびに監視オブジェクトが作り直され、
  3 時間前にハングした Worker の idle 時計が 0 に戻る。再起動のたびに延命される。
- **started_at だけ** — 時計ずれや書きかけの card で未来の値が入ると idle が
  負になり、どれだけ沈黙しても alive のままになる。

floor は notification の読み取りにも同じものを当てる。通知は 1 回しか鳴らず
解除されないまま残るので、前の task の通知が `_awaiting_human()` を永久に True に
すると、今の task のハングが terminate されなくなる — idle 側と向きが逆なだけで
原因は同じである。判断がひとつなので実装も `_signal_floor()` ひとつに置く。

**agent 単位と task 単位のシグナルを分けてはいない。** floor があれば「前の task の
シグナル」は定義上 floor より古いので落ちる。分けても同じ結果になり、判定の入口が
2 つに増えるぶん食い違う余地が生まれる。残る例外は「同じ Worker 名に in_progress の
task が 2 つある」場合 (= 幽霊 task) で、そのとき生きている Worker の heartbeat が
幽霊側の監視対象を生かしてしまう。これは floor ではなく幽霊 task 側の問題なので
ここでは直さない。

**spawn 直後の猶予は入れていない。** dispatcher には 90 秒の spawn grace があるが、
watchdog 側では floor がその役を果たす — 起動直後の Worker は floor が「今」なので
`idle_threshold * 2` ぶんの猶予を自動的に得る。固定秒の猶予を別に足すと「起動直後」
の定義が 2 つになり、ずれたときにどちらが効いているのか分からなくなる。

### プロセス層の 3 値 (`classify_process_tree()`)

| 値 | 条件 | terminate への影響 |
|---|---|---|
| `executing` | セッション起動から `PROCESS_WORK_START_GRACE` (60s) より後に始まった子孫が居る | 抑制する |
| `idle_process` | claude と MCP サーバーだけ | 抑制しない |
| `no_process` | 子が 1 つも無い | 抑制しない |
| `unknown` | 窓はあるが pane pid が引けない | 抑制する (判断不能) |

基準時刻は **最も古い直下の子 (= claude) の起動時刻**。ペインの bash は
Worker より先に生まれていることがあるので、root 自身を基準にすると claude の
起動自体が "executing" に見えてしまう。比較は `/proc` の starttime (boot からの
tick) 同士で行うので、壁時計の補正やサスペンドの影響を受けない。

**60 秒の根拠**: MCP サーバーは実測で claude の 1-2 秒後に起動する。これを
「実行中」と誤読すると idle 判定が永久に抑止され、今回の欠陥がそのまま再発する。
60s は実測の 30 倍の余裕。上限側が緩いのは構わない — 誤読の向きが「殺さない」だから。

### fail closed の向き

判断が付かないときは**殺さない**。`unknown` と「人間待ち」を warn に落とすのは
そのため。`_is_mass_kill()` が設定ミスを N 体の死と誤認しないのと同じ向きに倒す。

### 「人間待ち」の判定

Notification hook は承認待ち・入力待ちで発火するが **1 回しか鳴らない**。
その後は activity も heartbeat も止まるので、無音の理由がハングでも人間待ちでも
idle_seconds は同じように伸びる。区別できるのは「最後の通知より後に実活動が
あったか」だけ。

比較相手は必ず **notification を除いた** activity / heartbeat の mtime にする
(`_non_notification_mtime()`)。`_last_activity_mtime()` は通知自体を候補に含むので、
これと比べると常に「解除済み」に見えて抑止が効かなくなる。逆に解除判定を落とすと
古い通知が永久に terminate を抑止して watchdog が無力化する。両方向を
`tests/test_watchdog_idle.py` の `test_awaiting_human_*` /
`test_notification_cleared_by_activity_allows_terminate` で固定している。

## 4. 観測性

### ログの置き場が変わった

| | 旧 | 新 |
|---|---|---|
| watchdog | `registry/watchdog.log` (単一ファイル・無限に伸びる) | `logs/watchdog/watchdog-YYYYMMDD.log` (日次) |
| dispatcher | `registry/dispatcher.log` | `logs/dispatcher/dispatcher-YYYYMMDD.log` (日次) |

**`registry/dispatcher.log` が 2026-09-05 で止まって見えるのは壊れているからではない。**
dispatcher はとっくに `logs/dispatcher/` の日次ファイルへ移行しており、そちらは
今日の分まで正常に出ている。古いパスに残ったファイルが「ログ経路が壊れている」と
誤読される原因になっていた (このタスクの起票理由の 1 つがまさにそれ)。

同じ誤読を仕込まないよう、watchdog は起動時に旧パスへ引っ越し先を 1 行だけ
書き残す (`_leave_legacy_log_pointer()`)。再起動のたびには伸ばさない。

### 判定が毎回ログに残る

旧実装は warn / terminate / kill のときしか `_log()` を呼ばなかった。その非 alive が
一度も起きなかったため、`registry/watchdog.log` は 2026-09-18 の起動行以降が空で、
30 秒ごとの判定が 1 行も残っていなかった。これでは QA も本番運用も判定を検証できない。

`VerdictLogger` が次の規律で書く:

- 判定が**変わった**瞬間は必ず 1 行
- 同じ判定が続く間は `VERDICT_SUMMARY_EVERY` (10 cycle = 5 分) ごとに 1 行 (`still alive (N cycles)`)

```
[verdict] Ren/t016 warn idle=41s idle_threshold=30 max_threshold=99999 \
          process=idle_process awaiting_human=false reason=soft_idle
```

判定の根拠 (idle 秒数・しきい値・プロセス層・人間待ち・理由) が同じ行に載るので、
「なぜ terminate しなかったのか」を後から追える。
`registry/watchdog-observations.jsonl` にも `reason` / `process_signal` /
`awaiting_human` を足した (こちらは従来どおり観測専用で、判定には一切関与しない)。

## 5. 検証

- `tests/test_watchdog_idle.py` — 17 ケース。RED 群はプロセス層をスタブせず
  **実プロセス木**を立てて通す。スタブすると「子プロセスが居るのに idle を
  無視する」欠陥そのものを迂回してしまい、修正前でも通ってしまう (実際に一度
  そうなった)。
- `tests/watchdog-idle-e2e.sh` — 隔離環境で本物の watchdog.py をデーモンとして
  起動し、warn / terminate / 実行中の抑制を実証する。シナリオ 4 は **origin/main の
  watchdog.py を同条件で走らせる対照実験**で、`observations` に
  「hard idle 超過なのに alive」の記録が残ることまで確認して空振りを防いでいる。

隔離の方法: `--repo-root` に `mktemp -d`、`scripts/` に watchdog.py のコピーと
**偽 lib_mux.py** を置く。watchdog は自分の隣から lib_mux を import するので、
本番の herdr / tmux には一切接続しない。監視対象は自分で spawn した sleep の木。

## 6. 残る課題 (このタスクでは直さない)

- **`config/timeout-profiles.yaml` を watchdog が読んでいない。** watchdog.py 側に
  3 つだけハードコードされた `PROFILES` があり、yaml の 9 profile と skills
  マッピングは参照されない。実際に効くのは task frontmatter の `timeout:` と
  既定の feature_impl (idle 300 / max 3600) だけ。queue 49 タスク中 31 件は
  `timeout:` を明示しており、`worker_profile:` の利用は 0 件。
  素直に繋ぐと `quick_edit` の idle 120s が現行既定 300s より**短い**ため
  誤 terminate が増える方向になる。skills が複数一致するときの解決順を含め設計判断が要る。
  yaml 側の誤った記述 (「Watchdog v2 はこのファイルを読み取り…」) は t016 で訂正済み。
- **絶対上限 (`max`) はプロセス層で抑制していない。** 実行中でも max 超過なら
  terminate する (従来どおり)。`started_at` が monitor 生成時刻である点を含め、
  `knowledge/daemon-authority.md` §4-3 で backlog 送りと決まっている。
  長時間タスクは frontmatter の `timeout.max` で明示的に上げる運用のまま。
- **`run_in_background` の置き土産。** Worker が長寿命の子プロセスを残すと
  永久に `executing` と読まれ terminate が抑止されうる。倒れる向きは安全側
  (殺さない) なので許容するが、`setsid` で切り離されたプロセスは子孫ではなく
  なるため実際に該当するケースは限られる。
- **zombie の子を「不明」に数える (backlog、8 巡目、t003)。** `lib_pane_process.py` は回収されて
  いない子 (state=Z) の cmdline / environ が空であることを「不明」として扱い、そのセッションの
  hard-idle 終了を絶対上限まで止める。kill しない側 (安全側) の帰結で、子を回収しないほど
  ハングしたセッションでだけ起きる。直すなら stat の state 欄で Z を分類の前に除く
- **`lib_daemon_state.py` が未来の `job_since` を受け入れる (backlog、8 巡目、t003)。** 1e100 や
  時計の巻き戻りで書かれた値をそのまま信用すると、Rule 5 の `BACKGROUND_JOB_MAX_SECONDS` 上限
  (§9) が永久に効かなくなる。隣の `notify_cache_problem` は妥当性を検証しているのに、こちらは
  していない。状態ファイルが壊れた場合にだけ起きる話で、このミッションの findings には無い

## 7. デーモンへの反映

**merge しただけでは稼働中の watchdog タブには反映されない**
(`knowledge/dispatcher-restart-after-merge.md`)。反映するには watchdog を
kill + respawn する。今回の変更は watchdog.py 単独で閉じており dispatcher 側の
変更を伴わないため、`knowledge/daemon-authority.md` §5-3 が求める
「両デーモン同時 respawn」には該当しない (kill 権限の移譲は t002)。

## 7. 裏の shell / monitor は「止まっている」ではない (B1 / #27, 2026-09-27)

長いテストを `run_in_background` / Monitor で待つ Worker は、待っているあいだツールを呼ばない。
dispatcher の Rule 5 は herdr の agent_status だけを見ていたので、これを止まっていると読んで
`[Rule 5] idle-with-task` を約 2 分おきに Director へ送り続けた (mission 20260926-mechanize-guards-a
では Director が手でツールを 1 回使わせて回避した)。

### 実測 (自分の pane。読み取りのみ)

| 状況 | herdr `agent_status` | 画面末尾 | claude の直下 | `classify_process_tree(pane_pid)` |
|---|---|---|---|---|
| `run_in_background` の `sleep` が生存 | **`idle`** | `1 shell` | `bash -c ... eval 'sleep 300'` | **`executing`** (50 秒間ずっと) |
| Monitor が生存 (出力なしで 120 秒) | **`done`** | `1 monitor` | `bash -c ...` | `executing` |
| どちらも終了 | `working` に戻る (次のツールで) | — | MCP の `npm exec` だけ | `idle_process` |

- agent_status は `idle` / `done` — **どちらも Rule 5 の条件 B (`st in (idle, done)` + assignment) に当たる**。
- 根拠に選んだのはプロセス木。画面末尾の `N shell(s)` / `N monitor(s)` は claude の表示文言で、
  版で変わりうる。プロセス木は watchdog が t016 から使っている信号と同じで、shell と Monitor の
  両方が同じ形 (claude の直下に後から生える `bash -c`) で見える。
- 前景のツール実行と裏の job は木の形が同じ (どちらも `bash -c`)。区別する必要は無い —
  どちらも「Worker は待っているだけで止まっていない」。

### 変更

- `classify_process_tree()` を `scripts/lib_pane_process.py` に移した (watchdog.py にあったものを
  そのまま。分類の定義は 1 か所)。watchdog.py は import して使う。挙動は変えていない。
- dispatcher の Rule 5 (`check_rule5()`): 条件 B (idle/done + assignment) のとき、
  `worker_has_background_work()` が真なら **`working` 扱い** (通知済みの印を外し、state entry の
  grace を測り直す)。job が終わったあとの grace は終わった時点から数える。`blocked` (承認・質問待ち)
  は裏の job があっても通知する (待っているのは人間)。/proc の走査は条件 B のときだけ。
- watchdog は**変更なし**: プロセス層 (§3) が既に hard idle を `warn / hard_idle_but_executing` に
  落とす。実プロセス木を通した回帰テストと、上限 (max) が裏の job があっても効くテストを足した。

### fail の向き — 判定ごとに違う (memory: fail-direction-is-per-judgment)

| 判定 | 観測できない (`unknown` / pane pid が引けない / 例外 / `no_process`) | 理由 |
|---|---|---|
| Rule 5 の「裏の job あり」 | **通知する** (= 従来どおり) | 黙る誤りは詰まった Worker を隠す。通知する誤りは Director に 1 通余計に届くだけ。例外は WARNING を残す |
| watchdog の hard idle | **殺さない** (`hard_idle_but_process_unknown` → warn) | 殺す誤りは取り返せない (§3) |

### 上限 — 裏で永久に止まった job

Rule 5 は裏の job がある限り黙るので、job が終わらない Worker を通知では拾えない。
拾うのは watchdog の絶対上限 (`max`) — **裏の job があっても効く** (`check_detail()` の 1 番目。
プロセス層では抑制しない)。frontmatter の `timeout.max` を大きくした task は、その分だけ
Rule 5 の通知も遅れる。

### 戻し方

デーモンの挙動を変える (dispatcher の Rule 5 と、watchdog の import 元)。共有規則なので env の
停止スイッチは付けていない。戻すときは **PR を revert → 主 checkout を
`git merge --ff-only origin/main` → `python3 scripts/lib_daemon_watch.py restart dispatcher` と
`... restart watchdog`** (merge しただけでは動いている daemon は古いコードのまま。
`knowledge/dispatcher-restart-after-merge.md`)。戻すと Rule 5 は再び agent_status だけを見る
(裏の job 待ちの Worker に idle-with-task が届く)。watchdog の挙動は戻しても変わらない。

### 検証

- `tests/test_background_work_is_not_idle.py` — 実プロセス木 (本物の sh の親子) + 本物の
  dispatcher.sh 埋め込み python の `check_rule5()` + 本物の `WorkerMonitor.check_detail()`。
  差し替えるのは mux (状態と pane pid) だけ。
- `tests/red_proof_b1_background_work.sh` — 欠陥を 10 通り注入して全部赤になることを確かめる。
- `tests/watchdog-idle-e2e.sh` は手動・CI 外で、対照実験が origin/main の watchdog を読むため
  もう本来の意味を持たない (先に古くなっていた)。lib 移設に合わせて `PROCESS_WORK_START_GRACE` の
  書換先だけ追従させた。

## 8. 判定根拠を「何であるか (comm)」から「誰が起動したか」に移す (t074, 2026-09-27)

§7 の comm ベースの判定 (t065) は、**3巡目の Codex review で穴が向きを変えて残った**:
本番の `npm exec @playwright/mcp` は npm が `process.title` を書き換え、かつ `sh -c "playwright-mcp"`
を挟むため、comm が `npm exec @playw` (15 文字打ち切り) や `sh` になり、`_SHELL_COMMS` /
`_KNOWN_INFRASTRUCTURE_COMMS` の許可リストでは job と誤読される (偽陰性: MCP が生きている間
Rule 5 が永久に黙る)。「同定できないものは job 側に倒す」(族C) というコメントも、実際には
`executing` が通知/terminate を**抑制する**側なので、書いてある向きと逆だった。

### 実測 (2026-09-27, 本番プロセス比較)

| プロセス | comm | Bash tool のラッパー (`.claude/shell-snapshots/snapshot-` を cmdline に持つ祖先) があるか |
|---|---|---|
| Playwright MCP (`npm exec @playwright/mcp`) | `npm exec @playw` | **無い** |
| chrome-devtools MCP | `npm exec chrome` / `sh` | **無い** |
| Playwright の node 本体 | `MainThread` | **無い** |
| Bash tool (前景 / `run_in_background`) | 様々 | **有る** (直接の親が `bash -c source .../shell-snapshots/snapshot-....sh && eval '...'`) |
| Monitor | `bash` | **有る** (Monitor 自身がその形で生える) |

comm では見分けられない (MCP 側にも `sh` / `bash` が出る) が、起動元では綺麗に分かれる。

### 変更

- `classify_process_tree()` の判定基準を「祖先 (自分自身を含む) の cmdline に Bash tool /
  Monitor の shell snapshot wrapper が現れるか」に一本化。`_SHELL_COMMS` /
  `_KNOWN_INFRASTRUCTURE_COMMS` (comm の許可リスト) は撤去。マーカーを持たないものは
  (名前を知っているかどうかに関わらず) 常に job ではない — 族Cの「許可リストに無い名前を
  どちらに倒すか」という問い自体が無くなった。
- `/proc/<pid>/cmdline` を読む `_proc_cmdline()` を追加 (`_proc_stat` の comm は 15 文字で
  打ち切られ判定に使えない)。cmdline が読めない 1 ノードは「job ではない」に倒して走査を
  続ける (`_proc_stat` の「消滅以外は re-raise して `unknown` に倒す」= ツリー構築失敗とは
  別の話。1 ノードの分類失敗はツリー構造を壊さない)。
- テストの fixture は symlink で comm を偽装する方式 (`_fake_binary`) をやめ、本物の bash の
  `exec -a` で argv[0]/cmdline を書き換える方式にした (Bash tool wrapper 形は実際の shell
  snapshot ファイルを `source` する形をそのまま再現)。

### Rule 5 の「裏の job だから黙る」に上限を足す (t074 追補, Director 実例 2026-09-27 23:15〜23:45)

Ren の `while pgrep -f "<script>" > /dev/null; do sleep 15; done` は、Claude Code が
コマンド全体を `bash -c '… eval …'` に包むため **`pgrep -f` がループ自身の cmdline に
自己一致**し、赤の実証が終わった後も終わらず 15 分以上回った。このループは Bash tool の
ラッパーの子孫なので、起動元判定では (正しく) job と分かる — が、job の**中身が進んでいるか**
は起動元だけでは分からない。「末端が `sleep`/`pgrep`/`tail -f` なら job でない」という案は
採らなかった: 正当な CI 待ちループ (`while ...; do sleep 20; done` で `gh pr checks` を呼ぶ
パターン。本番で常用) も末端は同じ `sleep` になり、これを「job でない」にすると正当な長時間
待ちを常に誤検知する ([[time-as-proxy-flips-false-positive-to-false-negative]] と同じ罠)。

代わりに、watchdog の絶対上限 (`max_threshold`) と同じ考え方 (「プロセス層はこの上限だけは
抑制しない」) を dispatcher の Rule 5 にも 1 つ足した: `BACKGROUND_JOB_MAX_SECONDS`
(既定 1800 秒 = 30 分。`CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS` で上書き)。job が
**連続して見え続けている時間** (`registry/mux/<name>.job-since.json`。grace の `since` とは
別のタイマー — grace は job があるあいだ毎サイクル測り直されるので流用できない) がこれを
超えたら、たとえ本物の job が生きていても通常の idle-with-task 判定に進む。デフォルトは
本番で観測されている正当な長時間実行 (pytest 一式が約 25 分。`tests/CLAUDE.md`) より長く、
watchdog の絶対上限 (既定 3600 秒) より短い。「本当に進んでいるか」を判定しようとはしない
(その代理指標はまた別の穴を生む) — 上限は「黙る方向には倒さない安全弁」であって「進捗検知」
ではない。memory `pgrep-self-match-wait-loop-hangs-worker`。

### 検証 (t074)

- `tests/test_background_work_is_not_idle.py` — 本物の起動経路 (`exec -a` による cmdline
  書き換え、実際の shell snapshot ファイルを `source` する wrapper) で偽陽性・偽陰性・watchdog
  の 3 方向 + BACKGROUND_JOB_MAX_SECONDS の受入条件 (上限前後・grace との独立性・job 終了時の
  クリア) を固定。`tests/test_watchdog_idle.py` の comm ベース時代のテスト 2 本も同じ方式に
  書き換えた。
- `tests/red_proof_b1_background_work.sh` — case O/P を新しい定義に合わせて書き換え、
  BACKGROUND_JOB_MAX_SECONDS 用の case Q/R を追加。全 18 件が赤になることを確認。

## 9. t074 の族の掃除が漏らした 2 か所 + e2e の追従 (t082, Codex review 4巡目)

t074 は `_proc_stat` (族A) を確認したが、隣の `_proc_cmdline` と dispatcher 側の新設
タイマー (`_load_job_since` / `_save_job_since`) には同じ監査が及んでいなかった。

### [P1] `_proc_cmdline` の読み取り失敗を「マーカー無し」に潰していた

当初の実装は `_proc_cmdline` の `OSError` を（消滅かどうかに関わらず）全部 `None` にし、
呼び出し側はそれを「マーカーが無い = job ではない」として扱っていた。読めないノードが
**Bash tool のラッパー自身**だと、本物の job のマーカーが誰にも見えなくなり、木全体が
`idle_process` に化ける — watchdog はそれを見て hard-idle による terminate を許して
しまう (「観測できないなら kill しない」という要件に反する)。

`_proc_stat` と同じ基準に揃えた: 消滅 (`FileNotFoundError`/`ProcessLookupError`) だけを
「マーカー無し」と同じ扱いにし、それ以外は `classify_process_tree` 全体を `unknown` に
倒す (族A と同じ形の欠陥が、時刻から起動元への作り直しの後も違う関数で再発した — memory
`evidence-for-destructive-decisions` と同根)。

### [P2] `_load_job_since` / `_save_job_since` の「無い」と「読めない」の混同

`_load_job_since` は「まだファイルが無い」(is_missing, 正常) と「ファイルはあるが壊れて
いる/形が合わない」(is_unreadable, 異常) を同じ `None` に潰していた。`_save_job_since`
も書き込み失敗を握り潰し、呼び出し側は「保存できた」ものとして扱っていた。

registry/mux が壊れている等でこのタイマーを**確実に保てない**環境では、毎サイクル
「初めて見た」と読み直して `now` を新規タイマーに採用し続け、**`BACKGROUND_JOB_MAX_SECONDS`
が永久に切れない** — t074 で足した安全弁そのものが、黙る側に壊れる欠陥を持ったまま
マージされていた (P1 で見せたパターンと同根: 「読めない/保てない」を「正常な初期状態」に
潰さない)。

`_load_job_since` は `(job_since, reliable)` を返すよう変更し、`_save_job_since` は
書き込み成否を bool で返す。呼び出し側は `reliable` が False (読めない、または保存に
失敗した) なら上限判定を信用せず、通常の idle-with-task 判定に流す (「タイマーを
確実に保てないときは通知を許す」)。

### 族ごとの掃除 (t082 で追加した/確認した読み書き失敗点、全経路)

| 箇所 | 読み書き | 失敗の潰し先 | 影響する判定 | 向き |
|---|---|---|---|---|
| `lib_pane_process._proc_stat` | `/proc/<pid>/stat` 読み | 消滅→無し、それ以外→`unknown` (既存, t049) | Rule5 (通知)・watchdog (kill) | 両方とも安全側 (Rule5=通知, watchdog=殺さない) |
| `lib_pane_process._proc_cmdline` | `/proc/<pid>/cmdline` 読み | 消滅→マーカー無し、それ以外→`unknown` (t082 で修正) | 同上 | 同上 (修正前は「マーカー無し」に潰し、watchdog 側が危険な方向に壊れていた) |
| `/proc` 列挙 (`Path("/proc").iterdir()`) | 列挙 | 失敗→`unknown` (既存) | 同上 | 安全側 |
| dispatcher `_load_job_since` | `<name>.job-since.json` 読み | 無い→新規 (reliable=True)、壊れている→信用しない (reliable=False, t082 で区別) | Rule5 の BACKGROUND_JOB_MAX_SECONDS | 安全側 (信用しない→通常判定へ) |
| dispatcher `_save_job_since` | 同上 書き | 失敗→呼び出し側に bool で伝える (t082 で修正) | 同上 | 修正前は握り潰し (黙る方向に壊れていた)、修正後は安全側 |
| dispatcher `_load_state_entry` | `<name>.state.json` 読み | 無い/壊れている、どちらも `{}` (既存, t021) | Rule5 の grace 計測 | **意図的に区別しない** — grace が最初からやり直しになる = 通知が「遅れる」側 (`knowledge/empty-vs-unobservable.md` §2 の I)。書き込みが恒久的に失敗し続けると grace が永久に満了しない同型の穴が理論上あるが、その環境では registry/mux 全体 (mux spawn 記録・retirement marker 等) が書けなくなっており、Rule5 単体より広い障害として別経路で顕在化する。**このタスクの findings に無いので変更していない** — 変更するなら別 task |
| dispatcher `worker_has_background_work` | `_mux.pid` / `classify_process_tree` 呼び出し | 例外→`False` (既存, t074) | Rule5 | 安全側 (通知する) |
| watchdog `_process_signal` | `_mux.pid` | `None`→`"no_window"` (既存) | watchdog | 安全側 (殺さない) |
| watchdog `_last_activity_mtime` / `_notification_files` | activity/notification/heartbeat の `stat` | ENOENT のみ「無い」、それ以外は unobservable (既存, t016 族A) | watchdog | 安全側 |

族B (パスの相対/絶対解決の食い違い) はこのタスクの対象に無い (t070 固有の話)。
族C (許可リストで未知のものをどちらに倒すか) は t074 で許可リスト自体を撤去して解消済み
— t082 で新たな族C 相当の分岐は増えていない。

### 検証 (t082)

- P1: `test_an_unreadable_wrapper_is_unknown_not_infra` (classify レベル) +
  `test_watchdog_does_not_terminate_when_the_wrapper_cmdline_is_unreadable` (watchdog
  受入条件そのもの)。
- P2: `test_an_unreadable_job_since_file_does_not_suppress_forever` (壊れたファイル) +
  `test_a_job_since_write_failure_does_not_suppress_forever` (書き込み失敗。
  `registry/mux` を一時的に read-only にして実際に書き込みを失敗させる)。
- P2-3: `tests/watchdog-idle-e2e.sh` を最後まで走らせて緑にした。t016 (この e2e が
  書かれた時点) 以降に増えた依存 (`lib_retirement` / `lib_daemon_watch` /
  `lib_daemon_state` / `lib_task_cards`。t002 のリタイアマーカー方式) をコピーし忘れて
  いて `ModuleNotFoundError` で即落ちていた。さらに t044 の「floor」機構 (`monitoring_since
  = min(pulled_at, self.started_at)`) に `started_at` が無いと `self.started_at` (ほぼ
  「今」) が使われ、あらかじめ古くした activity ファイルが effectively 無視される
  ため、fixture の task card に `started_at` を足した。終了経路も t002 で
  retirement marker 方式 (要 plan.sh / mission.yaml) に変わっていたため、
  `CREWVIA_KILL_AUTHORITY=dispatcher` (rollback モード = t002 以前と同じ直接 kill) で
  この e2e が検証したいプロセス層の判定だけを隔離して見られるようにした。シナリオ4
  (対照) は「plain な idle 木ですら terminate しない origin/main」を見せていたが、
  それは main が t016 の修正を既に持つので現在は成立しない (対照として空振り) — 「B1 が
  無いと実行中の Worker も terminate してしまう」という、この PR が実際に守っている
  実害に置き換えた。
- `tests/red_proof_b1_background_work.sh` に case S (P1) / T・U (P2) を追加。全 21 件
  (baseline 含む) PASS。
