# plan.sh review 信頼性問題 修正ノート

**ミッション**: 20260908-launch-reliability (t002)
**対象ファイル**: `scripts/review-plan.sh`, `scripts/wait_for_plan_review.sh` (新規),
`scripts/normalize_plan_review_verdict.py` (新規), `hooks/pre-tool-use.sh`,
`config/skill-permissions.yaml`, `agents/plan_reviewer.md`, `scripts/plan.sh`
**関連テスト**: `scripts/test_plan_review_write_guard.sh`,
`scripts/test_normalize_plan_review_verdict.sh`, `scripts/test_wait_for_plan_review.sh`

---

## 症状 (2 ミッション連続で発生、毎回 Director の手動 workaround が必要だった)

`./scripts/plan.sh review <slug>` が 3 つの独立したパターンで壊れていた:

1. **verdict 形式不一致**: plan-reviewer (Opus) が規定形式 `**Verdict:** approve` ではなく
   `## 総合判定: **GO**` のように書き、`scripts/review-plan.sh` の
   `grep -q '^\*\*Verdict:\*\*'` に一致せず 600s タイムアウトする。しかも
   plan-reviewer が `mission.yaml` まで規格外の値 (`status: active` /
   `last_verdict: GO`) に書き換えてしまい、`plan.sh launch` (status=='ready' 必須)
   まで壊れる二次被害があった。
2. **出力先の詐称**: pane に「保存先: plan_review.md」と表示しながら実際には
   ファイルを一切書き出さない。
3. **【最重要】古い plan_review.md の誤採用**: `review-plan.sh` はレビュー開始前に
   既存の `plan_review.md` を削除していなかったため、前 cycle の残骸が残っていると
   待機ループが即座に成立し、**古い判定がそのまま新しい判定として採用される**。
   本ミッション自身の cycle 2 で実際に発生し、cycle 1 と完全に同一の内容
   (対応済みの指摘) が再度 `revise` として返り、`cycle_count` だけを浪費した。
   タイムアウトより質が悪い障害モード。

## 根本原因

パターン1の二次被害 (mission.yaml 破壊) の真因は個別に見つかった副次バグ:
**`review-plan.sh` は plan-reviewer 起動時に `CLAUDE_SKILL=plan_review` は
export していたが、`SKILLS=plan_review` を export していなかった。**
`hooks/pre-tool-use.sh` の per-skill 権限チェックは `SKILLS` 環境変数を見て
`config/skill-permissions.yaml` の該当セクションを適用するが、`CLAUDE_SKILL` は
どこからも参照されない死んだ変数だった。結果として plan-reviewer セッションは
`skill-permissions.yaml` の `plan_review` セクション (Bash 全面禁止 /
Edit・MultiEdit 禁止) を **完全にバイパスして** 動いていた。

さらに、たとえ `SKILLS` を正しく export しても、`skill-permissions.yaml` の
`plan_review.allow: Write` は非 Bash ツールの bare token signature
(`"Write"`, file_path を含まない) にしかマッチできないため、**「plan_review.md
だけ書ける」という制約はそもそも表現不可能** だった (実質「どの Write でも許可」
と同義)。`agents/plan_reviewer.md` の「plan_review.md 以外への出力禁止」は
プロンプト層のお願いに過ぎず、構造的な強制力が無かった。

## 対応 (Description の対応方針 1・2・4 + 必須項目をすべて実施)

### 1. mission.yaml を plan-reviewer に触らせない (方針4、最重要)

- `review-plan.sh` が plan-reviewer 起動時に `SKILLS=plan_review` を明示 export
  するように修正 (mux 経由・inline フォールバックの両方)。これにより
  `skill-permissions.yaml` の Bash 全面禁止 / Edit・MultiEdit 禁止が実際に働く。
- `hooks/pre-tool-use.sh` に **"Plan-review write scope guard"** を新設。
  `SKILLS` に `plan_review` が含まれるセッションの Write/Edit/MultiEdit/
  NotebookEdit を、書き込み先が `queue/missions/<slug>/plan_review.md` である
  場合のみ許可し、それ以外 (mission.yaml・task ファイル等) は deny する。
  skill-permissions.yaml の設定ミスや将来の緩和ではバイパスできない構造的ガード。
