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

除外の理由は 2 種類だけ（表は `tests/test_ci_runs_every_script_test.py` の `ALLOWED_REASONS`）:

- `live の herdr / tmux が要る（恒久）` — CI に無い実物が要り、偽物では代えられない
- `未調査 (t043)` — 新しく CI に入れたら落ちた。この task では直さず、棚卸しと修正は t043。直ったら行を消す

新しい `scripts/test_*.sh` は何も登録しなくても次の CI から走る。CI で走らせられないと分かったら、
除外ファイルに理由付きで 1 行足す（足した行はレビューに載る）。

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
