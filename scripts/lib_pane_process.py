"""lib_pane_process.py — mux ペインのプロセス木を 3 値に分類する (t016 → B1 で lib 化)。

watchdog.py にあった `classify_process_tree()` を、dispatcher.sh の Rule 5 からも
同じ定義で呼べるように切り出したもの。**「ペインの裏で何かが走っているか」の定義は
ここ 1 つ**。コピーしない (watchdog と dispatcher で答えが割れる)。

この lib は判定しない — 「terminate してよいか」「通知してよいか」は呼び出し側が、
自分の判定の fail の向きで決める (knowledge/watchdog-idle-judgment.md)。
読むのは /proc だけで、queue / registry / config は開かない。

## t074 (B1 設計変更 / Codex review 3巡目 P1): 判定根拠を「何であるか (comm)」から
## 「誰が起動したか (起動元)」に移す

t065 (2巡目) は判定根拠を時刻から comm (実行ファイル名) に移したが、**3巡目でも
穴が向きを変えて残った**:

  | 巡 | 代理指標                                   | 破れ方                                   |
  |----|--------------------------------------------|-------------------------------------------|
  | 1  | セッション起動から 60 秒以内の子孫は無視     | 起動直後に投げた長い job が永久 idle (偽陽性) |
  | 2  | assignment 以降に生えた子孫は job            | 遅延起動の MCP が永久 job (偽陰性)          |
  | 3  | comm の完全一致の許可リストで永続インフラを除外 | 本番の `npm exec @playwright/mcp` は npm が |
  |    |                                              | process.title を書き換え comm が           |
  |    |                                              | `npm exec @playw` (15 文字打ち切り)、       |
  |    |                                              | しかも `sh -c "playwright-mcp"` を挟む →    |
  |    |                                              | シェル一致で job 扱い (偽陰性: MCP が動いて  |
  |    |                                              | いる間 Rule 5 が永久に黙る)                 |

**comm (実行ファイル名) は本質的に代理指標**である — 「シェルという名前かどうか」も
「知っているインフラの名前かどうか」も、そのプロセスが**何をするために起動されたか**
とは無関係 (npm は自分の process.title を書き換えるし、MCP サーバーの起動経路は
`sh -c "..."` を挟む)。t074 (2026-09-27 本番実測, Director + Worker) で比較した:

  | プロセス                     | comm             | Bash tool のラッパー
  |                               |                  | (`.claude/shell-snapshots/snapshot-` を
  |                               |                  | cmdline に持つ祖先) があるか |
  |-------------------------------|------------------|--------------------------------------|
  | Playwright MCP                | `npm exec @playw`| **無い**                              |
  | chrome-devtools MCP           | `npm exec chrome`/`sh` | **無い**                        |
  | Playwright の node 本体       | `MainThread`     | **無い**                              |
  | Bash tool (`run_in_background`)| `sleep` 等       | **有る** (直接の親が `bash -c source |
  |                               |                  | .../shell-snapshots/snapshot-....sh  |
  |                               |                  | && eval '...'`)                      |
  | Monitor                       | `bash`           | **有る** (Monitor 自身が `bash -c    |
  |                               |                  | source .../shell-snapshots/snapshot-  |
  |                               |                  | ....sh && eval '<command>'`)         |

**comm では見分けられない**(MCP 側にも `sh` や `bash` が出るので、シェル comm =
job という旧ルールでは MCP を job と誤読する)。**起動元では綺麗に分かれる**:
Bash tool (前景・`run_in_background` の裏 job いずれも) と Monitor は、必ず
Claude Code 自身が書き出す shell snapshot ファイル (`~/.claude/shell-snapshots/
snapshot-<timestamp>-<hash>.sh`) を `source` する `bash -c "source <path> ... &&
eval '<command>' ..."` という形で生える。MCP サーバー (`npm exec ...`) は
`claude` プロセスの直接の子として起動し、この形を経由しない。

新しい判定基準: **祖先 (自分自身を含む) のどれかの cmdline に、この shell snapshot
を `source` する形が含まれるか**。含まれれば job (その子孫もすべて job — シェルが
起動した実体の一部)。含まれなければ job ではない (infra)。

**マーカーを持たない (=知らない名前・同定できないプロセス) は「job ではない」
側に倒す** — これは「観測に失敗した (読めない)」とは別の話。族C (t065 で
作った「同定できないものは job 側に倒す」という許可リスト方式の考え方) は
**この設計では成立しない**: 許可リストが要らなくなった (「既知のインフラ名を
全部知っている」という前提そのものを捨てた) ので、「未知の名前を job/infra の
どちらに倒すか」という問い自体が無くなった。さらに、旧実装の「同定できない
場合は job 側に倒す」という**コメント**は「1 回余計に通知する方が安い」と
書かれていたが、実際には classify=`executing` は **通知/terminate を抑制する
側**(dispatcher の `worker_has_background_work` / watchdog の `_process_signal`
参照) なので、**コメントと挙動が逆だった** (Codex 3巡目 P1)。

**「観測に失敗した (cmdline が消滅以外の理由で読めない)」は別の話で、`unknown`
に倒す** (t082 P1)。当初はここも「job ではない」に潰していたが、読めない
ノードが Bash tool のラッパー自身だと、走っている本物の job のマーカーが
誰にも見つからず `idle_process` に化ける — watchdog はそれを見て hard-idle
による terminate を許してしまう (「観測できないなら kill しない」という
要件に反する)。`_proc_stat` (プロセス木の列挙) と同じ基準で、「消滅」
(ENOENT/ESRCH) だけを「マーカー無し」と同じ扱いにし、それ以外の読み取り
失敗は木全体を `unknown` に倒す。

時刻 (`grace_seconds` / `min_start_epoch`) はもう判定に使わない (t065 で撤去済み)。
comm による同定 (`_SHELL_COMMS` / `_KNOWN_INFRASTRUCTURE_COMMS`) も撤去した —
残すと「起動元」と「名前」の 2 つの判定基準が食い違いうる。

## t091 (B1 5巡目 P1): `exec` が cmdline の印を消す — environ を第二の証拠にする

t074 の「起動元」判定は cmdline の `BASH_TOOL_WRAPPER_MARKER` **だけ**を見ていたが、
Bash tool の中で `exec sleep 30` のように `exec` を使うと、その pid の cmdline は
`sleep 30` に置き換わり (execve が argv を丸ごと差し替えるため)、ラッパーの印が
その pid 自身の cmdline からは失われる (実測、`bash -c 'exec sleep 30'` の
`/proc/<pid>/cmdline` は `sleep 30`)。この pid はもう祖先の cmdline も持たない
(execve は同じ pid のまま置き換わるだけで、親子関係も cmdline の履歴も残さない)。
結果、この job は `idle_process` に化け、watchdog の hard-idle が terminate を許してしまう
(本物の job を kill してよいことにする、という向きの誤り)。

**exec は cmdline を消すが、environ は消さない** — execve は呼び出し元が明示的に
新しい envp を渡さない限り、既存の環境変数をそのまま引き継ぐ (実測:
`bash -c 'exec env | grep CLAUDE'` は元の shell と同じ `CLAUDE_CODE_CHILD_SESSION=1`
`CLAUDE_CODE_EXECPATH=...` を含む)。Director が本番で実測した表 (2026-09-28 04:35):

| 環境変数 | MCP (`npm exec @playw` 等) | Bash tool の job (`exec` 後も) |
|---|---|---|
| `CLAUDECODE` | あり | あり |
| `CLAUDE_CODE_CHILD_SESSION` | **なし** | **あり** |
| `CLAUDE_CODE_EXECPATH` | **なし** | **あり** |

（`/proc/<pid>/environ` は同一 uid の非祖先プロセスでも読める — 本番 pane で実測済み。
Yama `ptrace_scope=1` の下でも `PTRACE_MODE_READ_FSCREDS` はこの読み取りを許す。
watchdog / dispatcher は pane の祖先ではないので、この読み取り可否が本設計の前提になる）。

**3 値に分ける** (per-node の起源判定。`_origin_of()`):

  - **job**: 自分の cmdline に wrapper marker がある、**または** 自分の environ に
    `CLAUDE_CODE_CHILD_SESSION` **または** `CLAUDE_CODE_EXECPATH` がある
  - **infra**: 上記が無く、`CLAUDECODE` はある (Claude が起動したが Bash tool /
    Monitor 経由ではないもの — MCP サーバー・claude 本体)
  - **unknown**: `CLAUDECODE` すら無い (`env -i` で消された、または Claude Code と
    無関係なプロセス)、または `environ` が「消滅」以外の理由で読めない

**job の env シグナルを 2 つ (`CHILD_SESSION` / `EXECPATH`) にした理由**: どちらも
Claude Code の内部実装の変数名なので、将来どちらかの名前が変わっても、もう一方が
残っていれば job 判定は壊れない (単一の定数に依存する設計は、その定数が壊れた
瞬間に「本物の exec 済み job が infra に化けて kill されうる」という、この task が
直そうとしている欠陥そのものを再導入してしまう — 受入条件(4)参照)。両方同時に
壊れる (= job の env シグナルが両方消える) 場合だけ、job は cmdline marker 頼みに
戻る (t074 までの状態と同じ露出)。これは残存リスクとして明記する。

**不明の扱いは判定ごとに非対称** (`classify_process_tree` 自体は判定しない —
呼び出し側の fail-direction は `watchdog-idle-judgment.md` 参照):
  - 木の中に **job が 1 つでもあれば** (`job` が見つかった時点で) 即 `executing`
    (既存動作を維持)
  - job が無く、`unknown` origin のノードが 1 つでもあれば木全体を `unknown` に倒す
    (Rule 5 は「job ではない」= 通知する側、watchdog は `unknown` を殺さない側。
    どちらも「安全側」で、`idle_process` (確実に job が無い) と区別する)
  - job も unknown も無ければ (全ノードが確実に infra) `idle_process`

`environ` の読み取り失敗 (「消滅」以外の `OSError`) は `_proc_cmdline` と同じ基準で
即座に木全体を `unknown` に倒す (t082 の cmdline 読み取り失敗と同じ理由 — 読めない
ノードが本物の job の証拠を持っていた可能性を握り潰さない)。

**この設計で観測不能な既知の限界**: `setsid` ユーティリティおよび `( cmd & )` の
二重 fork は、いずれも中間の親プロセスをすぐ終了させることで対象を pane の
祖先チェーンから完全に切り離し (init/subreaper の子になる)、**root_pid からの
`ppid` ベースの木構造走査そのものが対象を見失う**。cmdline・environ のどちらを
見ても解決できない (実測: `setsid sleep N` / `( sleep N & )` の実プロセスは
どちらも数百 ms 後に `ppid=1` になり、pane の子孫から消える)。この場合の安全弁は
既存の watchdog 絶対上限 (`max_threshold`) だけであり、この task (exec によるマーカー
消失) とは別の残存リスクとして明記する。`disown` (シェル組み込み、実行中プロセスの
親子関係・cmdline・environ には無関係) は判定に影響しない。

## t097 (B1 6巡目 P1, Codex review): セッション本体が `unknown` に化け hard-idle が永久に効かない

t091 は `_origin_of()` が `CLAUDECODE` の無いノードを `unknown` に倒すようにしたが、
**このノードには Claude のセッション本体 (`claude --model ...` 自身) も含まれる**。
本番では `_herdr_server_env()` が `CLAUDECODE` / `CLAUDE_*` を herdr server 起動時点で
消し、start.sh の `LAUNCH_CMD` もそれを戻さない。Claude が自分の Bash tool / MCP の
**子プロセスには** `CLAUDECODE=1` を明示的に付けて起動する (t091 の表の通り) が、
**セッション自身の environ にはそもそも誰も `CLAUDECODE` を書き戻さない**。Director が
本番で実測 (2026-09-28 12:40): `claude` プロセスの environ にあるのは `AGENT_NAME` /
`CREWVIA_*` / `HERDR_*` / `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE` — **`CLAUDECODE` は無い**。

結果、idle な Worker (job も無い) の木は `root sh → claude (unknown) → npm exec ... (infra)`
になり、`claude` ノード自身が `unknown` と判定されて `saw_unknown = True` になる。job が
無いのに木全体が `unknown` になり続け、watchdog の hard-idle terminate が永久に効かない
(絶対上限 `max_threshold` だけが安全弁として残る) — t016 が直したはずの欠陥が
「不明」という別の仮面で戻ってきた。6巡目まで気づかれなかったのは、
`tests/test_watchdog_idle.py` の `worker_pane` fixture が `subprocess.Popen(..., env=_infra_env())`
でセッション役の偽プロセスに `CLAUDECODE=1` を**直接**与えていたため (本番は herdr が
消すのでこの前提が食い違う)。

**直し方**: セッション本体を「起動元」(cmdline/environ の job/infra/unknown 判定) とは
別に、**構造的な位置と exe** で同定する。名前の部分一致 (comm が `claude` を含むか等) は
使わない — MCP の `npm exec @playwright/mcp` が `process.title` で comm を書き換える
のと同じ理由で、名前は代理指標にしかならない。使うのは 2 つ:

  1. **祖先関係**: pane の root (`root_pid`) の**直接の子**であること (本番の木は
     `root sh → claude → (MCP / Bash tool wrapper)` の 1 段構造。t074 の docstring 参照)
  2. **exe**: `/proc/<pid>/exe` (symlink の最終的な実体、comm や cmdline と違い
     `process.title` 書き換えの影響を受けない) が `/share/claude/versions/` を含むこと
     (実測: `~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.283` という
     **実体ファイル**。`$HOME` に依存しないよう末尾側だけを見る — `BASH_TOOL_WRAPPER_MARKER`
     と同じ設計)

この 2 条件を満たすノードは `job` でも `infra` でも `unknown` でもない第 4 の分類
**`session`** として判定から外す (`saw_unknown` に寄与しない・`executing` にもならない)。
`session` ノードの子は、`session` という起源からは何も継承せず (job だけが子に伝播する
という既存の規則のまま) 独立に `_origin_of()` で判定される — MCP サーバー・Bash tool
wrapper は今まで通り評価される。

**同定できない (=`unknown`) 子孫の既存の扱いは変えない** — 祖先関係が「直接の子」で
ない、または `/proc/<pid>/exe` が消滅以外の理由で読めない、またはマーカーに一致しない
場合は、これまで通り `_origin_of()` (cmdline → environ) にそのまま流す。

**セッション本体の同定に失敗した場合の向き**: `/proc/<pid>/exe` の読み取りが「消滅」
(ENOENT/ESRCH。BFS 列挙後に死んだだけの無害なレース) なら「session ではない」として
`_origin_of()` に委ねる (実質 infra — 既存の消滅時の扱いと同じ)。それ以外の `OSError`
(EACCES 等、`_proc_cmdline` / `_proc_environ` と同じ契約) は re-raise し、呼び出し側が
木全体を `unknown` に倒す。**これは「kill が増える側」には倒れない** — `unknown` は
watchdog では殺さない側、Rule 5 では通知する側であり (本 docstring 冒頭「3 値に分ける」
参照)、同定失敗は最終的にこの task が直そうとした「セッションが unknown に
化ける」のと同じ経路を通る。ただし今回はこれが**観測できる**: `unknown` は
`watchdog.py` の `check_detail()` が `hard_idle_but_process_unknown` として
`registry/watchdog-observations.jsonl` に記録し (warn に留め terminate しない)、
`dispatcher.sh` の `worker_has_background_work()` は `unknown` を「job ではない」
(= 通知する) 側として扱う。追加のログ経路を新設する必要はない — 既存の 2 つの
呼び出し側の非対称な fail-direction がそのまま「同定失敗を握り潰さない」を満たす。

## 族ごとの掃除: job の証拠が消える経路の一覧 (2026-09-28 実測)

Bash tool のラッパー配下で job の証拠 (cmdline marker / environ) がどう変わるかを
経路ごとに実測した (`bash -c '<経路> sleep N &'` 等、`/proc/<pid>/{cmdline,environ}`
を直接読んで確認。手順は t091 の Result に貼る)。「見える」は root_pid の子孫として
残る (= `classify_process_tree` の走査が到達できる) ことを指す:

| 経路 | cmdline の marker | environ の job シグナル | pane の子孫として見えるか | この lib の判定 | Rule 5 | watchdog |
|---|---|---|---|---|---|---|
| `cmd` (何もしない) | 残る | 残る | 見える | job | 黙る | kill しない |
| `exec cmd` | **消える** | 残る (execve は envp を継承) | 見える | **job (environ で救う)** | 黙る | kill しない |
| `exec env -i cmd` | 消える | **消える** (`CLAUDECODE` も無い) | 見える | **unknown** | 通知する | kill しない (warn) |
| `nohup cmd &` | 消える (`nohup` 自身が exec する) | 残る | 見える | job (environ で救う) | 黙る | kill しない |
| `exec -a <name> cmd` / Node の `process.title` 書き換え | 書き換わる (marker 消える) | 残る (argv の表示だけが変わり environ は別領域) | 見える | job (environ で救う) | 黙る | kill しない |
| `disown` (シェル組み込み、bookkeeping のみ) | 変化なし | 変化なし | 見える (親が生きている限り) | 変化なし | 変化なし | 変化なし |
| `setsid cmd` (setsid ユーティリティ) | 消える (setsid 自身が内部で exec) | 残る | **見えない** (setsid は内部で二重 fork し、grandparent が即終了するため init/subreaper の子になる — 実測: 数百 ms で `ppid=1`) | (到達不能 — この木にそもそも現れない) | 見えないので通常判定 (job 無しと同じ扱い) | 見えないので通常判定 (`max_threshold` だけが安全弁) |
| `( cmd & )` (サブシェルの二重 fork) | 消える (孫の cmdline は `cmd` 自身) | 残る | **見えない** (同上の理由で init/subreaper の子になる) | (到達不能) | 同上 | 同上 |

**「見えない」2 経路 (`setsid` / 二重 fork) は、cmdline・environ のどちらを見ても
直せない構造的な限界**: プロセスが pane の `ppid` チェーンそのものから外れる
(init や subreaper の子になる) ため、root_pid を起点にした木構造の走査が対象に
到達しない。安全弁は既存の watchdog 絶対上限 (`max_threshold`) のみで、この task
(exec によるマーカー消失) の対象外の残存リスクとして明記する。
"""
import errno
import os
from collections import deque
from pathlib import Path
from typing import Literal, Optional

