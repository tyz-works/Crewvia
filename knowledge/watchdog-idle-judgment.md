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


## 10. 利用枠切れを idle / max と別の状態にする (C2 / t005, 2026-09-30)

### 起きたこと (2026-09-27 夜)

Worker の画面が `⚠ Usage limit reached · continuing automatically at 6pm` で止まったのを、
Rule 5 も watchdog も普通の idle と読んだ。Rule 5 は 5 分おきに 5 人分発火して **2 時間で約 80 通**、
watchdog は idle×2 で Worker を終了させ、残った Worker も**リセット後に自動では再開せず**、
Haruto は止まっていた時間も max に数えられて t070 の途中で kill された。枠はアカウント単位なので
起動し直しても回復しない (memory `five-hour-limit-is-per-session` の 09-28 訂正)。

### 仕組み (定義は `scripts/lib_usage_limit.py` の 1 か所)

* **同定は位置と構造に束縛する** (文言の部分一致にしない — B6 の族 B と同じ轍を踏まない):
  (1) 通知行が**行頭の `⚠`** (ツール出力は `⎿` の下にインデント、`⏺` / `❯` 始まりは呼び出し・入力の写し)
  (2) 画面末尾側 (空行を除く最後の 14 行) (3) 直下 3 行以内に**入力欄の枠** (`╭──…` / `────…`)。
  パス・出力・引用・`cat` の中身・スクロールで上に流れた古い通知は、どれも外れる。
  ※ 実物の画面の記録がリポジトリに無かったため、fixture は Claude Code の画面の構造から組んだ。
  実物と形が違えば `detect()` の 3 条件を直す (テストの `_negatives()` が陰性の表)。
* **リセット時刻**は通知行から読む (`at 6pm` / `at 11:16 PM` / `at 18:00` / `+1h57m`)。読めなければ「時刻不明」。
  「次に来るその時刻」は**見た瞬間**に計算して記録 (`observe()`) し、同じ通知行が続く間は保つ —
  リセットを過ぎても画面の `at 6pm` は残るので、毎回計算し直すと 18:05 に「明日の 18:00」に化ける。
  残り時間の表示 (`+1h57m`) は毎分変わるので、同じ通知かの照合 (`identity()`) からは外す。
* **dispatcher (Rule 5)** `handle_usage_limit()`: idle / done / blocked の Worker (割り当てあり) の画面を
  capture し、利用枠切れなら idle-with-task を**出さない**。代わりに (1) Director に**1 回だけ**
  「利用枠切れ (リセット予定 <時刻|不明>)」 (2) リセット予定 + 120 秒で Worker に**1 回だけ**再開を促す
  (時刻不明は見え始め + 5 時間 5 分) (3) 促して 5 分経ってもなお続くなら Director に**1 回だけ**再通知。
  通知は台帳 `notified-state.json` の作法 (`notify_state_once`、key `usage-limit_<name>` /
  `usage-limit-still_<name>`、fp = 通知行の同一性)。**台帳の slug は `_daemon`** (mission の slug だと
  `prune_told()` が毎サイクル捨てて再通知になる)。解消 (表示が消えた) したら記録も台帳も畳む。
  記録: `registry/mux/<name>.usage-limit.json` (`first_seen` / `notice` / `reset_at` / `resumed_at`)。
* **watchdog** `WorkerMonitor._observe_usage_limit()`: 免除中は (a) idle で終了しない
  (`alive` / reason=`usage_limit`) (b) **max の経過時間から、免除して観測した時間を除く**。
  免除が終わった後も idle は**最後に免除した瞬間から**数える (止まっていた沈黙で、リセット後に
  再開する Worker が即 hard idle にならない)。1 回の観測で除ける長さは 300 秒まで
  (watchdog が止まっていた・mux が読めなかった空白を「利用枠切れだった」と主張しない)。
  累計はメモリだけ — monitor の `started_at` も object の生成時刻なので、再起動で max の時計ごと
  作り直される (同じ寿命)。

### 判定の向き (fail-direction)

画面が読めない・空・構造が合わない・窓が無い → **常に「利用枠切れではない」** (= 今までと同じ挙動:
Rule 5 は通知し、watchdog は idle / max で判定する)。誤検知しても、免除には**必ず上限**がある:
`lib_usage_limit.excuse_deadline()` = リセット予定 + 1 時間 (時刻不明は見え始め + 6 時間)。
上限を過ぎてもなお表示が続くなら、Rule 5 は通常判定に戻り、watchdog も通常の idle / max に戻して
Director に**1 回**通知する (`usage-limit-overdue_<name>`)。永久に黙る・永久に殺さない経路は無い。
(判定ごとに向きが違う: 「殺す」判定は殺さない側、「通知を止める」判定は通知する側。この判定は
両方に効くので、上限で必ず元の判定に戻す形にした。)

### 族ごとの掃除 (「Worker が止まって見える」を扱う全経路)

| 経路 | 利用枠切れの扱い | 処置 |
|---|---|---|
| dispatcher Rule 5 A/B (`check_rule5`) | idle-with-task / blocked を通知 | **処置**: `handle_usage_limit()` で別扱い |
| watchdog idle (soft / hard) | warn / terminate | **処置**: 免除 + idle の床 |
| watchdog max (`max_exceeded`) | 止まっていた時間も数える | **処置**: 免除して観測した時間を除く |
| dispatcher `shutdown_idle_workers` / Rule 2 blocked-stuck / no-task shutdown | 対象は**割り当てのない** idle Worker | 不処置: task を持たない Worker を退役させるだけで、枠切れの task を失わない (枠は起動し直しても戻らないが、割り当ても無いので害が無い) |
| dispatcher spawn grace / worker-vanish | pane の有無の判定 | 不処置: 画面の内容を見ない |
| `lib_pane_process` (裏の job) | プロセス木の判定 | 不処置: 枠切れの画面はプロセスと無関係。B1 の判定が先に働くときは従来どおり |
| codex-review の枠切れ (`You've hit your usage limit ... try again at`) | Kai-codex の別の枠 | 不処置: 画面ではなく kai-spawn の log に出る別経路 (memory の手当て: 時刻まで待って reset)。この task の範囲外 |

### 「読めない / 書けない / 消せない」の各経路 (t018 / Codex 2 巡目)

観測・保存の失敗が「免除が上限なく続く」か「通知が永久に出ない」に倒れる族。C2 が足したコード全体を洗った。
`Mux.capture()` は失敗を**空文字列**で返すので、「空」は「利用枠切れではない」ではなく「見えなかった」。
その区別は `lib_usage_limit.observable()` の 1 か所 (dispatcher と watchdog が共有)。

| 経路 | 失敗したとき | 向き | 上限を越える免除 | 通知が永久に出ない |
|---|---|---|---|---|
| watchdog: capture の例外 / 空 | 免除しない・その間を max から除かない。entry (確立済みの deadline) は**保つ** | 通常の idle / max | 無い | 無い |
| watchdog: 読めた画面に通知行が無い | entry を捨てる (回復) | 通常の判定 | 無い | 無い |
| watchdog: 上限超え | 免除しない + Director に 1 回 (fp = 通知行 + first_seen) | 通常の判定 | 無い | 無い (first_seen が変われば別 fp) |
| dispatcher: capture の例外 / 空 | 記録も台帳も触らない・免除しない | 通常の Rule 5 | 無い | 無い |
| dispatcher: 記録が読めない | prev=None で作り直して保存 (保存できなければ次行) | 保存成功なら次サイクルから保つ | 1 回だけ deadline が付く (壊れた記録の上書き) | 無い |
| dispatcher: 記録を書けない | 免除しない (`_save_usage_limit` が False → `handle_usage_limit` が False) | 通常の Rule 5 | 無い (タイマーを保てない間は免除自体が無い) | 無い |
| dispatcher: 記録を消せない (回復時) | 台帳キーを先に消し、記録は残す → 次サイクルでやり直し | 記録が残る | 無い (記録の deadline は保たれる) | 無い (fp が first_seen を含むので、キーが残っても別の枠切れは通知) |
| dispatcher: 台帳キーを消せない (ロック / 台帳が読めない) | `clear_told_key` が False → 記録も消さない | やり直し | 無い | 無い (同上) |
| dispatcher: 台帳に書けない (通知が記録されない) | `notify_state_once` のスロットルが直後の重複を止める | 次サイクルで再送 | 無い | 無い (送れるまで再送) |

