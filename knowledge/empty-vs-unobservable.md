# 「空」と「観測できなかった」を区別する

> Codex 7 巡目 P1 / P2 (t016) で入れた規則と、読み取り経路の全数調査。
> t017 (8 巡目) で固定パスにもガードを当て、**t018 (9 巡目) で「失敗を空で
> 表さない」形と、表に載せ忘れを許さない機械検出まで持っていった**。
> 関連: `knowledge/daemon-authority.md` (破壊の根拠), `scripts/lib_task_cards.py`

---

## 1. 規則

読み取りが **空を返す** とき、それは 2 つのまったく違うことを意味しうる。

| | 意味 | 正しい扱い |
|---|---|---|
| **本当に空** | そこに何も無いことを観測した | 空を返す |
| **観測できなかった** | 何があるか分からない | 空を返してはいけない |

両方を同じ `[]` / `{}` に潰すと、**呼び出し側は 2 つを区別できない**。そして
呼び出し側の述語は、たいてい空に対して「都合のよい答え」を返す。

```python
all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks)   # [] → True
```

`all()` が空リストに True を返すのは Python の仕様であって欠陥ではない。欠陥は
**観測の失敗をその入力に流し込んだこと**である。

判断の材料が無いときにどちらへ倒すかは、**判定ごとに** 決まる
(`fail-direction-is-per-judgment`)。

* 完了・破壊・承認のような **取り返しのつかない結論** → 観測できなかったら
  結論を出さない (fail closed)
* 抑制・警告の **窓を消費する判定** → 発火しない側に倒す

### t018: 倒す先を呼び出し側に任せるなら、取り違えられない形で渡す

上の規則は 2 回とも「呼び出し側が正しく分岐する」ことを前提にしていた。
7 巡目・8 巡目で直した箇所はそれで閉じたが、**9 巡目 P1 は直前の 8 巡目の
修正そのものが作った** —— `dispatcher.load_state()` が読み取りの失敗を `{}`
で返し、`dispatch()` の

```python
if not active_missions:
    shutdown_idle_workers()      # ← 全 idle Worker の退役
```

に落ちた。呼び出し側は「空」と「読めなかった」を **区別できない値** を
受け取っていたので、正しく分岐しようがなかった。

だから t018 で形を変えた。読み取りの失敗は `None` / `{}` / `[]` ではなく
`lib_task_cards.Unreadable` で返す。この値は **空の入れ物として振る舞わない**:
`bool()` / `len()` / `in` / `[]` / 反復 / `.get()` はすべて `TypeError` になる。
「読めなかったものを空として扱う」コードは、書けても **実行した瞬間に落ちる**。

| 返り値 | 意味 | 判定 |
|---|---|---|
| `str` / `dict` / `list` | 観測できた (空でもよい) | そのまま使う |
| `Unreadable` (errno=ENOENT) | **本当に無い** | `is_missing()` |
| `Unreadable` (その他) | **観測できなかった** | `is_unreadable()` |

`ENOENT` だけを「本当に無い」にするのは §5 のチェックリストと同じ理由で、
`Path.exists()` が `EACCES` も False に潰すからである。

---

## 2. 読み取り経路の全数調査 (2026-09-24 時点)

`return []` / `return {}` / `return ''` を失敗時に返している箇所すべて。

