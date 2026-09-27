"""lib_pane_process.py — mux ペインのプロセス木を 3 値に分類する (t016 → B1 で lib 化)。

watchdog.py にあった `classify_process_tree()` を、dispatcher.sh の Rule 5 からも
同じ定義で呼べるように切り出したもの。**「ペインの裏で何かが走っているか」の定義は
ここ 1 つ**。コピーしない (watchdog と dispatcher で答えが割れる)。

この lib は判定しない — 「terminate してよいか」「通知してよいか」は呼び出し側が、
自分の判定の fail の向きで決める (knowledge/watchdog-idle-judgment.md)。
読むのは /proc だけで、queue / registry / config は開かない。
"""
import os
import time
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
    """/proc/<pid>/stat から (ppid, starttime_ticks) を返す。

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

    comm (field 2) は括弧で囲まれ、空白や ')' を含みうるので最後の ')' で
    切ってから split する (例: "1234 (sh -c (x)) S 1 ..." )。
    切った残りの先頭が field 3 なので、field N は rest[N - 3] になる:
      ppid = field 4 = rest[1] / starttime = field 22 = rest[19]
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
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


def _boot_epoch() -> Optional[float]:
    """壁時計 (epoch 秒) と /proc の starttime (tick) を結びつける起動時刻を返す。

    `/proc/uptime` の起動からの経過秒を `time.time()` から引くだけ。読めなければ
    None (呼び出し側は `min_start_epoch` を無視して従来どおりの判定にフォールバックする)。
    """
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - uptime_seconds


def classify_process_tree(
    root_pid: int,
    grace_seconds: Optional[int] = None,
    min_start_epoch: Optional[float] = None,
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

    ## t049 (Codex review, PR#238 P2): grace_seconds だけでは区別できない窓

    `session_start` から `grace_seconds` 以内に始まった子孫は、それが MCP サーバー
    由来 (無視してよい) か、着手直後に投げた本物の裏 job (無視してはいけない) か
    を、開始時刻の絶対値だけでは区別できない — 両方とも claude 起動の数秒後に
    生まれうる。しきい値を伸ばしても縮めても同じ形の穴が残る (`grace_seconds` の
    枠内で始まった job は、生きている間ずっと `idle_process` のまま)。

    `min_start_epoch` はこれを**別の根拠**で区別するためのオプション引数。
    呼び出し側が「今の task の作業がいつから有効か」を示す壁時計時刻 (例:
    assignment file の mtime) を渡すと、それ以降に始まった子孫は
    `session_start` からの経過が `grace_seconds` の枠内でも無条件で "executing"
    になる。assignment file は必ず「今の task の作業が始まるより前」に書かれるので
    (`plan.sh pull` が書いてから Worker が動き出す)、以降に生える子孫は今の task
    由来と確定できる — MCP サーバーは task の割り当てより前 (セッション起動直後)
    に立ち上がっているので誤って拾わない。

    比較には壁時計 (`min_start_epoch`) を 1 箇所だけ混ぜるため、`_boot_epoch()`
    の読み取り誤差 (数十 ms) の分だけ従来の tick 同士の比較より粗くなるが、
    比較対象が秒単位の `grace_seconds` なので実用上無視できる。`min_start_epoch`
    を渡さない (または `_boot_epoch()` が読めない) 呼び出しは、従来どおり
    tick 同士の比較だけで判定する。
    """
    try:
        root_stat = _proc_stat(root_pid)
    except OSError:
        # root 自身が「消滅」以外の理由 (EACCES 等) で読めない — わからない
        # ことを no_process (= 死んだ扱い) に潰さない (t049 族A監査)。
        return "unknown"
    if root_stat is None:
        return "no_process"

    procs: dict[int, tuple[int, int]] = {}
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

    # t049 (P2): min_start_epoch を tick に変換しておく。boot_epoch が読めなければ
    # 素通し (None のまま) — 従来どおり grace_seconds だけで判定する。
    min_start_ticks: Optional[int] = None
    if min_start_epoch is not None:
        boot_epoch = _boot_epoch()
        if boot_epoch is not None:
            candidate = int((min_start_epoch - boot_epoch) * ticks_per_sec)
            # t049 族C監査: 負 (= assignment が「起動より前」に見える。壁時計の
            # ずれ・古い assignment が起動を跨いで残った場合に起こりうる) を
            # そのまま使うと、あらゆる tick 値が無条件に閾値以上になり、下の
            # ループが MCP サーバーまで含めて全部 "executing" と誤読する —
            # このガード自身が壊れた状態を「常に検知した」に倒してしまう。
            # 負のときは判定材料として使わない (min_start_epoch を渡さなかった
            # ときと同じ、grace_seconds だけの判定にフォールバックする)。
            if candidate >= 0:
                min_start_ticks = candidate

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
        if min_start_ticks is not None and procs[pid][1] >= min_start_ticks:
            return "executing"
        stack.extend(children.get(pid, []))

    return "idle_process"