ProcessSignal = Literal[
    "no_window",     # mux 窓が無い
    "not_probed",    # プロセス層を見るまでもなく判定が決まった (絶対上限など)
    "unknown",       # 窓はあるが pane pid が引けない、プロセス木の列挙、または
                     # 判定中のノードの cmdline / environ が「消滅」以外の
                     # 理由で読めない、または job も infra の確証も持たない
                     # ノードがあった (t091: CLAUDECODE すら無い)
                     # → terminate を抑制する
    "no_process",    # 子プロセスが 1 つも無い
    "idle_process",  # Bash tool / Monitor のラッパー経由でないプロセス
                     # (セッション本体・MCP サーバー・その子孫) だけ
    "executing",     # Bash tool (前景・run_in_background いずれも) か Monitor が
                     # 起動した子孫が居る = job 中
]

# Claude Code が Bash tool / Monitor の実行のたびに書き出す shell snapshot を
# `source` する wrapper の cmdline に必ず含まれる部分文字列 (t074 実測:
# `/bin/bash -c source /home/<user>/.claude/shell-snapshots/snapshot-<ts>-<hash>.sh
# ... && eval '<command>' ...`)。$HOME に依存しないよう、パスの末尾側だけを見る。
# Claude Code の内部実装なので、変わったときに気付けるよう 1 箇所の定数に閉じ込める
# (変わると「マーカーが見つからない」→ 全部 job ではない判定に倒れる = 誤検知が
# **増える**方向に壊れる。黙る方向には壊れない。下記 classify_process_tree 参照)。
BASH_TOOL_WRAPPER_MARKER = "/shell-snapshots/snapshot-"

