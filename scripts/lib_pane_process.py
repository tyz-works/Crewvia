"""lib_pane_process.py — mux ペインのプロセス木を 3 値に分類する (t016 → B1 で lib 化)。

watchdog.py にあった `classify_process_tree()` を、dispatcher.sh の Rule 5 からも
同じ定義で呼べるように切り出したもの。**「ペインの裏で何かが走っているか」の定義は
ここ 1 つ**。コピーしない (watchdog と dispatcher で答えが割れる)。

この lib は判定しない — 「terminate してよいか」「通知してよいか」は呼び出し側が、
自分の判定の fail の向きで決める (knowledge/watchdog-idle-judgment.md)。
読むのは /proc だけで、queue / registry / config は開かない。

## t065 (Codex review 2巡目, PR#238 P2): 判定根拠を「いつ生えたか」から「何であるか」に移す

t016 の元の分類は `grace_seconds` (セッション起動からの経過時間) で MCP サーバーの
起動ノイズと本物の job を区別していた。t049 はこれを「assignment file の mtime
より後か」に差し替えたが、**どちらも時刻を代理指標にしている**点は変わらず、穴が
向きを変えて残った: `plan.sh pull` の後で初めて起動する MCP サーバー (遅延起動の
Playwright ブラウザ等) は、時刻基準では「assignment より後」に見え、**すべての
ツール呼び出しと裏 job が終わった後も Rule 5 を永久に抑制し続ける** (偽陰性 —
本当に止まっている Worker に誰も気付けない。t049 の偽陽性より高くつく、
memory `fail-direction-is-per-judgment`)。

判定根拠を「そのプロセスが何であるか」(comm = 実行ファイル名) に置き換えた:

  - シェル (`bash` / `sh` / `dash` / `zsh` / `ksh` / `ash`) が見つかった時点で
    それは job である。Bash tool の実行 (`run_in_background` の裏 job も
    Monitor も) は必ずこの形で生える (t016 実測)。シェルの子孫もすべて job
    (シェルが起動した実体の一部だから)。
  - シェルでなく、既知の「永続インフラ」名 (`claude` = セッション本体、
    `node`/`npm`/`npx` = MCP サーバーの起動経路、`chrome`/`chromium`/
    `chrome-headless-shell`/`google-chrome` = Playwright / Chrome DevTools MCP
    が起動するブラウザ) に一致する、かつ祖先に job が居ない場合だけ infra。
  - **それ以外 (同定できない) は job 側に倒す。** 未知の永続プロセスを infra
    と誤認して黙り続けるより、正体不明のものを job 扱いして 1 回余計に通知する
    方が安い (族C: memory `a-new-guard-creates-a-new-state` — 除外リストを
    作ること自体が「リストに載っていない未知のものをどう倒すか」という新しい
    状態を生む。ここでは常に job 側に倒すことでその状態を無害化する)。

時刻 (`grace_seconds` / `min_start_epoch`) はもう判定に使わない — 同定という
別の根拠に完全に置き換えた (t049 の `min_start_epoch` 機構は撤去)。
"""
from collections import deque
from pathlib import Path
from typing import Literal, Optional

ProcessSignal = Literal[
    "no_window",     # mux 窓が無い
    "not_probed",    # プロセス層を見るまでもなく判定が決まった (絶対上限など)
    "unknown",       # 窓はあるが pane pid が引けない、または途中の pid が
                     # 「消滅」以外の理由で読めない → terminate を抑制する
    "no_process",    # 子プロセスが 1 つも無い
    "idle_process",  # 永続インフラ (セッション本体・MCP サーバー・その子孫) だけ
    "executing",     # シェル (Bash tool の実行) か、同定できない子孫が居る = job 中
]

# Bash tool の実行 (前景・run_in_background の裏 job・Monitor いずれも) は必ず
# シェル経由で生える (t016 実測: "/bin/bash -c source ...")。シェルが見つかった
# 時点で job — これより後の判定 (既知インフラ一覧) より優先する。
_SHELL_COMMS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash"})

# 既知の「永続インフラ」の comm (/proc/<pid>/stat の実行体名。15 文字で切り詰め)。
# **ここに載っていない永続プロセスは infra に含めない** — 同定できない場合は
# job 側 (executing) に倒す (族C監査、モジュール docstring 参照)。
_KNOWN_INFRASTRUCTURE_COMMS = frozenset({
    "claude",                # セッション本体
    "node", "npm", "npx",    # MCP サーバーの起動経路 (npm exec / npx 経由で node が実行体になることが多い)
    "chrome", "chromium",    # Playwright / Chrome DevTools MCP が起動するブラウザ
    "chrome-headless",       # "chrome-headless-shell" (21 文字) は 15 文字で切り詰まる
    "google-chrome",
})


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


def classify_process_tree(root_pid: int) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する。

    本番のペインは常にこの形をしている (2026-09-21 実測, Ren-worker):

        /bin/bash                      ← root_pid (pane_pid)
          claude --model ...           ← セッション。Worker が生きている限り常駐
            npm exec @playwright/mcp   ← MCP サーバー。claude の 1-2 秒後に起動
            npm exec chrome-devtools   ← 同上
            /bin/bash -c source ...    ← Bash tool の実行中だけ現れる

    旧実装 (t016) の `pgrep -P <pane_pid>` は常に claude 1 件を返すため「子
    プロセスが居る = 作業中」が恒真になり、idle 判定に一度も到達しなかった。
    続く実装 (t016 → t049) は「いつ生えたか」(session_start からの経過時間 /
    assignment file の mtime との比較) で区別していたが、時刻はしょせん代理
    指標であり、どちらの向きにも穴が残った (モジュール docstring 参照)。

    ここでは **そのプロセスが何であるか** (comm) で区別する:

      "executing"    … シェル (Bash tool の実行) か、同定できない子孫が居る
                       = job が走っている (と見なす)
      "idle_process" … セッション本体・MCP サーバー・その子孫 (既知インフラ)
                       だけ。木が有ること自体は「働いている」の証拠にならない
      "no_process"   … 子が 1 つも無い (claude が落ちた / 素のシェル)
      "unknown"      … 途中の pid が「消滅」以外の理由で読めず、同定できない
                       (t049 族A監査。terminate 側の抑制材料としては効くが、
                       Rule 5 は「観測できない → 通知する」に倒す)

    親の分類 (job / infra) は子に伝播する: シェルの子孫はすべて job (シェルが
    起動した実体の一部)。既知インフラの子孫はシェルに当たるまで infra
    (Playwright MCP が起動するブラウザ等)。ルート直下 (親の分類が無い) で
    既知インフラでもシェルでもない場合は job 側に倒す (族C: 未知の永続
    プロセスを infra と誤認して黙り続けるより、job 扱いして 1 回余計に
    通知する方が安い)。
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
        comm = procs[pid][2]
        if comm in _SHELL_COMMS:
            origin = "job"
        elif parent_origin == "job":
            origin = "job"
        elif comm in _KNOWN_INFRASTRUCTURE_COMMS:
            origin = "infra"
        else:
            # 同定できない (シェルでも既知インフラでもない、親も infra) —
            # job 側に倒す (族C: モジュール docstring 参照)。
            origin = "job"
        if origin == "job":
            return "executing"
        for child in children.get(pid, []):
            queue.append((child, origin))

    return "idle_process"
