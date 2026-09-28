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

## 自爆させない — kill の関門（2026-09-27 の事故）

ガードは `scan()` で `/proc` を全部見て、`_belongs()` が「自分の子孫か」を答え、`kill_all()` が
その全部を SIGKILL する。**ブレーキが `_belongs()` の 1 箇所しかなかった。**

2026-09-27 14:04、このガードの変異テスト（`_belongs` の頭に `return "any", True` を注入した
`G1-count-everything-new`）を素の環境で走らせた。判定が全部「子孫」と答え、`pytest_sessionfinish` の
`scan(set(), ...)`（`exclude` が空）が同 uid の全プロセスを拾い、`kill_all()` が全部殺した:
`systemd --user`（`user@1000.service`）、tmux、変異テストを走らせていた Claude Code 自身、
**"start WSL" キープアライブの `sleep infinity`**、そして **n8n コンテナの main process**
（`n8nio/n8n` は `node` = uid 1000 で動き、userns-remap が無いのでホストの tkadmin と同一 uid）。
root 所有のプロセスは EPERM で残った。キープアライブが消えたので WSL はクライアント無しと判断し、
14:04:18 に `poweroff.target`、14:05:22 に VM の NIC 撤去 — WSL ごと落ちて amun がオフラインになった。

### 関門（`tests/kill_budget.py`）

判定と**別ファイル**に置く。判定を壊す変異が安全弁まで一緒に無効化しないため。判定が何と答えても
次に当たる pid は殺さない:

- `pid <= 1`
- 自分（`os.getpid()`）と**その祖先すべて**
- 自分のセッションリーダー / プロセスグループリーダー
- **自分より古いプロセス** — `starttime`（boot からの tick）で比較。同じ tick は許す（同 tick に生まれた
  子孫は本物）。これが今回の 4 つを単独で全部止める（どれも pytest より 11 日古い）。
- 許可が上限（既定 16 / `CREWVIA_LEAK_KILL_BUDGET`）を超えたら **1 件も殺さない**。壊れた環境変数は
  既定に倒す（上限が消えないように）。

`kill_all(survivors, kill=os.kill)` に差し替え口を足した（`pytest_workspace_sweep.pid_state` と同じ作法）。
`KillReport` が「殺した / 断った / 全件見送った理由」を返し、失敗メッセージに出る。

### 残る穴と、それを閉じる層

年齢ルールは「古いものを守る」ので、**若くて無関係なプロセス**は、判定が壊れていて件数が上限以内なら
まだ殺され得る。完全に閉じるのは OS の境界だけ:

    unshare -Urpf --mount-proc python3 -m pytest …

名前空間の中からは外の pid が `/proc` に見えず `os.kill` も ESRCH。**判定を壊す変異テストはここでしか
走らせない**（`tests/CLAUDE.md`）。非特権で動くことは WSL2 で確認済み。

### 回帰テスト

`tests/test_leak_guard_self_preservation.py` が、あの日の G1 と同じ状態（`_belongs` を潰す）を作って
「断るべき pid に kill が飛ばない」「祖先・自分は何があっても殺されない」「自分より古いものは断る」
「上限超過なら 1 件も殺さない」「それでも若い子孫はちゃんと殺す」を固定する。本物のシグナルは送らない。

### 空で読めた environ は「観測できなかった」(2026-09-27)

`_belongs` は `/proc/<pid>/environ` が読めなければ観測できていないとするが、**0 バイトで読めた**
場合を「読めた・印が無い」に潰していた。exec の最中のプロセスは environ が空で読める —— 孤児を
起こした直後の 1 読みで 300 回中 8 回 (2.7%)、印が現れるまでは 1ms 未満だった。その隙に走査すると、
印を継承した子孫が survivors にも unobservable にも入らず **黙って消え**、ガードは残骸を見逃した
まま緑になる。残骸を見逃さないのが仕事のガードとしては倒す向きが逆。
`knowledge/empty-vs-unobservable.md` の **O** として記録し、`if not environ` に直した。

判定そのものを見るテストは、走査の前に `_wait_observable()` で environ が読めるようになるのを待つ。
待たないと、印を数えるテストは 2.7% で落ち (2026-09-27 の flaky。両ファイル丸ごとで 22 回中 2 回、
当該 1 本だけなら 0/24 で再現しなかった)、**数えないことを確かめるテストは同じ確率で理由なく緑**
になっていた (0 件で PASS にしない / `tests/CLAUDE.md`)。