- `agents/plan_reviewer.md` の禁止事項に mission.yaml への書き込み禁止を明記し、
  Bash が使えなくなった分 Step 1 の手順を `Glob` ツールに置き換えた。

### 2. 古い plan_review.md の誤採用を防ぐ (必須パターン3、最重要)

- `review-plan.sh` はレビュー開始前に既存の `plan_review.md` を `rm -f` する。
- 開始時刻 (`REVIEW_START_EPOCH`) を記録し、ポーリング判定
  (`scripts/wait_for_plan_review.sh` に切り出し) は **mtime が開始時刻以降の
  ファイルだけ** を「今回の実行の出力」とみなす (rm が何らかの理由で効かなかった
  場合の二重の安全策)。古いファイルは無視して待ち続け、来なければ
  `TIMEOUT_NONE` としてタイムアウトする — 誤って古い判定を採用しない
  (「倒れる方向」を安全側に倒す)。

### 3. verdict 別表記の受理・正規化 (方針2)

- `scripts/normalize_plan_review_verdict.py` を新設。規定形式が無い場合のみ、
  `## 総合判定` 見出し付近から `GO`/`NO-GO`/`承認`/`却下`/`要修正`/`差し戻し` 等の
  既知の別表記を検出し、ファイル冒頭に規定形式の `**Verdict:**` 行を追記する
  (元の内容は残す。誤検知防止のため見出し付近のみを探索範囲にする)。
  判定不能な場合は何もせず失敗を返す (当て推量しない)。

### 4. plan_reviewer.md の指示強化 (方針1)

- ファイル冒頭に「★ 最重要」セクションを追加し、`**Verdict:**` 行を
  出力の1行目に置くことを最優先事項として明記。

### 5. タイムアウト時の挙動改善

- `scripts/wait_for_plan_review.sh` はタイムアウト理由を `TIMEOUT_FRESH`
  (ファイルは書かれたが判定が読めなかった) と `TIMEOUT_NONE` (ファイル自体が
  無い) に区別する。
- `scripts/plan.sh` の `cmd_review` は `TIMEOUT_FRESH` 相当 (= `plan_review.md`
  が存在するのに `review-plan.sh` が失敗) の場合、Director 向けのエラー
  メッセージで「再レビューではなく手動確認」を促す。

## 設計上のポイント: ポーリングロジックの切り出し

`review-plan.sh` 本体は `claude` CLI を spawn するため end-to-end ではテスト
しにくい。ポーリング判定だけを `wait_for_plan_review.sh` に切り出すことで、
claude を一切起動せずに3パターン (規定形式 / 別表記 / 前 cycle の残骸) を
決定論的に回帰テストできるようにした。同じ理由で verdict 正規化ロジックも
独立した Python スクリプトに切り出してある。

---

## 追記 (t004, mission 20260909-dead-config-sweep): 散文パースから構造化出力への移行

**症状**: 上記パターン1 (verdict 形式不一致) を PR #188 で「対応」したはずが、
その後も plan-reviewer (Opus) が禁止形式 (`## 総合判定: **GO**`) で出力する
事故が再発し、`normalize_plan_review_verdict.py` の別表記救済でも拾えず
600s タイムアウト → Director が capture して手動で `plan_review.md` /
`mission.yaml` を書く事態になった。**「プロンプト指示 (`★ 最重要` セクション)
だけでは機構として成立していない」ことの再実証**。

**検討した案** (Description 記載の A〜D):
- **(A) 別ファイルに1語だけ書かせる**: 採用せず。`skill-permissions.yaml` の
  `plan_review.allow: Write` が bare token signature (file_path 無し) にしか
  マッチできず「plan_review.md だけ書ける」制約が技術的に表現不可能な設計
  (このファイル冒頭「根本原因」参照) のため、追加ファイルを許可するには
  hook 権限をさらに緩めるか複雑化させる必要があり、新しい攻撃面/バグ面が増える。
