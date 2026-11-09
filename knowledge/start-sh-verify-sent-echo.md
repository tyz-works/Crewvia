# start.sh の kickoff 確認が「届いているのに届いていない」と誤検知する件 (t015 / 20261109-codex-hc)

## 事故

```
[crewvia] WARNING: kickoff message not confirmed in <name>-worker (attempt 1/3)
...
[crewvia] ERROR: kickoff message did NOT land in <name>-worker after 3 attempts.
```

実際には届いている。`lib_mux.py capture` で見ると kickoff が**2 回**入っており（初回 + 再送）、
Worker は正常に `plan pull` して作業を始めていた。誤検知が重複送信を生んでいる。
今のところ Claude Code が 2 つ目のプロンプトを無害に扱うので実害は出ていないが、
安全弁が「届いているのに届いていないと言う」側に壊れている。

- 2026-09-13 以前から散発。2026-11-06 に Minjun / Mateo の起動で 2 回観測
- mission `20261109-hc-production-switch`（mux backend を herdr-core / hc へ切り替えたミッション）
  だけで **8 回**観測
- mission `20261109-codex-hc`（本件。既に `mode: hc` が本番設定）では Worker 起動 10 回以上で
  **0 回**（全て verified）

## 再現条件の特定 (t015。推測ではなくコード監査 + 既存証拠の突き合わせ)

**このセッションでは実機で再現を取っていない** — 生きた hc ペインが無いため。ただし以下の
コード監査が、観測された事実すべてと矛盾なく説明できる唯一の経路だった:

- `scripts/lib_mux.py` の `Mux.verify_sent()`（旧実装）は `RexBackend` と `HcBackend` だけを
  `isinstance` で特別扱いし、`RexBackend.verify_sent()` は PR #342 / #343（IRC 中継の配達確認を
  直したときに同じ所を触った）で既に「入力欄の枠の中だけを見る」判定 (`_rex_input_box()`) に
  直っていたが、**`HcBackend.verify_sent()` 自身は直っていなかった** — 自分の docstring に
  「today, the same heuristic」「a documented no-op fast path」と明記されたまま、PR #332
  （Rex PR-2）以降ずっと `_text_in_input_line()`（旧ヒューリスティック）を使い続けていた。
- `_text_in_input_line()` は画面の末尾 `tail_lines`（`verify_sent` は 30 行）の中で `❯` から
  始まる行を**無差別に全部**拾い、その行に送った文の先頭 30 文字が含まれていれば「まだ入力欄に
  残っている」と判定する。Claude Code は送信済みのメッセージを会話記録に `❯ <message>` として
  **エコー表示する**ため、kickoff が先頭 30 文字で区別できる長さ（Director の固定長プレフィクス
  で揃っている）だと、**エコー行そのものを「まだ入力欄にある」と誤認する**。
- `mission 20261109-hc-production-switch` は文字どおり mux backend を hc へ切り替える
  ミッションで、8 回の観測はこの切り替え検証中に集中している。本件 (codex-hc) は
  `config/crewvia.yaml` の `mode: hc`（同じ 2026-11-09 付で切り替え済み）だが 0 回だった。
  **この観測差は「backend = hc が必要条件」を意味しない**（t018 / Seo のレビューで訂正）:
  `Mux.verify_sent()`（`scripts/lib_mux.py:6305` 付近）の `isinstance` 分岐は
  `(RexBackend, HcBackend)` だけで、**herdr / tmux は base の `_text_in_input_line(...)`
  経路に落ちる = hc とまったく同じ旧ヒューリスティックを今も使っている**
  （`TmuxBackend` / `HerdrBackend` は `verify_sent` を override していない。
  `def verify_sent` は `RexBackend` / `HcBackend` / `Mux` facade の 3 か所だけ）。
  さらに mode: が hc になったのは 2026-11-09（commit `51541bd`）で、それ以前（2026-11-08 まで）
  は herdr だった — つまり「2026-09-13 以前から散発」「2026-11-06 に 2 回」という観測自体が
  **`HcBackend` が存在すらしない時期を含む**（`HcBackend` の追加は 2026-11-06 の #345 /
  `d0a966f`）。したがって観測頻度の差（8 回 vs 0 回）は backend の種類ではなく、**タイミング
  依存のレース**（Claude の応答が速く、verify_sent のポーリングが画面を読むタイミングで
  エコーが既に描画されているかどうか）だけで説明できる。

**この監査結果にもとづき、HcBackend.verify_sent() だけを直した**（TmuxBackend / HerdrBackend /
base `Mux.verify_sent()` のフォールバックは触っていない — この PR のスコープ外として**未対応
のまま残っている**。推測で手を広げないという card の指示に従ったが、上の訂正のとおり
**同じ欠陥がそのまま残っている**ことは明記しておく）。