# t091: cmdline のマーカーが `exec` で消えても environ は残る (モジュール
# docstring 実測)。job であることを示す environ のキーを 2 つ持つ (どちらか
# 1 つでもあれば job) — 単一の定数にすると、その名前が将来変わった瞬間に
# 「exec 済みの本物の job が infra に化けて kill されうる」という、この
# task が直す欠陥そのものを再導入してしまう (受入条件(4))。
JOB_ENVIRON_MARKERS = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH")

# Claude Code (本体・MCP サーバーいずれも) が起動した子孫であることを示す
# environ のキー。これが無ければ「Claude Code とは無関係、または env が
# 丸ごと消された (`env -i` 等)」= unknown に倒す (job/infra どちらの確証も
# 無いノードを安易に infra 側に倒さない)。
INFRA_ENVIRON_MARKER = "CLAUDECODE"

# t097: セッション本体 (`claude --model ...`) の /proc/<pid>/exe (symlink 解決後の
# 実体ファイルパス) に必ず含まれる部分文字列。$HOME に依存しないよう末尾側だけを見る
# (BASH_TOOL_WRAPPER_MARKER と同じ設計)。実測: `~/.local/bin/claude` は
# `~/.local/share/claude/versions/<version>` という実体ファイルへの symlink。
SESSION_EXE_MARKER = "/share/claude/versions/"


