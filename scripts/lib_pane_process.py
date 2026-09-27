"""lib_pane_process.py — mux ペインのプロセス木を 3 値に分類する (t016 → B1 で lib 化)。

watchdog.py にあった `classify_process_tree()` を、dispatcher.sh の Rule 5 からも
同じ定義で呼べるように切り出したもの。**「ペインの裏で何かが走っているか」の定義は
ここ 1 つ**。コピーしない (watchdog と dispatcher で答えが割れる)。

この lib は判定しない — 「terminate してよいか」「通知してよいか」は呼び出し側が、
自分の判定の fail の向きで決める (knowledge/watchdog-idle-judgment.md)。
読むのは /proc だけで、queue / registry / config は開かない。
"""
import os
from pathlib import Path
from typing import Literal, Optional

# t016: プロセス層の分類しきい値。ペインのセッション (claude) 起動からこれ以上
# 遅れて始まった子孫が居れば「Bash tool が実行中」とみなす。
#
# 下限の根拠: MCP サーバーは claude 起動の 1-2 秒後に立ち上がる (本番実測
# 2026-09-21: claude et=119s に対し MCP 2 本が et=117s)。これを「実行中」と
# 誤読すると idle 判定が永久に抑止され、今回直した欠陥がそのまま再発する。
# 60 秒は実測値の 30 倍で、MCP の起動が多少遅れても誤読しない余裕がある。
# 上限側は緩くて構わない — 誤読の向きが「殺さない」だからである。
PROCESS_WORK_START_GRACE = 60


ProcessSignal = Literal[
    "no_window",     # mux 窓が無い
    "not_probed",    # プロセス層を見るまでもなく判定が決まった (絶対上限など)
    "unknown",       # 窓はあるが pane pid が引けない → terminate を抑制する
    "no_process",    # 子プロセスが 1 つも無い
    "idle_process",  # claude と MCP サーバーだけ = 木が有るだけ
    "executing",     # セッション起動より十分後に始まった子孫が居る = tool 実行中
]


def _proc_stat(pid: int) -> Optional[tuple[int, int]]:
    """/proc/<pid>/stat から (ppid, starttime_ticks) を返す。読めなければ None。

    comm (field 2) は括弧で囲まれ、空白や ')' を含みうるので最後の ')' で
    切ってから split する (例: "1234 (sh -c (x)) S 1 ..." )。
    切った残りの先頭が field 3 なので、field N は rest[N - 3] になる:
      ppid = field 4 = rest[1] / starttime = field 22 = rest[19]
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1:].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[1]), int(rest[19])
    except ValueError:
        return None


def classify_process_tree(
    root_pid: int, grace_seconds: Optional[int] = None
) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する。

    本番のペインは常にこの形をしている (2026-09-21 実測, Ren-worker):

        /bin/bash                      ← root_pid (pane_pid)
          claude --model ...           ← セッション。Worker が生きている限り常駐
            npm exec @playwright/mcp   ← MCP サーバー。claude の 1-2 秒後に起動
            npm exec chrome-devtools   ← 同上
            /bin/bash -c source ...    ← Bash tool の実行中だけ現れる

    旧実装の `pgrep -P <pane_pid>` は常に claude 1 件を返すため「子プロセスが
    居る = 作業中」が恒真になり、idle 判定に一度も到達しなかった。ここでは
    **いつ生えたか** で区別する:

      "executing"    … セッション起動から grace_seconds より後に始まった子孫が
                       居る = Bash tool が今まさに走っている。長時間のビルド /
                       学習 / CI 待ちで activity が stale になる正当なケース
      "idle_process" … claude と MCP サーバーだけ。木が有ること自体は
                       「働いている」の証拠にならない
      "no_process"   … 子が 1 つも無い (claude が落ちた / 素のシェル)

    基準時刻は **最も古い直下の子 (= claude) の起動時刻** にする。root 自身では
    なく子を基準にするのは、ペインの bash が Worker より先に (crewvia 起動時に)
    生まれていることがあり、それを基準にすると claude の起動自体が "executing"
    に見えてしまうからである。深さは問わないので、ペイン直下に後から生えた
    プロセスも拾える。

    起動時刻は /proc の starttime (boot からの tick) 同士で比較する。壁時計に
    依存しないので、NTP 補正やサスペンドの影響を受けない。
    """
    if _proc_stat(root_pid) is None:
        return "no_process"

    procs: dict[int, tuple[int, int]] = {}
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return "unknown"
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        st = _proc_stat(int(entry.name))
        if st is not None:
            procs[int(entry.name)] = st

    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)

    direct = children.get(root_pid, [])
    if not direct:
        return "no_process"

    ticks_per_sec = os.sysconf("SC_CLK_TCK") or 100
    # None = 既定。呼び出し時に読む (定義時に束縛すると、モジュール定数を差し替える
    # e2e / テストが効かない)。
    if grace_seconds is None:
        grace_seconds = PROCESS_WORK_START_GRACE
    threshold_ticks = grace_seconds * ticks_per_sec
    session_start = min(procs[pid][1] for pid in direct)

    # root 配下を幅優先で走査 (root 自身は含めない)
    stack = list(direct)
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if procs[pid][1] - session_start > threshold_ticks:
            return "executing"
        stack.extend(children.get(pid, []))

    return "idle_process"