**運用上の帰結**: `config/crewvia.yaml` の `mode: hc` の行には `# 戻す時は herdr` とコメントが
付いている。**hc から herdr に戻すと、この文書が直す前に書いていた「herdr はこの事故に紐づいて
いない」という読み方は誤りで、実際には同じ旧ヒューリスティック（エコー行を入力欄の中身と誤認する
欠陥）がそのまま残る**。本リポジトリには、doc の誤った根拠が判断を誤らせた前例がある
（commit `48461e73` / `knowledge/codex-reviewer.md` の t005 節）。herdr / tmux 側の修正自体は
このミッションのスコープ外（下の「対象外にしたもの」）。

## 選んだ対応 — IRC 側の判定に寄せる (判定ロジックの一本化)

PR #342 / #343 で IRC 中継の配達確認を直したとき、`lib_irc_relay.py` に
`input_box_state()` / `classify_pane()` という「入力欄の枠の中だけを見る」判定が入った
（会話記録のエコーを送信成功と誤読して再送する経路を塞ぐため）。`RexBackend.verify_sent()` は
同じ考え方を `_rex_input_box()`（`lib_mux.py` 内の同等関数）で既に実装済みだった。

`HcBackend.verify_sent()` を次の形に書き換えた（`RexBackend.verify_sent()` と同一のロジック）:

```python
screen = self.capture(name)
if not screen:
    return False
box = _rex_input_box(screen)
if box is None:
    return not _text_in_input_line(screen, text, tail_lines=_VERIFY_SENT_TAIL_LINES)
return text[:30] not in "\n".join(box)
```

`_rex_input_box()` は Rex 専用ではない純粋関数（画面の文字列から、2 本の横罫線 `─`×20+ に挟まれ、
直後の行が `❯` で始まる区間を探すだけ）。hc の `capture()` も同じ Claude Code TUI を screen-scrape
しているため、Rex と同じ関数がそのまま使える。入力欄の枠が見つからない（ダイアログ・スピナー再描画中
など）場合だけ、従来の `_text_in_input_line()` にフォールバックする — これは `RexBackend` が元々
持っていたフォールバックと同じで、**判定できない画面では以前と同じ挙動のまま**（悪化させていない）。

判定ロジックを増やしたのではなく、**既存の 2 つの実装（Rex の box 判定 / hc の旧タイル走査）を
box 判定の 1 つに寄せた**。`lib_irc_relay.py` 側の `input_box_state()` 自体はコピーしていない
（IRC 側は配達判定以外にダイアログ / busy 判定も持つ独立した純粋関数群で、スコープが異なる。
`lib_mux.py` 内で完結する `_rex_input_box()` を再利用する方が最小の変更で済む）。

## P2: `_rex_input_box()` の枠の選び方が危険な向きに反転する (t018 / Seo の差し戻し。attempt 2 で修正)

attempt 1 の `_rex_input_box()` は画面の**先頭から最初の**罫線ペアを採っていた
（`for a, b in zip(rules, rules[1:])` の最初の該当を return）。そのため、**古い空の枠が画面の
上の方に残っている**（前の再描画の残骸がスクロールバックに残る等）と、そちらを「入力欄」と誤認し、
下の方にある生きた枠にまだ残っている本文を見落とす。Seo が実装を直接動かして作った真理値表:

| ケース | 旧 (`_text_in_input_line`) | attempt 1 (box・先頭から最初) |
|---|---|---|
| エコーのみ（事故の形） | False | True ← 直った |
| 本当に枠の中に残っている | False | False ← 維持 |
| **古い空の枠が上にあり、生きた枠に本文が残る** | False | **True ← 危険な向きに反転** |
| 枠が無い（ダイアログ / スピナー） | False | False ← 変化なし |

3 行目が「届いていないのに届いたと言う」側の新しい誤検知で、二重送信（元の事故）より明確に悪い
（kickoff が入っていないのに入ったことにされ、再送もされない）。

**直し方**: `lib_irc_relay.input_box_state()` は末尾 `_PROMPT_WINDOW`（12 行）から**下向きに最後の
`❯`** を探す（コメントに「全画面を探さない = スクロールバックの古い `❯` を拾わない」と明記）。
`_rex_input_box()` も同じ末尾窓・同じ探索方向（`_REX_PROMPT_WINDOW` = 12・`_REX_BOX_MAX_LINES` = 8）
に直した。画面の末尾に近い、生きている枠だけを見るようになり、上の真理値表の 3 行目も `False`
（未確認・再送なし）に戻った。