残る 2 件 (許容・どちらも有界): (a) 記録が**壊れている**と読み直せず、その 1 回だけ新しい first_seen で
作り直して上書きする (以後は保たれる。壊れたまま毎サイクル読めないなら保存も失敗し、上の「書けない」行で
通常の Rule 5 に戻る)。壊れた記録で通知を繰り返さない (`test_a_corrupt_record_does_not_make_the_notice_repeat`)
ことを優先した。(b) 見えなかった観測が長く続いた後、同じ通知行の**本当に新しい**利用枠切れが来ても、前の
entry (過ぎた deadline) を引き継ぐので免除されず通常の Rule 5 が出る。**通知が多く出る側**に倒れるだけで、
免除が延びる側ではない。

### 記録を消す (retire する) 経路と、その根拠 (t020 / Codex 3 巡目 P1)

上の表は「読めない / 書けない / 消せない」だった。**mux の state が `unknown` / 取得失敗**の行が漏れていた:
`HerdrBackend.state()` は一時的な lookup / RPC の失敗でも `unknown` を返すので、これは回復の証拠ではない。
消すと first_seen / reset_at / resumed_at が作り直され、18:00 以降の `at 6pm` から翌日のリセット時刻が付いて
免除と通知の抑制が延びる (失敗を繰り返せば何度でも)。原則: **記録・台帳キーを消してよい根拠は「回復を観測した」ことだけ**。

| 経路 | 消すか | 根拠 (回復の観測か) |
|---|---|---|
| dispatcher `handle_usage_limit`: 読めた画面 (`observable`) に通知行が無い | 消す | 回復を観測した (画面を読んで無かった) |
| dispatcher `check_rule5`: mux state が `working` | 消す | 回復を観測した (動いている) |
| dispatcher `check_rule5`: mux state が `unknown` (取得失敗) | **保つ** | 観測できていない (t020 で修正。以前は消していた) |
| dispatcher `check_rule5`: idle / done で割り当てなし | **保つ** | 画面を読んでいない = 未観測 (以前は消していた)。次に読めた健全な画面か working で畳まれる |
| dispatcher: capture の例外 / 空 | 保つ | 観測できていない (t018) |
| dispatcher: 記録が読めない | 消さず作り直す | (既知 (a)。有界) |
| watchdog `_observe_usage_limit`: 読めた画面に通知行が無い | entry を捨てる | 回復を観測した |
| watchdog: capture の例外 / 空 / 窓が無い | 保つ | 観測できていない (t018) |

`unknown` / 空 / 例外 / タイムアウトを回復扱いにして消す経路は 0 件。消す関数は `_retire_usage_limit` の 1 つで、
呼び出しは上の 2 か所だけ (`tests/test_usage_limit.py::test_an_unknown_mux_state_is_not_a_recovery_and_keeps_the_record`)。

### 戻し方

PR を revert → `scripts/sync-main-checkout.sh` (主 checkout を ff し dispatcher / watchdog を restart)。
`registry/mux/<name>.usage-limit.json` と台帳の `usage-limit*` は**消してよい** (無い = 新しい利用枠切れとして
1 回だけ通知し直す)。記録が壊れていても通知は台帳 (通知行 + 見え始め) が 1 回に止める。
env 停止スイッチは付けていない (dispatcher と watchdog で答えが割れるため)。

### 検証

`tests/test_usage_limit.py` (70 件: 同定の表・dispatcher 1 サイクル・watchdog の模擬時計・観測/保存の失敗)。
赤の実証: `tests/red_proof_c2_usage_limit.sh` (修正前の dispatcher.sh / watchdog.py に戻すと
(1)〜(3) が赤、加えて同定・上限・床・上限外し・空 capture・保存失敗・台帳キー・mux state unknown の欠陥注入 14 件が赤)。

## 11. `unknown` が hard_idle の終了を永久に止めた件 — 出どころ・届かない warn・直し方 (調査+設計, 2026-10-04, mission 20261004-watchdog-hard-idle-unknown t001)

**この節は調査と設計だけ。コードは変えていない** (実装は t003)。再現スクリプトは scratchpad にあり、要点を下に引用する。

### 11-1. 何が起きたか (observations の実測。2026-09-28 以降 14,244 行)

01c で Worker が長く止まったまま max まで終了されなかった 2 件は、**executing ではなく `unknown` で止まっていた**
(§6 の「`run_in_background` の置き土産」仮説は外れ)。

| 件 | 経過 |
|---|---|
| Ren / t004 | 10-01 21:25 (JST) に idle_process → `unknown` へ変わり (最後の活動の約 90 秒後)、idle 1816s で soft_idle の warn、3608s で `hard_idle_but_process_unknown` の warn が **192 回 / 5898 秒**、最後は max_exceeded (idle 9536s) で terminate |
| Mateo / t021 | 同じ形 (10-03 07:48 JST)。unknown になったのは最後の活動の約 70 秒後。`hard_idle_but_process_unknown` が **225 回 / 7132 秒**、max_exceeded (idle 10772s) で terminate |

全体の数え方 (`bt2.py`):

- `hard_idle_but_process_unknown` は **この 2 件だけ** (417 行)。`hard_idle_but_executing` は **0 行**。
- 抑止された hard_idle の episode は 5 件 = unknown 2 + **awaiting_human 3** (Hugo t078・Arjun t066 は人間が応答して復帰、**Arjun t017 は 3691 秒 warn のまま max で終了**)。
- 1 サイクルだけ `unknown` に振れた行が他に 9 (Seo t004 / t011 / t040・Ren t005 / t033・Amara t023 …)。idle は小さく影響なし。= 通常運用でも unknown は時々出る。長く続くのが問題。
- 同じ期間の terminate は 7 回: hard_idle 3 (Sofia t084・Astrid t013・Ren t016、いずれも idle 3606〜3630s) + max 4 (うち 3 が上の抑止された 3 episode)。**`idle_process` で止まった Worker は 3600 秒で正しく終了している** — 穴は「木に unknown が 1 つあると」だけ。

### 11-2. unknown の出どころ — 木が unknown を返す形の全列挙と実測

`classify_process_tree()` が `unknown` を返す経路は 5 つ (`scripts/lib_pane_process.py`)。当時の木は残っていないので、
**各形を実プロセスで作って実際に unknown になるか**を測った (`repro.py` / `paths.py`。自分で立てた木だけを対象にし、mux / herdr / registry には触れていない)。

| # | 形 | 実測 | 経路 |
|---|---|---|---|
| A | **zombie の子** (回収されていない `state=Z`) | **unknown** | `/proc/<pid>/environ` が **EACCES (errno 13)** → `_origin_of` が OSError → 木全体を即 `unknown` (§9 / t082 P1 の「読めない」) |
| B | **非 dumpable のプロセス** (`prctl(PR_SET_DUMPABLE, 0)` した子。ブラウザの sandbox 子プロセス等が該当しうるが本番では未確認) | **unknown** | 同上 (environ が EACCES) |
| C | `CLAUDECODE` の無い子 (`env -i` で起動 / Claude と無関係) | **unknown** | `_origin_of` が unknown を返す (t091) |
| D | environ が空 (`execve(..., envp=[])`) | **unknown** | 同上 |
| E | pid 列挙の失敗・`/proc` の読めない (hidepid 等) | 未再現 (本番に無い環境要因) | `_proc_stat` / `iterdir` の OSError |
| F | **窓はあるが pane pid が引けない** (mux の不調。木を見ていない) | node が無い経路 (`watchdog.py` の `_process_signal` が `unknown` を返す) | `_mux.pid(name)` が None。**通知文で `mux_pid_unavailable` と理由を区別する** (t002 review 指摘 2。実装: 11-9) |

**本番に A の実例が今ある** (2026-10-04 17:00 に読み取りだけで確認): Ren の稼働中セッション (pid 3402938) に、回収されていない
`npm exec @playw` / `npm exec chrome` / `sh` の zombie が 3 つぶら下がっている (うち 2 つはセッション起動の 2 秒後 = MCP が起動直後に死んで回収されないまま
2.7 日)。3 つとも `cmdline` は空 (読める)、`environ` は **EACCES**。`_origin_of` を実 pid に当てると 3 つとも `PermissionError`。

**どの形が 01c の 2 件に当たるか** (確からしさ付き):

