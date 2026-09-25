#!/usr/bin/env python3
"""tests/pytest_workspace_sweep.py — pytest が作った宛先を、終わるときに片付ける。

## なぜこれが要るのか (backlog #15)

`tests/conftest.py` は pytest セッションごとにユニークな宛先
`crewvia-pytest-<pid>-<hex>` を作り、`CREWVIA_HERDR_WORKSPACE` /
`CREWVIA_TMUX_SESSION` をそこへ向ける (本番 `crewvia` を撃たないための隔離)。
herdr は初回アクセスで workspace を自動作成するが、**conftest は終わるときに
閉じなかった** ので、pytest を 1 回まわすたびに空の workspace が 1 つ本番 herdr に
残り、2026-09-25 時点で 50 個溜まっていた (`herdr pane list` が 50KB 超)。

隔離そのものは正しい。足りなかったのは後始末だけ。

## 2 つの後始末

1. **自分の宛先** (`own_label`) — label が**完全一致**する workspace / session だけを
   閉じる。自分が作った名前だけを消すので危険が無く、停止スイッチも付けない
   (止めると元の漏れに戻る)。
2. **残骸** — pytest が SIGKILL された (watchdog の timeout 等) と終了フックは走らない。
   `crewvia-pytest-<pid>-<hex>` の形で、**pid のプロセスが死んでいて、かつ中に live な
   pane が 1 つも無い** ものだけを閉じる。`CREWVIA_PYTEST_WORKSPACE_SWEEP=0` で
   この掃除だけが止まる (何も見ず何も消さない)。

## 倒す向き (knowledge/empty-vs-unobservable.md)

破壊の根拠は「観測できたこと」だけ。label の形も pid の生死も対象そのものの
観測ではなく分類なので、どちらも単独では閉じる根拠にならない (AND 条件)。

  - pid の生死: `ESRCH` だけが「死んでいる」。`EPERM` (別ユーザーの生きた
    プロセス)・その他の例外・pid が 0 以下は「生きている」= 残す。
  - pane: 「中が空である」と**積極的に示せたときだけ**閉じる。pane 一覧が読めない・
    pane が 0 個と答えた・1 つでも idle でない (live / 判定不能) なら閉じない。
  - 何かが観測できなかったら、閉じずに 1 行だけ警告する。

## 後始末の失敗でテスト結果を変えない

herdr / tmux が居ない・timeout・CLI エラーはすべて警告 1 行で握りつぶす
(`run_cleanup()` は例外を出さない)。CLI 呼び出しには有限の timeout と、全体の
時間予算 (`BUDGET_SECONDS`) がある。

## 構成

判断はこのファイルの純粋関数 (`guard_refusal` / `leftover_pid` / `pid_state` /
`plan_cleanup`) に、実行 (herdr / tmux) は `HerdrBackend` / `TmuxBackend` に分けてある。
テストは偽の backend を渡す (本番に触れない)。
"""

import errno
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: pytest が作る宛先の接頭辞。これで始まらないものには一切触れない。
SESSION_PREFIX = "crewvia-pytest-"

#: conftest の `TEST_DESTINATION` が作る形そのもの: `crewvia-pytest-<pid>-<8 桁 hex>`。
#: 形に合わない label (`crewvia` / `~` / 手で作ったもの) は残骸として扱わない。
_LEFTOVER_LABEL = re.compile(r"^crewvia-pytest-([0-9]{1,10})-[0-9a-f]{8}$")

#: 残骸掃除の停止スイッチ。`0` のときだけ止まる。自分の後始末は止めない。
SWEEP_SWITCH = "CREWVIA_PYTEST_WORKSPACE_SWEEP"

#: CLI 1 回あたりの timeout (秒) と、後始末全体の時間予算 (秒)。
CLI_TIMEOUT_SECONDS = 5
BUDGET_SECONDS = 30

PID_DEAD, PID_ALIVE = "dead", "alive"


def _warn(message):
    sys.stderr.write(f"[pytest-workspace-sweep] {message}\n")


# ---------------------------------------------------------------------------
# 純粋な判断
# ---------------------------------------------------------------------------

def guard_refusal(label, production):
    """閉じてはいけない label なら理由を、閉じてよいなら None を返す。

    label の**完全一致**だけを根拠にする。前方一致・部分一致で閉じない。
    """
    if not isinstance(label, str) or not label:
        return f"label {label!r} を読めない"
    if label == production:
        return f"label {label!r} は本番の宛先"
    if not label.startswith(SESSION_PREFIX):
        return f"label {label!r} は {SESSION_PREFIX!r} で始まらない"
    return None


