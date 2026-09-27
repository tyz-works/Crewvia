# テストが子孫プロセスを残さない — FIFO テストの plan.sh 孤児と構造ガード（t029 / backlog #32）

> 対象コード: `tests/proc_group.py`、`tests/leaked_descendants.py`、`tests/conftest.py`（`install()` の呼び出し）、
> `tests/test_leaked_descendants_guard.py`、直したテスト（`tests/test_unobservable_is_not_empty.py` の `Sandbox.run` /
> `tests/test_retirement.py` の `_pull_parked_inside_the_queue_lock` / `tests/test_watchdog_idle.py` の process-tree fixture）

## 何が起きていたか（実測）

全 pytest を回すたびに plan.sh の `python3 -` が数個残り、2026-09-26 に 22 個・287MB を手で kill した。

**出どころ**: FIFO を使うテスト（`Sandbox.run` 経由の `tests/test_unobservable_is_not_empty.py` /
`tests/test_guarded_reads_on_direct_paths.py`）が plan.sh を `subprocess.run(..., timeout=)` で走らせる。
タイムアウトで kill されるのは**直接の子の bash だけ**で、その下の `python3 - <queue> <cmd> ...` は孤児になる。
その python は書き手のいない FIFO を `open()` したまま、カーネルの `wait_for_partner` で永久に待つ
（init に付け替わり、誰も回収しない）。

    bash plan.sh done ...            ← subprocess.run が kill するのはここだけ
      └ python3 - <queue> done ...   ← 残る（wchan = wait_for_partner）

**再現の条件**: 孤児になるのは、FIFO を「待たずに拒否する」ガード（`lib_task_cards.read_task_card()` の
`O_NONBLOCK` + `fstat`）が効かず、テストが**タイムアウトまで行った**ときだけ。ガードが効く今の main で
テストが緑のときは plan.sh は待たずに返るので残らない。実測（隔離コピー、pytest を subreaper の下で走らせて
孤児を親に付け替えて数えた）:

| 状態 | plan.sh の孤児 |
|---|---|
| main（緑）で全 pytest | **0 個** |
| `O_NONBLOCK` を外した複製（= FIFO を待たなくなる欠陥を戻した状態）で FIFO 3 ファイル | `python3 - <queue> done/needs-director/status ...` が **4 個以上**（`wait_for_partner`） |

つまり backlog の「毎回残る」は、FIFO のガードが赤かった間（開発中）の話で、テストが赤くなるたびに孤児が
溜まる仕組みが本体だった。**緑の main でも残っていたのは別の型**（下）。

同じ「直接の子だけ kill して木を残す」型が `tests/test_watchdog_idle.py` の process-tree fixture にもあり、
全 pytest のたびに `sh -c "sleep 300 …"` と `sleep` が計 6 個（各 2MB、最長 5 分で自然に消える）残っていた。

## 本体（plan.sh）が永久に待つ経路か

**ある — ただし既知の明示的な取引**。`plan.sh` の `load_state()` は `queue/state.yaml` が FIFO だと待つ。
閉じていないのは意図で、`tests/test_retirement.py` の `_pull_parked_inside_the_queue_lock()` が
Codex 6 巡目 P1 の回帰テストを成立させる**唯一の停止点**にしているから（`knowledge/empty-vs-unobservable.md` §4 /
`tests/test_queue_reads_go_through_the_guard.py` が「例外が 1 つだけ」であることを固定している）。
task card・mission.yaml・daemon 状態ストアなど他の読み取りは FIFO を待たずに拒否する。この task では直していない。

## 直したもの

1. **テスト側の後片付け**（`tests/proc_group.py`）: `run_in_own_group()` / `kill_group()`。子を新しいセッション
   （`start_new_session=True`）の頭にして、タイムアウト・後始末で `os.killpg` する。`Sandbox.run`（FIFO 3 ファイルが
   使う plan.sh の実行）・`_pull_parked_inside_the_queue_lock`・`test_watchdog_idle.py` の 3 fixture に適用。
   本番の plan.sh は別セッションなので巻き込まない。
2. **構造ガード**（`tests/leaked_descendants.py`、`tests/conftest.py` の `pytest_configure` から `install()`）:
   各テストの後に、**そのテストの間に増えて生きている、このセッションの子孫**を探し、あれば kill して
   そのテストを ERROR にする（メッセージに pid / ppid / 状態 / wchan / cmdline）。セッションの終わりにもう一度
   探す（module / session スコープの fixture のため）。

### 本番を誤って数えない・殺さない

「このセッションの子孫」は **祖先の探索ではなく** 次の OR で決める（孤児は init に付け替わって祖先が辿れない）:

- 環境に `CREWVIA_PYTEST_SESSION=<pid>-<hex>`（`conftest` が `os.environ` に置く印。セッションごとに違う）
- cmdline か cwd がこのセッションの pytest 一時ディレクトリ（basetemp）を指す（`env -i` で環境を捨てた子孫用）

本番の plan.sh / dispatcher / watchdog はこのセッションの環境を継承せず basetemp も指さないので数えない。
同時に走る別セッションの pytest も印が違う。同じ uid だけを見る・ゾンビ（`Z`）は死んでいるので数えない・
pytest 自身は数えない。環境が読めない同 uid のプロセスは「無い」にせず、観測できなかった数としてメッセージに残す。
`/proc` が無い環境（macOS）ではガードは動けないので警告を出す（黙って全部通さない）。

**env の停止スイッチは付けていない。**

## 戻し方

構造ガードが誤検出して全 pytest が落ちたら（テストが残していないのに「子孫を残した」と ERROR になるとき）:

- **PR を revert する**。proc_group・ガード・直したテストが一緒に元へ戻る（FIFO の孤児は再び溜まるので、
  手当ては、`ps -eo pid,ppid,wchan:20,args` で `wait_for_partner` のまま `/tmp/pytest-of-…/queue` を指す
  `python3 -` を**目で確かめて**個別に kill する。パターンで一括 kill しない — 同時に走る別セッションの
  pytest の子まで巻き込む）。
- **ガードだけ外す最小差分**: `tests/conftest.py` の `pytest_configure` にある
  `import leaked_descendants` と `leaked_descendants.install(config)` の 2 行を消し、
  `tests/test_leaked_descendants_guard.py` を消す。`install()` が呼ばれなければ fixture も sessionfinish も
  登録されず、印も置かれない。`tests/proc_group.py` と直したテストは残してよい（後片付けが木ごとになるだけ）。
- 誤検出の 1 件だけを許したいときは、ガードを外さず、そのテストが残しているものを片付ける
  （`kill_group()`）。残るのが正当なら、`leaked_descendants.py` の判定に理由付きの除外を足す（説明できない除外は足さない）。