| # | 場所 | 空が意味しうるもの | 危険な結論に落ちるか | 状態 |
|---|---|---|---|---|
| A | `lib_task_cards.list_task_cards` — `os.listdir` の `OSError` | **観測できなかった** | **YES** — mission 完了判定が True (`cmd_done` / `cmd_verify_result`) | **修正済** (t016)。`scan_failure_task()` を 1 件返す |
| B | 同上 — `not os.path.isdir(tasks_dir)` | 本当に無い **/** ディレクトリでない **/** stat できない の 3 つを同じ False に潰していた | **YES** — 同じ述語 | **修正済** (t016)。`os.stat()` で ENOENT だけを「本当に無い」にする |
| C | `lib_task_cards.read_task_card` — 個々のカードの失敗 | 観測できなかった | NO | 既に `[破損]` カードで保留 (t009/t013/t014) |
| D | `watchdog.WorkerMonitor._notification_files` — `iterdir` の `OSError` | **観測できなかった** | **YES** — 候補が減って idle が伸び、`_awaiting_human` の抑制も外れ、**両方 terminate の側** | **修正済** (t016)。ENOENT だけ `[]`、他は `None` で「観測不能」 |
| E | `watchdog.WorkerMonitor._mtimes_since_floor` — `p.stat()` の `OSError` を `continue` | **観測できなかった** | **YES** — 候補が 0 件になると `_last_activity_mtime()` が floor をそのまま返し、数時間前に pull された健全な Worker が `hard_idle` で terminate される | **修正済** (t017)。`ENOENT` だけ「本当に無い」、他は `(found, unobservable)` の 2 つ目で返す |
| E2 | 同上 — `_newest_notification` の per-file `stat` を `continue` | 列挙後に消えた (普通) と 権限 (異常) | **YES** — `(None, None)` → `_awaiting_human()` が False → 抑制が外れる | **修正済** (t017)。`FileNotFoundError` だけ `continue`、他は `(now, "(unobservable)")` |
| F | `watchdog.load_active_tasks` — `state_file.exists()` False | 本当に無い / stat できない | NO — 「in_progress の task が 1 件も無い」= **誰も監視しない** = 誰も kill しない | 安全な向き。ただし読み取り自体は t017 でガード経由に (下の §2-2) |
| G | `lib_retirement.agents_with_state` — `iterdir` の `OSError` | 観測できなかった | NO — 退役が進まない側 (保留) に倒れる | 安全な向き |
| H | `dispatcher.load_state` / `load_workers` — **読み取りの失敗** | 本当に無い / 読めなかった | **YES** — `dispatch()` は `if not active_missions: shutdown_idle_workers()` を持つので、読めない state.yaml が **pending の仕事を残したまま idle Worker の退役を認可する** (Codex 9 巡目 P1) | **修正済** (t018)。`Unreadable` を返し、`dispatch()` がそのサイクルを丸ごと見送る |
| I | `dispatcher._load_state_entry` (Rule 5 の grace) | 欠損 / 壊れている | NO — grace が最初からやり直しになる = **通知が遅れる側** | 安全な向き |
| J | `dispatcher.bench_current_strategy` → `''` | 観測できなかった | NO — ベンチ用の分岐のみ | 対象外 |
| K | `dispatcher._load_notify_cache` / `taskvia-sync.load_map` → `{}` | キャッシュ欠損 | NO — 再送・再同期の側 (冪等) | 安全な向き |
| L | `plan.sh._load_workers_from_registry` → `[]` | 観測できなかった | NO — Worker 一覧は表示と経験値加算のみ。空でも task は動く | 倒す先は安全な向きのまま。ただし **読み取りは t018 でガード経由に** (§2-2) |
| M | `plan.sh.build_task_graph` — `not os.path.isdir(mission_dir(slug))` で `continue` | 本当に無い / stat できない | NO (可視化のみ)。ただし **DAG からその mission が黙って消える** | §3 |
| N | `plan.sh.load_state` — `not os.path.exists(STATE_FILE)` | 本当に無い / stat できない | NO — active mission ゼロ → 何も割り当てない・何も完了しない | 安全な向き |

**t016 で修正したのは A / B / D の 3 件**、**t017 で E / E2 の 2 件**、
**t018 で H の 1 件**。いずれも「観測の失敗が、取り返しのつかない結論
(mission 完了 / Worker の終了) の側に落ちる」ものである。

H だけが毛色が違う —— **t017 の修正が作った**。ガードを足して「読めなかった」
という状態が初めて到達可能になり、その受け皿が `{}` だったために、それまで
存在しなかった経路が開いた。足した機構が偶然の backstop を消す、という形で
ある (memory: session-20260912-verdict-ci-launcher)。

---

## 2-2. 「開く相手の種類」の全数調査 (t017 → t018 で allowlist 化)

§4 のガード (通常ファイルだけを、待たずに読む) は t016 の時点で **`tasks/` を
列挙して読む経路にしか** 入っていなかった。同じ queue / registry を
**固定パスで直接開く** 経路が 5 つのファイルに残っていた。列挙するかどうかは
害の大きさを変えない —— 書き手のいない FIFO 1 枚で、その読み手は無期限に
座り込む。