def leftover_pid(label):
    """`crewvia-pytest-<pid>-<hex>` の pid を返す。形に合わなければ None。

    pid が 0 のときは None: `os.kill(0, 0)` は自分のプロセスグループへの
    シグナルで、常に「生きている」と答えてしまう。
    """
    if not isinstance(label, str):
        return None
    m = _LEFTOVER_LABEL.match(label)
    if m is None:
        return None
    pid = int(m.group(1))
    return pid if pid > 0 else None


def pid_state(pid, kill=os.kill):
    """`PID_DEAD` は `ESRCH` のときだけ。それ以外はすべて `PID_ALIVE`。

    `EPERM` は別ユーザーの生きたプロセス。`OverflowError` 等の観測できなかった
    答えも「生きている」に倒す — 観測できないものを「無い」にしない。
    """
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return PID_DEAD
    except OSError as e:
        return PID_DEAD if e.errno == errno.ESRCH else PID_ALIVE
    except Exception:
        return PID_ALIVE
    return PID_ALIVE


def plan_cleanup(names, own_label, production, sweep_enabled,
                 pid_alive=pid_state):
    """`(閉じる own, 残骸の候補, 警告)` を返す。名前だけを見る、副作用の無い判断。

    `names` は宛先の名前 (label) の列。同名が複数あればそれぞれ返す。
    残骸の候補はまだ「空か」を確かめていない — 呼び出し側が AND で確かめる。
    """
    warnings = []
    refusal = guard_refusal(own_label, production)
    if refusal is not None:
        warnings.append(f"自分の宛先を閉じない: {refusal}")
        return [], [], warnings

    own = [n for n in names if n == own_label]
    leftovers = []
    if sweep_enabled:
        for name in names:
            if name == own_label or guard_refusal(name, production) is not None:
                continue
            pid = leftover_pid(name)
            if pid is None:
                continue
            if pid_alive(pid) == PID_DEAD:
                leftovers.append(name)
    return own, leftovers, warnings


# ---------------------------------------------------------------------------
# 実行層 — herdr / tmux。どちらも同じ 3 つの口を持つ:
#   entries() -> [(name, handle)] | None   (None = 一覧を観測できなかった)
#   is_empty(handle) -> bool               (True は「空だと示せた」だけ)
#   close(handle) -> bool
# ---------------------------------------------------------------------------

def _exec(run, argv):
    """`run(argv)` の `(rc, stdout, stderr)`。例外 (不在・timeout) は rc=None に潰す。

    1 回の呼び出しの失敗が、ほかの宛先の後始末を止めないようにするため。
    rc=None は「観測できなかった」で、呼び出し側はどれも閉じない側に倒す。
    """
    try:
        return run(argv)
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}"


def _default_pane_state(shell_pid):
    """lib_mux と同じ「idle な pane シェルか」の 3 値判定。"""
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import lib_mux
    return lib_mux._pane_shell_state(shell_pid)


def _pane_is_idle(state):
    # `lib_mux.PANE_IDLE` の文字列。lib_mux を import せずに比べられるよう
    # ここでは値で持つ (判定の本体は lib_mux 側)。
    return state == "idle"


class HerdrBackend:
    """herdr の workspace。宛先の名前は workspace の label、handle は workspace_id。"""

    def __init__(self, run=None, pane_state=None):
        self._run = run or self._subprocess_run
        self._pane_state = pane_state or _default_pane_state

    @staticmethod
    def _subprocess_run(argv):
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_SECONDS)
        return r.returncode, r.stdout, r.stderr

    def _json(self, argv):
        import json
        rc, out, _ = _exec(self._run, ["herdr"] + argv)
        if rc != 0:
            return None
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "error" in data:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def entries(self):
        result = self._json(["workspace", "list"])
        workspaces = result.get("workspaces") if result else None
        if not isinstance(workspaces, list):
            return None
        out = []
        for ws in workspaces:
            if not isinstance(ws, dict):
                continue
            wid = ws.get("workspace_id")
            if isinstance(wid, str) and wid:
                out.append((ws.get("label"), wid))
        return out

    def is_empty(self, workspace_id):
        """全 pane が idle なシェルだと示せたときだけ True。

        pane 一覧が読めない / pane が 0 個 / process-info が読めない /
        idle でない (live・判定不能) pane が 1 つでもある → False。
        """
        result = self._json(["pane", "list", "--workspace", workspace_id])
        panes = result.get("panes") if result else None
        if not isinstance(panes, list) or not panes:
            return False
        for pane in panes:
            pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
            if not isinstance(pane_id, str) or not pane_id:
                return False
            info = self._json(["pane", "process-info", "--pane", pane_id])
            info = info.get("process_info") if info else None
            shell_pid = info.get("shell_pid") if isinstance(info, dict) else None
            if not isinstance(shell_pid, int) or isinstance(shell_pid, bool) \
                    or shell_pid <= 0:
                return False
            if not _pane_is_idle(self._pane_state(shell_pid)):
                return False
        return True

    def close(self, workspace_id):
        return self._json(["workspace", "close", workspace_id]) is not None