def _proc_stat(pid: int) -> Optional[tuple[int, int, str]]:
    """/proc/<pid>/stat から (ppid, starttime_ticks, comm) を返す。

    消滅 (`FileNotFoundError` / `ProcessLookupError` = ENOENT/ESRCH) だけを
    None として扱う — この pid はもう居ない、という確定した事実だからである。
    それ以外の `OSError` (EACCES 等。同じ uid の自分の子孫を読む限り実運用では
    起きないはずだが、hidepid マウント等の環境要因は排除できない) は
    そのまま re-raise する。読めない ≠ 居ない — これを None に潰すと、
    その pid が親として持つ子孫 (それ自体は読める) が `children` に一度も
    辿り着けなくなり (どの親からも「値」として指されない孤立ノードになる)、
    生きている裏 job のサブツリーがまるごと見えなくなる (t049 族A監査)。
    呼び出し側は「わからない」を "unknown" として扱うこと (fail-direction は
    呼び出し側の判定ごとに決まる。lib 自身は判定しない)。

    comm (field 2) は括弧で囲まれ、空白や ')' を含みうるので最初の '(' と
    最後の ')' で括り出す (例: "1234 (sh -c (x)) S 1 ..." )。
    切った残りの先頭が field 3 なので、field N は rest[N - 3] になる:
      ppid = field 4 = rest[1] / starttime = field 22 = rest[19]

    starttime は t074 時点で分類には使わない (t065 で時刻ベースの判定を撤去
    済み) が、デバッグ・将来の監査のために引き続き読む (タプルの形を変えると
    `_direct_children()` 等のテスト helper が壊れる)。comm も同様 (分類には
    使わないが、ログ・デバッグ表示に使える)。

    t101 (Codex review 7巡目 P1, t097 で入った回帰): プロセス名 (comm) は
    カーネルが任意のバイト列をそのまま許す (NUL 以外の制約が無い) ため、
    `exec -a $'\xff'` 等で 0xff のような不正な UTF-8 バイトを含む名前を作れる。
    `read_text()` は既定で strict decode するため、無関係な 1 プロセスの名前が
    不正なだけで `UnicodeDecodeError` を投げ、`classify_process_tree` の全走査
    (呼び出し元は `OSError` しか拾わない) を丸ごと落としていた —
    watchdog の評価サイクル全体が止まり、全 Worker の timeout 処理が効かなく
    なる (`_proc_cmdline` / `_proc_environ` と同じ族の欠陥。t077/t083 が
    B8 側で直した型と同じ)。`_proc_cmdline` / `_proc_environ` は既にバイト列で
    読んで `errors="replace"` で許容している (この関数だけが取り残されていた) —
    同じパターンに揃える。comm は分類に使わないので、不正なバイトが U+FFFD に
    化けても判定結果に影響しない (ppid/starttime は数字だけの ASCII なので
    影響を受けない)。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    open_paren = raw.find("(")
    close_paren = raw.rfind(")")
    if open_paren < 0 or close_paren < 0 or close_paren <= open_paren:
        return None
    comm = raw[open_paren + 1:close_paren]
    rest = raw[close_paren + 1:].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[1]), int(rest[19]), comm
    except ValueError:
        return None


def _proc_state(pid: int) -> Optional[str]:
    """/proc/<pid>/stat の state 欄 (`R` `S` `Z` ...) を返す (t003 / §11)。

    `_proc_stat` のタプルは `_direct_children()` 等のテスト helper が 3 要素で
    展開しているので拡張せず、state だけ別関数で読む。消滅・読めない・壊れた行は
    None (= 「zombie と確定できない」。呼び出し側は通常どおり分類に進む —
    zombie でないものを zombie と読んで木から外す向きの誤りを作らない)。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    close_paren = raw.rfind(")")
    if close_paren < 0:
        return None
    rest = raw[close_paren + 1:].split()
    return rest[0] if rest else None


