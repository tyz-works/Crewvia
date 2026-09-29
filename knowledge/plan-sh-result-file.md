# plan.sh の Result をファイル / 標準入力で受け取る (C1 / 運用の穴 #4)

## 何が起きたか

`plan.sh done <id> "<Result>"` の二重引用符の中の**バッククォートと `$(...)` は、plan.sh が起動する前に
シェルが展開する**（コマンド置換）。2026-09-28、Worker (t074) が Result に書いた `pgrep` 待ちループが
そのまま実行されて 12 分止まった。plan.sh の中では防げない（届く時点で既に展開済み）。それまでは
「クォート付きヒアドキュメントで渡せ」という**文書の規則**だけで防いでいた。

## 仕組み

| subcommand | 本文を受け取る引数 | ファイル / 標準入力で渡す形 | 備考 |
|---|---|---|---|
| `done` | `<result>`（位置引数） | `--result-file <path\|->` | QA Gate / `required_evidence` は展開後の本文に同じにかかる |
| `needs-director` | `<reason>`（位置引数） | `--result-file <path\|->` | 長い・複数行は従来どおり `split_long_freeform` が body の詳細節へ |
| `verify-result` | `--notes` | `--notes-file <path\|->` | option の値なので位置引数は無い |
| `fail` | **無し** | — | 引数は `handoff_path`（パス）と `--head <sha>` / `--no-head "<1 行の理由>"` だけ。Result の本文を持たない |

* `-` は**呼び出し元の標準入力**。plan.sh の python 本体は fd 0（ヒアドキュメント）から読まれるので、bash が
  起動前に `exec 3<&0` で退避し、python は fd 3 を読む。stdin が閉じられていても起動は止めず、`-` の読み取りだけが拒否される。
* 拒否は全部 `_usage_exit`（exit 2、queue / registry に 1 バイトも書かない）:
  位置引数との併用 / 読めない（無い・ディレクトリ・FIFO・権限）/ 空・空白のみ / UTF-8 でない。
  読みは `lib_task_cards.read_regular_text_or_unreadable`（不変条件 1）。「読めない」を空の Result に潰さない。
* 位置引数は**後方互換で残す**。`scripts/kai-review.sh` は `"$PLAN_SH" done … "$DONE_MSG"` / `needs-director … "$1"`
  と**変数**で渡している。シェルは変数の展開結果を再展開しない（コマンド置換は**リテラルの**二重引用符本文だけの問題）
  ので危険が無く、変更していない（変えないことが受入条件）。
* 手順書（`agents/worker.md` / `director.md` / `verifier.md` / `skills/crewvia-qa` / `skills/crewvia-plan-review`）は
  Write で scratchpad に書いた `--result-file <path>`、Write が deny される skill（review / research / verify / planning）は
  `--result-file -` + クォート付きヒアドキュメントに書き換えた。

## 既知の制約: ヒアドキュメント本文と task ファイル書き込みガード

`--result-file -` + ヒアドキュメントの形は、通常の Result なら `hooks/pre-tool-use.sh` に allow される
（バッククォート・`$(...)` を含んでも）。ただし本文に**task ファイルへの書き込みコマンドの文字列**
（`cat >> queue/missions/…/tasks/tNNN.md <<'EOF'` など）をそのまま書くと、hook は Bash コマンド全体を
直接書き込みと見なして deny する（位置引数の引用テキストは t019 が除外するが、ヒアドキュメントの本文は除外対象外）。
その Result は Write ツールで書いた `--result-file <path>` で渡す。hook 側の緩和は本 task の範囲外
（緩めると t015 のガードの穴になる）。確認は 2026-09-30 に隔離した hook 呼び出しで実測した
（plain heredoc = allow / 本文にパス付き書き込み文字列 = deny / 実書き込み = deny / ファイル形 = allow）。

## 族の掃除（同じ「本文を二重引用符で渡す」型）

`plan.sh` の自由記述を取る引数を実装から列挙した:

| 引数 | 扱い |
|---|---|
| `done` の Result / `needs-director` の理由 / `verify-result --notes` | 上の表のとおりファイル・標準入力を追加 |
| `fail --no-head "<理由>"` / `done --no-pr "<理由>"` / `retire --reason "<1 行>"` | 1 行の理由。長い散文・コマンド例を入れる場所ではないので対象外。手順書は単一引用符 `'…'` を勧める（単一引用符は展開しない） |
| `add` の title / `--description` / `update --description` | Director が作る計画文。Worker の Result と違い実行結果の引用（コマンド例）を含まない。対象外（Director が長い説明を書くときは既存どおり単一引用符かヒアドキュメント） |
| `init` の title | 短い題名。対象外 |

`tests/test_plan_result_file.py::test_every_body_argument_in_the_implementation_is_in_the_table` が、
`--result-file` / `--notes-file` を宣言するサブコマンドを実装から拾って上の表と突き合わせる（増えたら赤）。

## 検証

* `tests/test_plan_result_file.py`: 対照（二重引用符の位置引数では sentinel が作られる = 危険の再現）/
  ファイル・標準入力・実シェルのヒアドキュメントで sentinel が作られず本文がそのまま残る /
  拒否 5 種 × 3 subcommand で card が変わらない / QA Gate・required_evidence がファイル経由でも効く
* 赤の実証: `tests/red_proof_c1_result_file.sh`（`--result-file` を持たない修正前の plan.sh に差し替えて赤くなる）

## 戻し方

共有規則（呼び出し元すべてが同じ plan.sh を通る）なので env の停止スイッチは付けていない。戻すときは
**PR revert → `scripts/sync-main-checkout.sh`**（主 checkout の ff とデーモン restart まで完結する）。
手順書だけ旧形式に戻っても位置引数は残っているので、Worker の呼び出しは壊れない
（`--result-file` を使った呼び出しだけが `unknown option` で拒否される — 何も書かれない）。