### PR#240 2 巡目 codex review (2026-09-27, t067)

1. **`red_proof_t047.sh` の安全性の説明が誤っていた**。スクリプトは「注入点を直接少数の候補で呼ぶ
   だけだから `scan()` の全走査 + 本物の kill は通らない」と書いていたが、`run_py` が呼ぶのは
   フルの `python3 -m pytest tests/test_leaked_descendants_guard.py …` で、`conftest.py` の
   `pytest_configure` がこのセッション全体に (欠陥入りの木の) `LeakGuard` を登録する。つまり
   個々のテストの後 / session finish のたびに autouse fixture が本物の `scan()` + `kill_all()`
   を回す —— 前提が成立していなかった。直しは「ホストで走らせない」ではなく「`run_py` の呼び出し
   全部 (baseline も含む) を PID 名前空間の中に閉じ込める」。`unshare --user --map-root-user --pid
   --fork --mount-proc` を使い、作れなければホストにフォールバックせず拒否 (exit 3, fail closed)。
   スクリプト内で「本物に届いていない」ことを PID1 がラッパー自身であること・見えるプロセス数が
   小さいこと・ホストの pid へ `kill -0` が届かないこと、の 3 点で確認してから初めてケースを走らせる
   (PID 名前空間はカーネルの階層構造そのもので子から親を見せないので、最後の確認は原理的に必ず通る
   ―― 「unshare が本当に隔離できたか」の smoke test)。
2. **`_dir_in_cmdline` は一致の「後ろ」の境界しか見ていなかった** (`_belongs` の 1 巡目 fix は
   この境界チェックを追加した箇所そのもの)。`/backup/tmp/pytest-1/job.py` (前に別ディレクトリが
   付いているだけ) や `/tmp/pytest-1/../pytest-2/job.py` (`..` で実体が隣に逃げる) は、後ろの境界は
   満たすので誤って一致した。引数を NUL 区切りで 1 単位ずつ取り出し (`--rootdir=<path>` のような
   `=` 付きは値側も見る)、`os.path.normpath` で正規化してから完全一致 / 区切り付き前方一致を見る
   方式に直した (`os.path.commonpath` 相当)。相手プロセスの cmdline に現れた文字列は `realpath`
   しない (制御できないファイルシステムへの stat を pid ごとに発生させ、走査が固まりうるため) —
   これは意図した残存ギャップとして残す。一方 `basetemp` 自身 (呼び出し元が知っている自分のディレクトリ)
   は `scan()` が 1 回だけ `realpath` する。`/proc/<pid>/cwd` はカーネルが常に正規化済みパスを返す
   ので、`TMPDIR` がシンボリックリンク越しの環境でも cwd 側の判定と文字面が食い違わなくなる
   (族B: 観測対象と比較対象の同一性がずれる、の一種)。
3. **`kill_all` は pid 番号で `os.kill` していた**。`scan()` で見つけてから `kill_all` で実際に
   殺すまでの間 (settle の再試行・`GRACE_SECONDS` の待ち) に pid が再利用されると、`partition()`
   は新しい無関係なプロセスの年齢だけを見て判定するので、生まれ変わった別プロセスを殺しうる。
   `scan()` が候補を見つけた**その場**で `os.pidfd_open` して pidfd を束縛し、直後に starttime を
   読み直して一致を確認 (`_open_pidfd_verified`) してから `Survivor.pidfd` に載せる。実際の破壊
   (`_default_kill`) は `signal.pidfd_send_signal(survivor.pidfd, sig)` **限定** —— pidfd はその
   場で束縛した瞬間のプロセスインスタンスにしか届かないので、pid 番号がその後どれだけ再利用されても
   無関係な相手を殺せない (memory: verify-and-destroy-must-share-one-connection)。pidfd を束縛
   できなかった survivor は pid 番号へフォールバックせず kill しない (族A: 観測失敗を許可に倒さない)。
   `pidfd_open` 自体が使えないカーネルでは「検出はするが殺せない」に劣化するので `install()` が
   警告する (族C: ガードの安全弁が単独障害点で無効化されたことを黙って隠さない)。

