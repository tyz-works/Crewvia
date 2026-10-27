# PR の CI の見える化（`lib_ci_status.py`）

Worker が PR の CI を待っている間、その進み具合を Director とユーザーが crewvia の画面から見られるようにする（2026-10-27 ユーザー要望）。
**見える化だけ**。Rule 5 を黙らせる・watchdog の見送りを CI の状態で変える、といった判定には使わない（別の PR で扱う）。

## 取得（dispatcher の 1 か所）

- dispatcher のサイクルの `run_ci_poll()`（`dispatch()` の後・成否から独立）→ `lib_ci_status.poll_cycle()`。
- 対象: active mission の task のうち status が `in_progress` / `ready_for_verification` / `verifying`（`lib_ci_status.POLL_STATUSES`）。
- PR の見つけ方: card の `pr_number`。無ければ **Git Policy Resolver**（`lib_git_policy.task_branch`。card の `task_slug` から。式をコピーしない）の branch で
  `gh pr list --head <branch> --state open --json number,isCrossRepository`。**fork から来た同名 branch の PR は数えない**。2 つ以上なら決められない = `unknown`。
  branch に PR が無ければ `no_pr`（表示しない）。`target_dir` の task は gh を target_dir の中で呼ぶ（PR はその repo のもの）。
- 状態: `gh pr view <N> --json headRefOid,statusCheckRollup`。
- 間隔: task ごとに `daemons.ci_poll_seconds`（既定 60 秒。`config/crewvia.yaml`。正の整数だけ）。**env は付けない**（見える化の周期であって、
  dispatcher と plan.sh が答えを揃える共有規則でも停止スイッチでもない。止めたいときは記録を消しても次で取り直すので、止める手段は PR revert）。
- 1 サイクルの上限: 問い合わせる task は 4 件まで（記録の古い順）・gh を呼ぶ時間の合計 20 秒（残りが 1 秒を切ったら次を始めない）・gh 1 回 15 秒（残り時間で切る）。
  締切はサイクルに 1 つで、**gh の 1 回ごとに**残りを計り直す（`pr_number` の無い task は list と view の 2 回。2 回目は 1 回目の後の残りで切り、残りが 1 秒未満なら呼ばず `unknown (cycle_budget)`）。
  dispatcher の stale しきい値（既定 60 秒）より十分小さい。同じ PR（実装 task と review task）は 1 サイクル 1 回だけ引く
  （PR が決まった時点で引く = card の `pr_number` でも、branch から解決した PR でも。#314 r2）。
- card の `pr_number` と記録の `pr` が違う（PR を作り直した・後から `pr_number` が付いた）記録は、間隔を待たずに取り直す（#314 r2）。
- `gh` は subprocess + timeout。**失敗（exit≠0）・timeout・gh が無い・JSON が読めない・頼んだ欄が無い・形が違う → `unknown`**（理由の固定語を `reason` に）。
  緑にも「CI なし」にも倒さない。サイクルは止めない（例外は 1 行ログに出して次へ）。1 つの task の想定外の例外は
  その task を `unknown (internal_error)` にして閉じ込め、他の task の観測は続ける。
- `unknown` の記録にも card の `pr_number` を入れる（`target_dir_missing` / `internal_error` を含む。#314 r3。None で書くと読み手の照合で隠れ、
  照合の外れとして毎サイクル取り直していた）。
- 記録の名前（mission slug・task id）は card 由来なので、パスにする前に `lib_git_policy.check_mission_slug` / `check_task_id` を通す。
  通らない task は飛ばして warn（`registry/ci/` の外を指すパスを作らない）。
- tasks.json を作り直す `plan.sh task-graph` には、dispatcher 自身の `CREWVIA_QUEUE` / `CREWVIA_REPO_ROOT` を env で渡す
  （`reap_kai_codex_orphan_assignment` と同じ。ambient の env が本番を指していても隔離 dispatcher が本番の tasks.json を書かない）。

## 記録（`registry/ci/<mission>__<task>.json`）

```
{"pr": 313, "head": "<40 hex>", "state": "running", "observed_at": <epoch>, "summary": "...",
 "checks": [{"name", "state", "conclusion", "started_at", "completed_at"}], "reason": "<unknown のときだけ>"}
```

- state: `running` / `passed` / `failed` / `unknown` / `none`（gh は答えたが check が 0 = CI 未登録）/ `no_pr`。
  全体は failed > unknown > running > passed（check の 1 つでも知らない値なら unknown）。
- check の state: CheckRun の `COMPLETED` + `SUCCESS` = passed / `NEUTRAL`・`SKIPPED` = neutral / `FAILURE`・`TIMED_OUT`・`CANCELLED`・`ACTION_REQUIRED`・`STARTUP_FAILURE`・`STALE` = failed /
  `QUEUED`・`IN_PROGRESS`・`WAITING`・`PENDING`・`REQUESTED` = running / それ以外 = unknown。StatusContext は `SUCCESS` / `FAILURE`・`ERROR` / `PENDING`・`EXPECTED`。
  status / conclusion / state が文字列（conclusion は null も可）でなければ集合と照合せず unknown。
