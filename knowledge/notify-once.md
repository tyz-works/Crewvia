# 状態ベースの通知は、状態が変わるまで 1 回だけ (t010 / backlog #10 + #11)

mission `20260925-ops-gap-fixes`。出典: `~/obsidian/proposals/crewvia/20260912_crewvia-backlog-after-verdict-ci-launcher.md`

## 何が起きていたか

2026-09-25、同一内容の通知が数十通 Director に届き、**ユーザーがデーモンを手で止めた**。

`dispatcher.sh` の `should_notify()` は `NOTIFY_TTL=300` 秒の **スロットルであって受領確認ではない**。
`needs_director` や `failed` + `handoff_path` のような **状態ベース** の通知は、状態が続くかぎり
TTL が切れるたびに再送された。対処済みの FAIL でも 5 分ごとに永久に届く。スロットルを長くしても
直らない — 問題は「永久に再送される」ことであって、間隔ではない。

#11 は同じ根の別の顔。`kai-review.sh` は差分が `MAX_DIFF_BYTES` (300KB) を超えると codex を呼ばず
`needs-director` に倒す (fail-closed。これは正しい)。しかし拒否の事実がどこにも残らないので:

```
dispatcher が kai-review.sh を spawn → 拒否 → needs_director (通知が永久再送)
  → Director が pending に戻す → dispatcher がまた spawn → また拒否 → ...
```

同じ PR は何度やっても同じ大きさなので、再 spawn は必ず同じ結論に戻る。

## 直し方

### 1. 「伝えた」台帳 (`registry/daemons/notified-state.json`)

スロットルとは別に、「この状態については既に伝えた」を持つ (`.gitignore` 対象)。

```json
{"needs_director_<slug>_<task>": {"fp": "36b17670084abe2c", "kind": "needs_director", "slug": "...", "task": "t001"}}
```

- `fp` は **通知内容を決める入力** の畳み込み: needs_director は `reason` (+ 拒否記録)、
  handoff は `handoff_path`、review-refused は `PR番号 / 実バイト数 / 上限`。**入力が変わったときだけ**
  再通知する。
- **状態を離れたら記録を捨てる** (`prune_told`)。Director が pending に戻し、同じ理由でまた落ちたのは
  新しい事象で、黙っていてはいけない。破損カードのある mission は「観測できなかった」ので触らない
  (観測の失敗を「状態を離れた」の証拠にしない)。
- スロットルの key に fingerprint を含める (`<key>#<fp>`)。状態が変わったら残りのスロットルに
  遮られず届く。台帳に書けなくても、直後のサイクルで同じ通知が飛ばない。
- **台帳の記録を捨てるとき、対応するスロットルも捨てる** (t021 / Kai P2)。fingerprint を含めた
  `<key>#<fp>` は「同じ状態」を同じ key にするので、離脱→同じ理由で再入 (fp が同じ) や
  A → B → A (3 回目の A が 1 回目の A と同じ fp) では、台帳が「伝えていない」と言っても残った
  スロットルが `NOTIFY_TTL` のあいだ新しい事象の通知を遮っていた。捨てる場所は 2 つ:
  (a) `prune_told` — 状態を離れたとき `<key>#*` を全部捨てる、(b) `record_told` — 同じ key の
  fp が変わったとき (A → B) `<key>#<旧fp>` を捨てる (今送った分は残す)。
  - 「状態の回」をスロットル key に入れる案は採らなかった: 回の識別子を持てるのは台帳だけで、
    台帳が使えないとき (= 再送側に倒したいとき) に key が定まらず、連射防止が効かなくなる。
    捨てる案は、離脱を**観測できたとき**にしか捨てないので、倒す向きが変わらない。
  - 残る穴 (許容): 台帳が読めない/書けない間は離脱を観測できないので、離脱→再入の通知は
    最大 `NOTIFY_TTL` 遅れる (その間も `WARNING: notified-state` は出ている)。欠落ではなく遅延。
