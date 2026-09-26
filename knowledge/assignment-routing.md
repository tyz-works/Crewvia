# 割り当てを機械で照合する (t009 / backlog #21 #22 #24)

mission `20260926-mechanize-guards-a` PR3。出典: memory `cross-repo-mission-worker-routing`
(2026-09-25 のミッションで Director が手でやった作業と、Worker の自己確認に頼った箇所)。

## 何が起きていたか

| # | 事故 | 機序 |
|---|---|---|
| #21 | 別 repo 用 (TARGET_DIR 付き) の Worker に crewvia 本体の task が回り、差し戻しても同じ Worker に再割り当てされた | 自動の `plan.sh pull` は task の `target_dir` で絞るが、**`pull --task` は確かめない**。dispatcher は skill の交差だけで割り当て、Worker の TARGET_DIR を知らない |
| #22 | dispatcher が task A を送った 6 秒後に別の task B を同じ Worker に送り、`queue/assignments/<worker>` が上書きされた | 送った時点では assignment ファイルがまだ無いので `is_idle` は真のまま。「送った」という事実は通知スロットル (`assign_<agent>_<slug>_<task>`) にしか残っていない。優先度の高い B が現れると `best` が変わる |
| #24 | codex-review task の `pr_number` を Director が手で入れて開いていた / drafting 中の lint が `status: blocked` を拒否するので、PR 番号待ちの task を承認前から止められなかった | 番号を運ぶ経路が無い。lint の `VALID_STATUSES` に `blocked` が無い |

## 直し方

### 1. Worker の TARGET_DIR の記録 — `registry/workers/<Name>/target_dir.json`

`scripts/lib_worker_target.py` が唯一の定義 (書き・読み・判定・掃除)。`start.sh` が **起動が成功した後にだけ**
書く (既に生きた Worker が居て断られた呼び出しが、生きている Worker の記録を別の TARGET_DIR で上書き
してはいけない)。`{"agent", "target_dir": <絶対パス | null>, "written_at"}`。`null` は「crewvia 本体で
起動した」という**事実**で、「記録が無い」とは別。

**`registry/mux/<Name>-worker.json` (spawn 記録) には相乗りしない。** あれは kill の認可の唯一の証拠で、
`drop_pane_record(expect=)` の突合・掃除・`.records.lock` の区間が中身に依存している。相乗りすると、
kill の恒久拒否や、pane 消滅と同時に TARGET_DIR も消える経路ができる。

読みは `lib_daemon_state.load_json_store` の入口 1 つ (形まで検証。壊れていれば `Unreadable`)。
`.gitignore` 対象 (`registry/workers/`)。

### 2. dispatcher — `worker_may_take()` の判定表

| task の target_dir | Worker の記録 | 結果 |
|---|---|---|
| null | 無い / 読めない | **割り当てる** (従来どおり) |
| null | `target_dir: null` | 割り当てる |
| null | `target_dir: <パス>` | 割り当てない |
| <パス> | 一致 | 割り当てる |
| <パス> | 不一致 / `null` | 割り当てない |
| <パス> | 無い / 読めない | **割り当てない** (保留) |

保留に倒すのは **target_dir 付きの task のときだけ**。PR3 merge 時点で起動済みの Worker は記録を
持たないので、null の task まで止めると dispatcher の restart の瞬間に全 task の割り当てが止まる。
記録は start.sh の起動時にだけ書く — **起動済みの Worker は次の再起動までは `target_dir: null` の
Worker として扱われる** (= その間、TARGET_DIR 付きで起動済みの Worker には target_dir 付きの task が
回らない。Director が起動コマンドを受け取る)。

- **担当できる Worker が居ないとき** (`no_worker` の Director 通知) は、**そのまま貼れる起動コマンド**を添える:
  `cd <repo> && AGENT_NAME=$(bash scripts/assign-name.sh <skills> [--fresh]) [TARGET_DIR=<td>] CREWVIA_MUX_ENABLED=1
  CREWVIA_MUX=<backend> bash scripts/start.sh worker <skills>`。skill は合う Worker が (別の TARGET_DIR で) 居る
  ときは `--fresh` を付ける (registry-first の名前引きは同じ名前を返し、`start.sh` は「既に居る」で断る)。
  名前は貼った時点で決まる (dispatcher は registry を書き換えない)。