| 場所 | 何を止めるか | 状態 |
|---|---|---|
| `plan.sh.load_task` | **キューロックを握ったまま** plan.sh 全体 | **修正済** (t017) `read_queue_file()` |
| `plan.sh.load_mission` | 同上 | **修正済** (t017) |
| `plan.sh._print_mission_summary` / `_print_mission_detail` / `_mission_data` | `status` / JSON 出力 | **修正済** (t017)。一覧側は `try_read_queue_file()` で 1 件だけ落とす |
| `plan.sh.load_state` | plan.sh 全体 | **意図的に未修正** (下の §4 の取引) |
| `dispatcher.publish_agents` — `task_file.read_text()` | `dispatch()` の **前** に走るので全 mission の割り当て | **修正済** (t017) `read_task_card()` |
| `dispatcher.load_state` / `load_workers` / `dispatch` の mission.yaml | dispatch サイクル全体 | **修正済** (t017) `read_queue_text()` |
| `verifier-dispatcher.load_state` / `load_workers` | 検証の割り当てサイクル全体 | **修正済** (t017) |
| `verifier-dispatcher.update_task_fields` — **読んで書き戻す** | 読みで止まる。止まらなくても `os.replace()` が別の何かを置き換える | **修正済** (t017) `read_regular_text()` |
| `taskvia-sync.scan_missions` — state.yaml / mission.yaml | 同期全体 | **修正済** (t017) |
| `watchdog.load_active_tasks` — state.yaml | **全 Worker の生存監視** | **修正済** (t017) |

### t018: 表に「載せ忘れた」経路が 4 つ残っていた

上の表は **t017 で直した箇所** の一覧で、`tests/…` の
`GUARDED_READS` もそれをなぞっていた。だから **表に載っていない関数** は
最初から視界の外にいた —— Codex 9 巡目 P2 が名指ししたのは全部その形である。

| 場所 | 何を止めるか | 状態 |
|---|---|---|
| `plan.sh._load_workers_from_registry` — workers.yaml | Worker 同期 | **修正済** (t018) |
| `plan.sh.task_graph_assignment_holds` / `_read_assignment_identity` / `classify_assignment` — assignment | assignment の後始末・task-graph の生成 | **修正済** (t018) |
| `plan.sh._apply_risk_flags` / `_taskvia_map_update*` / `_task_graph_pending_outstanding` / `cmd_review` の verdict | review の適用・Taskvia 同期・再生成の要求 | **修正済** (t018) |
| `dispatcher.publish_agents` — assignment 本体 (ガードしたカード読み取りの **直前**) | `dispatch()` より前に走るので全 mission の割り当て | **修正済** (t018) |
| `dispatcher.check_rule5` — assignment ×2 | Rule 5 の通知 | **修正済** (t018) |
| `dispatcher._mux_created_at` / `_spawn_time_fallback` / `_load_state_entry` — registry/mux | dispatch サイクル全体 | **修正済** (t018) |
| `lib_retirement.read_task_started_at` / `read_json` / `assignment_execution_verdict` / `created_at_from_cache` | **watchdog のサイクルの中の退役処理** | **修正済** (t018) |
| `watchdog._newest_notification` — 通知本体 | idle 判定 | **修正済** (t018) |
| `lib_daemon_watch.load_config` / `read_pause_state` | 相互監視 | **修正済** (t018) |
| `lib_mux.read_pane_record` / `_config_mode` | spawn / kill の判定 | **修正済** (t018) |
| `lib_model._parse_yaml_fallback` | Worker 起動時のモデル解決 | **修正済** (t018) |
| `taskvia-sync.load_map` | 同期全体 | **修正済** (t018) |

**そして向きを逆にした。** `tests/test_queue_reads_go_through_the_guard.py::`
`test_no_unguarded_read_remains` は、対象モジュールの `open()` (読みモード) /
`.read_text()` / `.read_bytes()` を **AST で機械的に全部拾い**、
`ALLOWED_DIRECT_READS` (理由付き) に無ければ落とす。新しい直接読み取りが
増えたら、表に足し忘れても **必ず落ちる**。

allowlist に残っているのは 4 種類だけである。

1. `/proc` —— procfs。FIFO にも通常ファイルにも置き換えられない
2. `/tmp` のキャッシュ (notify cache / bench スイッチ) —— 失っても冪等
3. 呼び出し側から渡される任意のパス (`lib_verdict` の CLI 引数、watchdog の
   旧ログポインタ)
4. `plan.sh.load_state` —— §4 の取引 (意図的な 1 つ)