**族ごとの掃除で見た他ファイル (処置なし、理由あり)**:

- `tests/proc_group.py` の `kill_group` (`os.killpg(proc.pid, …)`) / `kill_tree`
  (`descendants()` → `os.kill` を pid 番号で) にも「観測 (pid を確定) してから作用するまでの間に
  pid が再利用されうる」という同じ形の族B はある。ただし対象は常に**呼び出し元が直前に自分で
  spawn した直接の子/子孫**で、システム全体を走査する `leaked_descendants.scan()` (2026-09-27 に
  `systemd --user` / tmux / n8n を巻き込んだ張本人) とは信頼モデルが違う。プロセスグループには
  pidfd 相当の束縛手段が無く (pidfd は個別 pid 用)、同じ強度の修正は構造的にできない。窓は「同じ
  関数呼び出しの中の数命令」で、成功パスは `communicate()` の reap 直後 (pid 解放から次の行までの
  マイクロ秒未満)、タイムアウトパスは対象がまだ生きていることが確定している (だから reuse 自体が
  起きない)。costs (複雑化) が bounded な残存リスクに見合わないと判断し、直さず記録に留める。
- `tests/conftest.py` の `idle_pane_shell` fixture の `os.kill(pid, SIGKILL)` も同型 (`pty.fork()`
  した直接の子を finally で殺す) で、同じ理由により処置なし。
- `tests/kill_budget.py` の `partition()` 自身は `_ppid_and_start(pid)` で毎回 pid 番号から年齢を
  読み直すので、`scan()` の pidfd 束縛と時点がずれうる。ただし kill_budget の役目は「殺していいか
  の判定」だけで、実際の破壊は必ず `kill_all` が `survivor.pidfd` 経由で送る (上記 3.) ため、
  kill_budget が pid 再利用で誤った年齢を見て判定を誤っても、その誤判定が実際の kill 対象を変える
  ことはない (判定と破壊が同じ pid 番号を再確認しているわけではなく、破壊は束縛済みの pidfd に
  固定されているため)。よって kill_budget 側の追加修正は不要と判断。

### PR#240 6巡目 codex review (2026-09-28, t093) — 4 モジュール横断の所有・待ち・握り潰しの監査

5巡目 (t083) が入れた「1 プロセス分の処理を丸ごと境界に閉じ込める」(`_scan_one`) という構造は、
**境界の外側で「観測できなかった」に変換する前に、境界の内側で確保した資源を閉じる**ところまでは
保証していなかった。6巡目はその抜けを直したうえで、`tests/leaked_descendants.py` /
`tests/kill_budget.py` / `tests/proc_group.py` / `tests/conftest.py` の 4 ファイルについて、
「所有」「待ち」「握り潰し」の 3 つの規則を横断で棚卸しした。

#### findings (2 件)

1. **[P2-1] `_scan_one` の pidfd 所有権**: `_open_pidfd_verified` が束縛に成功した**後**
   (cmdline / wchan の読み取り、`Survivor` の構築) で例外が起きると、外側の
   `except Exception: return None, True` がそれを「観測できなかった」に変えるだけで、
   束縛済みの pidfd を閉じ忘れていた。所有権は `Survivor` を返す 1 つの正常出口でしか
   渡らないので、pidfd 取得から `Survivor` 構築までを内側の `try/finally` で囲み、
   所有権が確定する前に例外が起きたら finally で閉じてから外側の境界へ再送出する形にした。
2. **[P2-2] `run_in_own_group` の後始末の待ち**: `communicate(timeout=)` がタイムアウトした後、
   `kill_group()` を送ってから**上限なしの `communicate()`** を呼んでいた。`kill_group` は
   `PermissionError` を含む終了エラーを黙って握り潰すので、シグナルが実際に届いたかはここでは
   分からない —— 届かなかった、または `set -m` (job control) で process group を抜けた子孫が
   pipe (stdout/stderr) を握ったまま残っていると、後始末の `communicate()` は EOF を無期限に
   待ち、呼び出し元の `TimeoutExpired` すら上がらない (leak guard まで制御が返らない)。
   `except BaseException:` 側の `proc.wait()` も対になる同じ形。どちらも
   `CLEANUP_GRACE_SECONDS` (5.0s) を上限に置き、上限に達したら元の例外の代わりに
   `ProcGroupCleanupError` を送出して報告するようにした。