- **(B) claude CLI の構造化出力機能を使う**: **採用**。`claude --help` で
  `--json-schema <schema>` (structured output validation) の実在を確認し、
  `claude --output-format json --json-schema '...'` を実機で検証 (Haiku で
  実行、`structured_output` フィールドにスキーマ通りの `{"verdict":...}` が
  返ることを確認)。Write 権限を一切変更せずに済む — 検証されるのは「セッション
  の最終応答」であり、`plan_review.md` への Write tool 呼び出しとは独立した
  チャネルのため。
- **(C) normalizer を賢くする**: 不採用。Description が明示するとおり、
  本 mission 内で kai-review.sh が「散文の判定は本質的にヒューリスティックで
  完全解が無い」ことを 6 回の実測欠陥で示した教訓と同型であり、normalize 側の
  対症療法をこれ以上重ねても同じ穴が別表記で再発するだけと判断。
- **(D) 現状維持**: 不採用。早期打ち切り (`_EARLY_BREAK_STREAK`) で待ち時間
  自体は既に軽減されているが、「Director の手動介入」自体は解消されておらず、
  実機で再発が確認されている以上、機構化の価値がある。

**実装**: `scripts/review-plan.sh` の plan-reviewer 起動コマンド (mux 経路・
inline フォールバック経路の両方) に `--output-format json --json-schema
<config/plan-review-verdict.schema.json>` を追加。既存の polling
(`wait_for_plan_review.sh`) / normalize 経路は**そのまま残し、置き換えない**
— これらが失敗した場合 (`WAIT_STATUS != OK`) のみ、`review-plan.sh` が
plan-reviewer プロセスの stdout ログ (`/tmp/plan_reviewer_$$.log`) から
`structured_output.verdict` を機械的に取り出し、`plan_review.md` 冒頭に
規定形式の行を追記して rescue する（`normalize_plan_review_verdict.py` と
同じ「原文は残す」idempotent な prepend パターン）。

**倒れる方向**: スキーマファイル欠如・JSON パース失敗・`"type":"result"` 行が
0件/複数件・verdict が3値以外、のいずれでも rescue は何もしない
(fail-closed。既存の polling/normalize 経路の判定をそのまま採用し、タイムアウト
なら Director 手動確認に倒れる — 当て推量で verdict を捏造しない)。mux 経路は
`review-plan.sh` が plan-reviewer プロセスの終了を直接待たない (`plan_review.md`
の mtime 安定化だけで判定) ため、rescue 実行前に短い bounded retry (最大 24秒、
失敗経路でのみ発火) を挟んで構造化出力の書き込みタイミングを待つ。

**副次効果**: `agents/plan_reviewer.md` の「★ 最重要」セクションから、散文
フォーマットの厳格な指示 (`**Verdict:**` を1行目に書けという命令) を撤去。
plan-reviewer は verdict の書式を一切気にする必要がなくなった (Write する
`plan_review.md` の内容は Summary/Issues として従来どおり重要だが、判定その
ものは CLI が強制する)。

**関連ファイル (追加分)**: `config/plan-review-verdict.schema.json` (新規),
`scripts/test_review_plan_json_rescue.sh` (新規)。既存の
`scripts/test_wait_for_plan_review.sh` / `scripts/test_normalize_plan_review_verdict.sh`
/ `scripts/test_review_plan_pane_leak.sh` / `scripts/test_review_plan_director_identity.sh`
は無改修のまま green (スキーマファイル欠如時は自動で機能を無効化し、既存の
scratch テストセットアップ — config/ を用意していない — でも review-plan.sh
全体を落とさない設計にしたため)。

---

## 追記 (t012, mission 20260909-dead-config-sweep): 同型 fail-open 7 回目で、判定の「場所」と「値」を閉じた文法に限定した

### 何が 7 回繰り返されたか

verdict 抽出は t004 以前からずっと、実質的に次の形をしていた:

> **ファイル全体を走査**して verdict らしき行を探し、その行の**中に** `approve`
> という語が**含まれていれば** approve。

この形には原理的に無限の抜け道が 2 系統ある。

1. **値の側 (部分一致)** — `not approve` / `approve できません` /
   `pending — do not approve yet` のように、approve を含みながら意味が反転する
   表現はいくらでも作れる。
