# PR task-line guard (t001/t002, mission 20261102-pr-task-line)

## 背景

`agents/worker.md` の `gh pr create` テンプレートは `task: <mission>/<tid>` 行を含んでいたが、
散文の一部でしかなく強制力が無かった。review Worker の stacked PR 検出手順（worker.md §Stacked PR）
もこの行を読むが、こちらも散文の手順。

t001 の実測: #327〜#336（直近 10 本、オープン中含む）の PR 本文に `task:` / `branch:` 行が
**1 件も存在しなかった**（`${TASK_MISSION}` のような未展開リテラルも残っていない = Worker が
テンプレートをそもそも参照していない）。消費側の保留手順も t003 の実測で一度も発火しなかった
（review Worker が「`main` と実際の base を直接比較する」という禁止されている近道をしていた）。
生産側・消費側どちらも「散文の手順は LLM である Worker に毎回守られるとは限らない」という
同じ弱点を持つ（10/10 で破られた実績がある）。

`scripts/git-helpers.sh` の `crewvia_create_pr` は t001 時点で**呼び出し元ゼロ**（本番コードからの
参照は無く tests と docs のみ。`knowledge/git-policy.md:460` にも明記済み）だったため、そこに
task: 行の注入ロジックを足しても効果が無い。

## 採用した案（Director 承認: 案 A′）

`hooks/pre-tool-use.sh` に構造的ガード（PR task-line guard）を追加した。既存の
「Task file direct-write guard」（t015/t019/t023）と同じ配置・emit_decision 経路。

- 対象: `TOOL_NAME=Bash` の `COMMAND` が実際に `gh pr create` を呼び出している場合
  （シングル/ダブルクォート区間を**1 回の交替パターン** `'[^']*'|"[^"]*"` で左から順に
  除去した文字列に対して `\bgh[[:space:]]+pr[[:space:]]+create\b` を探す。
  引用符の中に文字列として「gh pr create」が現れるだけの報告コマンドを誤って deny しない）
- `--body`（インラインの文字列引数。`--body-file` は除く）が無ければ、task: 行の有無を
  判定せずそのまま素通りする（下記「既知の穴」参照）
- mission_slug / task_id の解決は `queue/assignments/<AGENT_NAME>` を直接読む
  （env の `TASK_ID` / `CREWVIA_TASK_ID` は Bash tool の subshell 内に閉じて hook プロセスに
  届かないため使わない。t014 の P1 インシデントと同じ理由）
- `--body` に `task: <mission>/<task>` の行（解決した値の文字どおり。ERE メタ文字はエスケープし、
  一致の直後が英数字でないこと / 行末であることを要求してアンカーする）が無ければ deny
- **fail-open**: assignment が無い・AGENT_NAME 未設定・assignment ファイルの形式が壊れている
  （`:` を含まない）場合は、強制せずそのまま通す。Director 自身の PR・手動の PR・assignment を
  持たないセッションまで deny すると「誰も PR を出せなくなる」ため

## claude review (t005) で見つかった 3 件の修正（2 周目）

1周目の実装には claude review (t005) が 3 件の欠陥を見つけた。いずれも 2 周目で修正済み:

- **P2-1（クォート除去の 2 パス問題）**: 当初はシングルクォート除去とダブルクォート除去を
  別々の sed パスで行っていた。二重引用符内にアポストロフィ（`don't` 等）があると、1 パス目
  （シングルクォート除去）がそれを開始クォートとみなし、後方にある無関係な `'`（例:
  `echo 'done'`）と対になって、間にある実物の `gh pr create` ごと消してしまう
  → ガードの検出が外れ、**deny すべきコマンドが黙って通る**（最悪の方向の欠陥）。
  1 回の交替パターンで左から順に処理する形に直した（左から見て最初に現れたクォート文字の
  種類でそのまま閉じるため、二重引用符の中のアポストロフィを区切りとして誤用しない）。