#### 所有の規則 (境界の中で取得する資源) — 4 モジュール横断

| モジュール | 資源 | 正常出口 | 例外出口 | early return | 保証の形 |
|---|---|---|---|---|---|
| `leaked_descendants.py` `_scan_one` | pidfd (`_open_pidfd_verified`) | `Survivor` へ所有権を渡す (1 箇所) | **(6巡目まで漏れていた)** 内側 `try/finally` で close してから再送出 | pidfd 取得**前**の return は pidfd 自体が無いので該当なし | 構造的 (`try/finally`) |
| `leaked_descendants.py` `kill_all`/`settle` | `Survivor.pidfd` (走査で束縛済み) | `_close_survivor_pidfds` で必ず閉じる (`kill_all` の全 return 経路、`settle` の再走査前) | 同左 (`_close_survivor_pidfds` 自体は例外を出さない設計) | `fatal` 早期 return でも `_close_survivor_pidfds` を経由 | 構造的 (全 return 直前に一括 close) |
| `kill_budget.py` | なし (`/proc` の読み取りは `read_bytes()` で自動close) | — | — | — | 該当資源なし |
| `proc_group.py` `run_in_own_group` | `Popen` (子プロセス・stdout/stderr pipe) | `communicate()`/`wait()` が reap、`kill_group` で残党も掃除 | **(6巡目で追加)** `except subprocess.TimeoutExpired` / `except BaseException` の両方で `kill_group` → 上限つき `communicate`/`wait`。上限に達したら `ProcGroupCleanupError` (プロセス自体は道連れにできず、残存リスクとして下記に記録) | なし (Popen 起動後は必ずどちらかの except かフォールスルーを通る) | 部分的 (通常経路は構造的。上限超過後の完全な reap は保証できない — 下記「残る穴」) |
| `conftest.py` `idle_pane_shell` | `pty.fork()` の子 pid + master fd | `finally` で `kill` → `waitpid` → `close(fd)` (yield を挟んだ `try/finally`) | 同左 (`finally` は例外時も実行される) | デッドライン超過時も同じ `kill`/`waitpid`/`close` を実行してから `pytest.skip` | 構造的 (`try/finally`) |

#### 待ちの規則 (待つ箇所を全部列挙・上限の有無・上限到達時の扱い)

| モジュール・箇所 | 上限 | 上限到達時 |
|---|---|---|
| `leaked_descendants.py` `kill_all` の死亡確認ループ | あり (`GRACE_SECONDS`=1.5s) | **(6巡目まで欠落)** 死亡未確認のまま `killed` に含めて黙って返していた → `still_alive` フィールドを追加し `KillReport.describe()` で報告するようにした |
| `leaked_descendants.py` `settle` の再走査ループ | あり (`GRACE_SECONDS`) | 到達時点の `Scan` (survivors/unobservable) をそのまま返す。呼び出し元 (`LeakGuard`) が `pytest.fail` / `warnings.warn` で必ず報告するので、ここでの黙殺はない (既存、変更なし) |
| `proc_group.py run_in_own_group` の最初の `communicate(timeout=timeout)` | あり (呼び出し元指定) | `TimeoutExpired` をそのまま伝播 (既存、変更なし) |
| `proc_group.py run_in_own_group` のタイムアウト後の後始末 `communicate()` | **(6巡目まで無し)** → `CLEANUP_GRACE_SECONDS`=5.0s | **(6巡目まで無限に待っていた)** → `ProcGroupCleanupError` を送出して伝播 |
| `proc_group.py run_in_own_group` の `BaseException` 後の後始末 `wait()` | **(6巡目まで無し)** → `CLEANUP_GRACE_SECONDS`=5.0s | 同上 |
| `conftest.py idle_pane_shell` の idle 安定待ちループ | あり (10.0s) | `else` 節で子を kill・waitpid・fd close してから `pytest.skip` (既存、変更なし) |
| `conftest.py idle_pane_shell` の `finally` 内 `os.waitpid(pid, 0)` | **無し** (`waitpid` に timeout 引数は無い) | 対象は直前に自分で `SIGKILL` した pty の子であり、D state 等の例外を除けば実務上すぐ返る。この境界 (5〜6巡目の「例外の境界」) より前から存在するパターンで、`os.waitpid` を timeout 付きで書き直すには別の仕組み (`WNOHANG` ポーリング) が要り、この task の対象 4 モジュールのうち唯一 pty を扱う特殊な fixture であることも踏まえ、今回は直さず残存ギャップとして記録する |