2. **場所の側 (どこを本文とみなすか)** — ` ``` ` / `~~~` / 入れ子フェンス /
   閉じ忘れフェンス / HTML コメント / 引用 と、「本文に見えるが本文でない領域」の
   記法もいくらでもある。

QA は毎回「記法や言い回しを 1 つ変えるだけ」で新しい誤 approve を作れた。
とくに t010 の修正 (フェンスを正規表現 `` ```.*?``` `` で除去する) は、
` ``` ` が奇数個ある入力でペアリングがずれ、**フェンスの外にある本物の revise を
消して書式例の approve だけを残す**という、除去しなかった頃より悪い挙動まで
作った (QA t011 NEW-2)。「除去を賢くする」方向は袋小路である。

### t012 の不変条件

`scripts/lib_verdict.py` を次の 4 条件で作り直した。

1. **値は完全一致 allowlist** — 判定行の値部分が `approve` / `revise` / `reject`
   の**いずれか 1 語ちょうど**（前後の空白のみ許容、小文字）であること。
   部分一致・包含判定は禁止。`normalize_plan_review_verdict.py` の
   `APPROVE_EXACT` で先に到達していた結論を、規定形式側にも適用した。
2. **判定は 1 箇所からのみ読む** — **ファイルの最初の非空行だけ**を判定対象に
   する。ファイル全体を走査して「最初に一致した行」を採る形をやめた。
3. **それ以外はすべて判定不能** — approve に倒れる分岐をモジュールが持たない。
4. **除去ヒューリスティクスを主機構にしない** — 規定形式の判定からフェンス除去を
   **完全に削除**した。読む場所が 1 行目に固定されていれば、フェンスや HTML
   コメントの中身が判定対象に入り込む余地は原理的に無い
   (フェンス内の行が「最初の非空行」になるには開始記号の行がさらに手前に
   来るため、開始記号の行が読まれて不一致になる)。
   **記法を列挙しないので、記法を変えて抜けるという攻撃面自体が消える。**

補助として「1 行目と**異なる値**の完全一致 verdict 行が本文にもある場合は判定
不能に落とす」拒否専用スキャンを持つ。ファイル全体を見るが、結果を `None`
方向にしか動かさないため不変条件 2・3 を損なわない（この走査が approve を
生むことは原理的にありえない）。

### 厳しくした分をどこで回収するか — 構造化出力を主経路に昇格

上記のとおり `lib_verdict` は意図的に厳しく、書式を外した `plan_review.md`
(タイトル行が先にある / 値に註釈が付く / 判定が本文中にある) はすべて判定不能に
なる。その回収は `scripts/review-plan.sh` の構造化出力経路が担う。

t004 版はこれを「プローズ解析が失敗したときだけ動く後段の rescue」に置いていたが、
t012 で**プローズの成否に関わらず必ず読み、判定の権威とする**位置づけに変更した。
`claude --json-schema` が返す `structured_output.verdict` は CLI 自身が enum 適合を
保証した値であり、「フェンスの内側か」「否定文か」という曖昧性が原理的に存在しない
単一の判定 unit である (QA t008 が自作入力 25/25 で fail-closed を実測済み)。

- 構造化出力が取れ、プローズが判定不能 → 構造化出力の値を `plan_review.md` の
  **1 行目**に規定形式で書き戻す（原文は下に残す）。
- 両方取れて**食い違う** → **どちらも採らず判定不能に倒す**。同じセッションが
  自己矛盾しているなら判定は曖昧であり、`plan.sh` が cycle を refund した上で
  Director に手動確認を促す方が安全。
  （「厳しい側を書き戻す」案は却下: 書き戻すと `plan_review.md` 内に異なる値の
  verdict 行が併存し、上記の自己矛盾チェックで結局判定不能になるため二度手間。）
- 構造化出力が取れない (スキーマ欠如 / JSON パース失敗 / `"type":"result"` 行が
  0 件または複数件 / verdict が 3 値以外) → 何もしない。既存の判定をそのまま
  採用する (fail-closed)。

### 意図的な挙動変更 (安全側)