| 形 | 確からしさ | 根拠 |
|---|---|---|
| **A (zombie の MCP / sh の子)** | **約 70%** | ① 2 件とも idle_process → unknown が **最後の活動の 1〜1.5 分後に 1 回だけ** 切り替わり、以後 hard_idle 全期間 unknown のまま (木の中に「居続ける」ものがある)。② zombie は親が回収するまで消えないので永続する (本番の実例が 2.7 日)。③ 通常運用の unknown が 1 サイクルだけ出て消える 9 行は、zombie が回収される / 一瞬だけ居る形に合う。④ A は **Worker が止まる原因ではなく結果** と読める: MCP が落ちて回収されない = claude が止まっている (API 待ちで固まった等) と同時に起きる。2 件とも Rule 5 が **3 時間 1 度も出ていない** (dispatcher のログに Mateo の idle-with-task が無い) のは、herdr が画面を `working` と見ていた (= claude がリクエスト待ちで固まっていた) と整合する |
| B (非 dumpable の子) | 約 20% | ブラウザを使う MCP の下で起きうるが、これが永続すると**ブラウザを使った全 Worker で長い unknown** が出るはず。実測は 2 件だけ |
| C / D (env が消えた子) | 約 5% | crewvia のテストは `env -i` を多用するが、Worker の pane の中で走るのは Bash tool 経由 = 子は job。job が居れば `executing` が勝つ。長く止まった Worker が `env -i` の子だけを残す形は考えにくい |
| その他 | 約 5% | — |

**確証は持てない** (当時の木は無い)。次の 1 回で確定させる手段は 11-3 の通知に「unknown にした node の pid / state / errno」を載せること (観測の足し算。t003 に含める)。

**もう 1 つ見つかった欠陥 (A の副作用)**: OSError で **木全体を即 `unknown` にして返す**ので、**job が同じ木にあっても job が見つからない**。
docstring は「他のノードに本物の job があれば job が勝つ」と言うが、勝つのは `origin == "unknown"` の場合だけで、OSError の経路は勝たない。
実測 (`order2.py`): root の子が [zombie, `sleep` (job)] の順 (pid の若い順 = BFS の順) → **`unknown`**、[job, zombie] の順 → `executing`。
つまり zombie を抱えたセッション (本番の Ren) では、Bash tool で長いコマンドを走らせても pid の並び次第で `executing` にならず、
Rule 5 は「job ではない」と読んで通知する側に倒れる (実害は余計な idle-with-task)。watchdog は unknown も executing も殺さないので kill の向きには影響しない。

### 11-3. warn が Director に届かないこと

`scripts/watchdog.py` の `elif status == "warn":` は `_log(msg)` と `taskvia_alert()` だけ。`_notify_director` (= `make_notify_once` の `send`) は通らない。
実測の症状:

- Taskvia が無い・届かない環境では alert は黙って捨てられる。そうでなくても Taskvia は Director の画面ではなく、**Director には届かない**。
- `WARN: Mateo/t021 ... idle 1822s` の行が **サイクルごと (約 32 秒) に 1 行ずつ**ログに出る (hard_idle の warn だけで 225 + 192 行)。同じ内容の繰り返しでログが埋まり、「何が変わったか」が読めない (§4 の VerdictLogger は変化時 + 10 cycle ごとだが、この `WARN:` 行は別経路で毎回出る)。
- `soft_idle` (idle 1800s) と `hard_idle_but_*` (3600s) の両方が同じ経路なので、「終了を見送った」という重い warn が soft_idle と区別なく流れる。

**dedup の仕組みは既にある**: `make_notify_once(repo_root)` の `notify_once(key, fp, kind, slug, task, message)` (`registry/daemons/notified-state.json` の台帳。送れたときだけ記録・台帳が壊れていれば再送側)。
利用枠切れの `usage_notify_once(f"usage-limit-overdue_{agent}", fp, "usage-limit", "_daemon", agent, msg)` が同じ型の前例。
**同じ Worker・同じ試行で 1 回**にするには、`key = f"hard-idle-suppressed_{agent}"`・`fp = f"{current_execution_id}:{reason}"`
(**承認済みの変更**: 当初案の `task@started_at` は `monitor.started_at` が watchdog の再起動で変わるので使わない。試行の識別は Execution ID。
ID の無い旧形式 card だけ **card の `started_at`** (pull の時刻。試行ごとに書き直され、再起動で変わらない) に fallback する。
試行が変われば再通知、同じ試行で理由が変われば再通知、同じ理由が続く間は 1 回)。slug は `_daemon` (mission slug だと `prune_told()` が捨てる — §10 と同じ理由)。
抑止が解けた (`alive` / `terminate` / task 完了) ときは台帳キーを消す (回復を観測した場合だけ。§10 の原則)。

### 11-4. 直し方の案と比較

| 案 | 殺しすぎる側 | 殺さなすぎる側 | 評価 |
|---|---|---|---|
| (a) unknown / executing による抑止に上限 (Rule 5 の `BACKGROUND_JOB_MAX_SECONDS` と同じ型) | **増える**: 上限を過ぎた unknown を殺す = 「分からない」を根拠に kill (t074 で Director が名指しした向き)。実測では 2 件とも活動の再開は無かったので誤殺は 0 だが、原理的には観測できない Worker を殺す | 変わらない (原因は直らない。zombie があれば上限まで待つ) | 原因を直さず時間で割り切る。**executing 側には証拠が無い** (0 行) ので足す理由がない |
| (b) unknown の原因ごとに分類を直す | A (zombie) を除くのは**ちょうど正しい** — zombie は仕事をしておらず、子は親が回収済み・再親化済みで木から消えるので「見えなくなる job」は無い。B / C / D を job / infra に倒すのは**殺しすぎる**側 (job 扱いは executing = 抑止なので逆に殺さなさすぎ。B を infra に倒すと本物の job を見逃す) → **B / C / D は触らない** | A を直しても B / C / D の unknown は残る | **A だけ直す**。unknown の実測 2 件の約 70% を、kill の向きを変えずに消せる |
| (c) 抑止が長く続いたら terminate でなく **Director に通知** | 変わらない (kill は増えない) | 変わらない (止まった Worker は今までどおり max まで居るが、**3600 秒の時点で Director が気づける**) | **A / B / C / D / executing / awaiting_human の全部を拾う**。直せない形 (B・未知の形) の最後の受け皿 |
| (d) heartbeat / pane 画面の変化が無い時間で判定 | 画面の変化はスピナー・経過秒数の表示で止まらない可能性があり、固まった claude でも「動いている」に見えうる (殺さなさすぎ)。逆に TUI が静止する待ち (人間待ち) を「止まっている」と読むと殺しすぎ | — | **heartbeat は既に idle の時計に入っている** (`_last_activity_mtime` = activity + heartbeat + notification の最新)。足せるのは画面だけで、capture の誤読と §10 の「画面が読めない」の扱いを新たに増やす。実装コスト大・向きが不確か。**不採用** |

**推奨: (b) の zombie 除外 + (c) の Director 通知 (1 つの変更として出す)**。
(b) は原因 A を直し (hard_idle が本来の 3600 秒で動く)、(c) は直せない形 (B ほか) と awaiting_human まで同じ抜け道で塞ぐ。
(a) は入れない: unknown を殺す根拠にしない向きを保つ。通知で人が判断し、max (3 時間) が最後の網のまま。

**変更の向き**: (b) は `idle_process` → terminate が増える (zombie が原因だった Worker だけ・3600 秒の時点)。(c) は kill を増やさず通知だけ増える。**kill が新しく起きる条件は「zombie 以外に unknown / job の証拠が無い木で、3600 秒 idle」だけ** = 他の idle_process の Worker と同じ条件。

### 11-5. 族の掃除の範囲