- check 名は PR の workflow から来て Director への送信文に入るので、制御文字・改行を空白にして 80 字で切る。
- 読みは `lib_daemon_state.load_json_store`（形の検証 `record_problem`）。書き手は dispatcher だけ（`write_told_atomic` = tmp + replace）。
  `record_problem` が必須にする欄は `RECORD_FIELDS` / `CHECK_FIELDS` の 1 定義 = 読み手（`summarize` / `format_line` / `task_graph_ci` / `poll_cycle` の fingerprint）が `[]` で読む欄（値が null でも欄は有ること）。読み手に欄を足したらここに足す（#314 r4: `name` と `state` だけの running check が検査を通り、`summarize` が `started_at` で KeyError → 新しい記録を書いた後に `poll_cycle` が落ちた）。検査を通らない前の記録は「無い」と同じで、間隔を待たずに取り直す。
- **消してよい**（無ければ次のサイクルで取り直す。壊れていれば表示しないだけで、次の poll で上書きされる）。
- `registry/ci/.task-graph-pending.json`: tasks.json にまだ載せていない変化がある印（#314 r2）。記録は poll が先に書くので、`plan.sh task-graph` が
  失敗すると次のサイクルでは表示の変化が 0 になり、印が無いと tasks.json が古いまま残る。変化があれば印を置き、印がある間は（監視対象の task が
  無いサイクルでも）毎サイクル再生成を試み、**成功（exit 0）した後だけ**消す。壊れた印は「ある」扱い（再生成は何度走っても害が無い側）。
  消してよい（次に記録が変わるまで再生成しないだけ）。名前は `<mission>__<task>` の検査を通らない形なので記録と衝突しない。

## 表示（読むだけ・gh を呼ばない）

- `plan.sh status`: in_progress の行の下に 1 行。`plan.sh status --mission <slug>`: POLL_STATUSES の行の下に 1 行。
  例 `CI PR #313 @086c9f5: ✓5 ⏳1 (pytest 12m) ✗0`（✓ passed・⏳ running と最も長く走っている check の経過分・✗ failed と名前・`–N` neutral・`?N` unknown）。
  記録が 5 分より古ければ `(n 分前)`。gh の失敗の unknown（`reason` つき）は `CI PR #313 unknown (gh_timeout)` のように理由ごと出す。
  gh は答えたが知らない状態の check があるだけの unknown は内訳を出す（`… ✗0 ?1`。tasks.json の summary も同じ。#314 r3）。
- `tasks.json`（herdr-task-graph）: POLL_STATUSES の task に `ci: {state, summary}`。state は running / passed / failed / unknown の 4 値（CI 未登録は unknown に畳む）。
  summary は経過分を含まない（分が進むたびに書き直さない）。**既存の欄・形は変えない**。plugin 0.3.0（d2b8195）と 0.5.0 が未知の欄 `ci` を拒否せず読むことを実物で確認した（2026-10-27）。
  plugin 側の表示対応は別 repo。記録の表示が変わったサイクルだけ dispatcher が `plan.sh task-graph` を呼んで作り直す（`CREWVIA_TASK_GRAPH=0` なら何もしない）。
- 通知: watchdog の hard_idle 見送り通知（`suppression_message`）と Rule 5 の通知に、記録があれば同じ 1 行を添える（「CI 待ちの状況: …」）。**判定には使わない**。
  Rule 5 では 1 行の後に改行を入れ、続く「画面末尾:」とくっつけない（#314 r2）。
- どの表示も、card が `pr_number` を持っていて記録の `pr` と違えば出さない（`read_record(..., pr_number=)`。別の PR の CI を今の PR の状況として
  見せない。#314 r2）。読み手は 5 者（`plan.sh status` の要約・`--mission` の詳細・tasks.json・watchdog・Rule 5）で、どれも card の値を渡す
  （要約が渡していなかった = #314 r3）。watchdog は毎サイクルの card で `refresh_card_pr_number`（監視開始時の値のままにしない。#314 r3）、
  Rule 5 は通知のときに card を読み直す。網羅は `tests/test_ci_status.py` の `PR_MATCH_TABLE`。

## テスト

`tests/test_ci_status.py`（gh は stub。runner の差し替え / PATH の stub `gh`）。テスト中（`CREWVIA_MUX_TEST_ISOLATION`）は、テスト専用の印
`CREWVIA_CI_ALLOW_GH_IN_TESTS=1` が無ければ gh を呼ばない（既存の dispatcher のテストが本物の GitHub に問い合わせない）。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。`registry/ci/` は残ってよい（消してもよい）。tasks.json の `ci` 欄は次の再生成で消える。
