# 「空」と「観測できなかった」を区別する

> Codex 7 巡目 P1 / P2 (t016) で入れた規則と、読み取り経路の全数調査。
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

---

## 2. 読み取り経路の全数調査 (2026-09-24 時点)

`return []` / `return {}` / `return ''` を失敗時に返している箇所すべて。

| # | 場所 | 空が意味しうるもの | 危険な結論に落ちるか | 状態 |
|---|---|---|---|---|
| A | `lib_task_cards.list_task_cards` — `os.listdir` の `OSError` | **観測できなかった** | **YES** — mission 完了判定が True (`cmd_done` / `cmd_verify_result`) | **修正済** (t016)。`scan_failure_task()` を 1 件返す |
| B | 同上 — `not os.path.isdir(tasks_dir)` | 本当に無い **/** ディレクトリでない **/** stat できない の 3 つを同じ False に潰していた | **YES** — 同じ述語 | **修正済** (t016)。`os.stat()` で ENOENT だけを「本当に無い」にする |
| C | `lib_task_cards.read_task_card` — 個々のカードの失敗 | 観測できなかった | NO | 既に `[破損]` カードで保留 (t009/t013/t014) |
| D | `watchdog.WorkerMonitor._notification_files` — `iterdir` の `OSError` | **観測できなかった** | **YES** — 候補が減って idle が伸び、`_awaiting_human` の抑制も外れ、**両方 terminate の側** | **修正済** (t016)。ENOENT だけ `[]`、他は `None` で「観測不能」 |
| E | `watchdog.WorkerMonitor._mtimes_since_floor` — `p.stat()` の `OSError` を `continue` | ファイル 1 個の消失 (普通) と権限 (異常) | 理屈上 YES だが、D を直すと残るのは activity/heartbeat の 2 ファイルだけで、どちらも消えているのは正常な状態 | 現状維持 (下の §3) |
| F | `watchdog.load_active_tasks` — `state_file.exists()` False | 本当に無い / stat できない | NO — 「in_progress の task が 1 件も無い」= **誰も監視しない** = 誰も kill しない | 安全な向き |
| G | `lib_retirement.agents_with_state` — `iterdir` の `OSError` | 観測できなかった | NO — 退役が進まない側 (保留) に倒れる | 安全な向き |
| H | `dispatcher.load_state` / `load_workers` — ファイル欠損 | 本当に無い / stat できない | NO — active mission ゼロ → dispatcher は何も割り当てない | 安全な向き |
| I | `dispatcher._load_state_entry` (Rule 5 の grace) | 欠損 / 壊れている | NO — grace が最初からやり直しになる = **通知が遅れる側** | 安全な向き |
| J | `dispatcher.bench_current_strategy` → `''` | 観測できなかった | NO — ベンチ用の分岐のみ | 対象外 |
| K | `dispatcher._load_notify_cache` / `taskvia-sync.load_map` → `{}` | キャッシュ欠損 | NO — 再送・再同期の側 (冪等) | 安全な向き |
| L | `plan.sh._load_workers_from_registry` → `[]` | 観測できなかった | NO — Worker 一覧は表示と経験値加算のみ。空でも task は動く | 安全な向き |
| M | `plan.sh.build_task_graph` — `not os.path.isdir(mission_dir(slug))` で `continue` | 本当に無い / stat できない | NO (可視化のみ)。ただし **DAG からその mission が黙って消える** | §3 |
| N | `plan.sh.load_state` — `not os.path.exists(STATE_FILE)` | 本当に無い / stat できない | NO — active mission ゼロ → 何も割り当てない・何も完了しない | 安全な向き |

**修正したのは A / B / D の 3 件**。いずれも「観測の失敗が、取り返しのつかない
結論 (mission 完了 / Worker の終了) の側に落ちる」ものである。

---

## 3. 直していないもの、その理由

**E (`_mtimes_since_floor` の per-file `stat` 失敗)** — D を直した後にここへ
届くのは `activity` と `heartbeat` の 2 ファイルだけで、**両方とも「まだ無い」
のが普通の状態**である (pull 直後の Worker には activity ファイルが無い)。
ここで欠損を観測不能に倒すと、正常な起動直後が毎回「観測不能」になり、
`_signal_floor()` が持っている t044 の保証 —— 監視開始前の沈黙を数えない ——
と二重に効いて、**ハングした Worker が永久に検知されなくなる**。倒す向きが
逆なので、同じ形にはしない。

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

**backlog**: 「queue のファイルを開く」helper を 1 つ作り、カードと
`state.yaml` / `mission.yaml` が同じ判定を通るようにする。そのときは harness の
停止点を別の仕組み (strace の delay 注入など、本番コードに触らないもの) に
置き換える必要がある。閉じていないことをここに書いておく
(memory: and-condition-beats-unforgeable-evidence —— 閉じない指摘は閉じないと
明言する)。

---

## 5. 次に読み取り経路を足すときのチェックリスト

- [ ] この読み取りが失敗したとき、呼び出し側は「空」と区別できるか
- [ ] 区別できないなら、呼び出し側の述語は空に対して **どちらへ倒れる** か
- [ ] 倒れた先が完了・破壊・承認なら、観測の失敗を別の値で返す
- [ ] `open()` する相手の種類を確かめているか (通常ファイルか)
- [ ] 逆向きの担保 (本当に空のときは空のまま) のテストがあるか
