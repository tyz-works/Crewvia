# review-bench — レビュー先 (claude / agy) を Codex の指摘で採点するキット

Codex をやめる判断の材料。過去に Codex がレビューした 15 巡 (PR #312 / #313 / #314) を、claude / agy に同じ条件で
やらせ直し、Codex の P1/P2 18 件をどれだけ拾えるかを数える。元は mission `20261028-codex-free-review` の t005
（結果は `queue/archive/20261028-codex-free-review/tasks/t005.md` の Result）。

## 中身

| ファイル | 役割 |
|---|---|
| `run.sh` | runner。巡ごとに隔離 repo を作り、engine で 1 回レビューさせる |
| `rounds.tsv` | 15 巡 (label・head sha)。base は `git merge-base <head> origin/main` |
| `ground-truth.tsv` | 正解表: Codex の P1/P2 18 件 (巡・1 行要約・fix commit) |
| `claude-only.tsv` | claude だけが挙げた妥当な指摘 4 件 |
| `prompt.txt` / `schema.json` | 前回と同じ prompt・出力 schema（比較のため固定） |
| `gen_checklist.sh` | 攻めのチェックリストを正本 `knowledge/review-attack-checklist.md` から生成する。採点対象 PR (#312-#314) の事例の行は除く（reviewer が正解表の答えを読めないように）。型の一般論は残る |
| `check_contamination.py` | 汚染検査 |
| `tests/test_run_args.sh` | runner の引数解析だけの軽いテスト (`bash tests/test_run_args.sh`) |

`out/` は実行の出力。commit しない（`.gitignore` 済み）。本番の queue / registry / config には書かない。
claude / agy のログインが要るので **CI には載せない**。

## 実行手順

```bash
cd bench/review-bench
bash run.sh --engine claude --dry-run                 # 走らせる巡の確認 (何も起動しない)
bash run.sh --engine claude --rounds pr314-r2         # 1 巡だけ
bash run.sh --engine claude                           # 15 巡 (1 巡 約 100〜150 秒・15 巡で約 13 USD)
bash run.sh --engine agy                              # 枠が戻ってから (下記)
```

- 出力: `out/<label>.<engine>.{raw,json,err,rc,dirty}`。`json` があれば再実行で飛ばす (途中から再開できる)。
- 隔離: 巡ごとに `git init` + その head までの履歴だけ fetch した repo (後の commit・他の ref・queue/・worktrees は無い)。
  claude は `Read` を clone と diff dir に絞り、Bash/Write/Edit 等は禁止、env は allowlist。diff は argv に載せずファイルで渡す。
- `*.dirty` は読み取り専用のはずの clone に変更が出ていないかの記録 (空が正常)。
- `--src` で commit の持ち主の repo を、`--out` で出力先を変えられる。

## 脅威モデル

Director が手で回す計測道具。入力（`rounds.tsv`・正本の checklist・`--src` の repo・claude/agy 自体）は信頼する。
守るのは **黙った誤採点だけ**（正解を読んだ巡・clone が汚れた巡・検証できなかった巡を、採点に混ぜない）。悪意ある入力や回避は対象外。
そのため runner は、diff/log/checklist の生成と setup の失敗で巡を止め（rc 94-96）、engine の前後で clone が `HEAD = head sha` かつ clean かを検査する
（前でずれ = 95 で走らせない・後でずれ = 97 で `.json` を捨て `.rc` に `tainted_clone`）。汚染検査は判定できないとき exit 3（採点しない）。

## 汚染検査

claude の session jsonl (`~/.claude/projects/<clone の / . _ を - に置換>/*.jsonl`) の Read / Grep / Glob の path が
clone と diff dir の下かを調べる。**採点の前に必ず回す**（外を読んだ巡は採点から外す）。

```bash
python3 check_contamination.py --clone out/rounds/pr314-r2 --diff-dir out/diffs/pr314-r2.claude   # 0 = 外を読んだ記録なし
```

exit 1 = 外を読んだ疑い (`OUTSIDE <tool> <path>` を出す) / exit 2 = jsonl が無く検査できない（その巡は採点しない）/ exit 3 = 壊れた JSON 行・path が無い／文字列でない Read・検査した tool_use が 0 件（inconclusive。汚染なしとは言えないので採点しない）。
`--no-session-persistence` を付けて実行すると記録が残らず検査できない（前回の 1 回目はそれで採点に使えなかった）。
agy は jsonl が無いので、この検査は claude だけ。

## 採点の基準

Codex の P1/P2 は全て fix 済みなので妥当とみなす。巡ごとに、engine の findings を `ground-truth.tsv` の行と突き合わせる:

- **命中**: 同じ欠陥を P1/P2 で挙げた
- **弱い命中**: 同じ欠陥を P3 に降格して挙げた（再現率は「命中のみ」と「弱い命中を含む」の両方を出す）
- **見逃し**: 挙げていない
- engine だけが挙げた P1/P2 は別に数える: 妥当 (`claude-only.tsv` のように後の fix・別 engine の同意で確認できたもの) / 未確認 / 誤検知。
  Codex が LGTM を出した巡で P2 を出すと、その巡は needs-director になる（人手が増える側）ので未確認は分けて書く。

## agy の枠切れの見分け方

agy の出力は `validate_agy.py`（status SUCCESS・denied_actions 空・schema 適合。本番の kai-review.sh と同じ基準）を通ったときだけ `.agy.json` になる（通らなければ `.agy.rc` に `agy_invalid`）。`out/<label>.agy.rc` が `rc=3` で、`.err` に `Individual quota reached` / `RESOURCE_EXHAUSTED 429` /
`Resets in 127h…` が出ていれば枠切れ。**未採点**にする（0 件の指摘として数えない）。枠が戻ってから同じコマンドで再実行すればよい
（`.json` が無い巡だけ走る）。

## 前回の結果 (2026-10-28・t005)

- claude (Opus 5.5・`--permission-mode plan`): P1/P2 18 件のうち命中 **7 (39%)**、弱い命中 4 を足して 11 (61%)、見逃し 7。
  見逃しの傾向は「文書の断定が現状と食い違う」「入力の型・欄の検証の網羅」「指紋の中身」「別プロセスの更新漏れ」。
  claude だけの妥当な指摘 4 件（`claude-only.tsv`）、未確認 3（Codex LGTM の巡の P2）。明確な誤検知は無し。
- 結論: claude 単独は Codex の代わりにならない。補助 (併用) としては価値あり。
- agy: 15/15 が個人枠切れ (429, 約 127 時間 = 11/2 ごろ) で **未採点**。合議 (ensemble) の効果も未測定。
- 汚染: claude 15 実行すべて 0（tool_use 257 件の path を検査）。
- 注意: 前回の依頼文は 14 巡だったが、実際の spawn ログは #314 が 5 巡で **15 巡**。#312 の最終 LGTM (b8d9e7d) は含めない。
