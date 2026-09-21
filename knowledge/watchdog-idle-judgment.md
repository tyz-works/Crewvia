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
3. idle = now - 最新の activity/heartbeat/notification mtime   ← 常に評価する
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

## 7. デーモンへの反映

**merge しただけでは稼働中の watchdog タブには反映されない**
(`knowledge/dispatcher-restart-after-merge.md`)。反映するには watchdog を
kill + respawn する。今回の変更は watchdog.py 単独で閉じており dispatcher 側の
変更を伴わないため、`knowledge/daemon-authority.md` §5-3 が求める
「両デーモン同時 respawn」には該当しない (kill 権限の移譲は t002)。