| 項目 | 同じ修正に入れるか | 理由 |
|---|---|---|
| zombie を unknown に数える (§6 backlog) | **入れる** (本件の主因) | stat の state 欄 (`rest[0]`) が `Z` のノードは、分類の前に木から除く。`_proc_stat` は `(ppid, starttime, comm)` を返し state を返さないので、タプルを変えると `_direct_children()` 等の test helper が壊れる (docstring に明記)。state は別関数 (`_proc_state`) で読むか、タプルを拡張して helper を追従する (t003 が選ぶ) |
| OSError で job の探索を打ち切る (11-2 で見つけた順序依存) | **入れる** | zombie を除けば A は消えるが、B のような OSError が job より先に来る順序依存は残る。unknown を見つけても BFS を続けて job を探し、**job が見つかれば executing、無ければ unknown** に揃える (`origin == "unknown"` の経路と同じ扱い。docstring の約束どおり) |
| `job_since` の未来値 (§6 backlog) | **別 PR** | Rule 5 の上限タイマーの話で、unknown の件と原因が違う。状態ファイルが壊れた場合だけ |
| dispatcher `worker_has_background_work()` | **変更なし** | `unknown` → False (通知する) は正しい向きのまま。zombie 除外で `idle_process` を返すようになっても答えは同じ (False)。A を直した後は pid 順に依存せず `executing` が出る (11-2 の順序依存が消える) |
| Rule 5 (§7 / §8) の上限 `CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS` | **変更なし** | executing を黙る側の上限で、本件は watchdog の hard_idle 側。**木の読み方が変わる影響**: 読めないノードで即 unknown にしなくなり、job が他にあれば `executing` が出る = **executing が増える = Rule 5 が黙る側に動く**。黙りっぱなしは `CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS` (既定 30 分) が抑える (上限を過ぎれば通常の idle-with-task 判定に戻る) |
| watchdog の `WARN:` を毎サイクル出す行 | **入れる** | (c) と同じ分岐。Director 通知は台帳で 1 回、`WARN:` のログ行と Taskvia alert は suppress 理由が変わった時 + 10 cycle ごと (VerdictLogger と同じ間隔) に落とす |
| awaiting_human の通知 | **入れる (推奨)** | 同じ `hard_idle_but_*` の抜け道。Arjun t017 (3691 秒の warn → max) が実例。人間待ちが 1 時間以上続くのは Director が知ってよい |
| `max_exceeded` はプロセス層で抑制しない (§6) | 変更なし | 最後の網として残す |

### 11-6. 推奨案の過去データへの当てはめ (`bt2.py`、2026-09-28 〜 10-04 の全行)

| 観点 | 結果 |
|---|---|
| (c) の通知 (抑止が 600 秒続いたら 1 回) の件数 | **5 件 / 7 日**: Ren t004・Mateo t021 (unknown)、Arjun t017・Hugo t078・Arjun t066 (awaiting_human)。unknown + executing だけに絞れば **2 件**。executing は 0 件 |
| 通知が届くはずだった時刻 | idle 約 4200 秒の時点 (= hard_idle の warn が始まって 600 秒後)。実際の終了より Ren は約 5300 秒、Mateo は約 6500 秒早い |
| (b) zombie 除外で終了が早まる Worker | 2 件が **A だった場合に限り** hard_idle (idle 3608 秒) で終了: Ren t004 は max より **5928 秒早く**、Mateo t021 は **7164 秒早く** (合計約 3.6 時間の「止まった Worker の滞在」が消える)。A でなければ変わらない (通知だけが効く) |
| 新しく**誤って殺す**Worker | **0**。`unknown` かつ idle > 2×threshold の行は 417 行で、すべてこの 2 件の中にあり、2 件とも idle は 3608 → 9506 / 10740 と単調に伸びた (**活動が再開した行は無い**) |
| 影響を受けない側 | `idle_process` で terminate された 3 件 (Sofia t084・Astrid t013・Ren t016) は変わらない。awaiting_human で人間が戻った 2 件 (Hugo t078・Arjun t066) は kill されない (通知だけ。通知は Director の判断材料) |

**限界**: observations は `process_signal` の結果だけで木を持たないので、2 件が本当に A だったかは過去データからは確定できない。
当てはめが示すのは「推奨案が過去のどの Worker にも誤った kill を足さない」「通知なら 2 件とも 600 秒後に届く」の 2 点。

### 11-7. 付随して見つけたもの (本件の範囲外・Director に報告)

- **本番に孤児の `claude` が居る**: pid 3402938 (`AGENT_NAME=Ren`、2026-10-01 22:58 起動、ppid=1、tty なし、状態 `Rl`、**CPU 時間 2 日 19 時間 > 経過 2 日 18 時間**)。pane の外で CPU を回し続けている。上の zombie 3 つの親でもある。読み取りだけで確認し触っていない。止める・止めないは Director / ユーザーの判断。
- Arjun t017 の長い無音は `awaiting_human` で止まっていた (ログイン期限の仮説は memory にある。未確証)。

### 11-8. t003 (実装) に渡す変更範囲

1. `lib_pane_process.py`: state が `Z` のノードを `_origin_of` の前に除外 (`_proc_state` を追加、または `_proc_stat` を拡張して helper を追従)。zombie は子を持たないので `children` の伝播は不要。
2. `lib_pane_process.py`: `_origin_of` が OSError のとき即 return せず `saw_unknown = True` にして BFS を続ける (job が見つかれば `executing`)。ただし `_proc_stat` の列挙失敗 (E) は従来どおり即 `unknown` (木そのものが組み立てられない)。
3. `watchdog.py`: `hard_idle_but_*` (unknown / executing / awaiting_human) が 600 秒続いたら `make_notify_once` で Director に 1 回 (`key=hard-idle-suppressed_<agent>`、`fp=<task>@<started_at>:<reason>`、slug=`_daemon`)。通知文に unknown にした node の pid / state / errno を載せる (確証を取る手段)。解けたら台帳キーを消す。
4. `watchdog.py`: warn 分岐の `WARN:` ログ行と Taskvia alert を、理由が変わった時 + 10 cycle ごとに間引く。
5. 閾値 600 秒は `CREWVIA_*` の env にしない (規則を共有する値には停止スイッチを付けない)。`config/crewvia.yaml` の `daemons:` に既定値を置く案は t002 (承認ゲート) で決める。
6. テスト: 実プロセスの zombie を作って `idle_process` / 順序依存の 2 並びで `executing` になること (`repro.py` / `order2.py` の形)。赤の実証 (修正前に戻すと赤)。B / C / D は `unknown` のまま残ること (誤って infra に倒していない対照)。
7. **本番反映は watchdog の restart が要る** (§7 / `knowledge/dispatcher-restart-after-merge.md`)。`lib_pane_process.py` は dispatcher も import するので両デーモン同時 (`sync-main-checkout.sh`)。
8. 戻し方: PR revert → `sync-main-checkout.sh`。台帳の `hard-idle-suppressed_*` は消してよい (無い = 再通知)。

### 11-9. 実装の実績 (t003, 2026-10-04) と戻し方

承認済みの設計 (11-8 の 1〜8) に、ユーザー承認 (2026-10-04) の追加決定 4 点を入れて実装した。

| 変更 | 場所 | 要点 |
|---|---|---|
| zombie を木から外す | `lib_pane_process._classify` / `_proc_state` | state が `Z` と**読めたときだけ**外す (読めない・消滅は None = 通常の分類へ。zombie でないものを zombie と読む向きの誤りを作らない)。`_proc_stat` のタプルは変えない (`_direct_children()` 等の helper を壊さない) |
| 読めないノードで即 unknown にしない | 同上 | cmdline / environ / exe の OSError は `saw_unknown` を立てて BFS を続ける。job が見つかれば `executing`、無ければ最後に `unknown`。読めないノードの子は origin `unknown` で渡す (独立に判定)。**列挙そのものの失敗 (E) は従来どおり即 unknown** |
| unknown の診断 | `explain_unknown_tree(root_pid)` | 読み取り専用の再走査。`pid=N state=S errno=EACCES(13)` / `origin=unknown` を返す。通知文に載る (次に起きたとき A / B / C / D のどれか確定できる) |
| 見送りの通知 | `watchdog.SuppressedIdleNotifier` | `hard_idle_but_*` が `daemons.hard_idle_suppressed_notify_seconds` (既定 600) 続いたら `notify_once` で Director に 1 回。**unknown / executing / awaiting_human の 3 種 + mux_pid_unavailable**。key `hard-idle-suppressed_<agent>`・fp `<Execution ID>:<種別>`・slug `_daemon`。解けた (warn でなくなった・監視から外れた) ら `told_forget` で台帳キーを消す (消せなければ次のサイクルで再試行) |
| WARN 行の間引き | `watchdog.WarnLineThrottle` | 理由が変わった時 + 10 cycle ごと (`VerdictLogger` と同じ間隔)。warn でなくなれば初回扱いに戻る。行の末尾に `reason=` を足した |
| 設定値 | `lib_daemon_watch.WatchConfig` / `config/crewvia.yaml` | **env は付けない** (watchdog だけが読む値で停止スイッチではない。`_CONFIG_KEYS` に入れない)。既定値 600 は `WatchConfig` の 1 か所。0 以下・数でない値は既定値 |