def _proc_cmdline(pid: int) -> Optional[str]:
    """/proc/<pid>/cmdline を 1 つの文字列にして返す (NUL 区切りの引数を空白で連結)。

    comm (`/proc/<pid>/stat` の実行体名) は 15 文字で打ち切られ、かつ npm 等が
    `process.title` を書き換えると execve 時の実行体名とも一致しなくなる
    (モジュール docstring の実測)。cmdline は打ち切られず、Bash tool /
    Monitor が生成する `source .../shell-snapshots/snapshot-....sh` という
    文字列をそのまま含むので、判定はこちらを読む。

    消滅 (`FileNotFoundError` / `ProcessLookupError` = ENOENT/ESRCH) だけを
    None として扱う — `_proc_stat` と同じ契約 (この pid はもう居ない、という
    確定した事実)。それ以外の `OSError` (EACCES 等) は re-raise し、呼び出し側
    (`classify_process_tree`) で `unknown` に倒す。

    t082 (Codex review 4巡目 P1): 当初はここで全 `OSError` を握り潰して None
    (=「job ではない」) にしていた。読めないノードが Bash tool のラッパー
    自身だと、走っている job のマーカーが誰にも見つからず**本物の job が
    `idle_process` に化ける**——watchdog の `check_detail()` はそれを見て
    hard-idle による terminate を許してしまう (「観測に失敗したら kill しない」
    という要件に反する。t074 のカードで Director が名指ししていた向き:
    「kill してよい根拠を『分からない』から作らないこと」)。cmdline の読み取り
    失敗はツリー構造 (親子関係。`_proc_stat` の ppid だけで決まる) を壊さない
    が、**判定の根拠を握り潰してよいわけではない** — 「消滅」と「消滅以外」を
    _proc_stat と同じ基準で分ける。
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _proc_environ(pid: int) -> Optional[dict[str, str]]:
    """/proc/<pid>/environ を `{key: value}` にして返す (t091)。

    NUL 区切りの `KEY=VALUE` エントリを分割する。`=` を含まない壊れたエントリは
    無視する (キーだけ拾えても判定には使わない)。空 (`env -i` で起動した
    プロセス) は `{}` — これは「消滅」でも「読めない」でもなく、正当な結果
    (`INFRA_ENVIRON_MARKER` すら無い = unknown 側に倒す)。

    消滅 (`FileNotFoundError` / `ProcessLookupError` = ENOENT/ESRCH) だけを
    None として扱う契約は `_proc_stat` / `_proc_cmdline` と同じ。それ以外の
    `OSError` (EACCES 等) は re-raise し、呼び出し側で `unknown` に倒す —
    /proc/<pid>/environ は同一 uid の非祖先プロセスでも読める (本番実測、
    モジュール docstring 参照) ので、実運用で EACCES が起きるとすれば
    hidepid マウント等の環境要因であり、「読めない」を「無い」に潰して
    良い理由にはならない (_proc_cmdline と同じ判断)。
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    result: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        key, sep, value = entry.partition(b"=")
        if not sep:
            continue
        result[key.decode("utf-8", errors="replace")] = value.decode(
            "utf-8", errors="replace")
    return result


