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

**分からないとき (祖先の cmdline が読めない等) は「job ではない」側に倒す**
(通知する側 / 殺さない側は呼び出し側の判定だが、ここでの「わからない」は
「その 1 ノードが job のマーカーを持つと確認できなかった」というだけなので、
そのノードだけを job から除外して走査は続ける — 呼び出し側に `unknown` を
返すのは `/proc` の列挙自体が失敗した場合だけ [族A、下記])。

族C (t065 で作った「同定できないものは job 側に倒す」という許可リスト方式の
考え方) は**この設計では成立しない**: 許可リストが要らなくなった (「既知の
インフラ名を全部知っている」という前提そのものを捨てた) ので、「未知の名前を
job/infra のどちらに倒すか」という問い自体が無くなった。さらに、旧実装の
「同定できない場合は job 側に倒す」という**コメント**は「1 回余計に通知する
方が安い」と書かれていたが、実際には classify=`executing` は **通知/terminate
を抑制する側**(dispatcher の `worker_has_background_work` / watchdog の
`_process_signal` 参照) なので、**コメントと挙動が逆だった** (Codex 3巡目 P1)。
新しい設計はデフォルトを「job ではない」にすることで、この向きの取り違えごと
無くす — 「わからない/知らない」はすべて「job ではない」(= 通知・terminate を
許す側) に統一される。

時刻 (`grace_seconds` / `min_start_epoch`) はもう判定に使わない (t065 で撤去済み)。
comm による同定 (`_SHELL_COMMS` / `_KNOWN_INFRASTRUCTURE_COMMS`) も撤去した —
残すと「起動元」と「名前」の 2 つの判定基準が食い違いうる。
"""
from collections import deque
from pathlib import Path
from typing import Literal, Optional

ProcessSignal = Literal[
    "no_window",     # mux 窓が無い
    "not_probed",    # プロセス層を見るまでもなく判定が決まった (絶対上限など)
    "unknown",       # 窓はあるが pane pid が引けない、またはプロセス木の列挙自体が
                     # 「消滅」以外の理由で読めない → terminate を抑制する
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
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
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


def _proc_cmdline(pid: int) -> Optional[str]:
    """/proc/<pid>/cmdline を 1 つの文字列にして返す (NUL 区切りの引数を空白で連結)。

    comm (`/proc/<pid>/stat` の実行体名) は 15 文字で打ち切られ、かつ npm 等が
    `process.title` を書き換えると execve 時の実行体名とも一致しなくなる
    (モジュール docstring の実測)。cmdline は打ち切られず、Bash tool /
    Monitor が生成する `source .../shell-snapshots/snapshot-....sh` という
    文字列をそのまま含むので、判定はこちらを読む。

    読めない場合 (消滅・権限問題いずれも) は None を返し、呼び出し側 (t074:
    分からないときは「job ではない」側に倒す。モジュール docstring 参照) に
    「このノードにはマーカーが見つからなかった」ものとして扱わせる。これは
    `_proc_stat` (消滅以外は re-raise してツリー構築全体を "unknown" にする)
    とは意図的に違う流儀: cmdline の読み取り失敗はツリー構造 (親子関係) を
    壊さない (親子関係は `_proc_stat` の ppid だけで決まる) ので、1 ノードの
    判定だけを「job ではない」に倒して走査を続けられる。
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def classify_process_tree(root_pid: int) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する。

    本番のペインは常にこの形をしている (2026-09-21 実測, Ren-worker。
    t074 で「MCP はどれも Bash tool のラッパーを経由しない」ことを再実測):

        /bin/bash                      ← root_pid (pane_pid)
          claude --model ...           ← セッション。Worker が生きている限り常駐
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
    shell snapshot wrapper が現れるか) で区別する:

      "executing"    … 自分自身か祖先の cmdline に `BASH_TOOL_WRAPPER_MARKER`
                       を含むノードが居る = job が走っている
      "idle_process" … それ以外 (セッション本体・MCP サーバー・その子孫)
                       だけ。木が有ること自体は「働いている」の証拠にならない
      "no_process"   … 子が 1 つも無い (claude が落ちた / 素のシェル)
      "unknown"      … `/proc` の列挙自体 (`_proc_stat`) が「消滅」以外の
                       理由で読めず、木そのものが組み立てられない
                       (t049 族A監査。terminate 側の抑制材料としては効くが、
                       Rule 5 は「観測できない → 通知する」に倒す)

    親の分類 (job / not-job) は子に伝播する: job と分類されたノードの子孫は
    すべて job (そのシェルが起動した実体の一部だから)。ルート直下 (親の分類が
    無い) でマーカーを持たない場合は not-job (t065 の「同定できないものは
    job 側に倒す」族C の許可リスト方式はここでは撤去 — 許可リストという
    概念自体が要らなくなった。モジュール docstring 参照)。cmdline が読めない
    ノードもマーカー無しと同じ扱い (not-job) にする — `_proc_cmdline` 参照。
    """
    try:
        root_stat = _proc_stat(root_pid)
    except OSError:
        # root 自身が「消滅」以外の理由 (EACCES 等) で読めない — わからない
        # ことを no_process (= 死んだ扱い) に潰さない (t049 族A監査)。
        return "unknown"
    if root_stat is None:
        return "no_process"

    procs: dict[int, tuple[int, int, str]] = {}
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return "unknown"
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            st = _proc_stat(int(entry.name))
        except OSError:
            # この pid が「消滅」以外の理由で読めない。None に潰して静かに
            # スキップすると、この pid を親に持つ (読めている) 子孫が
            # children から永久に辿り着けなくなり、生きている裏 job のサブ
            # ツリーごと見えなくなる (t049 族A監査: 観測失敗を「子孫なし」に
            # 倒していた)。わからないことは "unknown" として呼び出し側に返す。
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
    while queue:
        pid, parent_origin = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        if parent_origin == "job":
            origin = "job"  # job の子孫はシェルの cmdline を見るまでもなく job
        else:
            cmdline = _proc_cmdline(pid)
            has_marker = cmdline is not None and BASH_TOOL_WRAPPER_MARKER in cmdline
            origin = "job" if has_marker else "infra"
        if origin == "job":
            return "executing"
        for child in children.get(pid, []):
            queue.append((child, origin))

    return "idle_process"