**利用枠切れとの関係** (t002 review 指摘 1): `limit_excused` は `check_detail()` の中で idle の判定より先に効くので、利用枠切れの表示が出ている間は
zombie の有無に関わらず `alive / usage_limit` のまま (影響なし)。免除の上限を過ぎてなお表示が続くときは**従来どおり通常の idle / max 判定に戻る**
(その場合の zombie は今回の変更で木から外れ、hard_idle の判定が本来の秒数で動く)。テスト: `test_usage_limit_with_a_zombie_tree_stays_alive`。

**残る既知の穴 (通知で受ける)**: B (非 dumpable の子) / C / D は今までどおり `unknown` で、kill の根拠にしない。長く続けば 600 秒後に Director に届く。
最後の網は max (3 時間) のまま。

**検証**: `tests/test_hard_idle_suppression_unknown_zombie.py` (実プロセスの zombie / 非 dumpable / CLAUDECODE 無し + 通知・間引き・設定値・run() の配線)。
赤の実証 `tests/red_proof_hard_idle_unknown.py` (8 欠陥・約 2 分): zombie 除外・即 unknown・通知しきい値・fp・台帳の掃除・mux_pid_unavailable・WARN の間引き・run() の配線
を 1 つずつ戻すと、狙ったテストが落ちる。プロセス系の新規テストは単独 100 回で回した (結果は PR 本文)。
既存の `test_an_unreadable_wrapper_is_unknown_not_infra` / `..._cmdline_is_unreadable` は「`unknown`」から「`unknown` か `executing`」に緩めた
(ラッパーの子が job と読めるので executing が出るのが新しい正解。**守る向きは同じ: `idle_process` (= kill してよい) にならない**)。

**本番反映**: merge しても watchdog / dispatcher は再起動するまで古いコードのまま (`lib_pane_process.py` は dispatcher も import する)。
Director が `sync-main-checkout.sh` で ff + 両デーモン再起動 (3 点の証明は `knowledge/dispatcher-restart-after-merge.md`)。この task は本番を再起動していない。

**戻し方**: PR を revert → `sync-main-checkout.sh`。台帳の `hard-idle-suppressed_*` は消してよい (無い = 再通知)。
通知だけ止めたいなら `daemons.hard_idle_suppressed_notify_seconds` を大きくする (env は無い。watchdog の再起動が要る)。