def _proc_exe(pid: int) -> Optional[str]:
    """/proc/<pid>/exe のリンク先 (symlink 解決後の実体ファイルパス) を返す (t097)。

    comm (15 文字打ち切り・`process.title` で書き換え可能) や cmdline (`exec` で
    消える・`process.title` で書き換わる) と違い、exe はカーネルが execve 時点の
    実体 inode から辿った絶対パスなので、プロセス自身がどう argv/環境を偽装しても
    変わらない (`_origin_of` と同じファイル内の他の判定基準より構造的に硬い)。

    消滅 (`FileNotFoundError` / `ProcessLookupError` = ENOENT/ESRCH) だけを None
    として扱う契約は `_proc_stat` / `_proc_cmdline` / `_proc_environ` と同じ。
    それ以外の `OSError` (EACCES 等) は re-raise し、呼び出し側で `unknown` に倒す。
    """
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (FileNotFoundError, ProcessLookupError):
        return None


def _is_session_body(pid: int) -> bool:
    """このノードが Claude Code のセッション本体 (`claude --model ...`) かどうか (t097)。

    呼び出し側 (`classify_process_tree`) が「pane root の直接の子」であることを
    保証した上で呼ぶこと — この関数自体は祖先関係を見ない (exe だけでは pane の
    どの深さに居るプロセスかは分からない。祖先関係との AND がモジュール docstring
    「t097」節の同定条件)。

    exe が読めない (消滅) 場合は False — 「session ではない」として `_origin_of()`
    に委ねる (既存の消滅時の扱いと同じ、実質 infra)。それ以外の `OSError` は
    re-raise する (`_proc_exe` と同じ契約)。
    """
    exe = _proc_exe(pid)
    if exe is None:
        return False
    return SESSION_EXE_MARKER in exe


def _origin_of(pid: int) -> Literal["job", "infra", "unknown"]:
    """1 ノードの起源を判定する (t091)。cmdline → environ の順に見る。

    cmdline に wrapper marker があれば environ を読むまでもなく job (既存の
    t074 判定をそのまま維持)。無ければ environ を見る — job / infra どちらの
    根拠も無ければ unknown (安易に infra に倒さない。モジュール docstring
    「3 値に分ける」参照)。

    cmdline / environ の読み取りが「消滅」以外の理由で失敗した場合は
    `OSError` を re-raise する (呼び出し側 `classify_process_tree` が木全体を
    `unknown` に倒す — `_proc_cmdline` と同じ契約)。

    どちらかが「消滅」(None) なら、この pid は BFS 対象の列挙から生きて
    見えていたのに読む時点で居なくなった、というだけの無害なレースなので
    infra (=判定に寄与しない) として扱う (t082 以前からの既存の扱いを維持)。
    """
    cmdline = _proc_cmdline(pid)
    if cmdline is None:
        return "infra"
    if BASH_TOOL_WRAPPER_MARKER in cmdline:
        return "job"
    environ = _proc_environ(pid)
    if environ is None:
        return "infra"
    if any(marker in environ for marker in JOB_ENVIRON_MARKERS):
        return "job"
    if INFRA_ENVIRON_MARKER in environ:
        return "infra"
    return "unknown"