死んだ行が残って **黙って許可** にならないよう、
`test_the_allowlist_has_no_dead_entries` が実在しない行を落とす。

### 入口と、倒す先

判定の本体は `lib_task_cards` に 1 つだけで、入口が 3 つある。

* `read_task_card()` —— カード 1 枚。読めなければ `[破損]` (例外を出さない)
* `read_regular_text()` —— 中身か例外
* `read_regular_text_or_unreadable()` —— 中身か `Unreadable` (警告 1 行)。
  常駐デーモン用。**例外を出さない** (§6)

**倒す先は入口では決めない。** 呼び出し側ごとに違うからで、t017 / t018 で
入れたのはどれも「読めなかったことを、割り当て・完了・破壊の許可に使わない」
側である。

赤の実証: `tests/red_proof_stat_and_direct_reads.sh` (t017) /
`tests/red_proof_t018.sh` (t018)。

---

## 3. 直していないもの、その理由

**E は t017 で修正した。** t016 の時点でここを後回しにした理由は

> D を直した後にここへ届くのは `activity` と `heartbeat` の 2 ファイルだけで、
> 両方とも「まだ無い」のが普通の状態である

だったが、これは **「無い」と「読めない」を同じものとして扱っていた**。「まだ
無い」が普通なのは `ENOENT` の話であって、`EACCES` や `EIO` の話ではない。
区別する側を `ENOENT` **だけ** の allowlist にすれば、懸念 (起動直後の Worker に
activity ファイルが無い) はそのまま通り、権限事故だけが「観測不能」になる。
`registry/heartbeats/` は全 Worker 共通なので、1 回の権限事故で全員が同時に
terminate 対象になる —— 後回しにしてよい大きさではなかった。

**M (`build_task_graph` の mission ディレクトリ)** — 落ちる先は DAG の表示だけ
で、queue の状態は変わらない。ただし「stat できない mission が画面から黙って
消える」のは望ましくないので backlog。可視化のゲートは
`enforce_task_graph_contract()` に 1 つだけあるので、直すならそこに寄せる。

---

## 4. カードは通常ファイルだけ (P2)

`read_task_card()` は `O_RDONLY | O_NONBLOCK` で開き、`fstat` で **通常ファイル
であることを確かめてから** 読む。それ以外 (FIFO / ディレクトリ / ソケット /
デバイス) は待たずに `[破損]` として拒否する。

* **なぜ待ち時間の上限ではないのか** —— 待てば読めるものが 1 つも無いから。
  カードは通常ファイルしかありえない。
* **なぜ「FIFO なら拒否」ではないのか** —— denylist は次の種類で必ず穴が開く。
  受理する側 (通常ファイル) を列挙する (`approve-judgment-needs-allowlist-and-scope`)。
* **なぜ `os.stat(path)` を先に見ないのか** —— 見た対象と開いた対象が別物で
  ありうる。判定は開いた **その fd** に対して行う
  (`verify-and-destroy-must-share-one-connection` と同じ形)。

### 上限が無いと何が止まるか

変更系の `plan.sh` は commit の **後に** 全 active mission を同期で走査して
`tasks.json` を作り直す。カードの読み取りが無期限にブロックすると、

* **別 mission の健全なカードを 1 枚直しただけの実行**が返らなくなる
* `retire --no-wait` が、退役を commit した **後に** watchdog のタイムアウトを
  使い切る
* 同じ読み取りを使う常駐デーモン 3 者 (dispatcher / verifier-dispatcher /
  watchdog) が座り込み、**全 mission の割り当てと Worker の生存監視が同時に止まる**

### テスト側の回避を外したこと

`tests/test_retirement.py` の `_pull_parked_inside_the_queue_lock()` は
`CREWVIA_TASK_GRAPH=0` を立てていた —— FIFO の card を commit 後の走査が
もう一度読みに来て永久に止まるからで、**実装者はこのハングに既に遭遇していて、
テスト側で回避していた**。t016 で回避を外し、生成を有効にしたまま通ることを
`test_a_parked_pull_still_refreshes_the_task_graph` が固定する
(`tasks.json` が実際に書かれたことまで見る —— 生成が別の理由で黙って
何もしていない形でも緑になるのを防ぐため)。