**11-10. 回復の判定は永続の台帳で (PR #278 Codex P2 / t009)**: 回復 (forget) が通知済みかをプロセス内の `st["notified"]` で決めていたため、watchdog の再起動をまたぐと台帳キーが残り、同じ試行の次の見送りで通知が出なかった。**§11-13 で構造ごと置き換えた** — 通知済みかは台帳だけで決め (`plan_suppression`)、プロセス内の状態に判定を依存させない。再発防止は網羅テスト (`tests/test_suppression_notifier_model.py` の I3。再起動と削除失敗の出来事を含む)。赤の実証: `tests/red_proof_hard_idle_unknown.py` の「【1 巡目 t009】」。族の洗い出し (台帳の書き込み / 掃除でプロセス内状態に依存するもの): `usage-limit-overdue_*` の notify_once = 書くだけで掃除なし (fp で重複を止めるので再起動で二重にならない・対象外) / timeout 通知の台帳 = dispatcher の TTL prune が掃除 / `VerdictLogger`・`WarnLineThrottle` = ログのみで再起動は「初回扱い」(安全側)。

**11-11. 台帳の掃除は呼び出し経路に頼らない (PR #278 Codex P2 2 巡目 / t011)**: 掃除が失敗してプロセス内の `_pending_forget` に残ると、その Worker の `forget` が二度と来ない状況で永久に残り、次の見送りで古いキーが通知を黙らせた。**§11-13 で構造ごと置き換えた** — `_pending_forget` を廃止し、掃除は毎回台帳を読んで「居る分を消す」(`forget` = 回復・消滅の観測、`cycle` = 監視されていない Worker のキー)。失敗は次のサイクルの再計算で自然にやり直される。再発防止は網羅テスト (I3・I5)。赤の実証: 「【2 巡目 t011】」。

**11-12. zombie を飛ばすときに集めた子孫を落とさない (PR #278 Codex P1 3 巡目 / t013)**: 指摘は「`_classify` は children map を作ってから `_proc_state()` を読む。その間に包み役 (Bash tool の `bash -c` 等) が終わって zombie になると、`continue` がその子孫のキュー投入まで飛ばす」。生きている長いテストが木から消えて `executing` ではなく `idle_process` になり、**働いている Worker を hard_idle で terminate する向き**に倒れる (§11 の「kill が増えるのは zombie しか根拠がない場合だけ」を破る)。

直し: zombie 自身は判定に寄与させない (`saw_unknown` も立てない) が、**children map に載っている子は必ずキューに積む**。子に渡す parent_origin は `"zombie"`:
- `"job"` ではない — zombie の起源は読めない (環境が消える) ので、job を捏造して `executing` にしない (kill を止める向きの誤判定も作らない)。
- `None` でもない — `None` は「pane root の直接の子」の印で、`_is_session_body()` に入る。孫を session と読む誤りを避ける。
- よって子は `_origin_of()` で**独立に**判定される。cmdline の wrapper marker / environ の job シグナルがあれば job (= executing)、読めなければ従来どおり unknown 側 (kill しない)、CLAUDECODE があれば infra。

選ばなかった案: zombie の子を無条件に "job" として伝播 (job 判定の捏造。zombie の下の本物の infra を executing にして hard_idle を永久に止める = 本件の元の欠陥の逆向き)。

`_classify` の「スキップ / 早期 return / continue」の全分岐 (既に集めた子孫を落として kill 側に倒れないか):

| 分岐 | 何をする | 集めた子孫を落とすか | kill 側に倒れるか |
|---|---|---|---|
| root の `_proc_stat` が OSError | `unknown` を即 return | 子孫を集める前 (落とす物が無い) | しない (unknown は殺さない) |
| root が消滅 (None) | `no_process` | 同上 | 元から (pane が無い) |
| `/proc` の列挙 / 個別 `_proc_stat` の OSError | `unknown` を即 return | 木が組めない = 判定を放棄 | しない |
| 直接の子が無い | `no_process` | 無い | 元から |
| `pid in seen` で continue | 二重処理の抑止 | 無い (既にキュー済み・処理済み) | しない |
| **zombie で continue** | 判定に寄与させない | **落としていた → 本件で子をキューに積む** | **していた → 直した** |
| session 本体 (`_is_session_body`) | origin=session で子を積む | 落とさない | しない |
| `_origin_of` / `_is_session_body` の OSError | `saw_unknown` を立て、子を `"unknown"` で積んで continue | 落とさない (t003) | しない (unknown) |
| `origin == "job"` で return `executing` | 確定 | 残りは不要 (job が最優先) | しない (殺さない向き) |
| `origin == "unknown"` / infra | 子を積む | 落とさない | しない |

`_origin_of` が cmdline / environ の消滅 (None) で `infra` を返すのは、その pid 自身が居なくなっただけで子は children map から積まれる (落とさない)。落としていたのは zombie 分岐だけ。

検証: `test_a_wrapper_that_turns_zombie_after_the_snapshot_keeps_its_job` (実プロセスの木 + `_proc_state` の差し替えで「snapshot 時は生きている・読む時点で Z」を作る。マイクロ秒の窓は実時間で再現できない)、`test_the_children_of_a_zombie_are_not_taken_for_a_session_body`。赤の実証は `tests/red_proof_hard_idle_unknown.py` に 3 点 (子を積まない / 子に None を渡す / 通知文から mission を外す)。

**通知文 (t013 追加)**: 文頭に `mission <slug>` を入れた (task id だけでは別 mission の同名 task と区別できない。`WorkerMonitor(mission_slug=...)`、run が slug を渡す)。文末は「…ため終了を見送っています」をやめ、「終了を見送っています。理由: <理由>・該当ノード pid=… state=… errno=…。」と理由と根拠を分けた。

**11-13. 見送り通知の状態を 1 つの純粋関数にする (PR #278 Codex P2 4 巡目 / t016。設計)**: 1・2・4 巡目の指摘は同じ型 — 通知の状態が「プロセス内 (`_state` / `_pending_forget`)」と「台帳 (Worker ごとに 1 キー・fp が `<Execution ID>:<種別>` の 1 つ)」に散り、一部の経路だけ直しても別の経路が残った。4 巡目は、1 キーに fp が 1 つしか入らないため、episode の中で unknown → executing → unknown と理由が往復するたびに fp が変わり同じ理由が何度も通知される、というもの。

**episode の定義 (ここ 1 か所)**: ある Worker・ある Execution ID の「見送りが続いている区間」。
- 始まり: 見送り (`suppression_kind() is not None`) を最初に観測したとき。
- 終わり: ① 見送りでない判定 (alive / terminate など) を観測 ② Worker が監視から外れる (消滅) ③ Execution ID が変わる。終わったら、その Worker の台帳キーを全部消す。
- episode の中では**理由 (kind) ごとに最大 1 回**通知する。理由が往復しても、通知済みの理由は再通知しない。しきい値 (既定 600 秒) は episode の経過時間で、再起動すると数え直す (通知が最大しきい値ぶん遅れるだけ。台帳が重複を止める)。

**永続の状態は台帳だけ**: キーを理由ごとに分ける — `hard-idle-suppressed_<agent>@<kind>`・fp `<Execution ID>:<kind>`。「その理由は通知済みか」は台帳の fp を見れば分かる。プロセス内に判定を依存させない (`_pending_forget` は廃止。掃除は毎回台帳を読んで「居る分を消す」ので、失敗は次のサイクルで自然にやり直される)。

**純粋関数**: `plan_suppression(told, agent, kind, ident, lasted, threshold) -> SuppressionPlan(notify, forget)`。`told` は `{key: fp}` (台帳が無ければ `{}`、読めなければ `None`)。I/O をしない。`SuppressedIdleNotifier` は (1) 台帳を読む (2) `plan_suppression` に渡す (3) plan を実行する (台帳の掃除・notify_once) だけ。
- 見送りでない (kind なし。回復・消滅): その Worker の全キーを消す。台帳が読めなければ全種別のキーを消しにいく (消す側に倒す)。
- 見送り・lasted がしきい値未満: 何もしない。
- 見送り・しきい値以上: `told[key] == fp` なら黙る。そうでなければその理由を通知する。fp の Execution ID が現在と違う古い試行のキーは消す (I4: 前の試行から引き継がない)。台帳が読めなければ通知する (再送側に倒す。従来どおり)。

**実装の実績 (t016)**: `watchdog.plan_suppression()` (純粋関数。docstring に episode の定義) と、それを実行するだけの `SuppressedIdleNotifier` (`observe` / `forget` / `cycle`)。台帳キーは `hard-idle-suppressed_<agent>@<kind>`・fp `<Execution ID>:<kind>` (旧キー `hard-idle-suppressed_<agent>` は孤児として消えない — 接頭辞が一致しても `@` を含まないので触らない。台帳は消してよい = 再通知、で足りる)。コンストラクタは `has_key` / `list_keys` をやめ `read_ledger` (台帳の {key: fp}。無ければ {}、読めなければ None) 1 つにした。プロセス内に残るのは episode の開始時刻・通知文の使い回し・`_unsettled` (同じプロセスで episode を閉じた Worker。掃除が済んだと台帳で確認できるまで、台帳の自分のキーを「閉じた episode のもの」として扱う)。

**網羅テスト** (`tests/test_suppression_notifier_model.py`): 出来事 11 種 (S_unk / S_exe / S_awh / recover / restart / write_fail / delete_fail / delete_raise / new_exec / vanish / tick。故障の 3 種は次のサイクルに効く) の**長さ 5 までの全ての並び** + 出来事を絞った全列挙 3 組 (長さ 6〜7) + seed 固定のランダム列 (長さ 6〜14)。実際の `SuppressedIdleNotifier` と tmp の台帳ファイルに流し、毎ステップ I1〜I5 を assert する。全 11 種の長さ 6 は 177 万並びで重いので、深い並びは出来事を絞って稼ぐ (閉じる掃除の失敗 → 再発 → 通知は長さ 6〜7 でないと現れず、最初は長さ 5 の全列挙だけで見逃した)。約 3 分。

**既知の穴 (I2 から外している)**: 回復の掃除が失敗したまま watchdog が再起動し、同じ Execution ID で見送りが再発すると、古いキーが通知を黙らせる (掃除が成功するまで。`_unsettled` はプロセス内の状態で、再起動で失う)。掃除は毎サイクル台帳を読んでやり直すので、窓は「台帳の削除が失敗し続けている間」だけ。台帳に episode の印を持たせれば塞げるが、台帳の形を変えるので見送った。**→ §11-15 で訂正と設計**: 実測では、閉じるサイクルで 1 回削除に失敗すれば足り、再起動後は episode が終わるまで黙る。塞ぎ方は fp に無音区間の起点を足すもので、台帳の形は変えない。

**5 巡目 (t017) の指摘と、t006 で見つかった連投 (t018 で修正)**: Codex 5 巡目の P2「新しい通知を記録する前に `_unsettled` を解け」は当初 backlog (「黙るだけ」) としたが、最終レビュー (t006) で前提が違うと分かった — 回復時と再発後 ~600 秒の間、台帳の削除が失敗し続けた後に成功すると、新しいキーを記録した同じ plan で `_unsettled` が解けず、次のサイクルからそのキーを閉じた episode のものとして消して再送し、回復か max まで毎サイクル通知した。`observe` で通知する plan のとき `_unsettled` を解く 2 行で直した (削除がまだ失敗していれば古い fp が一致して黙る = 上の既知の穴に戻るだけ)。網羅テストが見逃したのは、この並びが長さ 9 で、全列挙 (長さ 5) と絞った列挙 (長さ 6〜7) の長さの外にあったため。名指しのテスト `test_a_deletion_that_recovers_after_a_relapse_does_not_renotify_every_cycle` と red_proof「【5 巡目 t017】」を足した。

**赤の実証** (`tests/red_proof_hard_idle_unknown.py`): 1 巡目 (回復の判定をプロセス内の状態に戻す)・2 巡目 (cycle が掃除をやり直さない)・4 巡目 (台帳キーが理由ごとでなく Worker ごと) の欠陥を 1 つずつ戻すと網羅テストが狙った不変条件 (I3 / I3 / I1) で落ちる。他に: 閉じた episode の古いキーを消さない (I2) / 古い試行のキーを消さない (I4) / 掃除の例外を外に出す (I5)。

**11-14. 本番確認 (t007, 2026-10-05 11:08〜)**: #278 (merge 11:05:54 JST、`18ea9d1`) が本番で動いていること、判定が設計どおりであること、見られなかったものを記録する。

| 項目 | 結果 |
|---|---|
| 走っているコードの版 | **新版**。watchdog pid 1724197 の起動は 11:07:17 (`ps -o lstart`)、dispatcher pid 1723894 は 11:07:16 で、どちらも merge の約 1 分後。`registry/daemons/{watchdog,dispatcher}.version.json` の `head` は両方 `18ea9d1…` (主 checkout の HEAD = `origin/main` と一致)。watchdog.log に `Starting Watchdog v2 (PID 1724197 …)` と、`Luna: resuming interrupted retirement at phase=sigterm_sent` (再起動をまたぐ retirement の再開) が出ている |
| 稼働中の Worker の判定 | 再起動直後の時点で監視対象は Ren (この task の Worker) だけ。Luna は再起動の前から retirement 中 (verdict ではなく `[retire]` の経路で終了した)。Ren: `alive reason=active process=idle_process awaiting_human=false`。**terminate された正常な Worker は無い** |
| 新版でしか出ない出力 | 見送り通知・間引かれた WARN 行 (`reason=` 付き) は、hard_idle が起きなかったので**まだ 1 行も出ていない** (正常な Worker だけなので当然)。「新しい reason が出た」ことの本番での確認は**未実施** |
| 本番で unknown の形を作って観測 (項目 3) | **省略**。理由: 通知まで見るには、自分の Worker を 3600 秒 (hard_idle) + 600 秒 (通知しきい値) 無活動にして unknown の木を保つ必要があり、実際の Director 通知が飛ぶ。その間 `max` (3600 秒) で自分が終了する危険もある。代わりに読み取りだけで木の分類を実測した (下) |

**木の分類の実測** (自分の claude pid 1724671 を根にして、短命の子を自分で立てた。mux / registry には触れていない):

- 素の木: `executing` (実行中の Bash tool 自身が job として見える。`idle_process` が出るのはツール呼び出しの外のとき)
- zombie の子 (`state=Z` を確認) + 非 dumpable の子 (`prctl(PR_SET_DUMPABLE, 0)` = 11-2 の形 B) を足した木: **`executing`** (旧版なら environ が EACCES で木全体が `unknown`。新版は読めないノードで打ち切らず、同じ木の job を見つけて executing になる = 11-9 の「job が見つかれば executing」)
- さらに `sleep` の job を足した木: `executing`

限界: これは job が居る木での確認で、**job の無い木で zombie だけが残る形が `idle_process` になること** (今回の主因 A の直り) は、本番では測っていない (Worker が動いている間は Bash tool 自身が job になるので、自分では作れない)。そこは `tests/test_hard_idle_suppression_unknown_zombie.py` の実プロセステストが根拠。
次に本物の hard_idle が起きたとき、通知文の `pid / state / errno` が A〜D の確定材料になる。

**11-15. 見送り通知の既知の穴 (台帳削除の失敗中に再起動 → 黙る) を塞ぐ (設計, 2026-10-11, mission 20261011-small-backlog-agent-flag-and-suppression-hole t004。実装は t006)**

**11-15-1. 穴の正確な形 (実測で §11-13 の記述を訂正する)**: §11-13 は窓を「台帳の削除が失敗し続けている間」と書いたが、実物の `SuppressedIdleNotifier` と網羅テストの Harness で並びを流すと、**削除の失敗は閉じるサイクルの 1 回で足り、再起動のあと黙りは episode が終わるまで続く**。再起動後のプロセスは `_unsettled` を持たず、Worker は監視中で見送り中なので `forget` も `cycle` の孤児掃除も来ない。誰も削除をやり直さない。

| 並び (Harness の出来事) | 長さ | 通知 | 結果 |
|---|---|---|---|
| `S_unk tick delete_fail recover restart S_unk tick` | 7 | 1 | **episode 2 が黙る** (最短の再現) |
| 同上 + `S_unk tick S_unk` | 10 | 1 | 3 サイクル後も黙ったまま。台帳のキーも残る |
| `S_unk tick delete_fail recover restart delete_fail S_unk tick` | 8 | 1 | 再発のサイクルでも削除が失敗する形。黙る |
| `S_unk tick delete_fail vanish restart S_unk tick` | 7 | 1 | 消滅 → 同じ試行で再び監視される形。黙る |
| `S_unk tick delete_fail recover restart S_exe tick` | 7 | 2 | 違う理由は黙らない。ただし古い `process_unknown` のキーが残る (後で unknown に戻れば黙る) |
| `S_unk tick delete_fail recover recover restart S_unk tick` | 8 | 2 | 再起動前に削除をやり直せた。黙らない |
| `S_unk tick delete_fail recover restart recover S_unk tick` | 8 | 2 | 再起動後に回復を観測して消せた。黙らない |
| `S_unk tick delete_fail recover restart new_exec tick` | 7 | 1 | 新しい試行は見送りでない (alive)。回復の観測で古いキーが消え、episode 2 は始まっていない (正しい) |
| `S_unk tick delete_fail recover S_unk tick` | 6 | 2 | 再起動なし。`_unsettled` が効いて黙らない |

穴が開く条件は次の 3 つの AND。どれも本番で現実にありうる。
1. episode を閉じたサイクルの `told_forget` が失敗する。主な原因は `told_lock` の 2 秒待ち切れで、dispatcher と取り合う。台帳が**読めない**ときは穴にならない: 再起動後の `plan_suppression` は `told=None` で通知する側に倒れる。
2. 削除をやり直す前に watchdog が再起動する。merge のたびの `sync-main-checkout.sh` と flap の respawn。
3. 同じ Execution ID で、同じ理由の見送りが再発する。

**11-15-2. 塞ぎ方の案**

| | A. fp に「無音区間の起点」を足す (推奨) | B. 閉じた印を台帳に書く | C. 閉じた印を別ファイルに書く (`_unsettled` の永続化) | D. 再起動後は同じ試行の残りキーを信用しない |
|---|---|---|---|---|
| やること | fp を `<Execution ID>:<kind>` から `<Execution ID>:<kind>@<起点>` にする。起点は `check_detail()` が idle の計算に使う `activity_mtime` (floor・利用枠の floor を適用した後) の整数秒。台帳キーは変えない | 閉じるとき、キーを消す代わりに entry に `closed` を書く (または fp を `…:closed` に書き換える) | `registry/daemons/` に watchdog 専用の小さなファイルを足し、閉じた Worker を記録する。消せたら外す | 起動後の最初の観測で、同じ試行のキーが残っていたら (出どころが分からないので) 居ないものとして再通知する |
| 台帳の形の変化 | なし (fp の値が伸びるだけ。`TOLD_ENTRY_FIELDS` も検証も同じ) | entry に欄が増える。または fp の意味が変わる | 台帳は変えない。ファイルが 1 つ増える (CLAUDE.md 不変条件 7 の「消してよい」一覧に足す) | なし |
| 新コードが旧キーを読む | 旧 fp (起点なし) は新 fp と一致しない。merge 時に通知済みの episode があれば 1 回だけ再通知する (再送側) | 旧 entry に印が無いので「閉じていない」= 黙る。今の穴が残るだけ | 印のファイルが無い = 閉じた Worker は居ない。今の穴が残るだけ | 再起動直後に 1 回だけ再通知する |
| rollback で旧コードが新キーを読む | 旧コードの比較 `told[key] == "<ID>:<kind>"` が一致しない。1 回だけ再通知して上書きする。古い試行の掃除 `startswith("<ID>:")` は同じに動く | 旧コードは印を読まない。`closed` の entry を通知済みとみなして黙る = 今の穴 | 旧コードはファイルを読まない。孤児ファイルが残る (消してよい) | 変化なし |
| 書き込み失敗時の倒れ方 | fp の記録失敗は今と同じで、次のサイクルで再送する (I1 の除外)。起点が観測できないとき `_last_activity_mtime()` は `now` を返し、idle≈0 で見送りにならないので、起点の無い見送りは生じない | 印の書き込みは削除と同じファイル・同じロックなので、**削除が失敗する条件 (ロック・書けない) でそのまま失敗する**。穴が塞がらない | ロックは取り合わない。それでも置き場が書けない (ディスク・権限) と失敗し、そのときは黙る側に倒れる。読めないときは「閉じた」とみなし通知する側 | 再起動のたびに、通知済みの見送り中 Worker × 理由ぶん 1 通ずつ重なる (連投側。flap なら 15 分に 3 回) |
| I1〜I5 への影響 | I1 は変えない。I2 の除外を「同じ無音区間 + 再起動」の 1 つに狭める (11-15-3) | 変えない (塞がらないので I2 の除外も残る) | I2 の除外を「印のファイルが書けなかった」に置き換える | **I1 を破る** (再起動で再通知) |
| 判定 | 外部に永続している観測 (ファイルの mtime・card の `started_at`) から episode を識別するので、プロセス内の状態に頼らない。§11-13 の「永続の状態は台帳だけ」に沿う | 却下: 失敗の原因が同じなので塞がらない | 却下: 2 つ目の永続状態が台帳と食い違う組み合わせ (印あり・キーなし等) を新たに作る。§11-13 で 1 つにまとめた状態をまた散らす | 却下: 黙りの代わりに再起動のたびの重複。I1 を変える理由が弱い |

**A が塞げる理由**: 見送りは `idle_seconds > idle_threshold × 2` のときだけ起きる。activity / heartbeat / 通知のどれかが動けば idle は 0 に戻り、見送りでなくなる (episode が閉じる)。つまり 1 つの見送り区間の中で `activity_mtime` は変わらず、回復を挟んで再発すれば必ず新しい値になる。再起動をまたいでも同じ値になる: floor は card の `started_at` (`monitoring_since`) で、ファイルの mtime もプロセスの外にある。

**A でも残るもの (I2 の唯一の例外。同じ無音区間はすでに伝えてある)**: 閉じた原因が活動ではない場合、たとえば `hard_idle` の terminate を判定したが Worker が残った、または消滅したあと同じ試行で再び監視された、で起点が変わらない場合。このとき削除失敗 + 再起動 + 再発の並びは黙る。ただし Director は**同じ無音区間・同じ理由**で通知を受けている。黙っても「Director が知らない見送り」は生じない。同じ形を再起動なしで踏んだ場合は `_unsettled` が今どおり再通知する。
- 起点が再起動で変わる例外: `_limit_floor` (利用枠切れの免除) はプロセス内の状態なので、免除を挟んだ無音区間で再起動すると起点が変わり、1 回だけ再通知する (連投側)。card に `started_at` が無い試行は floor がオブジェクトの生成時刻になる (§8 の `min()`) ので、無音区間に一度も信号が無いまま再起動すると同じく 1 回だけ再通知する。どちらも重複 1 通で、黙る側には倒れない。

**11-15-3. 推奨 (A) と t006 が守る不変条件**

実装の範囲 (t006):
1. `WorkerMonitor.check_detail()` が `activity_mtime` (floor と `_limit_floor` を適用した後の値) を monitor の属性に残す (例 `self.idle_since`。毎回上書き)。`CheckResult` の形は変えない。観測ログ・テストの 14 か所が位置引数で組み立てているため。
2. fp を作るのは 1 か所 (`suppression_fp(kind, idle_since)` か `plan_suppression` の中。どちらか一方): `f"{ident}:{kind}@{int(idle_since)}"`。`idle_since` が None (check_detail を通っていない monitor) なら今の `f"{ident}:{kind}"`。`plan_suppression` には起点を引数で渡す (純粋関数のまま)。
3. 古い試行の掃除 (`not str(v).startswith(f"{ident}:")`)・キーの形・`_split_suppression_key`・`_unsettled` は**変えない**。`_unsettled` は活動なしで閉じた episode の再発 (再起動なし) をまだ受け持つ。
4. env の停止スイッチは付けない。台帳は「消してよい = 再通知」のまま。書き手・ロックは変えない (`told_record` / `told_forget` が `told_lock` の中)。

不変条件 (§11-13 の I1〜I5 を改める。網羅テストの docstring も同じ文に):
- **I1** 同じ (Execution ID, episode, 理由) の通知は最大 1 回 (台帳への書き込みが失敗した通知は除く)。**変えない。**
- **I2** episode 内である理由の見送りが、プロセスが観測した範囲で 600 秒続き、台帳の書き込み・削除が成功していれば、その理由の通知が出ている。**例外は 1 つだけ**: その episode が前の episode と同じ無音区間 (起点が同じ) で、間に再起動を挟み、前の episode で同じ理由の通知が出ている場合 (同じ無音区間を Director は知っている)。§11-13 の「回復の掃除失敗 + 再起動 + 再発」の除外 (`blocked_episodes`) は**撤去する**。
- **I3〜I5** 変えない。

**11-15-4. 網羅テスト (`tests/test_suppression_notifier_model.py`) に足すもの**
- Harness に無音区間の時計を持たせる: `clock`。`recover` と `new_exec` で `clock = t` (活動または新しい試行)。見送りの出来事・`tick`・`restart` では動かさない。`_monitor()` は観測の前に monitor の起点の属性を `clock` にする。今は `idle_seconds=3700` 固定の CheckResult を渡しているので、起点は Harness の時計から渡す (`now - 3700` から逆算すると毎サイクル変わり、I1 を偽の赤にする)。
- 出来事を 1 つ足す: `quiet_close`。見送りでない判定 (`terminate` / `hard_idle`) で `clock` を動かさない = 活動なしで閉じる。`vanish` も `clock` を動かさない。
- `blocked_episodes` を撤去し、I2 の例外を 11-15-3 の 1 つに置き換える (前の episode と同じ `clock`・間に `restart`・前の episode で同じ理由を通知済み)。
- **今回の穴を再現する最短の並び**: `S_unk tick delete_fail recover restart S_unk tick` (長さ 7。`delete_fail` は自分ではサイクルを回さない修飾子)。いまの「cleanup-and-relapse」(`S_exe tick recover delete_fail restart`、長さ 5〜7) はこの形 (`S_exe` 版) をすでに含む。今は `blocked_episodes` で I2 から外れているだけなので、除外を撤去すればこの列挙が穴を捕まえる。長さの範囲は 7 のままで足りる。
- 新しい絞った列挙 `quiet-close-and-relapse`: (`S_exe`, `tick`, `quiet_close`, `delete_fail`, `restart`) を長さ 5〜7。残る形 (活動なしで閉じる → 削除失敗 → 再起動 → 再発) が I2 の例外にだけ入り、それ以外では通知が出ることを確かめる。
- 名指しのテスト 3 本 (列挙の長さを超える・意味を固定する):
  - `S_unk tick delete_fail recover restart S_unk tick S_unk tick S_unk` (長さ 10): 通知 2 通。黙りが episode の終わりまで続く形の留め金
  - `S_unk tick delete_fail recover restart delete_fail S_unk tick` (長さ 8): 再発のサイクルでも削除が失敗する。通知 2 通
  - `S_unk tick delete_fail vanish restart S_unk tick` (長さ 7): 活動なしの消滅。通知 1 通 (I2 の例外が効く形として固定する)
- 所要時間 (2026-10-11 実測、このファイルの 12 件で 177.7 秒): 全 11 種の長さ 5 の全列挙が 61.9 秒、`cleanup-and-relapse` 54.1 秒、`reasons-and-attempts` 34.6 秒、`failures-on-close` 23.4 秒、残りは 1 秒未満。`quiet_close` を全列挙に入れると 12 種の長さ 5 = 248,832 並びで、今の 161,051 の約 1.5 倍 (見込み約 93 秒)。新しい絞った列挙は `cleanup-and-relapse` と同じ規模 (5 種・長さ 5〜7) で約 54 秒。合計は約 4.4 分の見込み。t006 は実測し、5 分を超えるなら `quiet_close` を全列挙から外して絞った列挙だけで受ける。

**11-15-5. 赤の実証 (`tests/red_proof_hard_idle_unknown.py` に足す)**
- 「【11-15 t006】fp に起点を足さない」: `suppression_fp` (または plan_suppression の fp の組み立て) を `f"{ident}:{kind}"` に戻す → `test_every_sequence_over_a_reduced_alphabet_keeps_the_invariants[cleanup-and-relapse]` が **I2** で落ち、名指しの長さ 10 の並びも落ちる。
- 「起点に毎サイクル変わる値を使う」: `idle_since` を `now - idle_seconds` に置き換える (または Harness に合わせて `self._now()`) → 長さ 5 の全列挙が **I1** で落ちる (理由ごとに 1 回が毎サイクルの再通知になる)。
- 「I2 の例外を広げすぎる」の対照: 名指しの 3 本は通知の数を直接 assert する (I2 の例外の書き方に左右されない)。例外を「間に restart があれば許す」に緩めても、長さ 10 の並びの「2 通」が赤を出す。例外が穴を隠していないことの確認として、t006 の PR 本文に「1 つ目の欠陥を入れる → 名指しの長さ 10 が赤」を載せる。
- 既存の「閉じた episode の古いキーを次の episode の前に消さない」(`unsettled=False`) は、A の後は `recover` の再発を fp の不一致でも止めるので、`cleanup-and-relapse` の I2 では赤にならなくなる。狙い先を `quiet-close-and-relapse` の **I2** (再起動なしで活動なしの再発) に付け替える。赤のまま残ることを t006 で確認する。

**11-15-6. 戻し方**: PR を revert → `scripts/sync-main-checkout.sh`。**watchdog の restart が要る** (merge しても再起動まで古いコード。`sync-main-checkout.sh` が両デーモンを再起動する。dispatcher は fp を読まないが、まとめて再起動して構わない)。台帳に残った新形式の fp (`…@<起点>`) は旧コードでは一致しないので、通知済みの見送り中 Worker があれば理由ごとに 1 回だけ再通知して上書きされる (黙る側には倒れない)。気になるなら `hard-idle-suppressed_*` のキーを消してよい (無い = 再通知)。env の停止スイッチは無い。

**11-15-7. Director の判断が要る分岐 (推奨つき。決めなくても t006 は推奨で進められる)**
- (a) `_unsettled` を残すか: **推奨 = 残す**。A は活動で閉じた episode の穴を塞ぐが、活動なしで閉じた episode の再発 (再起動なし) はまだ `_unsettled` が受け持つ。外すと、その形でも同じ無音区間として黙る (I2 の例外を「再起動なし」にも広げることになる)。§11-13 の 5 巡目の連投は `_unsettled` から出たので、外すと状態は台帳だけになり単純になる。外すなら別 task に分け、I2 の例外の文を変える承認を取る。
- (b) `quiet_close` を全列挙に入れるか: 11-15-4 の規則 (5 分) で t006 が決める。Director の判断は不要。
