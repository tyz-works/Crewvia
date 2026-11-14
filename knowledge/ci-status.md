# PR の CI の見える化（`lib_ci_status.py`）

Worker が PR の CI を待っている間、その進み具合を Director とユーザーが crewvia の画面から見られるようにする（2026-10-27 ユーザー要望）。
**読み口は 2 本立て**（`knowledge/ci-wait-operations.md` §2.3。2026-11-13 PR-D2）:

- **表示用**（`read_record` / `format_line` / `summarize`。下の「表示」）: 見える化だけ。Rule 5 を黙らせる・watchdog の見送りを CI の状態で変える、といった判定には使わない。
- **門用**（`gate_verdict` / `plan.sh ci-gate`。末尾の「門」）: Director の merge の「通さない」と、dispatcher の `ci-failed` / `ci-green` / `merge-ready` / `ci-unknown` 通知にだけ使う。待ちを解く・done にする・Rule 5 / watchdog を黙らせる・割り当てには使わない。共有規則なので env の停止スイッチは無い（止めるなら PR revert）。

運用: Worker・review は CI を待たず done し、結果は dispatcher が Director に通知、merge は Director（`agents/director.md` §12「merge 手順」）。

## 取得（dispatcher の 1 か所）

- dispatcher のサイクルの `run_ci_poll()`（`dispatch()` の後・成否から独立）→ `lib_ci_status.poll_cycle()`。
- 対象: active mission の task のうち、`pr_number` を持つものは status 不問（done を含む。`lib_task_status.CI_FOLLOW_PR_STATUSES`。PR が MERGED / CLOSED になれば外れる）、持たないものは `in_progress` / `ready_for_verification` / `verifying`（`lib_ci_status.POLL_STATUSES`）。
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

## 門（`gate_verdict` / `plan.sh ci-gate`。ci-wait-operations PR-B）

- `lib_ci_status.gate_verdict(record, *, pr_head, now)` は「通す / 通さない + 固定語の理由」を返す**門用の読み口**（表示用の `read_record` / `format_line` とは別）。通すのは記録の head == PR の現 head ∧ 全体 passed ∧ 必須 check `ci-ok` が passed ∧ `observed_at` が 10 分以内のときだけ。それ以外・読めない・取り直せないは全部通さない。設計: `knowledge/ci-wait-operations.md` §2.2。
- `plan.sh ci-gate <PR> [--target-dir]`: 通せば exit 0 + `ci-gate: PASS PR #N head=<sha>`、通さなければ exit 1 + `ci-gate: DENY PR #N <理由>`。PASS のときは 2 行目に `ci-gate: approved_head ok|no_approval|approved_head_mismatch`（review の承認 head と現 head の比較。PR-D1）が出る。読み取り専用。`gate_verdict` を呼ぶのは dispatcher の `pr_events`（`ci-green` / `merge-ready` を出す条件）と `plan.sh ci-gate` だけ。`plan.sh ci-gate` を引くのは Director の手だけ（`tests/test_ci_gate.py::test_nobody_calls_the_gate_yet`）。
- 固定語の理由 `gh_pr_not_found` (PR が見つからない) は gh の全呼び出しに効くので、`ci-gate` の DENY の理由にも `unknown:gh_pr_not_found` として出る。害はなく挙動は変えない (P3-5)。
- 「判定に使わない」の文言の改訂（表示用 / 門用の 2 つに割る）は PR-D2 で済み（冒頭）。門は待ちを解く・done にする・Rule 5 / watchdog を黙らせることには使わない。

## PR の持ち主の repo（`pr_repo`。t001）

- 範囲は `lib_ci_status.pr_scope(task)` の 1 か所（`pr_repo` → `target_dir` → crewvia の repo）。`plan.sh done --pr` は pr_number と一緒に、上流の `pr_repo` も下流の review / codex-review に伝える（下流に別の `pr_repo` があれば pr_number も書かず stderr に出す）。
- `pr_repo` のある card は `--repo` で repo が決まるので、`target_dir` が実在しなくても cwd は repo_root で取得する（`target_dir_missing` にしない）。
- **台帳キーの形式が変わった**: `target_dir` の card は `pr<N>` から `pr<N>x<6 桁>` へ。デプロイ直後に、伝え済みの通知が 1 回ずつ再送される。旧形式のキーは畳まれずに残る（台帳は消してよい）。

## CLOSED の PR の取り直し (P3-10)

- `pr_state` が `CLOSED` の記録は終端にせず、`CLOSED_RECHECK_SECONDS` (900 秒) ごとに取り直す。reopen された PR の `ci-green` / `merge-ready` / `ci-failed` が出なくなるのを防ぐ。`MERGED` / `NOT_FOUND` は従来どおり終端。gh の呼び出しは通常の取得と同じ `max_tasks` / `budget` に数える。

## daemons: しきい値の型検査 (P3-2)

- `lib_daemon_watch._config_int` の 1 か所で検査する。整数だけ受け入れ、bool (`yes` / `true`)・文字列 (`"900"`)・小数は拒否して既定値に戻し、同じ欄・同じ値の WARNING を stderr に 1 回出す。0 以下は欄ごとの既存の規則のまま (`ci_poll_seconds` / `hard_idle_suppressed_notify_seconds` は正のみ、他は 0 以下 = 無効を許す)。変えたら dispatcher / watchdog の restart が要る。
