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
  `config/crewvia.yaml` の `mode: hc`（同じ 2026-11-09 付で切り替え済み）だが 0 回だった —
  観測頻度の差は「タイミング依存のレース」（Claude の応答が速く、verify_sent のポーリングが
  画面を読むタイミングでエコーが既に描画されているかどうか）で説明がつき、**backend = hc
  であること自体が必要条件ではあるが十分条件ではない**と判断した（コード上 HcBackend だけが
  旧ヒューリスティックへ丸ごと委譲しているため、hc 以外の backend ではこの経路自体を通らない）。

**この監査結果にもとづき、HcBackend.verify_sent() だけを直した**（TmuxBackend / HerdrBackend /
base `Mux.verify_sent()` のフォールバックは触っていない — 現在の本番 backend ではなく、
この事故の観測にも紐づいていないため。推測で手を広げないという card の指示に従った）。

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

## 誤りの向きの確認 (card 要求 #3)

直した結果、「届いていないのに届いたと言う」側に倒れていないかを確認した:

- box が見つかり、かつテキストがまだ入力欄に残っている場合: `text[:30] in box` が真になり
  `verify_sent()` は `False`（未確認・再送）を返す。これは `RexBackend` で既にテストされている
  経路 (`test_verify_sent_false_while_text_is_still_in_the_input_box`) と同一のロジックなので、
  新しい偽陽性 (false positive = 届いていないのに届いたと誤判定) を導入していない。
- box が見つからない場合は旧ヒューリスティックにフォールバックするので、判定できない画面の挙動は
  変えていない（従来どおり）。
- 唯一変わるのは「box が見つかり、かつテキストが box の外（= 既にエコーとして会話記録に出た後）に
  しか無い」場合で、これは **以前 False (誤検知) だったものが True (正しく確認) になる**方向だけ
  （逆方向の変化は無い）。

**結論**: この変更は「届いていないのに届いたと言う」側には倒れていない。むしろ逆方向の既知の誤検知
（「届いているのに届いていないと言う」→ 重複送信）だけを減らす、一方向の修正。

## 既存の実装・テストとの整合性

- `tests/test_hc_backend.py` の既存 2 本（`test_verify_sent_falls_back_to_text_in_input_line` /
  `test_verify_sent_true_when_text_left_the_input_line`）は、どちらも罫線を含まない screen
  （box が見つからない = フォールバック経路）なので、**無改修で緑のまま**（契約を変えていない
  ことの陽性対照として残してある）。
- 新しく `tests/test_rex_pr2_wiring.py` と同じ fixture 形（`_RULE = "─" * 40` の間に `❯` の
  プロンプト）を使う回帰テストを `tests/test_hc_backend.py` に追加した。

## 対象外にしたもの (このミッションのスコープ外。推測で広げない)

- `TmuxBackend` / `HerdrBackend` と base `Mux.verify_sent()` のフォールバック —
  今の本番 backend ではなく、この事故の観測にも紐づいていない。直すなら別途、観測された事故が
  出てから再検討する。
- `lib_irc_relay.py` の `input_box_state()` 自体を `lib_mux.py` に寄せて単一実装にする統合 —
  スコープが異なる（ダイアログ / busy 判定を含む）ので、このミッションでは行っていない。
  「判定ロジックが 2 箇所にある」の解消は `_rex_input_box()` を hc に広げたことで、
  **start.sh の kickoff 確認経路については**解消済み。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。`HcBackend.verify_sent()` は PR #332 当時の
`_text_in_input_line()` 一本の実装に戻る（= この文書の「事故」の状態に戻る）。他のファイルは
一切変更していない。
