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

### 観測できなかったときは、台帳もスロットルも捨てない (t023 / Kai 2 巡目 P2)

離脱時の掃除 `prune_told()` は「観測できた mission の、いま成り立っていない key」を捨てる。
**「観測できた」の判定と「いま成り立っている key」の収集は、同じ 1 つのスナップショット
(`all_tasks`) から作らなければならない。**

t021 は `prune_told` を足したが、handoff 検知だけが `list_tasks_for_mission()` で mission を
**もう一度走査**していた。1 回目 (`all_tasks`) が成功・2 回目が破損カード / 走査失敗のとき、
handoff key が 1 件も集まらないのに mission は「観測できた」扱いになり、台帳とスロットルが
捨てられる。読み取りが回復すると、変わっていない failed task が再通知される。断続的な失敗が
繰り返されると、この PR が潰したはずの通知洪水が戻る。「空 (もう無い)」と「観測不能 (見られなかった)」
を同じものとして扱う、crewvia で繰り返し出ている型そのもの
(`knowledge/empty-vs-unobservable.md`、`lib_task_cards.list_task_cards()` は走査失敗で
`[]` ではなく非終端のプレースホルダを返す)。

| | 直し方 | 採否 |
|---|---|---|
| (a) | handoff 検知でも `all_tasks` を使う。判断の材料を 1 つにする | **採用** |
| (b) | 2 回目の走査の失敗も pruning のガードに含める | 不採用 |

(a) を採った理由: 欠陥の根は「同じサイクルで同じ mission を 2 回読み、2 つの結果から 1 つの判断をする」
ことにある。(b) はそれを残したまま、2 回目の結果を見張る別のガードを足すことになり、ガードが持つ
場所が増えるぶん漏れる (t018 で表の「載せた関数しか見ない」を機械的な走査に置き換えたのと同じ理由)。
(a) なら食い違う 2 つの結果がそもそも存在せず、走査も 1 回減る (needs_director / vanished 検知は
すでに `all_tasks` を使っている)。どちらでも「観測できなかったときは捨てない」に倒れる。捨てる側に
倒すと元の洪水が戻る。

これから足す状態ベースの通知も同じ規則に従う: **live key は `all_tasks` から集める。mission を
自分で走査し直さない。**

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

#### `--mission` を省略した `kai-review.sh` (t026 / Kai 3 巡目 P2 の 2 件目)