- **skill は合うが TARGET_DIR が合わない task しか残っていない Worker は退役させない**。Rule 2
  (blocked-stuck) は「全部 blocked」の判定で当てはまらない。退役させると、記録を持たない TARGET_DIR 付き
  Worker が自分の task を待っているだけで殺される。
- **送信済み・pull 待ちの Worker には別の task を送らない** (#22)。task A が pending のまま、A への
  送信が通知スロットルの TTL の内にあるあいだ、その Worker は割り当て済みとして扱う。TTL を過ぎても
  pull されなければ保護は外れる (従来どおり A の再送)。
- 記録の掃除 (`sweep_stale_records`): 窓も新しい heartbeat も持たず (= `_alive_workers` に居らず)、かつ
  `SWEEP_MIN_AGE_SECONDS` (300 秒) より古い記録だけ。生きている Worker の一覧が空のときは何もしない (mux が
  一時的に空を返しただけで全記録を消さない)。dispatcher は窓のある Worker の記録しか引かないので、退役した
  Worker の記録が残っても誤用されない。

### 3. `plan.sh pull --task` — 何も書かずに exit 3

`PRECONDITION_UNMET` (3)。pull の exit 2 は「タスクなし」(Worker が待つ) なので区別できる。

- **target_dir**: card の `target_dir` と実効 target (`--target-dir` > `TARGET_DIR`) が一致しなければ拒否。
  自動 pull の絞り込みと同じ比較。skill の絞り込みは従来どおり迂回する (dispatcher が済ませている)。
- **二重割り当て**: 自分が **別の** task を持っている Worker は拒否 (`agent_busy_elsewhere()`):
  in_progress の card、または **手放されていない** task を指す assignment。
  - **同じ task** は拒否しない (割り当てメッセージの二重着弾・同名後任の取り直し)。従来の判定
    (`already in_progress`, exit 1 / 世代判定) に委ねる。
  - **孤児の assignment** (完了・failed・cancelled・pending・存在しない mission を指す) は拒否しない。
    塞ぐと Kai-codex の codex-review が恒久的に取れなくなる (#13 の再発)。次の pull が上書きする。
  - **needs_director の card だけ**では拒否しない (同じ理由: Kai-codex は名前が card に残る)。
  - **読めない assignment** は拒否 (別の task を指していないと証明できない)。

### 4. `plan.sh done <id> --pr <N>` — PR 番号の自動伝播

`--pr` は **明示フラグだけ** (Result の本文から推測しない)。その task を `blocked_by` に持ち、skills に
`codex-review` か `review` を含み、`pr_number` が **未設定** の task に `pr_number` を書く (Director が手で
入れた値は上書きしない)。`codex-review` の task が `blocked` なら `pending` に戻し `blocked_reason` を消す。
`review` の task は番号だけで status は触らない (止まっている理由が PR 番号とは限らない)。`done` と
同じトランザクションで行う。伝える先が無ければ 1 行そう言う (無言にしない)。

**運用: `--pr` は Director が「PR3 を本番に反映した」と伝えるまで付けない。** 本番の plan.sh が PR2 より
前の版だと、未知のフラグを Result として飲み込み、Result が `--pr` の 1 語になって本文が消える
(t017 で実際に起きた)。それまでは `plan.sh done <id> "<Result>"` で閉じ、Result 1 行目の `PR #<N>` で
Director が codex-review を開く。

#### `--pr` の付け忘れを機械で断る (t036 / t027 P2-1)

自動伝播にしたことで、**実装 Worker が `--pr` を付け忘れると codex-review が pending のまま誰にも
知らされず、Codex を通らずに merge されうる**ようになった (手作業のころは Director が plan 時に番号を
書く段で気づけた)。2 段で塞ぐ:

1. **`plan.sh done <id>` が拒否する (exit 2、何も書かない)**: その task を **直接** `blocked_by` に持ち、
   skills に `codex-review` を含み、`pr_number` が未設定で、まだ終わっていない task があるのに `--pr` も
   `--no-pr` も無いとき。メッセージに待っている task と打つべきコマンドを出す。数える対象は
   `codex_reviews_awaiting_pr()` の 1 か所 (`review` の task は数えない — 番号が無くても Worker が PR を探せる。
   `[破損]` の card も数えない — 読めた card だけを拒否の根拠にする)。
2. **PR を作らない task は `--no-pr "<理由 1 行>"`**: 理由は card の `no_pr_waiver` 欄と stderr に残る
   (`fail --no-head` と同じ作法。静かに飛ばす経路にしない)。理由が空・`--pr` との併用は使い方の誤り
   (exit 2)。免除しても番号は伝播しない。

それでも番号が入らなかった codex-review (Director が手で開いた・`--no-pr` で閉じた等) のために、
**dispatcher が `pr_number` の無い ready (依存が満たされて pending) な codex-review を Director に 1 回だけ
知らせる** (`[review-no-pr]`。`notify_state_once()` の kind `no-pr-number`、key `review_no_pr_<mission>_<task>`)。
案内は `plan.sh update <id> --pr-number <N> --status pending --mission <slug>`。番号が入れば状態を離れ、
台帳から捨てられる。`blocked` のままの codex-review は Director が意図して止めているので通知しない。
以前は `spawn_kai_review()` の `log()` だけで、呼び出し側が `handle_codex_review()` の後に無条件 continue
するため no_worker の通知にも届かなかった。

### 5. lint — drafting の `blocked`

`lint_plan.py` の `VALID_STATUSES` に `blocked` を追加。**`blocked_reason` (空でない文字列) が必須** —
理由の無い停止は、何を待っているのかが誰にも分からず、`done --pr` のような機械の解除にも任せられない。

## 戻し方 (停止スイッチは付けない)

`worker_may_take()` は dispatcher と `plan.sh pull --task` の答えが割れないように 1 つの規則にしてあり、
env の停止スイッチを付けると、長寿命の dispatcher と呼ばれるたびに読み直す plan.sh で答えが割れる
(memory `no-env-killswitch-for-shared-rule`)。だから戻し方は 1 つだけ:

**PR を revert → 主 checkout を `git merge --ff-only origin/main` → `python3 scripts/lib_daemon_watch.py restart`
(dispatcher を再起動)。** `registry/workers/` の記録は revert 後の旧コードからは読まれないだけで、放置して
害は無い。`start.sh` は revert 後は記録を書かない (既存の記録は古くなるが誰も引かない)。
revert すると #21 (別 repo の Worker に task が回る) と #22 (送信済みの Worker に二重送信) が戻る点に注意。

**t036 (`--pr` の付け忘れ防止) だけを戻すなら**: PR を revert → 主 checkout を
`git merge --ff-only origin/main` → `python3 scripts/lib_daemon_watch.py restart` (dispatcher の通知だけが
再起動を要する。`plan.sh` は呼ばれるたびに読み直すので ff だけで戻る)。revert すると `done` は `--pr` 無しでも
通り (`--no-pr` は未知の option として拒否される)、pr_number の無い codex-review はまた log だけになる。
軽い手当て: `[review-no-pr]` が届く task は `plan.sh update <id> --pr-number <N> --status pending` で解消する。
それより軽い手当て (再起動不要):

- **target_dir 付きの task が回らない (記録が無い / 壊れている)**: `python3 scripts/lib_worker_target.py record
  registry <Name> [<target_dir>]` で記録を書き直す (dispatcher は毎サイクル読み直す)。または Worker を
  start.sh で起動し直す。
- **記録が嘘 (別の TARGET_DIR のまま)**: 同じ手当て。記録を消せば「無い」= null の task だけを受ける。

## 検証

- `python3 -m pytest tests/test_assignment_routing.py -q` — 判定表・pull の拒否 (何も書かない)・二重割り当て・
  `done --pr`・lint・**本物の `dispatch()` を 1 サイクル回す** (mux だけフェイク)。
- `bats tests/start-sh-target-dir-record.bats tests/start-sh-spawn-refusal.bats` — start.sh が記録を書く・
  断られた起動では書かない (使い捨ての checkout の複製で走る)。
- 赤の実証: `tests/red_proof_t009.sh`、`--pr` の付け忘れ防止は `tests/red_proof_t036.sh` (done の拒否・`--no-pr`・
  dispatcher の通知の 10 ケース。`TestDoneRequiresPr` と `test_dispatcher_notify_once.py` を使う)。