- **P2-2（--body 以外の形を誤って deny していた）**: 「既知の穴」として文書には
  `--body-file` 等は検出できないと書いていたが、実装はそれを反映しておらず、
  `--body` 自体が無いコマンド（`--body-file` / `--fill` / `--web` / `--help`）でも
  「task: 行が無い」として deny してしまっていた。Director の fail-open の精神（正当な
  PR 作成を誤って止めない）に反するため、`--body`（`--body-file` を除く）の有無を
  先に見て、無ければ判定自体をスキップするようにした。
- **P3（task id の接頭辞衝突）**: `grep -qF` の無アンカー一致では、割り当てられた task が
  `t1` のとき、本文にある別の task の行 `task: <mission>/t10` を部分一致で「ある」と
  誤判定してしまう（`t1` は `t10` の文字列として前方一致する）。ERE メタ文字をエスケープした
  正規表現に切り替え、一致の直後が英数字でないこと（または行末）を要求するアンカーを足した。

## 既知の穴（意図的にスコープ外。黙って見逃していない）

- `--body-file` / ヒアドキュメント / 標準入力で本文を渡す形は検出できない
  （`--body "..."` の inline 形だけを見る。実測では Worker は常にこの形を使っている。
  **コードもこの通りに動く**: `--body` が見当たらなければ判定自体をスキップして素通りする。
  P2-2 の修正前は、ここに書いた「既知の穴」と実装の挙動が食い違っていた）
- `bash -c "gh pr create ..."` のようにクォートごとネストしたインタプリタに丸投げする形は、
  クォート除去で中身ごと消えるため検出できない（見逃し方向のリスク。false positive を避ける
  ことを優先した Director の判断）
- `gh pr edit --body` で後から行を消すケースは対象外（生成時点のガードのみ。`gh pr edit` は
  PR 本文以外の目的でも頻繁に使われ、対象に含めると誤検知面が広がるため）

## テスト

- `tests/test_pr_task_line_guard.py`（本物の `hooks/pre-tool-use.sh` を subprocess で起動）:
  行がある → 通る / 行が無い → deny / 解決できない（assignment 無し・壊れている）→ fail-open で通る /
  クォートの中の言及だけでは誤検知しない / compound command でも本物の呼び出しは検出する /
  `gh pr edit` は対象外 / 二重引用符内のアポストロフィで実物の呼び出しを見失わない (P2-1) /
  `--body-file` / `--fill` / `--web` / `--help` は deny しない (P2-2) /
  task id の接頭辞衝突 (`t1` vs `t10`) を誤って「ある」と判定しない (P3)
- red proof: `bash tests/red_proof_pr_task_line_guard.sh`（ガードのブロックを丸ごと削ると
  複数のテストが赤くなることを確認する。約数秒）

## 戻し方

このガードは `hooks/pre-tool-use.sh` 内の 1 ブロック（`# --- PR task-line guard` から
`# --- Plan-review write scope guard` の直前まで）。PR を revert すれば消える。
dispatcher/watchdog の再起動は不要（hook は Bash tool 呼び出しのたびに新規プロセスとして
起動されるため、`scripts/sync-main-checkout.sh` で主 checkout を merge 後の main に合わせれば
次の Bash 呼び出しから反映される）。

## 残作業 / 検証事項

- このガードは主 checkout の `hooks/pre-tool-use.sh` が merge 後の版になって初めて全 Worker に効く
  （t002 の worktree 内で `gh pr create` を打つ時点では、主 checkout はまだ旧版の可能性が高い。
  director.md:1078-1089 の kai-review.sh 「鶏と卵」と同型）。実際に本番で効いているかの検証は
  後続タスク（t004 等）に委ねる
- 一次証拠は自動テスト（上記）であり、個々の PR 本文に `task:` 行があること自体は実装が効いた
  証拠にならない（手で打てば満たせるため）