#### 握り潰しの規則 (`except …: pass` / `contextlib.suppress` を全部列挙・報告に出るか)

| モジュール・箇所 | 握り潰す例外 | 報告に出るか |
|---|---|---|
| `leaked_descendants.py` `_close_survivor_pidfds` | `Exception` (`os.close` 失敗) | 出ない。POSIX 上 `close()` はエラーでも fd 自体は解放される規約のため実害が薄く、他の全箇所 (`_signal_one` 等) と同じ「後始末は失敗しても他件を止めない」という既存方針に合わせた既知の意図的な黙殺 |
| `leaked_descendants.py` `_signal_one` (`ProcessLookupError`) | `ProcessLookupError` のみ | 出ないが「シグナル不要 (既に死亡)」という成功と等価な意味しか持たないので実害なし (既存) |
| `kill_budget.py` `protected()` | `OSError` (`os.getsid(0)` / `os.getpgrp()`) | 出ない。読めなければ `keep` に追加されないだけで、真に致命的な保護 (自分・祖先・`pid<=1`) は別途 `ancestors(me)` で確保されている。`tests/CLAUDE.md` の「このファイルは変異させない」対象そのものであり、6巡目では手を入れず記録に留める |
| `proc_group.py` `kill_group` | `Exception` (`os.killpg` — `PermissionError` 含む) | **出ない (これが P2-2 の根本原因)**。ただし kill_group 自体の戻り値を「成功したか」に変えるのではなく、呼び出し元 (`run_in_own_group`) 側で「後始末が上限内に終わったか」を見て `ProcGroupCleanupError` に変換する形にした — kill_group は他の 3 箇所 (`test_watchdog_idle.py` / `test_retirement.py` / 自身の別呼び出し) からも「例外を出さないこと」を前提に使われており、戻り値の意味を変える改修はそれらの呼び出し規約を壊すため見送った |
| `proc_group.py` `kill_tree` (per-victim ループ) | `Exception` (`os.kill`) | 出ないが、対象は常に呼び出し元が直前に自分で作った子孫で、失敗しても他の victim の kill は続く (5巡目で確立した既存方針)。システム全体を走査する `leaked_descendants.scan()` とは信頼モデルが違うため 6巡目でも処置なし |
| `conftest.py` `idle_pane_shell` の `finally` | `(ProcessLookupError, ChildProcessError)` | 出ないが、対象は自分が `pty.fork()` した子で、既に死んでいる/回収済みという想定内の状態のみを握り潰す (既存) |

#### 残る穴 (処置なし、理由あり)

- `run_in_own_group` が `ProcGroupCleanupError` を送出した後、`Popen` オブジェクト自体は
  呼び出し元に返らない (例外を送出するため)。escaped した子孫がその後も生き続ける可能性はある
  が、これは「殺せなかった」ことを**黙って隠さず例外で報告する**という 6巡目の主眼そのもの
  であり、実際の子孫の掃除は `tests/leaked_descendants.py` の構造ガード (session finish の
  再走査) が最後の網として引き継ぐ (このガード自身は同 uid 全体を見るので、pgid を抜けた
  子孫であっても basetemp/marker で拾える)。
- `conftest.py idle_pane_shell` の `os.waitpid(pid, 0)` に上限を付けなかった件は上表のとおり。
- **既知の限界（backlog、7 巡目、t031）**: `test_leaked_descendants_guard.py` の統合テストは、
  ガードが意図的に kill しない (pidfd を使えない) カーネルに能力ゲートが無く、要約の「漏れ 1 件」の
  assert がそのまま落ちる。`os.pidfd_open` を削除する case はテスト内の新しいインタプリタにしか
  効かず、外側の実行環境そのものを変えられない。CI・本番のカーネルには pidfd があるため本題外。
  観察テスト自身が `scan()` の返す pidfd を閉じないため fd がテストセッション内で漏れる件も同様
  （プロセスは残らないので害はない）。