次の入力は t010 版では verdict が確定していたが、t012 では**判定不能**になる。
いずれも「危険側 (approve) に倒れない」方向の変更であり、本番では上記の構造化
出力経路が回収するため運用上の後退は無い。

| 入力 | t010 | t012 (プローズ単体) |
|---|---|---|
| `**Verdict:** approve (軽微な指摘あり)` | approve | 判定不能 (不変条件1) |
| タイトル行の後に `**Verdict:** approve` | approve | 判定不能 (不変条件2) |
| フェンス内 approve + 本文末尾の本物の revise | revise | 判定不能 (不変条件2) |
| `**Verdict:** APPROVE` (大文字) | approve | 判定不能 (不変条件1) |

`agents/plan_reviewer.md` には「1 行目に判定語 1 語だけを書く」という指示を
**機構と一致する形で復活**させた (t004 で撤去されていたもの)。指示に頼るのでは
なく、指示どおりに書かれた場所だけを読む、という関係になっている。

### 関連ファイル (t012 分)

`scripts/lib_verdict.py` (全面改訂), `scripts/normalize_plan_review_verdict.py`
(フェンス除去を、閉じ忘れを fail-closed に扱う行スキャナに置換。別表記探索
専用で主機構には不関与), `scripts/review-plan.sh` (構造化出力を権威に昇格),
`agents/plan_reviewer.md`, `scripts/test_lib_verdict.sh` (41 assertions に拡張。
フェンス記法バリエーション・値の否定形・HTML コメントを追加),
`scripts/test_plan_review_verdict_e2e_variants.sh` (25 variants に拡張。
QA t011 の危険側入力 8 件を e2e で恒久化),
`scripts/test_review_plan_json_rescue.sh` (8 cases に拡張)。

---

## 追記 (F1/F2/F3, t002 mission 20260912-verdict-ci-launcher): PR #199 QA (Finn) が実測した未解決3件の恒久化

t012 の後も、PR #199 (このブランチ) は QA で 3 回連続 FAIL していた
(memory: pr199-verdict-mechanism-handoff.md)。実測された3件をここで閉じた。

### F1 [高]: 不変条件が normalize 側に適用されていなかった (真の判定源)

`lib_verdict.extract_canonical_verdict()` は「最初の非空行だけを完全一致で
読む」形に閉じていたが、**その1行目を書き込む
`scripts/normalize_plan_review_verdict.py` の `find_alt_verdict()` は
今もファイル全体を走査し、`_strip_fenced_lines` / `_strip_html_comments` と
いう記法列挙の除去ヒューリスティクスを通していた**。blockquote と
インデントコードブロックを知らないため、前 cycle の判定を引用しただけの
入力 (`> ## 総合判定: **GO**` の後に保留の意思表示が続く) を誤って approve
と判定していた。t012 で主機構を厳しくした分、プローズで読めなくなった
ファイルが全部この経路に流れ込むようになっており、実質的な判定源は
lib_verdict ではなくここだった。

**対応**: `find_alt_verdict()` と、それ専用の stripper
(`_strip_fenced_lines` / `_strip_html_comments` / `_strip_non_prose_regions`)
を丸ごと削除。別表記の救済は `scripts/review-plan.sh` の構造化出力経路
(`claude --json-schema`) に一本化されており、この経路と役割が重複していた
うえ、不変条件を丸ごと迂回する唯一の穴になっていた。**`scripts/
normalize_plan_review_verdict.py` 自体を削除**し、
`scripts/wait_for_plan_review.sh` からの呼び出しも除去した (削除後は
lib_verdict が判定できなければ素直に判定不能 = 安全側に倒れる)。

### F2 [高]: 食い違い検出が approve 経路でだけ armed されない

`review-plan.sh` は「プローズが読めなかったとき (`WAIT_STATUS != OK`)」
だけ構造化出力の到着を待っていた。プローズが読めた瞬間 (=多くの場合
approve) は待たずに1回だけ非同期に読んでいたが、mux 経路では
`wait_for_plan_review.sh` が plan_review.md に verdict が現れた時点で即 OK
を返し、`"type":"result"` の JSON はセッション終了時に出るため**構造化ログは
必ず後から届く** — つまり approve 経路では構造化出力による確認が実質
一度も行われず、reviewer の最終応答が revise/reject でも mission が ready
になってしまっていた。