harness の停止点は `tasks/t000.md` の FIFO から `queue/state.yaml` の FIFO へ
移した。止まる位置は `cmd_pull._do()` の `state = load_state()` ——
退役予約チェックの **後**、assignment 公開の **前** で、位置は経過時間ではなく
`_do()` の中の文の順番で決まる。

### 残していること: `load_state()` には同じ判定を入れていない

**これは見落としではなく、明示的な取引である。**

`cmd_pull._do()` の中で、退役予約チェックの後にブロックしうる読み取りは 2 つ
しかない —— カードの `open()` と `load_state()` の `open()` である。カードを
塞いだ時点で、**両方を塞ぐと「本物の `plan.sh` を自分のキューロックの中で
確実に止める」手段が 1 つも無くなる**。止められなくなると、
`test_red_marker_is_not_created_while_a_pull_transaction_is_open`
—— このミッションが直した Codex 6 巡目 P1 (pull のトランザクションが開いて
いる間に退役 marker が作られ、task を掴む Worker に shutdown が飛ぶ) を
固定しているテスト —— が成立しなくなる。**直したばかりの P1 の回帰テストを
捨てる**のは、残る危険より大きい。

残る危険と、その大きさ:

* 危険: `queue/state.yaml` が FIFO だと `plan.sh` が無期限に止まる。
* カードとの違いは **列挙されるかどうか**。`tasks/` は `tNNN.md` に合うものを
  何でも開くので、置かれたものが読まれる。`state.yaml` は固定パスで、書くのは
  `plan.sh` の `_atomic_write` (= `os.replace`) だけである。
* 自己修復性: 次に queue が state を書き換えた時点で `os.replace` が FIFO を
  通常ファイルに置き換える。

**t018 の時点**: helper (`read_regular_text` /
`read_regular_text_or_unreadable`) は作り、§2-2 の全行がそこを通るように
なった。機械検出 (`test_no_unguarded_read_remains`) の allowlist に残る
「crewvia のファイルを、意図的にガードの外で読む」行は **これ 1 つだけ**
である。取引の中身は上のとおりで、変わっていない。閉じるには harness の
停止点を別の仕組み (strace の delay 注入など、本番コードに触らないもの) に
置き換えるのが先である。**本番コードにテスト用のフックを足して閉じるのは、
この選択肢に含めない** (memory: microsecond-race-fix-needs-structural-test)。

例外が 1 つのままであることは
`tests/test_queue_reads_go_through_the_guard.py::test_the_one_deliberate_exception_is_still_the_only_one`
が見張る (allowlist のうち `plan.sh` の行を数えて、1 行であることまで
assert する)。閉じたらそのテストが赤で知らせるので、そのときに §4 ごと
更新する。閉じていないことをここに書いておく
(memory: and-condition-beats-unforgeable-evidence —— 閉じない指摘は閉じないと
明言する)。

---

## 6. 「例外を出さない」読み取りの、例外契約 (t018)

同じ漏れが 3 回出ている。

| 巡 | どこ | 漏れた例外 |
|---|---|---|
| 6 | `read_task_card()` (t014 で集約したとき) | `UnicodeDecodeError` |
| 9 | `read_regular_text_or_unreadable()` (t017 で新設) | `UnicodeDecodeError` |
| 9 | `plan.sh:try_read_queue_file()` (t017 で新設) | `UnicodeDecodeError` |

3 回とも形は同じ —— **`OSError` だけを名前で捕まえ、`ValueError` 側にいる
`UnicodeDecodeError` を素通りさせた**。`read_task_card()` は 6 巡目のあと
`except Exception` の backstop を持ったので 9 巡目では無傷だったが、その
とき一緒に作った新しい wrapper 2 つには backstop が無かった。

### 読み取りで起こりうるもの / wrapper が捕まえるもの

`_read_regular_file()` が通る文は 6 つ (`os.open` / `os.fstat` /
`raise NotARegularFile` / `os.set_blocking` / `os.fdopen` / `f.read`)。
そこから出うる例外と、wrapper の except の対応:

| 読み取りで起こりうるもの | 出どころ | 捕まえる except |
|---|---|---|
| `NotARegularFile` | 種類の判定 | `except NotARegularFile` |
| `FileNotFoundError` | `os.open` | `except FileNotFoundError` (→ `is_missing()`) |
| `PermissionError` / `IsADirectoryError` / その他 `OSError` | `os.open` / `os.fstat` / `os.set_blocking` / `f.read` | `except OSError` |
| `UnicodeDecodeError` | `f.read` のデコード | `except UnicodeError` |
| `MemoryError` / `RecursionError` / **まだ名前の無いもの** | どこでも | `except Exception` (backstop) |
| `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` | シグナル・終了要求 | **捕まえない (意図的)** |

