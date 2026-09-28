# CI が scripts/test_*.sh を glob で全部走らせる（t025 / backlog #30）

## なぜ

`scripts/test_*.sh` は 26 本あったが、CI が名指しで走らせていたのは 4 本
（`test_handoff_path` / `test_registry_lock` / `test_dispatcher_needs_director_notify` / `test_dispatcher_notify`）だけだった。
事故を防ぐために書いたテストほど CI の外にあり、新しく足したテストも黙って CI の外に置かれた。

## 仕組み

| 部品 | 役割 |
|---|---|
| `.github/workflows/ci.yml` の `script-tests` job | `scripts/ci-run-script-tests.sh` を呼ぶ 1 job。名指しの一覧は持たない |
| `scripts/ci-run-script-tests.sh` | `scripts/test_*.sh` を glob し、1 本ずつ走らせて PASS / FAIL を出す。1 本落ちても残りは止めず、最後に一覧を出して exit 1。0 本しか走らせなかったら PASS にしない。`--list` は走らせずに RUN / SKIP を出す |
| `scripts/ci-script-tests-excluded.txt` | 走らせないものの**唯一の**宣言。`<path> \| <理由>`。理由の無い行・存在しないファイル・重複は runner が exit 2 で落ちる |
| `tests/test_ci_runs_every_script_test.py` | 全 `scripts/test_*.sh` が RUN か SKIP にちょうど 1 回入ること・除外の理由が 2 種類だけであること・workflow が runner を呼び名指しをしないこと・runner の振る舞いを検査する。検査件数を毎回出す |

除外の理由は 3 種類だけ（表は `tests/test_ci_runs_every_script_test.py` の `ALLOWED_REASONS`）:

- `live の herdr / tmux が要る（恒久）` — CI に無い実物が要り、偽物では代えられない
- `live の Taskvia dev server / Upstash Redis / ntfy の実クレデンシャルが要る（恒久）` — CI に持ち込まない外部サービスの実 secret が要る（t043 で追加。`test_phase_c.sh` / `test_phase_e.sh`）
- `未調査 (t043)` — 新しく CI に入れたら落ちた。この理由文字列は t043 専用（task id が埋め込まれている）。t043 完了時点で 0 行（`test_no_uninvestigated_reason_remains`）。**将来別 task で同種の一時退避が要るときは、この文字列を再利用せず、その task id の新しい理由を `ALLOWED_REASONS` に追加すること**

新しい `scripts/test_*.sh` は何も登録しなくても次の CI から走る。CI で走らせられないと分かったら、
除外ファイルに理由付きで 1 行足す（足した行はレビューに載る）。

## t043: 除外 3 本の棚卸し結果

- `test_phase_c.sh` / `test_phase_e.sh` — (c) 恒久的な live 依存。`NTFY_TOPIC` / `NTFY_PASS` /
  `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`（`Taskvia/.env.local` 前提）が無いと
  変数チェックで即 `exit 1`。CI にこれらの secret を持ち込む計画は無いので、理由を上記の
  恒久カテゴリに書き直した（直せない・直さない）。
- `test_watchdog_config_mode.sh` — (a) テスト側の欠陥。Test 26 が `start.sh` の
  `mux_spawn "dispatcher"` / `mux_spawn "watchdog"` という**もう存在しない**呼び出し形を
  grep していた。dispatcher/watchdog の起動は `lib_daemon_watch.py spawn <name>` 経由の
  共有関数 `spawn_command()` に一本化された refactor 後、この grep は常に空になり Test 26
  が固定で FAIL していた（他 25 Test は無関係で全部 PASS）。Test 26 を `spawn_command()`
  の実際の出力（`CREWVIA_MUX` を dispatcher/watchdog 両方の起動コマンドに explicit export
  しているか）を直接検証する形に直し、allowlist から外した。

## P3-1 / P3-2（t028 レビュー、Seo 指摘）

- **P3-1**: `scripts/ci-run-script-tests.sh` のコメント判定 `case "$line" in
  [[:space:]]*"#"*)` は「先頭が空白文字 1 個 + 行のどこかに # がある」にマッチし、
  pytest 側 (`_parse_allowlist`: strip 後に先頭が `#` かだけを見る) と食い違っていた。
  先頭に 1 個の空白があり理由の中に `#` を含む正当な行を、runner が黙ってコメット扱いして
  除外リストから漏らす方向。runner を「行を trim してから先頭 `#` だけを見る」規則に統一。
  回帰: `tests/test_ci_runs_every_script_test.py::test_runner_comment_check_only_matches_a_truly_leading_hash`