**対応**: 待つかどうかの分岐を `WAIT_STATUS` ではなく実際のプローズ判定
(`PROSE_VERDICT`) で決めるように変更。プローズが `approve` または判定不能の
場合は必ず構造化出力の到着を待ち、確認できなければ approve を通さない。
revise/reject は安全な結論なので従来どおり待たずにプローズを直接信頼する。

判定マトリクス (最終形):

    prose=approve   & structured=approve         → approve
    prose と structured が食い違う                 → 判定不能 (conflict)
    prose=approve   & 待ち切っても structured 無し  → 判定不能 (安全側)
    prose 判定不能   & structured=approve         → approve (救済、残す)
    prose 判定不能   & structured 無し            → 判定不能 (上と同じ経路)
    prose=revise/reject                          → そのまま採用

**待ち時間の設計**: 旧実装は固定 8×3=24秒 (プローズ失敗経路専用の値)。
Director 指摘: 「本番の実遅延を測るか、reviewer プロセス/pane の終了を
待つ設計にする」。実測手段が無かったため、ログサイズの quiescence を
完了の代理シグナルにする案を検討したが**却下**した — claude セッションは
長い思考や道具の実行で出力が止まることがあり、一時的な静止と本当の終了を
区別できない (F2 と同型の「見積もりが正常系を下回って安全側に誤爆する」
失敗を作る)。代わりに **「正常系の所要時間を言い当てる」ことを諦め、
失敗方向にしか効かない固定間隔ポーリング** にした
(`scripts/review-plan.sh` の `_wait_for_structured_verdict`)。見つかれば
即座に抜けるため正常系には影響せず、見つからない場合だけ
`REVIEW_PLAN_STRUCTURED_MAX_WAIT` (デフォルト180秒。正常系の見積もりでは
なく暴走防止の安全弁) を待ってから諦める。

### F3 [中]: `str.splitlines()` が `\n` 以外の8種+`\r`の行区切りを扱ってしまう

`scripts/lib_verdict.py` の `first_content_line()` / `extract_canonical_verdict()`
の自己矛盾チェックが `str.splitlines()` を使っていた。splitlines() は `\n`
以外にも `\r` / `\x0b` (VT) / `\x0c` (FF) / `\x1c` (FS) / `\x1d` (GS) /
`\x1e` (RS) / U+0085 (NEL) / U+2028 (LS) / U+2029 (PS) の計9種で分割するが、
呼び出し元 (grep・シェルの文字列比較) はいずれも `\n` だけを行区切りと
みなすため、`**Verdict:** approve<SEP>ではない。修正が必要です` の
`<SEP>` にこれらの文字を使うと lib_verdict だけが1行目を短く区切って
しまい、完全一致 allowlist を素通りしていた (ファイル経由の呼び出しでは
Python の universal newlines が `\r` を先に `\n` へ変換してしまうため、
`\r` 単体は file I/O 越しには顕在化しない — 直接 `extract_canonical_verdict()`
を呼ぶ経路や `newline=""` で開く将来の呼び出し元に対する防御)。

**対応**: `text.splitlines()` → `text.split("\n")` に置換 (2箇所)。9種
すべての回帰テストは `tests/test_lib_verdict.py` (pytest) に置いた —
`scripts/test_*.sh` は CI で実行されないため。

### 関連ファイル (F1/F2/F3 分)

`scripts/lib_verdict.py` (splitlines → split("\n")), `scripts/
normalize_plan_review_verdict.py` (削除), `scripts/wait_for_plan_review.sh`
(normalize 呼び出しの除去), `scripts/review-plan.sh` (F2 判定マトリクス +
`_wait_for_structured_verdict`), `tests/test_lib_verdict.py` (新規、pytest)、
`scripts/test_review_plan_json_rescue.sh` / `scripts/test_wait_for_plan_review.sh`
/ `scripts/test_plan_review_verdict_e2e_variants.sh` (F1/F2 の挙動変更に
合わせて期待値を更新・新規ケース追加)。
