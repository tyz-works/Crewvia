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

- 対象: `TOOL_NAME=Bash` の `COMMAND` が実際に `gh pr create` / `gh pr new` を呼び出している場合
- `--body` / `-b`（インラインの文字列引数。`--body-file` は除く）が無ければ、task: 行の有無を
  判定せずそのまま素通りする（下記「既知の穴」参照）
- mission_slug / task_id の解決は `queue/assignments/<AGENT_NAME>` を直接読む
  （env の `TASK_ID` / `CREWVIA_TASK_ID` は Bash tool の subshell 内に閉じて hook プロセスに
  届かないため使わない。t014 の P1 インシデントと同じ理由）
- `--body` / `-b` の値に `task: <mission>/<task>` の行（解決した値の文字どおり。一致の直後が
  英数字でないこと / 行末であることを要求してアンカーする）が無ければ deny
- **fail-open**: assignment が無い・AGENT_NAME 未設定・assignment ファイルの形式が壊れている
  （`:` を含まない）場合は、強制せずそのまま通す。Director 自身の PR・手動の PR・assignment を
  持たないセッションまで deny すると「誰も PR を出せなくなる」ため

## 1〜2 巡目: sed/grep による実装とその欠陥

1 巡目の実装は bash の sed/grep でシェル構文（クォート・`--body` の引数）を解析していた。
claude review (t005) が 3 件の欠陥を見つけ、2 巡目で修正した:

- **P2-1（クォート除去の 2 パス問題）**: シングルクォート除去とダブルクォート除去を別々の
  sed パスで行っていたため、二重引用符内のアポストロフィ（`don't` 等）を誤ってクォート開始と
  みなし、後方の無関係な `'` と対になって間にある実物の `gh pr create` ごと消してしまう
  （検出漏れが最悪の形で起きる）。1 回の交替パターン（`'[^']*'|"[^"]*"`）で左から順に処理する
  形に直した。
- **P2-2（--body 以外の形を誤って deny していた）**: `--body-file` / `--fill` / `--web` /
  `--help` でも「task: 行が無い」として誤って deny していた。`--body`（`--body-file` を除く）の
  有無を先に見て、無ければ判定自体をスキップするようにした。
- **P3（task id の接頭辞衝突）**: `grep -qF` の無アンカー一致では `t1` が `t10` の行を部分一致で
  「ある」と誤判定していた。ERE メタ文字をエスケープし、一致の直後が英数字でないこと（または
  行末）を要求するアンカーを足した。

## 3 巡目: sed の行単位評価による検出漏れ

2 巡目で「1 回の交替パターン」に直したが、**sed は正規表現を行単位で評価する**ため、
`--body` の値が複数行にまたがる（開きクォートと閉じクォートが別の物理行にある）場合、
そのクォート区間をクォートとして認識できなかった。結果として、クォートの中に文字列として
`gh pr create --body` という言及が含まれるだけの報告コマンド（`plan.sh done` の複数行 Result に
事故コマンドを引用した場合など）を、masked 文字列上に露出した文字列と誤認して deny してしまう
（1 巡目・2 巡目とは逆方向の、誤検知の再発）。

加えて、短縮フラグ `-b` やエイリアス `gh pr new` を文字列マッチが素通りしていたこと、
`grep -qF` ベースの task id 照合が本質的に「シェルの引数構造」を見ずに「文字列のどこかに
一致するか」しか見ていないことも、同じ「sed/grep でシェルを解析しようとしている」ことに
起因する穴として指摘された。2 巡を重ねても同じ系統の指摘が続いたため、Director が
`agents/director.md` の「巡を重ねる前に設計を見直す」に従って方式そのものを変える判断をした。

## 4 巡目: Python ヘルパーへの移行（現在の実装）

判定を `hooks/lib_pr_task_line_guard.py` に移した。`hooks/lib_main_repo_git_guard.py` +
`hooks/pre-tool-use.sh` の main repo git guard（:311-325）と同じ「bash はプレコンディション
確認と assignments の解決だけ、構文解析は python に丸ごと委譲し、python が失敗したり
DENY 以外を返したりしたら fail-open（`|| echo OK`）」という型に揃えた。

実装のポイント:

- クォートマスクは `lib_main_repo_git_guard.mask_quotes()` を import して再利用する
  （文字単位でスキャンするため複数行のクォート区間を正しく扱えることを Director が実測
  確認済み。独自に書き直さない — この repo の「規則は 1 か所」の原則）