- 送れなかった通知 (mux send 失敗・Director 不在) は **記録しない** — 戻ったらすぐ送る。
- 順序 (安い判定を先に): 台帳 → スロットル → Director 不在ガード → 送信。共有スロットルを
  役割ゲートより前に出さない (別の役割の通知が飢えた実例がある。
  memory: shared-throttle-before-role-gate-starves)。
- **Director 生存確認 (`mux list`) は遅延評価**: `notify_state_once(director_live=<関数>)` と
  関数のまま渡し、台帳とスロットルを通り抜けて実際に送ろうとしたときにだけ呼ぶ。サイクル内の
  結果は使い回す。以前はループの前で無条件に呼んでいたので、通知対象が無い idle サイクルでも
  mux への問い合わせが増えていた (QA t011 実測: main 2 回 → PR 3 回/サイクル)。
  今は通知対象が無い / 伝え済みのサイクルでは 0 回。

### 置き場が「無い」と「使えない」を分ける

| 台帳の状態 | 読み | 挙動 |
|---|---|---|
| ファイルが無い (ENOENT) | 初回。普通 | 空として始め、最初の送信で作る。ログなし |
| 読めない / 壊れている / ディレクトリが作れない・書けない | **起動失敗** (「通知すべきものが無い」ではない) | `WARNING: notified-state: ...` を TTL に 1 回出し、**スロットルだけの旧挙動 (再送側) に倒す**。壊れた JSON は次の記録で作り直す (自己修復) |

倒す先が「再送」なのは、通知の欠落 (t027 の 10 時間全停止) のほうが再送より高くつくため。
`Unreadable` を空の入れ物として扱わない (`lib_task_cards.py` の規則)。

### 2. codex-review の拒否記録 (`registry/daemons/review-refusals/<mission>__<task>.json`)

`scripts/lib_review_refusal.py` が唯一の定義 (書き手: `kai-review.sh`、読み手: dispatcher)。

- `kai-review.sh` は差分サイズ超過で **`needs-director` より先に** `{pr, diff_bytes, max_bytes}` を書く
  (needs_director に見えた時点で記録が既にある、という順序)。書けなくても拒否自体は止めない。
  `--dry-run` は書かない。
- dispatcher は unblocked-pending の codex-review task について、記録が **ある間は spawn しない**。
  Director に **1 回だけ**「手動差分レビューに切り替えよ」+ PR 番号 + 実バイト数・上限・超過分を伝える。
  needs_director 通知にも同じ 1 文が付く。
- **記録が読めない / 壊れている → spawn を保留** (「拒否されていない」に倒さない。壊れた記録 1 枚で
  ループが戻る)。記録が **無い** (ENOENT) だけが「拒否されていない」。
- **「壊れている」は欄の値まで見る** (t021 / Kai P2)。`load()` は欄が在るだけでは受理せず、
  `pr` = 正の整数 (または十進表記の文字列) / `diff_bytes`・`max_bytes` = 0 以上の整数
  (null・文字列・bool は不可) / 記録の `mission`・`task` = 置き場所 (ファイル名) と一致、を
  満たさなければ `Unreadable` を返す。検証しないと 2 通りに壊れる: `diff_bytes: null` は
  `describe()` を TypeError で落とし、**通知の組み立てが dispatch サイクル全体を中断して
  後続タスクと通知が全 mission で止まる**。`pr` が不正だと `refused_for_pr()` が偽になり、
  拒否チェックを迂回して再 spawn ループが戻る。dispatcher 側の `review_refusal_for()` にも
  想定外の例外を「保留」に倒す backstop がある。

#### Director が意図して再試行する経路

| やりたいこと | やること |
|---|---|
| PR を分割した / 出し直した | `plan.sh update <id> --pr-number <新PR> --mission <slug>` (記録の PR 番号と食い違えば別の PR なので拒否は効かない) |
| 同じ PR のまま codex-review を再試行 | `python3 scripts/lib_review_refusal.py clear --mission <slug> --task <id>` |
| task を作り直す | 何もしない (記録は task id に紐づく) |
| 記録の中身を見る | `python3 scripts/lib_review_refusal.py show --mission <slug> --task <id>` |