- **P3-2**: allowlist path の許容文字が 3 箇所で食い違っていた: runner の
  `case "$path" in scripts/test_*.sh)`（glob。`-` も受理）/ pytest の
  `re.fullmatch(r"scripts/test_[A-Za-z0-9_]+\.sh", path)`（`-` を拒否）/ workflow
  名指し検出の `re.finditer(r"scripts/test_\w+\.sh", cmd)`。3 箇所とも
  `TEST_FILENAME_PATTERN = r"scripts/test_[A-Za-z0-9_]+\.sh"`（pytest 側の定数、
  runner は bash 側で同じ字クラスを直接埋め込み、コメントで揃える先を明記）に統一。
  回帰: `test_runner_rejects_a_malformed_allowlist[hyphenated-name]`

## P3-3（t028 レビュー、Seo 指摘）: `tests/*.sh` の棚卸し（t043）と CI 化（t063）

`tests/*.sh` は（t043 棚卸し時点で）19 本あり、どの CI job も走らせていなかった（bats は
`tests/*.bats` だけ拾い、pytest は python glob なので `.sh` を拾わない）。ファイル名の
慣習ではなく **中身の性質**で 3 分類する:

| 分類 | 判別できる中身の性質 |
|---|---|
| (i) source される lib | トップレベルに実行文・PASS/FAIL 集計・`exit` が無く、関数定義だけ。ヘッダに「使い方 (source して呼ぶ)」と明記 |
| (ii) 使い捨ての red proof | ヘッダの「使い方」が人間向けの単発コマンド。`mktemp -d` で使い捨てコピーを作り、そこに**1 つの task id に紐づく既に直った欠陥**を注入し、既存の permanent テスト（`tests/test_*.py` / `scripts/test_*.sh`）がそれを検出することを確認するだけ。多くは特定の historical commit（`git show <固定 sha>:<path>` や `git archive HEAD` 時点の特定関数の文字列）を前提にしており、コード側の後続 refactor でその前提が黙って崩れる（memory: red-proof-scripts-go-stale-on-stacked-prs）。ここでの assert は「今の本番コードが正しいか」ではなく「過去に書いた regression テストが赤くなるか」であり、対象の regression テストは既に CI に載っている |
| (iii) 常時走らせる価値のある本物のテスト | 現在の本番コードを**そのまま**、実の外部プロセス（tmux/デーモン）を相手に動かし、単体テストが構造的に届かない経路（early return で到達しないログ経路など）を検証する。特定の historical commit に依存しない |

t043 時点は 19 本（(i) 1 / (ii) 17 / (iii) 1）。t063 で CI 化するまでの間に
`red_proof_t013.sh` / `red_proof_t033.sh` / `red_proof_t047.sh` / `red_proof_t055.sh` /
`red_proof_b1_background_work.sh` / `red_proof_b6_trust_precheck.sh` / `red_proof_t017.sh`
の 7 本が増え、t063 時点で 26 → (rebase で `red_proof_t017.sh` が追加され) 27 本。
新しく増えた 7 本も同じ中身の性質（mktemp コピー + 特定 task id の欠陥注入 + 既存 permanent
テストの検出確認）で (ii) と判定できた — ファイル名の慣習ではなく毎回中身を見て分類する
という P3-3 の原則が、本数が増えても機械的に適用できることの実例。

### t063: CI 化した仕組み

`scripts/ci-run-script-tests.sh` / `scripts/ci-script-tests-excluded.txt` /
`tests/test_ci_runs_every_script_test.py`（`scripts/test_*.sh` 用）と同じ「glob + 理由付き
allowlist + 構造ガード」の形を、`tests/*.sh` 用に別ファイルで用意した:

| 部品（tests/*.sh 用） | scripts/test_*.sh 用の対応物 |
|---|---|
| `scripts/ci-run-tests-sh.sh` | `scripts/ci-run-script-tests.sh` |
| `scripts/ci-tests-sh-excluded.txt` | `scripts/ci-script-tests-excluded.txt` |
| `tests/test_ci_runs_every_tests_sh.py` | `tests/test_ci_runs_every_script_test.py` |
| `.github/workflows/ci.yml` の `tests-sh-tests` job | `script-tests` job |

**コードは共有しない**（各ファイル冒頭のコメントに理由あり）: 走る本数の比率が逆
（`scripts/test_*.sh` は大半が RUN、`tests/*.sh` は 27 本中 26 本が SKIP）・ファイル名の
許容文字が違う（`tests/*.sh` は `watchdog-idle-e2e.sh` のようにハイフンを含む。
`TEST_FILENAME_PATTERN = r"tests/[A-Za-z0-9_-]+\.sh"`）・除外理由の分類が違う、という
差があり、既に 4 並行 PR (#237-240) が依存していた `ci-run-script-tests.sh` 側に共有の
ための抽象化を挟むリスクの方が、コード重複より高いと判断した。「形」（glob + 理由付き
allowlist + 構造ガード）は踏襲し、コメント判定・ファイル名許容文字の定義は pytest 側
定数と揃える先を相互参照コメントで明記する（t043 P3-1/P3-2 の「3 箇所の定義が食い違う」
事故をコード共有ではなく規律で防ぐ）。

**除外理由は 2 種類だけ**（scripts/test_*.sh 側の「未調査 (t043)」はここへ持ち込まない。
t063 の受入条件は「未調査」を含む理由が 0 行であること）:

- `テストではない（source される lib。トップレベルに実行文が無い）` — `fixture_tree.sh` / `trust_fixture.sh`
- `1 回限りの historical red proof（CI 化しない）` — red_proof_*.sh（件数は `scripts/ci-tests-sh-excluded.txt` の
  (ii) を参照。ここに固定件数を書くと登録のたびにずれる — t116 で実際に 23 本表記のまま 24 本に
  ずれていたのを機に、本文からは件数を外した）

**(iii) `watchdog-idle-e2e.sh` を CI に載せる過程で見つかった欠陥**: このファイルの
シナリオ 4（「B1 が無い origin/main は実行中の Worker も殺してしまう」という対照）は、
B1 (#238) が既に origin/main に merge 済みだったため `git show origin/main:scripts/watchdog.py`
が「B1 が無い版」を返さなくなっており、かつその版が新しく要求する `lib_pane_process.py`
をこの対照のコピー先が持たない（依存 lib 一覧が t082 時点のまま）ため
`ModuleNotFoundError` で起動直後に落ちていた。t082 が一度直したのと同じ型の再発
（memory: red-proof-scripts-go-stale-on-stacked-prs）。**この対照は撤去した** — B1 は
main の恒久部分になったので二度と「無い」状態には戻らず、対照を維持するには B1 merge
前の固定 SHA (`da3784ba8dabe1d0a859c33afadfebf26026d557`) へ依存を移すしかないが、それは
(iii)（historical commit に依存しない）の定義そのものに反する。e2e が守る価値は
シナリオ 1-3（warn / terminate / 誤 terminate 防止が今の本番コードで実際に発火すること）
にあり、そこは撤去の影響を受けない。詳細は `tests/watchdog-idle-e2e.sh` 内のコメント。

## P3-4（t028 レビュー、Seo 指摘）: script-tests job の直列実行時間

t043 実行時点で script-tests job は 24 本（除外 2 本を除く）を直列実行。ローカル計測
（隔離環境、tmux あり）で概ね全体 run の critical path になり得る規模（`test_retirement_authority.sh`
等の実 tmux e2e が数十秒かかる本を複数含む）。**判断: 現時点では matrix 分割は不要**。
理由:
- 23→24 本（±1）の規模はまだ 1 job で許容できる（GitHub Actions の並列 job 起動オーバーヘッド
  ―checkout + 依存インストールを job ごとに毎回払う― の方が、直列実行の数分より高くつく
  分割点にまだ達していない）
- flaky 1 本が全体を止める問題は「1 本落ちても残りは止めない」設計（`ci-run-script-tests.sh`
  は 1 本ずつ実行し最後に一覧を出す）で緩和済み。CI 全体を止めるのは job 全体の失敗表示のみで、
  他 job（bats / pytest / shell-validate）は並行して結果が出る
- 本当に効果が出るのは (iii) 側の `tests/*.sh` を今後 glob 化して本数が増えたとき。その時点で
  再検討すればよく、今 speculative に分割線を引く理由が無い

再検討のトリガー: script-tests job の実行時間が bats/pytest 他 job の 1.5〜2 倍を超えて
CI 全体の critical path になった時、または flaky な 1 本が週に複数回 CI を止めるようになった時。

## ローカルで走らせる

実 checkout で走らせない（テストによっては registry に残骸を残す）。隔離コピーで `env -i` にし、herdr の無い PATH と
存在しない `CREWVIA_HERDR_SOCK`、短い `TMUX_TMPDIR`（tmux の socket パスは約 100 字まで。長いと
`File name too long` で tmux を使うテストが落ちる）を与える。

## 並行 PR への影響

この PR が merge されると、他の open PR が新しく足した `scripts/test_*.sh` も glob で CI に入る。
merge 後に全 open PR を `gh pr update-branch` して CI を確認する（実施は t028）。

t063 (tests/*.sh の CI 化) が merge されると、同様に他の open PR が新しく足した
`tests/*.sh` も `tests-sh-tests` job の glob に入る（除外していなければ RUN される）。
merge 後に全 open PR を `gh pr update-branch` して CI を確認すること。

## 戻し方

PR を revert する。CI は名指しの 4 本に戻り、除外ファイルと構造テストも一緒に消える（残る参照は無い）。
一部のテストだけ CI から外したいときは revert ではなく除外ファイルに理由付きで 1 行足す。

t063 (tests-sh-tests job) を戻す場合も同じ: PR を revert すれば `tests-sh-tests` job・
`scripts/ci-run-tests-sh.sh`・`scripts/ci-tests-sh-excluded.txt`・
`tests/test_ci_runs_every_tests_sh.py` が一緒に消え、`tests/*.sh` はどの CI job も
走らせない元の状態に戻る（`tests/watchdog-idle-e2e.sh` のシナリオ 4 撤去だけは残る —
実装上の欠陥修正であり、CI 化そのものとは独立）。