def classify_process_tree(root_pid: int) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する (本体は `_classify`)。

    §11 (t003) の変更 2 点 (詳細は `_classify` の docstring 末尾):
      - state が `Z` (zombie) のノードは木から外す (仕事をしておらず、
        environ が EACCES で `unknown` の主因だった)。
      - cmdline / environ / exe が読めない (EACCES 等) ノードで木全体を即
        `unknown` にせず、BFS を続けて job を探す (job が見つかれば `executing`)。
    """
    return _classify(root_pid, None)


def explain_unknown_tree(root_pid: int) -> list[str]:
    """`classify_process_tree` が `unknown` を返した理由のノード別の一覧 (診断用)。

    通知文に載せて「何が unknown にしたか」(pid / state / errno) を確定させるための
    読み取り専用の再走査。判定には使わない。空 = 説明できる材料が無い (木が変わった等)。
    """
    notes: list[str] = []
    _classify(root_pid, notes)
    return notes


def _errno_name(exc: OSError) -> str:
    code = getattr(exc, "errno", None)
    if code is None:
        return type(exc).__name__
    return f"{errno.errorcode.get(code, code)}({code})"


def _classify(root_pid: int, notes: Optional[list[str]]) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する。

    本番のペインは常にこの形をしている (2026-09-21 実測, Ren-worker。
    t074 で「MCP はどれも Bash tool のラッパーを経由しない」ことを再実測。
    t097: `claude --model ...` 自身は `_origin_of()` (cmdline/environ) の対象外 —
    pane root の直接の子かつ exe が `SESSION_EXE_MARKER` に一致するノードは
    `session` として判定から外す。モジュール docstring「t097」節参照):

        /bin/bash                      ← root_pid (pane_pid)
          claude --model ...           ← セッション本体 (`session`。job/infra/unknown
                                          いずれでもない第 4 分類。判定に寄与しない)
            npm exec @playwright/mcp   ← MCP サーバー。claude の 1-2 秒後に起動
            npm exec chrome-devtools   ← 同上
            /bin/bash -c source \
              .../shell-snapshots/snapshot-....sh && eval '...'
                                        ← Bash tool (前景 / run_in_background) か
                                          Monitor の実行中だけ現れる

    旧実装 (t016) の `pgrep -P <pane_pid>` は常に claude 1 件を返すため「子
    プロセスが居る = 作業中」が恒真になり、idle 判定に一度も到達しなかった。
    続く実装 (t016 → t049 → t065) は「いつ生えたか」(時刻) → 「何であるか」
    (comm) の順に判定基準を移したが、どちらも代理指標であり穴が向きを変えて
    残った (モジュール docstring 参照)。

    ここでは **誰が起動したか** (祖先の cmdline に Bash tool / Monitor の
    shell snapshot wrapper が現れるか、**または** 祖先の environ に t091 の
    job シグナルがあるか) で区別する:

      "executing"    … 自分自身か祖先が `_origin_of()` で job と判定された
                       (cmdline の wrapper marker、または environ の
                       `JOB_ENVIRON_MARKERS`) = job が走っている
      "idle_process" … job が 1 つも無く、かつ全ノードが確証を持って infra
                       (`INFRA_ENVIRON_MARKER` あり) と判定できた。木が有る
                       こと自体は「働いている」の証拠にならない
      "no_process"   … 子が 1 つも無い (claude が落ちた / 素のシェル)
      "unknown"      … `/proc` の列挙自体 (`_proc_stat`) か、判定中の 1 ノードの
                       cmdline / environ / exe (`_proc_cmdline` / `_proc_environ` /
                       `_proc_exe`) のどちらかが「消滅」以外の理由で読めず木そのものが
                       組み立てられない (即座に unknown。t049 族A監査 / t082 P1 / t097)、
                       **または** job も infra の確証も無いノードが 1 つでも
                       あった (t091: `CLAUDECODE` すら無い = Claude Code と
                       無関係か env が消された。この場合は BFS を中断せず
                       他のノードも見る — job が他所で見つかればそちらが勝つ)。
                       terminate 側の抑制材料としては効くが、Rule 5 は
                       「観測できない → 通知する」に倒す

    親の分類が job なら子は cmdline/environ を見るまでもなく job (そのシェルが
    起動した実体の一部だから)。job はどこで見つかっても即座に確定するので
    "executing" は即 return する。infra / unknown / session の親は子に伝播しない —
    各ノードは (pane root の直接の子なら `_is_session_body()` を先に、それ以外は)
    `_origin_of()` で独立に判定される (t065 の「同定できないものは job 側に倒す」
    族C の許可リスト方式はここでは撤去したまま — 許可リストという概念自体が要らない。
    モジュール docstring 参照)。`session` 自体は `saw_unknown` に寄与せず
    `executing` も返さない — 木に job が無く session だけが判定から外れた残り全員が
    infra なら、この関数は `idle_process` を返す (t097 が直す欠陥そのもの:
    従来は session ノードが `unknown` になり `idle_process` に一度も到達しなかった)。
    """
    try:
        root_stat = _proc_stat(root_pid)
    except OSError as exc:
        # root 自身が「消滅」以外の理由 (EACCES 等) で読めない — わからない
        # ことを no_process (= 死んだ扱い) に潰さない (t049 族A監査)。
        if notes is not None:
            notes.append(f"pane root pid={root_pid} stat unreadable errno={_errno_name(exc)}")
        return "unknown"
    if root_stat is None:
        return "no_process"

    procs: dict[int, tuple[int, int, str]] = {}
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError as exc:
        if notes is not None:
            notes.append(f"/proc enumeration failed errno={_errno_name(exc)}")
        return "unknown"
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            st = _proc_stat(int(entry.name))
        except OSError as exc:
            # この pid が「消滅」以外の理由で読めない。None に潰して静かに
            # スキップすると、この pid を親に持つ (読めている) 子孫が
            # children から永久に辿り着けなくなり、生きている裏 job のサブ
            # ツリーごと見えなくなる (t049 族A監査: 観測失敗を「子孫なし」に
            # 倒していた)。わからないことは "unknown" として呼び出し側に返す。
            if notes is not None:
                notes.append(f"pid={entry.name} stat unreadable errno={_errno_name(exc)}")
            return "unknown"
        if st is not None:
            procs[int(entry.name)] = st

    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)

    direct = children.get(root_pid, [])
    if not direct:
        return "no_process"

    # root 配下を幅優先で走査 (root 自身は含めない)。親の分類を子に伝播するので
    # 深さ順 (親を先に処理) で回す必要がある — キューを使う (スタックだと深さ
    # 優先になり、子が親より先に処理される場合がある)。
    queue: deque[tuple[int, Optional[str]]] = deque(
        (pid, None) for pid in direct)
    seen: set[int] = set()
    saw_unknown = False
    while queue:
        pid, parent_origin = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        if _proc_state(pid) == "Z":
            # §11 (t003): zombie は仕事をしていない (回収待ちの死骸)。environ が EACCES で
            # 木全体を unknown にしていた主因。子は親の回収時に再親化されて木から
            # 消えるので、zombie の下に「見えなくなる job」は無い。判定に寄与させない
            # (saw_unknown も立てない)。state が読めない / 消滅は None = 通常の分類へ。
            continue
        try:
            if parent_origin == "job":
                origin = "job"  # job の子孫は cmdline/environ を見るまでもなく job
            elif parent_origin is None and _is_session_body(pid):
                # pane root の直接の子で、exe がセッション本体のパターンに一致 (t097)。
                # job でも infra でも unknown でもない第 4 の分類 — 判定に寄与しない
                # (executing にもならず saw_unknown も立てない)。子は独立に判定する
                # (下の `for child in ...` で origin="session" を渡すが、"job" 以外は
                # 特別扱いしないので通常どおり _origin_of() に落ちる)。
                origin = "session"
            else:
                origin = _origin_of(pid)
        except OSError as exc:
            # t082 P1 / t091 / t097: cmdline / environ / exe のいずれかが消滅以外の
            # 理由で読めない (EACCES 等)。「マーカーが無い」(= job でもセッションでも
            # ない) に潰すと、読めないノードが Bash tool のラッパー自身やセッション
            # 本体だったときに本物の job が idle_process に化け、watchdog が
            # hard-idle で terminate してしまう。よって `idle_process` にはしない。
            #
            # §11 (t003): ただし木全体を即 `unknown` にもしない (旧実装は即 return で、
            # 別ノードに本物の job があっても pid の並び次第で `executing` にならなかった
            # = docstring の「job が他所で見つかればそちらが勝つ」に反していた)。
            # `unknown` 扱いにして BFS を続ける: job が見つかれば `executing`、
            # 無ければ最後に `unknown`。読めないノードの子は origin 不明のまま渡す
            # ("job" でなければ特別扱いしないので、子は独立に判定される)。
            if notes is not None:
                notes.append(
                    f"pid={pid} state={_proc_state(pid) or '?'} errno={_errno_name(exc)}")
            saw_unknown = True
            for child in children.get(pid, []):
                queue.append((child, "unknown"))
            continue
        if origin == "job":
            return "executing"  # job はどこで見つかっても即座に確定する
        if origin == "unknown":
            # t091: CLAUDECODE すら無い = job/infra どちらの確証も無い。
            # ここで即 return しない — 他のノードに本物の job があれば
            # そちらを優先する (job が最優先の signal であることは変わらない)。
            # job が他に無ければ、最後に unknown として返す。
            saw_unknown = True
            if notes is not None:
                notes.append(
                    f"pid={pid} state={_proc_state(pid) or '?'} origin=unknown "
                    f"(CLAUDECODE も job も無い)")
        for child in children.get(pid, []):
            queue.append((child, origin))

    return "unknown" if saw_unknown else "idle_process"
