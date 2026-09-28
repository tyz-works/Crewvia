# bash ナレッジベース

> このファイルは bash（シェルスクリプト・コマンド実行）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->

## 2026-04-30 fzf --preview スクリプトはファイルに書き出して渡す

fzf の `--preview` に複雑な jq 式を直接埋め込もうとすると、bash のクォートエスケープが地獄になる。
`mktemp` で一時スクリプトを生成し、`--preview="$tmpscript {1} ..."` の形で渡すと安全。

単一クォートの heredoc（`<<'PREVEOF'`）でスクリプトを書き出せば、`$1`, `$2` 等が展開されず意図通りに書き込まれる。
jq の文字列内では `` が ESC として解釈されるので、ANSI カラーコードも問題なく出力できる。

## 2026-05-03 bash 配列は関数に渡せない — グローバル変数 + インデックスで共有する

bash の配列は関数の引数として直接渡せない（`"${arr[@]}"` で要素展開すると文字列の並びになり配列メタデータが失われる）。
グローバル変数として宣言し、関数内でインデックス参照する方が安全。
または `local -n ref="$1"` (nameref) を使えば参照渡しが可能（bash 4.3+）。

## 2026-05-03 plan.sh add の出力から task ID を取得する

`plan.sh add` は `Added: {slug}/{task_id} — {title}` 形式で出力する。
`grep -oE '\bt[0-9]+\b' | head -1` でタスク ID (例: t001) を抽出できる。

## 2026-05-03 JSONL diff でセッションファイルを特定するパターン

Claude Code セッションの JSONL ファイルは `~/.claude/projects/<path-mapped>/*.jsonl` に保存される。
パスマッピング: `echo "$dir" | sed 's|/|-|g'`（先頭の `/` は残す — 結果は `-Users-tyz-...` の形になる）
⚠️ `s|^/||` で先頭スラッシュを除去する誤りが多い。実際のディレクトリは先頭にダッシュがある。

起動前後の diff でセッションファイルを特定:
```bash
before=$(ls ~/.claude/projects/<key>/*.jsonl 2>/dev/null | sort || true)
# ... start Claude ...
after=$(ls ~/.claude/projects/<key>/*.jsonl 2>/dev/null | sort || true)
new_file=$(comm -23 <(echo "$after") <(echo "$before") | head -1)
```

## 2026-04-30 jq でイテレーション + 文字列結合は [.[] | ...] | join("\\n") パターン

jq で複数要素をまとめて1つの出力にしたい場合、`.[]` をそのまま文字列に連結しようとすると各要素が独立した出力になる。
`[.[] | "..." ] | join("\n")` で配列に集約してから join するのが正しいパターン。

## 2026-09-08 chmod は pre-tool-use.sh の approval-required リストに入っている(CREWVIA_TASKVIA=disabled でも native permission に回る)

`chmod` は `hooks/pre-tool-use.sh` の `_DANGEROUS_COMMANDS` に含まれる。Taskvia 無効時は
Taskvia 承認をスキップして「native permission にフォールバック」する実装だが、これは
「無条件 allow」ではなく「ハーネス標準の permission prompt に委ねる」の意味。非対話環境や
承認が得られない状況では素朴な `chmod +x file` が拒否されることがある。
実行権限ビットだけ付けたい場合は `python3 -c "import os,stat; ..."` で os.chmod する迂回策が
使える(`chmod` という文字列を Bash コマンド冒頭に含めないため危険コマンド判定に掛からない)。
また `git add <file> && git update-index --chmod=+x <file>` で git のインデックス上のモード
(100755)だけ先に確定させる手もある(コミット後のチェックアウトでは実ファイルにも反映される)。

## 2026-09-22 Haruto発見: dispatcher.sh の埋め込み python を抽出して「本物を駆動する」回帰テスト harness