**差分はゼロである。** `Exception` を継承するものは最後の backstop が必ず
受けるので、上の 4 行に漏れがあっても隔離は失われない。`BaseException` で
`Exception` ではない 3 つだけが通り抜けるが、これは「読み取りの失敗」では
なく「この実行を終わらせろ」という指示であり、飲むと Ctrl-C が効かなくなる。

**表を目で合わせる形はやめた。** 3 回ともそれで失敗している。代わりに

* `test_the_wrapper_has_a_backstop` —— `except Exception` があることを AST で見る
  (裸の `except:` は逆に落とす)
* `test_the_wrapper_never_raises` —— 代表的な例外を実際に注入して、
  `Unreadable` / `(None, 理由)` が返ることを確かめる
* `test_the_wrapper_does_not_swallow_interrupts` —— `KeyboardInterrupt` /
  `SystemExit` は通ること

の 3 つで見張る (`tests/test_read_wrapper_exception_contract.py`)。

---

## 5. 次に読み取り経路を足すときのチェックリスト

- [ ] この読み取りが失敗したとき、呼び出し側は「空」と区別できるか
- [ ] 区別できないなら、呼び出し側の述語は空に対して **どちらへ倒れる** か
- [ ] 倒れた先が完了・破壊・承認なら、観測の失敗を別の値で返す
- [ ] `open()` する相手の種類を確かめているか (通常ファイルか)
- [ ] **`ENOENT` と、それ以外の `OSError` を分けているか** —— 「まだ無いのが
      普通」は `ENOENT` の話であって、権限や I/O の失敗の話ではない (t017)
- [ ] **列挙の失敗だけでなく、列挙した各要素の `stat` の失敗も分けているか**
      (t017: 守ったのは一覧の入口だけで、読み手 2 者が捨てていた)
- [ ] 逆向きの担保 (本当に空のときは空のまま) のテストがあるか
- [ ] **失敗を `None` / `{}` / `[]` で返していないか** —— 呼び出し側の倒す先が
      破壊・完了・承認に届くなら `Unreadable` で返す (t018)
- [ ] **「例外を出さない」と名乗るなら `except Exception` の backstop があるか**
      —— 名前で並べた except だけだと、次の 1 種類で必ず漏れる (§6)
- [ ] `tests/test_queue_reads_go_through_the_guard.py` の表に 1 行足したか
      (足し忘れても `test_no_unguarded_read_remains` が落とすが、
      落ちた理由が 1 行で分かるほうがよい)

## 7. mux への問い合わせ: 「エラー本文がある」は「無い」ではない (t001, 2026-09-25)

ファイルの `ENOENT` と同じ区別が、mux への問い合わせにもある。herdr は失敗をすべて
`{"error": {"code": ...}}` の本文で返す (終了コードは 1)。**pane が無いとき (`pane_not_found`) も、
server が動いていないとき (`server_not_running`) も同じ形**で来る。

`HerdrBackend._resolve_pane_id()` / `_resolve_ids()` は「本文が返った = herdr が答えた = pane は
無い」と読み、記録 (`registry/mux/<name>.json`) を消していた。timeout (本文なし) は残すよう
書いてあったので「観測できなかったら残す」は意図されていたが、**server 不達が「答え」の形で
来る**ことを見落としていた。herdr の停止中に send / capture / pid が 1 回呼ばれるだけで、
その pane を作った証拠が消え、次の kill が恒久拒否になる。

- 「無い」と読んでよいのは `error.code == "pane_not_found"` だけ (`HerdrBackend._pane_existence()`。
  `pane get` の答えを読む場所はここ 1 箇所。テストが AST で数える)
- それ以外 (`server_not_running`・未知のコード・本文なし・空の本文) は「観測できなかった」で、
  記録を残す
- 空の id を尋ねるのも観測ではない (`pane get ""` は `pane_not_found` を返す)。尋ねずに保留する

失効記録の掃除 (`lib_mux.reap_stale_pane_records()`) も同じ判定を通る。設計と契約は
`knowledge/daemon-authority.md` §7-14。