- マスク済み文字列上でクォートの**外**にある制御演算子（`;` `&` `|` `&&` `||` 改行）の位置を
  見つけ、その同じ文字位置で**元の**（マスクしていない）コマンド文字列を「単純コマンド」の
  断片に分割する（mask_quotes はクォート区間を空白に置換するだけで長さ・オフセットを
  変えないため、マスク済み文字列で見つけた位置がそのまま元の文字列の位置になる）
- 各断片を `shlex.split()` で実際の argv にトークナイズし、`gh` `pr` `create|new` の並びを
  検出する。`--body` / `-b` / `--body=...` の**値そのもの**を取り出して task: 行の有無を
  判定するため、`--body-file` との文字列レベルでの混同や、task id の接頭辞衝突が構造的に
  起きない
- **パースできないとき（閉じていない引用符等で `shlex.split` が `ValueError` を投げたとき）は
  deny しない（fail-open）**。Director の設計制約「解決できないときは deny しない」と同型

## 既知の穴（意図的にスコープ外。黙って見逃していない）

- `--body-file` / ヒアドキュメント / 標準入力で本文を渡す形は検出できない
  （`--body` / `-b` の inline 形だけを見る。実測では Worker は常にこの形を使っている。
  見つからなければ判定自体をスキップして素通りする）
- `bash -c "gh pr create ..."` のようにクォートごとネストしたインタプリタに丸投げする形は、
  外側のクォートが 1 つの引数としてトークナイズされるため検出できない（見逃し方向のリスク。
  false positive を避けることを優先した Director の判断）
- `gh pr edit --body` で後から行を消すケースは対象外（生成時点のガードのみ。`gh pr edit` は
  PR 本文以外の目的でも頻繁に使われ、対象に含めると誤検知面が広がるため）
- シェル変数の展開・コマンド置換の結果は見ない（静的なトークン化であり、実行時の値は
  分からない。例えば `--body "$MSG"` の `$MSG` の中身は判定できない）

## テスト

- `tests/test_pr_task_line_guard.py`（本物の `hooks/pre-tool-use.sh` を subprocess で起動）:
  行がある → 通る / 行が無い → deny / 解決できない（assignment 無し・壊れている）→ fail-open で通る /
  クォートの中の言及だけでは誤検知しない / compound command でも本物の呼び出しは検出する /
  `gh pr edit` は対象外 / 二重引用符内のアポストロフィで実物の呼び出しを見失わない (P2-1) /
  `--body-file` / `--fill` / `--web` / `--help` は deny しない (P2-2) /
  task id の接頭辞衝突 (`t1` vs `t10`) を誤って「ある」と判定しない (P3) /
  **複数行の引用符内の言及を誤検知しない (4 巡目, 3 巡目で見つかった sed の行単位評価の穴)** /
  **二重引用符内のアポストロフィを含む compound command でも実物の呼び出しは deny され続ける** /
  **短縮フラグ `-b` / エイリアス `gh pr new` も `--body` / `create` と同じ扱いを受ける** /
  **閉じていない引用符で `shlex.split` が例外を投げても deny しない (fail-open)**
- red proof: `bash tests/red_proof_pr_task_line_guard.sh`（ガードのブロックを丸ごと削ると
  複数のテストが赤くなることを確認する。約数秒）
- 4 巡目の修正が本物の欠陥を直したことの実証: 旧 (3 巡目, commit `5bc4a5f`) の
  `hooks/pre-tool-use.sh` を使い捨ての一時ディレクトリに取り出し、複数行の引用符内言及・
  短縮フラグ `-b`・`gh pr new` の 3 ケースを直接走らせ、いずれも 4 巡目の実装と異なる結果
  （前者は誤って deny、後2者は検出漏れで素通り）になることを確認した

## 戻し方

このガードは `hooks/pre-tool-use.sh` 内の 1 ブロック（`# --- PR task-line guard` から
`# --- Plan-review write scope guard` の直前まで）と `hooks/lib_pr_task_line_guard.py`。
PR を revert すれば消える。dispatcher/watchdog の再起動は不要（hook は Bash tool 呼び出しの
たびに新規プロセスとして起動されるため、`scripts/sync-main-checkout.sh` で主 checkout を
merge 後の main に合わせれば次の Bash 呼び出しから反映される）。

## 残作業 / 検証事項

- このガードは主 checkout の `hooks/pre-tool-use.sh` が merge 後の版になって初めて全 Worker に効く
  （t002 の worktree 内で `gh pr create` を打つ時点では、主 checkout はまだ旧版の可能性が高い。
  director.md:1078-1089 の kai-review.sh 「鶏と卵」と同型）。実際に本番で効いているかの検証は
  後続タスク（t004 等）に委ねる
- 一次証拠は自動テスト（上記）であり、個々の PR 本文に `task:` 行があること自体は実装が効いた
  証拠にならない（手で打てば満たせるため）