**同じ PR の head が更新されただけでは自動では解除されない** (dispatcher は PR の head を見ない)。
差分を縮めて push したなら `clear` する。

## merge 後に必要なこと

- **`scripts/dispatcher.sh` を変更したので、merge 後に dispatcher restart が必要** (Director が行う)。
  restart するまで旧コードが動き続ける (`knowledge/dispatcher-restart-after-merge.md`)。
  `python3 scripts/lib_daemon_watch.py restart dispatcher` を使う (`lib_mux.py kill`/`spawn` を素で叩かない)。
- restart 直後、いま needs_director / failed+handoff の task があれば、台帳が空なので **1 回だけ**
  再通知される。その後は黙る。
- `scripts/kai-review.sh` の変更 (拒否記録の書き込み) は、dispatcher が常に main 版を起動するので
  **merge されるまで dogfood できない** (鶏と卵)。この PR の diff のレビュー自体は成立する。

## 本番で問題が出たときの戻し方 (停止スイッチは設けていない)

env var の停止スイッチは足していない — 常駐デーモンの分岐が 1 つ増え、その分岐自体が新しい
不具合の入口になるため。代わりに、**再起動なしで効く手当て** と **PR の revert** を用意した。

1. **通知が届かない (台帳が「伝えた」と言っているが Director は見ていない)**:
   `registry/daemons/notified-state.json` を消す (ファイル 1 枚)。次のサイクルで、いま成り立っている
   状態が全部 1 回だけ再通知される。dispatcher の再起動は不要 (毎サイクル読み直す)。
   **直近 `NOTIFY_TTL` (5 分) 以内に送った通知は、スロットル (`/tmp/dispatcher-notify-cache.json`、
   `<key>#<fp>`) が残っているので最大 5 分待つ**。すぐ送り直したいなら、その `<key>#…` の
   エントリ (またはキャッシュファイルごと) も消す。
2. **codex-review が拒否済みのまま動かない**:
   `python3 scripts/lib_review_refusal.py clear --mission <slug> --task <id>`。全部消すなら
   `registry/daemons/review-refusals/` 内の `*.json` を消す (ディレクトリごとは消さない)。
3. **どうにもならない**: この PR を revert し、**dispatcher を restart** する。revert 順序は不要
   (1 PR で完結)。台帳と拒否記録は revert 後の旧コードからは読まれないだけで、放置して害はない。
   revert すると「同じ通知が TTL ごとに永久に再送される」旧挙動に戻る点に注意 — 戻す前に、
   困っているのが (1)(2) で解けないかを見ること。

## 検証

- `python3 -m pytest tests/test_dispatcher_notify_once.py -q` — 本物の `dispatcher.sh` の埋め込み python を
  `exec()` して `dispatch()` を回す (複製ではない)。「TTL が過ぎた」は /tmp のスロットルを消して表す。
  **ただし離脱→再入・A → B → A のテストはスロットルを消さない** (t021)。以前の再入テストは離脱・
  再入のサイクルで `ttl_expired=True` (キャッシュ削除) を使っていて、離脱時にスロットルが生き残る穴を
  隠したまま緑だった (キャッシュ削除を外すと赤になる)。TTL が過ぎた状況を表す `ttl_expired=True` は
  「TTL 後も 1 回だけ」を確かめるテストにだけ使う。
- `bash scripts/test_kai_review.sh` — サイズ超過で拒否記録が書かれる / `--dry-run` は書かない。
- `bash tests/red_proof_t010.sh` — 欠陥を 1 つずつ注入し、見張るテストが赤になることを確かめる
  (M1〜M7 = t010 本体、M8〜M12 = t021: 拒否記録の値の検証・離脱時/fp 変更時のスロットル破棄・
  生存確認の遅延評価と使い回し)。
- `scripts/test_dispatcher_needs_director_notify.sh` の「TTL dedup」節は、旧仕様
  (「TTL 経過後は再送される」) を固定していたので、新仕様 (再送されない / 入力が変われば再通知) に書き換えた。
