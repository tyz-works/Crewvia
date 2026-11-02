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
  （シングル/ダブルクォート区間を除去した文字列に対して `\bgh[[:space:]]+pr[[:space:]]+create\b` を探す。
  引用符の中に文字列として「gh pr create」が現れるだけの報告コマンドを誤って deny しない）
- mission_slug / task_id の解決は `queue/assignments/<AGENT_NAME>` を直接読む
  （env の `TASK_ID` / `CREWVIA_TASK_ID` は Bash tool の subshell 内に閉じて hook プロセスに
  届かないため使わない。t014 の P1 インシデントと同じ理由）
- `--body` に `task: <mission>/<task>` の行（解決した値の文字どおり）が無ければ deny
- **fail-open**: assignment が無い・AGENT_NAME 未設定・assignment ファイルの形式が壊れている
  （`:` を含まない）場合は、強制せずそのまま通す。Director 自身の PR・手動の PR・assignment を
  持たないセッションまで deny すると「誰も PR を出せなくなる」ため

## 既知の穴（意図的にスコープ外。黙って見逃していない）

- `--body-file` / ヒアドキュメント / 標準入力で本文を渡す形は検出できない
  （`--body "..."` の inline 形だけを見る。実測では Worker は常にこの形を使っている）
- `bash -c "gh pr create ..."` のようにクォートごとネストしたインタプリタに丸投げする形は、
  クォート除去で中身ごと消えるため検出できない（見逃し方向のリスク。false positive を避ける
  ことを優先した Director の判断）
- `gh pr edit --body` で後から行を消すケースは対象外（生成時点のガードのみ。`gh pr edit` は
  PR 本文以外の目的でも頻繁に使われ、対象に含めると誤検知面が広がるため）

## テスト

- `tests/test_pr_task_line_guard.py`（本物の `hooks/pre-tool-use.sh` を subprocess で起動）:
  行がある → 通る / 行が無い → deny / 解決できない（assignment 無し・壊れている）→ fail-open で通る /
  クォートの中の言及だけでは誤検知しない / compound command でも本物の呼び出しは検出する /
  `gh pr edit` は対象外
- red proof: `bash tests/red_proof_pr_task_line_guard.sh`（ガードのブロックを丸ごと削ると
  3 つのテストが赤くなることを確認する。約数秒）

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