class TmuxBackend:
    """tmux の session。宛先の名前は session 名、handle も session 名。"""

    def __init__(self, run=None, pane_state=None):
        self._run = run or self._subprocess_run
        self._pane_state = pane_state or _default_pane_state

    @staticmethod
    def _subprocess_run(argv):
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_SECONDS)
        return r.returncode, r.stdout, r.stderr

    def entries(self):
        rc, out, err = _exec(self._run,
                             ["tmux", "list-sessions", "-F", "#{session_name}"])
        if rc != 0:
            # server が居ないのは異常ではない (閉じるものが無い)。それ以外の失敗は
            # 観測できなかった。
            return [] if "no server running" in err else None
        return [(name, name) for name in out.splitlines() if name]

    def is_empty(self, session):
        rc, out, _ = _exec(self._run, ["tmux", "list-panes", "-s", "-t",
                                       f"={session}", "-F", "#{pane_pid}"])
        if rc != 0:
            return False
        pids = out.split()
        if not pids:
            return False
        for raw in pids:
            if not raw.isdigit() or int(raw) <= 0:
                return False
            if not _pane_is_idle(self._pane_state(int(raw))):
                return False
        return True

    def close(self, session):
        # `=` は完全一致 (前方一致で別のセッションを撃たない)。
        rc, _, _ = _exec(self._run, ["tmux", "kill-session", "-t", f"={session}"])
        return rc == 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def cleanup(backend, own_label, production, sweep_enabled,
            pid_alive=pid_state, warn=_warn, clock=time.monotonic,
            budget=BUDGET_SECONDS):
    """1 つの backend に対して自分の後始末と残骸掃除を行う。閉じた名前の列を返す。

    例外は出さない。観測できなかったことは警告 1 行にして先へ進む。
    """
    closed = []
    deadline = clock() + budget
    try:
        entries = backend.entries()
        if entries is None:
            warn("宛先の一覧を読めなかったので何も閉じない")
            return closed
        by_name = {}
        for name, handle in entries:
            by_name.setdefault(name, []).append(handle)

        own, leftovers, warnings = plan_cleanup(
            [n for n, _ in entries], own_label, production, sweep_enabled,
            pid_alive)
        for w in warnings:
            warn(w)

        # 1. 自分の宛先。label が完全一致する分だけ。空かどうかは問わない
        #    (自分が作った名前で、中身も自分のテストが作ったものだけ)。
        for name in dict.fromkeys(own):
            for handle in by_name[name]:
                if backend.close(handle):
                    closed.append(name)
                else:
                    warn(f"{name!r} を閉じられなかった")

        # 2. 残骸。形 + pid の死 + 空、の AND。
        for name in dict.fromkeys(leftovers):
            for handle in by_name[name]:
                if clock() > deadline:
                    warn("時間予算を使い切ったので残りの残骸は次回に回す")
                    return closed
                if not backend.is_empty(handle):
                    warn(f"{name!r} は空だと確認できなかったので残す")
                    continue
                if backend.close(handle):
                    closed.append(name)
                else:
                    warn(f"{name!r} を閉じられなかった")
    except Exception as e:  # noqa: BLE001 — 後始末の失敗でテスト結果を変えない
        warn(f"後始末を中断した: {type(e).__name__}: {e}")
    return closed


def _herdr_socket_exists():
    """herdr の server が居そうか。居ないなら CLI を呼ばない (server を起こさない)。"""
    sock = os.environ.get("CREWVIA_HERDR_SOCK") or str(
        Path.home() / ".config" / "herdr" / "herdr.sock")
    try:
        return os.path.exists(sock)
    except OSError:
        return False


def default_backends():
    """この環境で実際に話しかけられる backend。居ないものは含めない。"""
    backends = []
    if shutil.which("herdr") is not None and _herdr_socket_exists():
        backends.append(HerdrBackend())
    if shutil.which("tmux") is not None:
        backends.append(TmuxBackend())
    return backends


def run_cleanup(own_label, production, environ=None, backends=None):
    """conftest の `pytest_unconfigure` から呼ぶ。何があっても例外を出さない。"""
    env = os.environ if environ is None else environ
    try:
        sweep_enabled = env.get(SWEEP_SWITCH) != "0"
        for backend in (default_backends() if backends is None else backends):
            cleanup(backend, own_label, production, sweep_enabled)
    except Exception as e:  # noqa: BLE001
        _warn(f"後始末を中断した: {type(e).__name__}: {e}")
