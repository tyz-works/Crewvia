# scripts/ の作業メモ（このディレクトリで作業するときだけ読み込まれる）

lib ごとの**破ってはいけない契約**の要約。理由・経緯・全経路の棚卸しは `knowledge/file-map.md`
（旧 root CLAUDE.md の各ファイル項）と、そこから張った設計文書にある。

## 読み取りの入口（`lib_task_cards.py` / `lib_daemon_state.py`）

- queue / registry / config のファイルを開くコードは、必ず `lib_task_cards` の入口
  （`read_task_card()` / `read_regular_text()` / `read_regular_text_or_unreadable()`）を通す。
  task カードを読むのは 5 者（plan.sh・dispatcher.sh・watchdog.py・verifier-dispatcher.sh・taskvia-sync.sh）とも
  `list_task_cards()` だけ。フォールバックは持たない（単体コピーの隔離テストでは lib ごと置く）。
- **`Unreadable` 契約**: 読み取りの失敗を `None` / `{}` / `[]` で返さない。`Unreadable` は空の入れ物として
  振る舞わず、`bool()` / `len()` / `in` / `[]` / 反復 / `.get()` がすべて `TypeError` になる。分岐は
  `is_unreadable()` / `is_missing()`（**ENOENT だけが「本当に無い」**。`Path.exists()` は `EACCES` も False に潰す）。
- **空リストは「カードが 1 枚も無い」の意味だけ**。走査に失敗したときは終端でないプレースホルダを 1 件返す
  （`all(status in TERMINAL)` が `[]` に True を返し、未完了の兄弟を残したまま mission が done になる）。
- task の識別子はファイル名。`id` 欄の食い違うカード・通常ファイルでないカードは `[破損]` として保留（例外にしない）。
- 依存欄の検証（`blocked_deps_problem()` / `released_deps_problem()`）は要素を 1 つも落とさない
  （`blocked_by: [null]` を「依存なし」にしない）。
- デーモン側 JSON 状態ストアは `lib_daemon_state.load_json_store(path, check=...)` が唯一の入口。壊れたエントリが
  1 つでもあればストア全体が `Unreadable`。
- 例外は `plan.sh` の `load_state()` 1 つだけ（`knowledge/empty-vs-unobservable.md` §4）。

## 依存（`lib_dep_rules.py`）

- 「依存が満たされた」の唯一の定義（`card_dependencies()`）。pull・task-graph・status・dispatcher がここだけを読む。
  コピーしない。**`failed` の依存は「保留（HELD）」**で、進める出口は Director の `plan.sh release-dep <id> --mission <slug>` だけ。
- 共有規則に env 停止スイッチを付けない（dispatcher と plan.sh で答えが割れる）。

## mux（`lib_mux.py`）

- `registry/mux/<name>.json`（spawn 記録）は **kill の認可の唯一の証拠**。消してよいのは mux が
  「その id は無い」と明確に答えた（`error.code == "pane_not_found"`）ときだけ。herdr は失敗を全部
  `{"error"}` で返し server 不達も同じ形なので、それ以外は「観測できなかった」で記録を残す。
- 記録を書く `write_pane_record()` と消す `drop_pane_record(expect=<判定した記録>)` は
  `registry/mux/.records.lock` の同じ区間で直列化する。問い合わせは名前でなく**記録の id**を、
  記録の server の世代と同じ接続の上で流す（検証と破壊が別の接続なら別の server でありうる）。
- デーモンの再起動は `lib_daemon_watch.py restart`。`lib_mux.py kill` / `spawn` を素で叩かない。
- `keys <name> <key>...` は名前付きキー（`up` / `down` / `left` / `right` / `enter` / `escape` / `tab`）だけ。
  `send` はテキストなので選択ダイアログのカーソルは動かない。使う前に `capture` で位置を見る。
- `Mux.spawn()` の `env=` 引数は廃止（env は起動コマンドに `export` として埋める）。

## 通知・記録（dispatcher / watchdog / kai-review）

- 状態ベースの通知（needs_director / failed+handoff / review 拒否 / `[timeout]`）は、状態が変わるまで **1 回だけ**。
  `registry/daemons/notified-state.json`（台帳。fingerprint = 通知内容を決める入力）を `notify_state_once()` が見る。
  台帳が読めない・壊れているときは**再送側**に倒す。書き手は dispatcher と watchdog の 2 人で、書き換えはすべて
  `told_lock()`（`lib_daemon_state.py`）の中。`kind=timeout` は 24 時間 prune されない。
- codex-review の差分 300KB 超の拒否は `registry/daemons/review-refusals/<mission>__<task>.json` に記録し
  （`lib_review_refusal.py` が唯一の定義）、dispatcher は記録がある間 spawn しない。記録が読めない・
  欄の値が不正なら保留。
- 台帳・拒否記録は**消してよい**（無い = 再通知 / 拒否されていない）。通知が届かない・review が動かないときの
  手当てはそのファイルを消すこと（dispatcher の再起動は不要。`knowledge/notify-once.md`「戻し方」）。

## plan.sh

- `fail` は `--head <sha>`（実在する commit）必須、免除は `--no-head "<理由>"`。`handoff_path` は**絶対パスのみ**。
- `done` は `--pr <N>` か `--no-pr "<理由>"` のどちらかが要る場合がある（後続の codex-review に pr_number が無いとき）。
- 引数は厳格（未知の option は exit 2 で何も書かない）。`pull` だけ使い方の誤りが exit 1（exit 2 は「タスクなし」）。
- queue を書き換えるサブコマンドの後、`registry/task-graph/tasks.json` を再生成する（`CREWVIA_TASK_GRAPH=0` で停止）。
