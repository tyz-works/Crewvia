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

## P3-3（t028 レビュー、Seo 指摘）: `tests/*.sh` 19 本の棚卸し（この PR ではCI 化しない）

`tests/*.sh` は 19 本あり、どの CI job も走らせていない（bats は `tests/*.bats` だけ拾い、
pytest は python glob なので `.sh` を拾わない）。ファイル名の慣習ではなく **中身の性質**で
3 分類する:

| 分類 | 判別できる中身の性質 | 該当 | 本数 |
|---|---|---|---|
| (i) source される lib | トップレベルに実行文・PASS/FAIL 集計・`exit` が無く、関数定義だけ。ヘッダに「使い方 (source して呼ぶ)」と明記 | `fixture_tree.sh`（`copy_scripts_libs` / `copy_plan_tree` の入口。`tests/CLAUDE.md` が定義） | 1 |
| (ii) 使い捨ての red proof | ヘッダの「使い方」が人間向けの単発コマンド。`mktemp -d` で使い捨てコピーを作り、そこに**1 つの task id に紐づく既に直った欠陥**を注入し、既存の permanent テスト（`tests/test_*.py` / `scripts/test_*.sh`）がそれを検出することを確認するだけ。多くは特定の historical commit（`git show <固定 sha>:<path>` や `git archive HEAD` 時点の特定関数の文字列）を前提にしており、コード側の後続 refactor でその前提が黙って崩れる（memory: red-proof-scripts-go-stale-on-stacked-prs）。ここでの assert は「今の本番コードが正しいか」ではなく「過去に書いた regression テストが赤くなるか」であり、対象の regression テストは既に CI に載っている | `red_proof_t001_needs_director.sh` / `red_proof_t001_pytest_workspace.sh` / `red_proof_t001_stale_records.sh` / `red_proof_t004.sh` / `red_proof_t005.sh` / `red_proof_t009.sh` / `red_proof_t010.sh` / `red_proof_t018.sh` / `red_proof_t019.sh` / `red_proof_t020.sh` / `red_proof_t021.sh` / `red_proof_t021_pr6.sh` / `red_proof_t022.sh` / `red_proof_t025.sh` / `red_proof_t036.sh` / `red_proof_stat_and_direct_reads.sh` / `red_proof_unobservable.sh` | 17 |
| (iii) 常時走らせる価値のある本物のテスト | 現在の本番コードを**そのまま**、実の外部プロセス（tmux/デーモン）を相手に動かし、単体テストが構造的に届かない経路（early return で到達しないログ経路など）を検証する。特定の historical commit に依存しない | `watchdog-idle-e2e.sh`（t016: `tests/test_watchdog_idle.py` は `check()` を直接叩くだけで、常駐ループを通しで動かさないと検出できない dead-code 経路がある、という明示的な理由が書かれている） | 1 |

合計 19 本（1 + 17 + 1）。次に glob 拡張をする task（Director が積む）は (iii) の
`watchdog-idle-e2e.sh` を CI 化対象にし、(ii) の 17 本は理由付きで allowlist に残す
（reason は本 PR の 3 番目のカテゴリに当たらないので、新しい恒久カテゴリ
「1 回限りの historical red proof（CI 化しない）」を追加検討する）。(i) は
そもそも `scripts/test_*.sh` / `tests/*.py` の glob 対象にしない（テストではない）。

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

## 戻し方

PR を revert する。CI は名指しの 4 本に戻り、除外ファイルと構造テストも一緒に消える（残る参照は無い）。
一部のテストだけ CI から外したいときは revert ではなく除外ファイルに理由付きで 1 行足す。