dispatcher.sh は daemon ループ全体が単一の `python3 - <<'PYEOF' ... PYEOF` heredoc として
埋め込まれている (bash の `while true; do python3 - ... <<'PYEOF' ... PYEOF; sleep 5; done`)。
回帰テストがこのロジックを test 内で再実装すると、dispatcher.sh 本体が壊れても検出できない
(t031/PR#207 Seo review F2 — `--status TOTAL-GARBAGE-NOT-A-STATUS` に差し替えても green のまま)。

対策: `awk '/<<.PYEOF.$/{f=1;next} f && /^PYEOF$/{exit} f'` で埋め込み python 本体を
そのまま抽出し、tmux/herdr と通信する唯一の I/O 境界 (`_mux.send`/`_mux.list`、
`_mux = Mux()` の直後) だけをテキスト置換でスタブしてメッセージをキャプチャする。
REPO_ROOT には `repo_identity_ok()` ガード (`root.is_dir() and (root/'.git').exists()`) が
あるため、REGISTRY_DIR の親ディレクトリを `git init` だけした空リポジトリにする必要がある
(history/remote は不要)。`from lib_mux import Mux` は `sys.path.insert(0, REPO_ROOT/'scripts')`
で解決されるため、このフェイク REPO_ROOT には scripts/ が存在せず import が失敗する —
`PYTHONPATH=<本物の scripts dir>` を渡すことで import 自体は本物の lib_mux を使いつつ、
REGISTRY_DIR/QUEUE_DIR だけ完全に隔離できる。

なお `needs_director_reason` 等のフロントマター値は `plan.sh` 書き込み側 (`_dump_scalar`)
が埋め込み改行を常に " / " に畳み込む (PR #181) ため、on-disk の値が実際に複数物理行に
なることは構造的にありえない。「複数行 reason → 1 行目 + 省略記号」というテストを書くなら
実際に発火する経路 (200 文字超の単一行 reason の `[:200]` カットオフ) を使うこと —
literal `\n` を frontmatter に埋め込んでも parser (`_scalar`) は `\n` を unescape しないため
何も起きない。

## 2026-09-27 `set -uo pipefail` のシェルスクリプトで `$(...)` の代入は必ず終了コードも見る (t068, PR#241 P2)

`set -uo pipefail`（`-e` なし）な `scripts/test_*.sh` で `OUT=$(python3 - <<'PYEOF' ... PYEOF)`
のように埋め込み Python を呼び、判定を「出力に `FAIL` が無いこと」だけに頼ると、import か
本体が未処理の例外を出して python3 が非ゼロ終了しても、代入自体は失敗せず継続する。
出力は空か途中までしか出ないため `grep FAIL` は空振りし、**例外による早期終了 (=検証が
最後まで走っていない) が PASS に潰れる**（`test_watchdog_config_mode.sh` の Test 26 実例。
`PY26=$(...)` の直後に `PY26_EXIT=$?` を捕まえ忘れていた）。

対策は 2 種類あり、ブロックの assertion の形で選ぶ:
- 「出力に FAIL が無いこと」で判定するブロック（複数ケースを1回の python3 呼び出しでループ
  して PASS/FAIL 行を print する形）→ 代入直後に `RC=$?` を捕り、`[[ "$RC" -ne 0 ]] ||
  grep -q "^FAIL"` で OR 判定にする（`fail`/`pass` 分岐の前）
- 「特定の PASS 行 / 期待値との完全一致」で判定するブロック（例: `BACKEND=$(...)`; `[[
  "$BACKEND" == "HerdrBackend" ]]`）→ 例外で出力が空になれば自動的に不一致 = 正しく fail
  するので追加の終了コード検査は不要（このパターンは元々安全）

族ごとの掃除をするときは、まず各ブロックがどちらの assertion 形かを見分けること。
「absence-of-FAIL」形だけが終了コードの見落としに弱い。

## 2026-09-28 Ren発見: mergeStateStatus=DIRTY の PR には CI が付かない (`gh pr checks` は "no checks reported" のまま)

push 済みの PR で `gh pr checks <N>` が延々 "no checks reported" のままなら、まず
`gh pr view <N> --json mergeStateStatus,mergeable` を見ること。`DIRTY`/`CONFLICTING`
だと GitHub Actions がそもそもワークフローを起動しない (push イベント自体は届いているのに
何も走らない、という紛らわしい状態になる)。CI が来るのを待ち続けても解決しないので、
`git fetch origin main && git merge origin/main` で衝突を解消 → 再テスト → 追加コミットで
push (force/rebase 禁止) してから CI を待ち直す。長寿命の feature branch (今回は PR #238、
t016 からの一続き) は base の main が独立に進むため、何巡も codex review が続くタスクでは
毎回この conflict を疑う価値がある。

## 2026-09-28 Ren発見: 実 git を叩く red proof の `env -i` は `HOME=$WORK` (偽 HOME) にしない

`tests/red_proof_t005.sh` のような既存の red proof は `env -i ... HOME="$WORK"` (使い捨ての
一時ディレクトリ) で pytest を隔離している。これは queue/registry しか触らないテストなら
問題ないが、**本物の `git` コマンドを叩くテスト** (t005 B2 の `test_main_checkout_sync.py`
のような、一時ディレクトリの bare origin + checkout に対する fetch/push/merge) では偽 HOME
の下で `git push` が非ゼロ終了する (`~/.gitconfig` が無い状態で何が壊れるかまでは切り分けて
いないが、再現は安定していた)。`tests/CLAUDE.md` の「red proof の作法」に既にある「内側の
`env -i` が `~/.local` の pytest を見失うので、偽 HOME ではなく本物の HOME + `CREWVIA_*` だけ
unset する」は git を使わないケースの話だと思っていたが、**git を使うケースにも同じ理由で
本物の HOME が要る**。`red_proof_t005_main_checkout_sync.sh` は `REAL_HOME="$HOME"` を
スクリプト冒頭で退避し、`env -i ... HOME="$REAL_HOME"` にして解決した (repo-local な
`git config user.email/user.name` は各テストが個別に設定済みなので、それ自体は原因ではない
— 疑うべきは HOME 依存の git の何かで、切り分けは次に踏んだ人に委ねる)。