`--mission` は省略できる正当な呼び出し (plan.sh が解決する) だが、拒否記録の名前 `<mission>__<task>.json` には
mission が要る。以前は `MISSION_SLUG` が空だと記録が **書かれず**、pending に戻された task を dispatcher が
もう一度 spawn した (#11 のループが戻る)。今は、**pull の前に** `plan.sh resolve-mission <task>` で
実効 mission を 1 度だけ解決して保持し、以降の `pull` / `needs-director` / `done` と拒否記録の全部に同じ値を渡す。

- `resolve-mission` は読み取り専用で、探索順は `pull` と **同じ定義** (`mission_search_order()`:
  `--mission` があればそれだけ、無ければ default_mission 優先で active mission を走査し最初に task を持つもの)。
  別々に解決すると食い違う (`tests/test_daemon_state_reads_go_through_the_entry.py` が、`pull` と
  `resolve-mission` の両方が同じ関数を通ることを構造で見張る)。
- `--mission` を明示した呼び出しは従来どおり (解決を挟まない)。
- mission を解決できない (task がどこにも無い) 非 dry-run は、`could not resolve the mission … pass --mission`
  で exit 1 (何も書かない)。`--dry-run` は警告だけで続ける (plan.sh の状態に依存しない smoke test のまま)。

### 3. デーモン側 JSON 状態ストアを読む入口は 1 つ (t026 / Kai 3 巡目 P2 の 1 件目)

**同じ根の欠陥が PR #214 で 3 回出た**: 読めた JSON の **中身の形を確かめずに使う**。

1. t019 1 巡目: 拒否記録が欄の存在しか見ず、`diff_bytes: null` で `describe()` が TypeError → dispatch サイクル全体が落ちる
2. t019 3 巡目: 台帳が **外側** (JSON object か) しか見ず、`{"bad": {"slug": []}}` で `prune_told()` が
   毎サイクル TypeError → prune もサイクルの残りも止まり、状態を離れて戻った task が **永久に黙る**
3. t015 (#217): `released_deps` が未検証で `true` / `123` なら TypeError

1 件ずつ site patch を当てても 4 回目は「まだ書かれていないストア」に出る。`lib_task_cards` が queue の
カードについて既にそうしているのと同じ作法で、読み取りの **入口を 1 つ** にした。

**入口**: `scripts/lib_daemon_state.py` の `load_json_store(path, check=<形の検証>, warn=<警告>, expect=dict)`。
戻り値は「検証済みの値」または `Unreadable` (`bool()` / `len()` / `in` / `[]` / 反復 / `.get()` がすべて
`TypeError`。空の入れ物として振る舞わない)。ENOENT だけが「まだ無い」(`is_missing()`)。読めない・JSON として
壊れている (`RecursionError` 等も)・形が使えない・`check` が例外を出す、はすべて `Unreadable`。
**壊れたエントリが 1 つでもあれば、ストア全体が `Unreadable`**。この関数は例外を出さない。

| ストア | 検証 (`lib_daemon_state`) | 使えないときの向き (判定ごとに決めてある) |
|---|---|---|
| `registry/daemons/notified-state.json` (台帳) | `told_ledger_problem`: キーは空でない文字列、エントリは object、`fp`/`kind`/`slug`/`task` は空でない文字列 | **再送側**: `already_told` は False、`prune_told` は何もしない、次に送れたとき `record_told` が作り直す (自己修復)。WARNING を TTL に 1 回 (`notified-state`) |
| `registry/daemons/review-refusals/*.json` | `lib_review_refusal._invalid_reason` (t021) | **spawn を保留** (拒否されていないと証明できない) |
| `/tmp/dispatcher-notify-cache.json` (dispatcher / verifier-dispatcher) | `notify_cache_problem`: 値は有限の数で、負でなく、未来 24 時間以内 | `{}` = スロットルを失う = **もう一度送る** (冪等)。NaN・遠い未来を通すとその key の通知を **永久に遮る** ので落とす |
| `registry/mux/<name>.state.json` (Rule 5) | `rule5_state_problem`: `state` は文字列、`since` は有限の数 | `{}` = grace が最初からやり直し = **通知が遅れる側** (破壊も割り当ても起きない) |
| `registry/daemons/<peer>.watch.json` | `watch_state_problem`: `grace_until`/`last_respawn_at`/`hold_since` は有限の数か null | 既定値 (grace なし・hold の起点は取り直し) = ファイルが無いときと同じ |
| `registry/daemons/<peer>.respawns.json` | 外側が object で `entries` が list。**エントリごと** に、object でない / `at` が有限の数でないものを捨てる (この記録は 1 件壊れても残りで flap を数える設計) | 壊れた 1 件だけ捨てる。ログ全体が使えなければ履歴なし (従来どおり) |
| pause marker / reports / `lib_retirement.read_json` / pane record | 入口経由 (外側が object であることのみ。`None` に潰す契約は従来のまま) | 従来のまま。区別が要る呼び出し側は `read_pause_state()` のように入口を直接使う |

**書き手と読み手は同じ形**: `record_told` は書く直前に同じ `told_entry_problem` を通し、通らなければ書かずに
`notified-state` の WARNING を出す。読み手だけが厳しいと、書いたばかりの台帳を自分が「壊れている」と読み、
永久に再送側へ倒れる。

**構造で閉じている**: `tests/test_daemon_state_reads_go_through_the_entry.py` が、対象モジュール
(`AUDITED_MODULES` + `lib_*.py` 全部。`.sh` は **`dispatcher.sh` の埋め込み python を含め全ブロック**) の
`json.load` / `json.loads` / `json.JSONDecoder` を AST で全部拾い、入口の中にあるもの以外は理由付き
allowlist に無ければ落とす。新しい経路を足したら必ず赤になる (表で示すだけにしていない)。あわせて
「この読み手はこの入口を、この検証器付きで通っている」(`ENTRY_READERS` / `VALIDATED_STORES`) を表で見張る。

**allowlist の中身** (`ALLOWED_JSON_PARSES`。理由を書けないものは載せていない):
(E) 外から来る応答 (herdr socket / CLI、Taskvia の HTTP、Claude Code の notification payload = 観測専用) と、
(Q) queue 側 (`registry/daemons/` の外) の sidecar — `plan.sh` の `_read_assignment_identity` /
`_load_taskvia_map`、`taskvia-sync.sh` の `load_map`。

**入口を通していない読み取り (backlog。allowlist に明示して凍結してある)**:
- (Q) は `plan.sh` が単体コピーの隔離テストで使われており、新しい lib への依存を足すと fixture がまとめて壊れる
  (memory: shared-module-breaks-single-script-fixtures) ので移していない。
- (t021) `plan.sh` の `predecessor_cleanup_pending()` は registry/retirements の marker を `json.loads` で読む
  (allowlist の (Q))。判定は「前任の後始末待ちと証明できるか」だけで、証明できない形はすべて False (= 待たずに
  従来どおり拒否) に倒れるので、壊れた marker が拒否を緩めることはない。入口へ移すのは (Q) 全体と一緒に。
- **既知の穴**: `taskvia-sync.sh` の `load_map()` は **外側の型も未検証** (`.taskvia-map.json` が list だと呼び出し側の
  `.get` が落ちうる)。`plan.sh` 側 (`_load_taskvia_map`) と同じ 1 行 (`isinstance(data, dict)`) で閉じるが、
  registry/daemons の外なので t026 では触っていない。
- `lib_retirement.read_json` / `lib_mux.read_pane_record` / pause marker は **外側が object であること** までしか
  検証しない (契約は従来のまま)。内側の欄の型は呼び出し側が検証している (heartbeat の `updated_at`/`pid` など)。

**#215 (registry/mux の記録を消す経路の走査) との相互衝突 (t031)**: 2 つの構造ガードが、互いの新しいコードを
拾う形があった。どちらも実欠陥ではなく、**個別の呼び出しを理由付きで allowlist に足して閉じた** (走査の範囲・判定は緩めていない)。
- #215 の `ALLOWED_DELETIONS` (`tests/test_stale_pane_record_sweep.py`) は、`dispatcher.sh:save_told` の
  `os.replace(tmp, TOLD_FILE)` を拾う。これは本台帳の原子的な書き込みで、registry/mux の記録ではない。
- 本節の `ALLOWED_JSON_PARSES` は、#215 が足した `lib_mux.py:_herdr_pane_get_bound` の `json.loads` を拾う。
  herdr の `pane.get` 応答 (E) であって、デーモン側状態ストアではない (既存の `_herdr_close_tab_bound` と同区分)。
- キーは (ファイル, 関数, 呼び出しのソース) なので、同じ形を**別の関数**に足せば依然として赤になる (欠陥注入で確認済み)。

**検出器が見えないもの** (実態より狭く書かない): `exec()`/`eval()`、`json` を変数に入れて渡す形、JSON 以外の
パーサ (`yaml.safe_load` / `pickle`)、走査対象外のモジュール (`hooks/` 等)。「うっかり足す」ことを止める補助で、
敵対的なすり抜けを防ぐ境界ではない。

**WARNING を見たとき (`notified-state` / `rule5 state entry` / `unusable JSON store`)**:
1. 中身を見る: `python3 -m json.tool registry/daemons/notified-state.json`
2. 直せるなら壊れたエントリを直す。**消してよい** (どのストアも「無い」= 既定値 / 再送側)。台帳を消すと、いま成り立って
   いる状態が全部 1 回だけ再通知される (通知が欠けることはない)。スロットル (`/tmp`) を消すと、直近 5 分に送った通知も
   もう一度届く。Rule 5 / watch の状態は消すと grace がやり直しになるだけ。
3. 壊れた原因 (手編集・部分書き込み・別バージョンの書き手) の見当を付けておく。書き手は原子的に書く (tmp + rename)
   ので、通常は壊れない。

## merge 後に必要なこと

- **`scripts/dispatcher.sh` を変更したので、merge 後に dispatcher restart が必要** (Director が行う)。
  restart するまで旧コードが動き続ける (`knowledge/dispatcher-restart-after-merge.md`)。
  `python3 scripts/lib_daemon_watch.py restart dispatcher` を使う (`lib_mux.py kill`/`spawn` を素で叩かない)。
- **t026 で `lib_mux.py` / `lib_retirement.py` / `lib_daemon_watch.py` / `verifier-dispatcher.sh` も入口経由に変えた**
  (新規: `lib_daemon_state.py`)。**watchdog も restart が必要** (これらを import する常駐 python) —
  `python3 scripts/lib_daemon_watch.py restart watchdog`。verifier-dispatcher を常駐させている環境では、それも
  restart する。restart までは旧コードが動くだけで、新旧の混在で壊れる書式変更はない (ストアの形は変えていない)。
- restart 直後、いま needs_director / failed+handoff の task があれば、台帳が空なので **1 回だけ**
  再通知される。その後は黙る。
- `scripts/kai-review.sh` の変更 (拒否記録の書き込み) は、dispatcher が常に main 版を起動するので
  **merge されるまで dogfood できない** (鶏と卵)。この PR の diff のレビュー自体は成立する。

## watchdog の timeout 終了通知も同じ台帳に乗る (t021 / PR6)

台帳の書き手は dispatcher だけではなくなった。watchdog が timeout (idle / max) で Worker を終了させた
あとの Director 宛の通知 (`kind=timeout`、key は `timeout_<mission>_<task>`、fingerprint は退役の
`request_id`) を、`watchdog.make_notify_once()` がこの台帳に乗せて 1 通だけ送る。設計と根拠は
`knowledge/daemon-authority.md` §7-18。この台帳を読み書きするときの約束:

- **書き換えはすべて `lib_daemon_state.told_lock()` の中で**。dispatcher の `record_told()` /
  `prune_told()` も同じ。取れなければ「書けなかった」に倒れる (通知は次のサイクルで再試行 / prune は遅れる)。
- `kind=timeout` のエントリは、書かれてから 24 時間 (`TOLD_TIMEOUT_TTL_SECONDS`) は dispatcher の prune の
  対象外 (その task は pending に戻っていて live key に現れないため)。TTL 後に dispatcher が掃除する。
  **エントリに `at` (書いた epoch 秒) を持つ**のはこのため。他の kind は `at` を持たない。
- 送れなかった通知は台帳に書かない (再送側)。台帳が使えないときも再送側 — 上の表と同じ向き。

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
  生存確認の遅延評価と使い回し
  M13〜M14 = t023: handoff 検知が mission を走査し直す / prune が観測の可否を見ない
  M15〜M30 = t026: 台帳/スロットル/Rule 5/watch 状態の形の検証・入口・構造テストの検出力・kai-review の mission 解決)。
- `python3 -m pytest tests/test_daemon_state_reads_go_through_the_entry.py tests/test_daemon_state_fail_direction.py -q`
  — 入口を通さない `json.loads` が増えたら落ちる構造テストと、壊れたストアでの「落ちない・WARNING・再送側・自己修復」。
- `scripts/test_dispatcher_needs_director_notify.sh` の「TTL dedup」節は、旧仕様
  (「TTL 経過後は再送される」) を固定していたので、新仕様 (再送されない / 入力が変われば再通知) に書き換えた。