`_rex_input_box()` は `RexBackend.verify_sent()` 自身も使う純粋関数なので、この修正は Rex 側の
挙動にも等しく効く（Rex 側の既存テストは全部緑のまま — 単一の枠しか無い画面では探索方向を
変えても結果は変わらないため）。hc の `capture()` は `hc read --lines 200` で履歴 200 行を読むため
（Rex より古いフレームを拾いやすい構造）、この P2 は hc で特に踏みやすい。

## 誤りの向きの確認 (card 要求 #3。P2 修正後の最終形)

直した結果、「届いていないのに届いたと言う」側に倒れていないかを確認した（上の真理値表の 4 行
すべてが固定されている。回帰テスト: `test_verify_sent_false_when_a_stale_empty_box_sits_above_the_live_box`
系。「赤の実証」は attempt 1 の `_rex_input_box()`（先頭から最初の枠を採る版）に戻すと、この
テストが `True` を返して落ちることで確認済み）:

- box が見つかり、かつテキストがまだ入力欄に残っている場合（本当に枠の中に残っている / 古い空の
  枠が上にあり生きた枠に本文が残る、のどちらも）: `text[:30] in box` が真になり `verify_sent()` は
  `False`（未確認・再送）を返す。
- box が見つからない場合は旧ヒューリスティックにフォールバックするので、判定できない画面の挙動は
  変えていない（従来どおり）。
- 変わるのは「box が見つかり、かつテキストが（生きている）box の外（= 既にエコーとして会話記録に
  出た後）にしか無い」場合だけで、これは **以前 False (誤検知) だったものが True (正しく確認) に
  なる**方向だけ（逆方向の変化は無い）。

**結論**: この変更は「届いていないのに届いたと言う」側には倒れていない。むしろ逆方向の既知の誤検知
（「届いているのに届いていないと言う」→ 重複送信）だけを減らす、一方向の修正。

## 既存の実装・テストとの整合性

- `tests/test_hc_backend.py` の既存 2 本（`test_verify_sent_falls_back_to_text_in_input_line` /
  `test_verify_sent_true_when_text_left_the_input_line`）は、どちらも罫線を含まない screen
  （box が見つからない = フォールバック経路）なので、**無改修で緑のまま**（契約を変えていない
  ことの陽性対照として残してある）。
- `tests/test_rex_pr2_wiring.py` と同じ fixture 形（`_RULE = "─" * 40` の間に `❯` の
  プロンプト）を使う回帰テストを `tests/test_hc_backend.py` に追加した（attempt 1）。
- attempt 2 (P2) で、`tests/test_rex_pr2_wiring.py` /
  `tests/test_hc_backend.py` の両方に、古い空の枠が上にあり生きた枠に本文が残っている画面での
  `verify_sent() is False` を固定する回帰テストを追加した
  （`test_verify_sent_false_when_a_stale_empty_box_sits_above_the_live_{box,hc_box}`）。
  `Mux.verify_sent()` は `RexBackend` / `HcBackend` の両方で `_rex_input_box()` を共有するので、
  どちらのテストも同じ修正を固定する。

## 対象外にしたもの (このミッションのスコープ外。未対応として残っている)

- `TmuxBackend` / `HerdrBackend` と base `Mux.verify_sent()` のフォールバック —
  上の訂正のとおり、こちらも **同じ旧ヒューリスティック（エコー行の誤認）をそのまま持っている**
  （「今の本番 backend ではなく、この事故に紐づいていない」という t015 初回の判断は誤りだった）。
  この PR では直していない。`mode: hc  # 戻す時は herdr` で戻した先にこの欠陥が残ることは
  上の「運用上の帰結」に明記した。base 経路自体を直すかどうかは Director が別 card として
  判断する。
- `lib_irc_relay.py` の `input_box_state()` 自体を `lib_mux.py` に寄せて単一実装にする統合 —
  スコープが異なる（ダイアログ / busy 判定を含む）ので、このミッションでは行っていない。
  「判定ロジックが 2 箇所にある」の解消は `_rex_input_box()` を hc に広げたことで、
  **start.sh の kickoff 確認経路については**解消済み。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。`HcBackend.verify_sent()` は PR #332 当時の
`_text_in_input_line()` 一本の実装に戻る（= この文書の「事故」の状態に戻る）。**`_rex_input_box()`
の末尾窓・下向き探索（P2 の修正）も一緒に戻るため、`RexBackend.verify_sent()` も attempt 1 の
「先頭から最初の枠」の危険な挙動に戻る** — hc だけでなく Rex にも影響する revert である点に注意。
他のファイルは一切変更していない。
