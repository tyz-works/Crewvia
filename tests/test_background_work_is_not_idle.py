#!/usr/bin/env python3
"""
tests/test_background_work_is_not_idle.py — 裏の shell / monitor は「止まっている」ではない (B1 / #27)

## 背景

長いテストを `run_in_background` / Monitor で待っている Worker は、ツール呼び出しが止まる。
2026-09-27 に自分の pane で実測した (裏の `sleep` が生きている間):

    herdr agent_status : `idle` (Monitor のときは `done`)   ← どちらも Rule 5 の通知対象
    画面末尾           : `1 shell` / `1 monitor`
    claude の直下      : `bash -c ... eval 'sleep 300'` が生える (後から生えた子)
    classify_process_tree(pane_pid) : 終始 `executing`、終わると `idle_process`

その結果、dispatcher の Rule 5 が `[Rule 5] idle-with-task` を約 2 分おきに Director に送り続けた
(mission 20260926-mechanize-guards-a では Director が手でツールを 1 回使わせて回避した)。
watchdog はプロセス層 (t016) が既に hard idle を terminate にしない — ここではそれを
**実プロセス木で** 通しで確かめ、上限 (max) が裏の子があっても効くことを固定する。

## t074 (Codex review 3巡目 P1): 判定根拠を「何であるか (comm)」から「誰が起動したか」に移す

t016 (`grace_seconds`) → t049 (`min_start_epoch`) → t065 (comm の同定) の 3 巡とも、
「いつ生えたか」「何という名前か」という**代理指標**を使っていたため、それぞれ違う形で
穴が残った (詳細は `scripts/lib_pane_process.py` docstring)。3巡目の穴は、本番の
`npm exec @playwright/mcp` が npm の `process.title` 書き換えと `sh -c "..."` を挟む経路
のせいで、comm ベースの許可リストでは MCP を job と誤読する (偽陰性)、というものだった。
`lib_pane_process.classify_process_tree()` はもう comm も時刻も見ない —
**祖先 (自分自身を含む) の cmdline に Bash tool / Monitor の shell snapshot wrapper
(`/shell-snapshots/snapshot-` を `source` する形) が現れるか**で区別する。

## 方法 (t074: symlink で名前を作る fixture は使わない — Codex がそれで素通りしたと指摘)

* プロセス木は本物。MCP 相当は `_mcp_like_cmd()` で **本物の bash の `exec -a`** を使い、
  実バイナリ (`/bin/sleep`) を実際に fork/exec しつつ argv[0]/cmdline だけを本番の
  npm 実測値 (`npm exec @playwright/mcp@latest` 等) に書き換える (npm の
  `process.title` 書き換えの実物相当。symlink で comm を偽装するのとは違い、
  cmdline そのものが本物の観測対象になる — 分類は cmdline しか見ない)。
* 裏の job 相当は `_job_wrapper_cmd()` で、Bash tool / Monitor が実際に生成する
  wrapper の形 (`bash -c "source <shell-snapshot> ... && eval '<command>'"`) を
  実測どおりそのまま再現する (2026-09-27 実測: 本ファイル群の実行時に自分の
  `run_in_background` / Monitor で確認した実物の cmdline)。
* dispatcher は **本物の** dispatcher.sh の埋め込み python を `exec()` して `check_rule5()` を呼ぶ
  (`tests/test_dispatcher_notify_once.py` の Harness)。差し替えるのは mux (状態と pane pid) だけ。
* watchdog は本物の `WorkerMonitor.check_detail()`。差し替えるのは mux の pane pid 解決だけで、
  プロセス層は実物を通す (`_process_signal` をスタブしない)。

fail の向き (memory: fail-direction-is-per-judgment) はテスト名に出す:
  * Rule 5 (通知を止める判定): 観測できない → **通知する**側
  * watchdog (殺す判定):        観測できない → **殺さない**側 (既存: test_watchdog_idle.py)

## t091 (B1 5巡目 P1追補): `exec` が cmdline の印を消す

`exec sleep 30` のように Bash tool の中で `exec` を使うと、その pid の cmdline は
`sleep 30` に置き換わり (execve が argv を丸ごと差し替える)、上の `_job_wrapper_cmd()`
が作る `source <snapshot> && eval ...` という cmdline の印が失われる。だが environ は
execve で明示的に envp を渡さない限り引き継がれる (`_env_prefix()` で明示的に curate した
`CLAUDE_CODE_CHILD_SESSION` / `CLAUDE_CODE_EXECPATH` はそのまま残る) — これを第二の証拠
として使う (`lib_pane_process.JOB_ENVIRON_MARKERS`)。全 fixture の environ は
`_env_prefix()` で明示的に決める — pytest をどの環境 (CI / Claude Code の Bash tool から
の Worker) で走らせても結果が変わらないようにするため (Bash tool 自身の呼び出しは
`CLAUDE_CODE_CHILD_SESSION=1` を持つので、明示的に curate しないと fixture がこれを
継承してしまう)。

実行: python3 -m pytest tests/test_background_work_is_not_idle.py -v
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_mux  # noqa: E402
import lib_pane_process  # noqa: E402
import watchdog  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness, SLUG  # noqa: E402
from test_watchdog_idle import (  # noqa: E402
    WINDOW, _FakeMux as _WatchdogFakeMux, _make_monitor, _write_activity,
)

AGENT = "sofia"
TARGET = f"{AGENT}-worker"


# ---------------------------------------------------------------------------
# 実プロセス木 (pane 相当)。1 テストファイルで 2 本だけ立てて使い回す
# ---------------------------------------------------------------------------

def _pgid_of(pid: int) -> Optional[int]:
    """/proc/<pid>/stat の field 5 (pgid)。読めなければ None (= 既に居ない)。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1:].split()
    if len(rest) < 3:
        return None
    try:
        return int(rest[2])
    except ValueError:
        return None


def _members_of_group(pgid: int) -> list:
    """この pgid に属する現存 pid の一覧 (テストの「孤児が残っていないか」の判定に使う)。"""
    members = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in entries:
        if entry.name.isdigit() and _pgid_of(int(entry.name)) == pgid:
            members.append(int(entry.name))
    return members


def _kill_pane_tree(proc: subprocess.Popen) -> None:
    """t049 (Codex review, PR#238 P3): root だけでなく group ごと kill する。

    root の `sh` 自身しか kill しないと、非対話シェルの背後の子 (孫の `sh` /
    `sleep`) は同じ group のまま孤児になり、自分の `sleep 300` の寿命 (最大 5分)
    だけ生き残って標準出力の fd を掴み続ける (mutation script を繰り返すたびに
    残骸が積み上がり、EOF 待ちの呼び出し側を遅延させうる)。spawn 側が
    `start_new_session=True` (setsid) で自分専用の process group を持つ前提。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # 既に居ない (group ごと消えた) — teardown の目的は既に達成
    proc.wait()


def _env_prefix(extra: dict) -> str:
    """`env -i PATH=... KEY=VALUE ...` — 後続コマンドの environ を丸ごと
    差し替える shell 断片 (t091)。

    fixture のプロセス木は pytest を起動した process から environ を継承する。
    このテストを Claude Code の Bash tool から (= Worker として) 走らせると、
    その Bash tool 自身の呼び出しがすでに `CLAUDE_CODE_CHILD_SESSION=1` を
    持つ (実測: 本 task の着手時に自分の Bash tool の env で確認した) ため、
    fixture の全ノードがこれを**継承**してしまい、"quiet" (job 無し) の
    はずのノードまで job に誤判定される。各ノードの environ は必ずここで
    明示的に決め、周囲の実行環境 (pytest を CI で走らせるか、Worker の
    Bash tool から走らせるか) に結果が依存しないようにする。
    """
    parts = ["env", "-i", f"PATH={shlex.quote(os.environ.get('PATH', '/usr/bin:/bin'))}"]
    for key, value in extra.items():
        parts.append(f"{key}={shlex.quote(value)}")
    return " ".join(parts)


def _mcp_like_cmd(title: str, seconds: int = 300, *, claudecode: bool = True) -> str:
    """本物の bash の `exec -a` で argv[0]/cmdline だけを書き換えて、実バイナリ
    (`/bin/sleep`) を実際に fork/exec する (t074: symlink で名前を作る fixture は
    使わない — Codex が「本物の起動経路を通っていない」と指摘している)。

    npm は自分の `process.title` を書き換えるので、本番の MCP サーバーの
    cmdline は `npm exec @playwright/mcp@latest ...` のような形になる
    (`scripts/lib_pane_process.py` docstring の実測表)。`exec -a` はこれと
    同じ効果 (argv[0]/cmdline の書き換え) を、実際の fork/exec を経て起こす —
    comm を偽装する symlink と違い、cmdline そのものが本物の観測対象になる。
    重要なのは、この cmdline のどこにも Bash tool / Monitor の wrapper marker
    (`/shell-snapshots/snapshot-`) が **絶対に現れない**こと (分類はそれしか
    見ない)。

    t091: environ は `_env_prefix()` で明示的に決める。`claudecode=True`
    (既定) は本番実測どおり `CLAUDECODE=1` だけを持つ (`CLAUDE_CODE_CHILD_SESSION`
    / `CLAUDE_CODE_EXECPATH` は持たない) — Claude が起動したが Bash tool /
    Monitor 経由ではないもの (MCP サーバー) を再現する。`claudecode=False` は
    `CLAUDECODE` すら持たない、Claude Code と無関係なプロセスを再現する
    (t091 の新しい "unknown" 経路のテスト用)。
    """
    inner = f'exec -a {shlex.quote(title)} /bin/sleep {seconds}'
    env = {"CLAUDECODE": "1"} if claudecode else {}
    return f'{_env_prefix(env)} bash -c {shlex.quote(inner)}'


def _write_wrapper_snapshot(directory) -> Path:
    """Bash tool / Monitor が起動のたびに書き出す shell snapshot ファイルの
    使い捨てフィクスチャ (中身は無害な no-op)。パスに `/shell-snapshots/snapshot-`
    を含めることが重要 — 分類はパスの中身でなく、cmdline に現れるこの部分
    文字列だけを見る。
    """
    snap_dir = Path(directory) / ".claude" / "shell-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / "snapshot-test-fixture.sh"
    if not snap.exists():
        snap.write_text(": # no-op fixture snapshot\n")
    return snap


def _job_wrapper_cmd(snapshot_path: Path, inner: str, *, with_marker: bool = True) -> str:
    """Bash tool (前景 / `run_in_background`) と Monitor が実際に生成する
    wrapper の形そのもの (2026-09-27 t074 実測: 自分の `run_in_background` /
    Monitor 呼び出しで `/proc/<pid>/cmdline` を直接読んで確認した):

        /bin/bash -c source <shell-snapshot> ... && eval '<command>' ...

    実測では他に `export CODEX_COMPANION_*` 等が挟まるが、分類が見るのは
    `source` の後に続く `/shell-snapshots/snapshot-` という部分文字列だけなので、
    ここでは本質だけを再現する。

    t091: environ も `_env_prefix()` で明示的に `CLAUDECODE` / `CLAUDE_CODE_CHILD_SESSION`
    / `CLAUDE_CODE_EXECPATH` を持たせる (Director 実測表どおり、本物の Bash tool
    job は 3 つとも持つ)。`with_marker=False` は cmdline の wrapper marker を
    含めない `exec` 後の状態を再現する — environ だけが job の証拠になる
    (この task の本題)。
    """
    if with_marker:
        script = (
            f"source {shlex.quote(str(snapshot_path))} 2>/dev/null || true "
            f"&& eval {shlex.quote(inner)}")
    else:
        # exec で cmdline のマーカーが失われた後の状態そのもの: この pid の
        # cmdline はもう `source .../shell-snapshots/...` を含まない。
        script = f"exec {inner}"
    env = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_EXECPATH": "/fake/claude/execpath",
    }
    return f"{_env_prefix(env)} bash -c {shlex.quote(script)}"


@pytest.fixture(scope="module")
def panes(tmp_path_factory):
    """{"busy": pid, "quiet": pid} — どちらも本番のペインと同じ形の木。

    busy   root sh ─ MCP相当 (node風 cmdline, t=0) ─ Bash tool wrapper (裏の job)
    quiet  root sh ─ MCP相当 (node風 cmdline, t=0)                       … 裏の job 無し

    MCP 相当は `_mcp_like_cmd()` で cmdline だけを本番実測の npm 表記に似せる
    (`exec -a`。symlink は使わない)。**quiet に Bash tool wrapper 形を一切
    含めないこと** — wrapper marker (`/shell-snapshots/snapshot-`) を持つ
    cmdline が見つかった時点で job と判定するので、中身が空でもこの形を
    quiet 側に置くと、それだけで誤って job 側に倒れてしまう。
    各木は `start_new_session=True` で自分専用の process group を持つ
    (setsid)。teardown は `_kill_pane_tree()` で group ごと落とす (P3)。
    """
    fake_dir = tmp_path_factory.mktemp("fake-panes")
    snapshot = _write_wrapper_snapshot(fake_dir)
    mcp_cmd = _mcp_like_cmd("npm exec @playwright/mcp@latest")
    job_cmd = _job_wrapper_cmd(snapshot, "sleep 300")
    busy = subprocess.Popen(
        ["sh", "-c", f'{mcp_cmd} & {job_cmd} & wait'],
        start_new_session=True)
    quiet = subprocess.Popen(
        ["sh", "-c", f'{mcp_cmd} & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 遅れて生える子が出そろうまで
    try:
        yield {"busy": busy.pid, "quiet": quiet.pid}
    finally:
        for p in (busy, quiet):
            _kill_pane_tree(p)


def test_the_fixture_trees_classify_as_intended(panes):
    """前提: 木の形が意図どおり。ここが崩れると以降の緑・赤は何の証明にもならない。"""
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "executing"
    assert lib_pane_process.classify_process_tree(panes["quiet"]) == "idle_process"


def _direct_children(pid):
    """`_proc_stat` を直接使って ppid==pid の pid 一覧を返す (テスト専用ヘルパー)。"""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = lib_pane_process._proc_stat(int(entry.name))
        except OSError:
            continue
        if stat is not None and stat[0] == pid:
            out.append(int(entry.name))
    return out


def test_an_unreadable_intermediate_pid_falls_to_unknown_not_idle(monkeypatch):
    """族A監査 (t049): `_proc_stat` が「消滅」以外の理由 (EACCES 等) である pid を
    読めないと、それを None に潰して静かにスキップする実装では、その pid を親に
    持つ子孫 (それ自体は読める) が誰からも「値」として指されなくなり、
    children から永久に辿り着けなくなる。生きている裏 job のサブツリーが
    まるごと `idle_process` に見えてしまう欠陥を、実プロセス木 + `_proc_stat`
    への読み取り失敗の注入で固定する (`unknown` を返し、静かに `idle_process`
    へ倒さないこと)。
    """
    root = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.5)  # 孫の裏 job まで出そろうまで

        # root の直下の子のうち、自分自身がさらに子を持つ方が wrapper (裏 job の親)。
        direct = _direct_children(root.pid)
        wrapper_pid = next(pid for pid in direct if _direct_children(pid))

        real_proc_stat = lib_pane_process._proc_stat

        def flaky_proc_stat(pid):
            if pid == wrapper_pid:
                raise PermissionError(13, "Permission denied (test injection)")
            return real_proc_stat(pid)

        monkeypatch.setattr(lib_pane_process, "_proc_stat", flaky_proc_stat)

        assert lib_pane_process.classify_process_tree(root.pid) == "unknown"
    finally:
        _kill_pane_tree(root)


def test_an_unreadable_wrapper_is_unknown_not_infra(panes, monkeypatch):
    """t082 (Codex review 4巡目 P1): 赤の実証。ラッパー自身の cmdline が
    「消滅」以外の理由 (EACCES 等) で読めないとき、それを「マーカーが無い」
    (= job ではない = infra) に潰す実装は、**走っている本物の job を
    idle_process に誤分類する** — watchdog の check_detail() はそれを見て
    hard-idle による terminate を許してしまう (「観測に失敗したら kill しない」
    という要件に反する)。`_proc_stat` (族A監査) と同じ基準で、木全体を
    `unknown` に倒すことを固定する。
    """
    real_proc_cmdline = lib_pane_process._proc_cmdline

    # busy fixture の直下 (root の子) のうち、wrapper marker を持つ方が job の
    # ラッパー自身 (もう一方は MCP 相当で marker を持たない)。
    direct = _direct_children(panes["busy"])
    wrapper_pid = next(
        pid for pid in direct
        if (real_proc_cmdline(pid) or "") and
        lib_pane_process.BASH_TOOL_WRAPPER_MARKER in real_proc_cmdline(pid))

    def flaky_proc_cmdline(pid):
        if pid == wrapper_pid:
            raise PermissionError(13, "Permission denied (test injection)")
        return real_proc_cmdline(pid)

    monkeypatch.setattr(lib_pane_process, "_proc_cmdline", flaky_proc_cmdline)

    assert lib_pane_process.classify_process_tree(panes["busy"]) == "unknown"


def test_watchdog_does_not_terminate_when_the_wrapper_cmdline_is_unreadable(
    tmp_path, monkeypatch, panes
):
    """t082 P1 受入条件そのもの: ラッパーの cmdline が読めない状態でも、
    その下に本物の job を持つ Worker は watchdog に kill されない
    (`unknown` は殺さない側に倒れる — 既存の fail-direction と同じ)。
    """
    real_proc_cmdline = lib_pane_process._proc_cmdline
    direct = _direct_children(panes["busy"])
    wrapper_pid = next(
        pid for pid in direct
        if (real_proc_cmdline(pid) or "") and
        lib_pane_process.BASH_TOOL_WRAPPER_MARKER in real_proc_cmdline(pid))

    def flaky_proc_cmdline(pid):
        if pid == wrapper_pid:
            raise PermissionError(13, "Permission denied (test injection)")
        return real_proc_cmdline(pid)

    monkeypatch.setattr(lib_pane_process, "_proc_cmdline", flaky_proc_cmdline)
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert detail.process_signal == "unknown"
    assert detail.verdict != "terminate"


# ---------------------------------------------------------------------------
# dispatcher Rule 5
# ---------------------------------------------------------------------------

class PaneMux(FakeMux):
    """herdr が返す agent_status と pane pid を、テストが決める。"""

    pane_state = "idle"
    pane_pid = None
    pid_raises = None

    def state(self, *a, **kw):
        return PaneMux.pane_state

    def pid(self, name):
        if PaneMux.pid_raises is not None:
            raise PaneMux.pid_raises
        return PaneMux.pane_pid


class Rule5:
    """`check_rule5()` を、grace 経過済みの idle-with-task から 1 回呼ぶ口。"""

    def __init__(self, h: Harness, monkeypatch):
        monkeypatch.setattr(lib_mux, "Mux", PaneMux)
        PaneMux.pane_state, PaneMux.pane_pid, PaneMux.pid_raises = "idle", None, None
        self.h = h
        h.cycle()   # 本物の埋め込み python の名前空間を作る (Worker 窓は無いので何も送らない)
        self.ns = h.ns
        self.assignment = self.ns["ASSIGNMENTS_DIR"] / AGENT
        self.assignment.parent.mkdir(parents=True, exist_ok=True)
        self.assignment.write_text(f"{SLUG}:t001\n")

    def age_state(self, state="idle-with-task", seconds=600):
        """Rule 5 の grace (60 秒) を経過済みにする。"""
        self.ns["_save_state_entry"](AGENT, state, time.time() - seconds)

    def entry(self):
        return json.loads(
            (self.h.registry / "mux" / f"{AGENT}.state.json").read_text())

    def job_since_path(self):
        return self.h.registry / "mux" / f"{AGENT}.job-since.json"

    def seed_job_since(self, seconds_ago):
        """job が `seconds_ago` 秒前から連続して見え続けている、という状態を作る
        (t074 追補: BACKGROUND_JOB_MAX_SECONDS の安全弁テスト用)。"""
        self.ns["_save_job_since"](AGENT, time.time() - seconds_ago)

    def run(self):
        FakeMux.sent = []
        self.ns["check_rule5"](AGENT, TARGET, self.assignment, {})
        return [m["message"] for m in FakeMux.sent]


@pytest.fixture
def r5(tmp_path, monkeypatch):
    return Rule5(Harness(tmp_path / "repo", monkeypatch), monkeypatch)


@pytest.mark.parametrize("herdr_state", ["idle", "done"])
def test_a_live_background_job_is_not_idle_with_task(r5, panes, herdr_state):
    """裏の shell (`idle`) / Monitor (`done`) が生きている間は Rule 5 は黙る。"""
    PaneMux.pane_state, PaneMux.pane_pid = herdr_state, panes["busy"]
    r5.age_state()
    assert r5.run() == []


def test_the_same_pane_without_a_background_job_is_still_notified(r5, panes):
    """対照: 裏の job が無ければ従来どおり通知する (常に黙る実装を弾く)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["quiet"]
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_a_background_job_restarts_the_grace_and_clears_the_dedup_key(r5, panes):
    """裏の job 中は 'working' 扱い: grace を測り直し、通知済みの印を外す。"""
    r5.ns["record_notify"](f"idle_with_task_{AGENT}")
    assert f"idle_with_task_{AGENT}" in r5.ns["load_notify_cache"]()
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []
    assert r5.entry()["state"] == "working"
    assert time.time() - r5.entry()["since"] < 30          # 測り直された
    assert f"idle_with_task_{AGENT}" not in r5.ns["load_notify_cache"]()


# ---------------------------------------------------------------------------
# t074 追補 (Director 実例, 2026-09-27 23:15〜23:45): Ren の
# `while pgrep -f "<script>" > /dev/null; do sleep 15; done` は Bash tool の
# ラッパーの子孫なので「起動元」判定では job と分かるが、`pgrep -f` が
# ループ自身の cmdline に自己一致して赤の実証が終わった後も永遠に回り続けた。
# 「job だから黙る」に上限 (BACKGROUND_JOB_MAX_SECONDS) を足し、黙る方向には
# 倒さない安全弁を固定する。memory `pgrep-self-match-wait-loop-hangs-worker`。
# ---------------------------------------------------------------------------

def test_a_job_younger_than_the_ceiling_still_suppresses_rule5(r5, panes):
    """上限に達していない裏の job は、従来どおり黙る (回帰させない)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.seed_job_since(r5.ns["BACKGROUND_JOB_MAX_SECONDS"] - 60)
    r5.age_state()
    assert r5.run() == []


def test_a_job_older_than_the_ceiling_no_longer_suppresses_rule5(r5, panes):
    """赤の実証: 裏の job (本物のシェル、`executing`) が生きたままでも、
    連続して見え続けている時間が上限を超えたら通常の idle-with-task 判定に
    進む (黙り続けない)。Ren の pgrep 自己一致ループのように、Bash tool の
    ラッパーの子孫のまま何十分も進んでいない job を想定。
    """
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.seed_job_since(r5.ns["BACKGROUND_JOB_MAX_SECONDS"] + 60)
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_the_ceiling_timer_is_independent_of_the_grace_timer(r5, panes):
    """job_since (連続 job 検出の起点) は grace の `since` (現在の条件が
    始まった時刻。job があるあいだ毎サイクル書き直される) とは別物であること
    を固定する — 同じフィールドを 2 つの意味で使うと、どちらかが壊れる。
    grace を何度測り直しても (`run()` を複数回呼んでも)、job_since は
    最初に job を見た時刻のまま変わらない。
    """
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []
    first_job_since = json.loads(r5.job_since_path().read_text())["job_since"]
    time.sleep(1.1)
    assert r5.run() == []                                    # grace ('since') は測り直る
    assert r5.entry()["state"] == "working"
    assert time.time() - r5.entry()["since"] < 5              # grace は測り直された
    second_job_since = json.loads(r5.job_since_path().read_text())["job_since"]
    assert second_job_since == first_job_since                 # job_since は変わらない


def test_job_since_clears_when_the_job_ends(r5, panes):
    """job が終われば job_since も消える — 次に別の job が生えたら新しく計り直す。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []
    assert r5.job_since_path().exists()
    PaneMux.pane_pid = panes["quiet"]                          # job が終わった
    r5.run()
    assert not r5.job_since_path().exists()


def test_an_unreadable_job_since_file_does_not_suppress_forever(r5, panes):
    """t082 (Codex review 4巡目 P2): 赤の実証。job_since ファイルが**存在するが
    壊れている** (JSON として不正) とき、それを「無い」(=初めて見た) に潰すと
    毎サイクル `now` を新規タイマーとして採用し続け、`BACKGROUND_JOB_MAX_SECONDS`
    が永久に切れない (安全弁そのものが黙る側に壊れる)。「無い」と「読めない」を
    区別し、読めないときは上限判定を信用せず通常の idle-with-task 判定に
    流すことを固定する。
    """
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.job_since_path().parent.mkdir(parents=True, exist_ok=True)
    r5.job_since_path().write_text("{not valid json")
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_a_job_since_write_failure_does_not_suppress_forever(r5, panes):
    """t082 (Codex review 4巡目 P2): 赤の実証。job_since を初めて書こうとして
    失敗する (registry/mux が書き込めない) と、それを握り潰して「保存できた」
    ふりをする実装は、次のサイクルでもファイルが無いまま `now` を新規タイマー
    として採用し続け、上限が永久に切れない。書き込みの失敗を呼び出し側に
    伝え、上限判定を信用しない (通常の idle-with-task 判定に流す) ことを固定する。
    """
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    mux_dir = r5.job_since_path().parent
    mux_dir.mkdir(parents=True, exist_ok=True)
    assert not r5.job_since_path().exists()  # 前提: まだ無い (「初めて見た」経路)
    r5.age_state()  # grace の state entry はディレクトリが書ける間に先に書く
    old_mode = mux_dir.stat().st_mode
    os.chmod(mux_dir, 0o500)  # r-x: 既存ファイルは読めるが新規作成はできない
    try:
        msgs = r5.run()
        assert len(msgs) == 1 and "idle-with-task" in msgs[0]
    finally:
        os.chmod(mux_dir, old_mode)


def test_grace_counts_from_the_end_of_the_job(r5, panes):
    """job が終わったあとの grace は、終わった時点から数える (終わった瞬間に通知しない)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []                                   # job 中
    PaneMux.pane_pid = panes["quiet"]                       # job が終わった
    assert r5.run() == []                                   # 状態が変わった最初の cycle は grace の起点
    assert r5.entry()["state"] == "idle-with-task"
    r5.age_state()                                          # そのまま grace が過ぎた
    assert len(r5.run()) == 1                               # 本当に止まっているなら通知する


def test_blocked_is_still_notified_with_a_background_job(r5, panes):
    """`blocked` (承認・質問待ち) は裏の job があっても通知する — 待っているのは人間。"""
    PaneMux.pane_state, PaneMux.pane_pid = "blocked", panes["busy"]
    r5.age_state("blocked")
    msgs = r5.run()
    assert len(msgs) == 1 and "blocked" in msgs[0]


def test_no_assignment_means_the_process_tree_is_not_even_read(r5, panes):
    """assignment が無ければ条件 B ではない — /proc の走査は idle-with-task のときだけ。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.assignment.unlink()
    calls = []
    real = r5.ns["classify_process_tree"]
    r5.ns["classify_process_tree"] = lambda *a, **kw: calls.append(a) or real(*a, **kw)
    r5.run()
    assert calls == []


# -- fail の向き: 観測できない → 通知する側 ------------------------------------

def test_unobservable_pane_pid_falls_to_the_notifying_side(r5):
    """pane pid が引けない (mux 不調) → 通知する。誤って黙るより 1 通余計な方が安い。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", None
    r5.age_state()
    assert len(r5.run()) == 1


def test_a_classifier_error_falls_to_the_notifying_side_and_is_logged(r5):
    """分類が例外 → 通知する。黙って握りつぶさず WARNING を残す。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 12345
    PaneMux.pid_raises = OSError("herdr unreachable")
    r5.age_state()
    assert len(r5.run()) == 1
    assert "cannot classify pane process tree" in r5.h.log_text()


def test_a_vanished_pane_process_falls_to_the_notifying_side(r5):
    """pane pid はあるが /proc に無い (`no_process`) → 裏の job は見えない → 通知する。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 2 ** 22
    r5.age_state()
    assert len(r5.run()) == 1


# ---------------------------------------------------------------------------
# t074 (Codex review 3巡目 P1): 判定根拠を「何であるか (comm)」から
# 「誰が起動したか (起動元)」に移す。symlink で名前を作る fixture は使わない。
# ---------------------------------------------------------------------------

def _nested_infra_cmd(titles: list, seconds: int = 300, *, claudecode: bool = True) -> str:
    """本番の MCP 起動経路 (`npm exec ...` → `sh -c "playwright-mcp"` → ...) を、
    実際の fork 境界を挟んだ複数の実プロセスで再現する
    (`titles[0]` が末端・`titles[-1]` が根に近い側)。

    `exec -a` は現在のプロセスを**置き換えるだけで fork しない**ため、
    `bash -c 'exec -a T1 bash -c "…exec -a T2…"'` のように単純に入れ子にすると
    1 プロセスに畳み込まれてしまう (本ファイルで実測して踏んだ)。ここでは
    各段のあいだに `&` (バックグラウンド化 = 必ず fork) を挟むことで、
    本番と同じく**複数の別 PID が親子関係を持つ**木を作る。

    t091: environ は一番外側だけ `_env_prefix()` で明示的に決める — 内側の
    `exec -a` / 入れ子 `bash -c` はどれも自分では environ を変更しないので、
    最外側の起点だけ curate すれば木全体に伝わる (execve は明示的な envp を
    渡さない限り継承する)。`claudecode=False` は `CLAUDECODE` すら無い、
    Claude Code と無関係な木を再現する。
    """
    result = f"exec -a {shlex.quote(titles[0])} /bin/sleep {seconds}"
    for title in titles[1:]:
        inner = f"bash -c {shlex.quote(result)} & wait"
        result = f"exec -a {shlex.quote(title)} bash -c {shlex.quote(inner)}"
    env = {"CLAUDECODE": "1"} if claudecode else {}
    return f"{_env_prefix(env)} bash -c {shlex.quote(result)}"


def test_the_real_production_tree_shape_is_not_a_job(tmp_path):
    """偽陰性側の赤の実証 (主眼): 本番実測 (`scripts/lib_pane_process.py`
    docstring の表) と同じ 2 段の入れ子 (`npm exec @playwright/mcp@latest` →
    `sh -c "playwright-mcp"`、実 fork 境界つき) を job (Bash tool wrapper) 無しで
    再現する。旧 comm ベースの許可リストなら、この形の内側にある `sh -c` の
    comm がシェル一致で job と誤読していた (t074 P1: 本番で実際に踏んだ偽陰性)。
    起動元ベースでは、cmdline のどこにも wrapper marker が無く、environ も
    本番実測どおり `CLAUDECODE` だけ (t091) なので、idle_process と判定される
    ことを固定する。
    """
    cmd = _nested_infra_cmd(["sh -c playwright-mcp", "npm exec @playwright/mcp@latest"])
    root = subprocess.Popen(["sh", "-c", f"{cmd} & wait"], start_new_session=True)
    try:
        time.sleep(0.5)
        assert lib_pane_process.classify_process_tree(root.pid) == "idle_process"
    finally:
        _kill_pane_tree(root)


def test_an_unidentified_persistent_process_is_not_treated_as_a_job(tmp_path):
    """族C監査の向きの訂正 (t074 P1): t065 は「同定できない永続プロセスは job
    (executing) に倒す」と決めていたが、そのコメントは「1 回余計に通知する方が
    安い」と書きながら、実際には `executing` は**通知/terminate を抑制する側**
    (`worker_has_background_work` / `_process_signal` 参照) なので、コメントと
    挙動が逆だった (Codex 3巡目 P1)。

    起動元ベースの設計はこの「同定できないものをどちらに倒すか」という
    許可リスト方式の問い自体を無くした — マーカーを持たない (=起動元が
    Bash tool / Monitor でない) ものは、名前を知っているかどうかに関わらず
    常に job ではない。ここでは「完全に未知の実行体名」でも同じ結果になる
    ことを固定する (旧テストは逆の結果 [executing] を期待していた)。

    t091: environ は明示的に `CLAUDECODE=1` だけを持たせる (= Claude が起動した
    が名前は未知、というシナリオ。名前が判定に効かないことを固定するのが
    このテストの主眼であって、environ を空にすると別の主張 [unknown 側の
    テスト] になってしまう — それは
    `test_a_process_with_no_claudecode_evidence_at_all_is_unknown_not_idle`
    が別に持つ)。
    """
    unknown_cmd = f"exec -a {shlex.quote('totally-unknown-binary')} /bin/sleep 300"
    env_prefix = _env_prefix({"CLAUDECODE": "1"})
    root = subprocess.Popen(
        ["sh", "-c", f'{env_prefix} bash -c {shlex.quote(unknown_cmd)} & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)
        assert lib_pane_process.classify_process_tree(root.pid) == "idle_process"
    finally:
        _kill_pane_tree(root)


def test_a_process_with_no_claudecode_evidence_at_all_is_unknown_not_idle(tmp_path):
    """t091 受入条件 (3) の cmdline 側の対照: `CLAUDECODE` すら無い (Claude Code
    と無関係、または `env -i` で丸ごと消された) プロセスは、旧設計なら
    「マーカーが無い」= infra/idle_process に倒れていたが、それは「本当に
    infra だと確認できた」わけではない。job/infra どちらの確証も無いので
    unknown に倒すことを固定する (Rule 5 は unknown も「job ではない」として
    通知するので退行しない — 下の `test_the_same_pane_without_a_background_job_is_still_notified`
    系と対照)。
    """
    unknown_cmd = f"exec -a {shlex.quote('totally-unknown-binary')} /bin/sleep 300"
    env_prefix = _env_prefix({})  # CLAUDECODE も含め何も持たない
    root = subprocess.Popen(
        ["sh", "-c", f'{env_prefix} bash -c {shlex.quote(unknown_cmd)} & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)
        assert lib_pane_process.classify_process_tree(root.pid) == "unknown"
    finally:
        _kill_pane_tree(root)


def test_a_changed_wrapper_marker_no_longer_loses_the_job_thanks_to_environ(
    monkeypatch, panes
):
    """Bash tool の内部実装が変わって wrapper marker が変わった (= 定数が古く
    なった) 想定の欠陥注入。t074 時点では、本物の job (`panes["busy"]`) が
    マーカー不一致で「job ではない」に見えるようになっていた
    (誤検知が増える方向に壊れ、黙る方向には壊れない、という設計だった)。

    t091: cmdline マーカーが壊れても、environ の `JOB_ENVIRON_MARKERS`
    (`panes["busy"]` の job ノードは本物の Bash tool job と同じく
    `CLAUDE_CODE_CHILD_SESSION` / `CLAUDE_CODE_EXECPATH` を持つ) が第二の
    証拠として残るので、**この特定の欠陥注入では job を見失わない** ことを
    固定する — t091 が実際に直した P1 (`exec` で cmdline のマーカーが消える)
    そのものに対する防御力の向上を示す。cmdline と environ の**両方**が
    同時に壊れる (=job の証拠が全部消える) ケースは
    `test_breaking_all_job_signals_falls_back_to_the_documented_residual_risk`
    が別に持つ (残存リスクとして明記)。
    """
    monkeypatch.setattr(lib_pane_process, "BASH_TOOL_WRAPPER_MARKER", "/no-such-marker/")
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "executing"


def test_breaking_one_job_env_marker_is_still_caught_by_the_other(panes, monkeypatch):
    """受入条件 (4) 相当: job の environ シグナルを 2 つ (`CLAUDE_CODE_CHILD_SESSION`
    / `CLAUDE_CODE_EXECPATH`) にしたのは、将来どちらか一方の名前が変わっても
    (Claude Code の内部実装が変わる想定)、もう一方が残っていれば job 判定が
    壊れないようにするため。`panes["busy"]` の job ノードは実際に両方持つ
    (`_job_wrapper_cmd` 実測)。cmdline マーカーと環境変数の片方 (CHILD_SESSION)
    が同時に壊れても、EXECPATH が残っていれば依然 job と判定されることを固定する。
    """
    monkeypatch.setattr(lib_pane_process, "BASH_TOOL_WRAPPER_MARKER", "/no-such-marker/")
    monkeypatch.setattr(
        lib_pane_process, "JOB_ENVIRON_MARKERS",
        ("CLAUDE_CODE_CHILD_SESSION_RENAMED_BY_FUTURE_CLAUDE_CODE",
         "CLAUDE_CODE_EXECPATH"))
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "executing"


def test_breaking_all_job_signals_falls_back_to_the_documented_residual_risk(
    panes, monkeypatch
):
    """既知の残存リスク (モジュール docstring に明記): cmdline マーカーと
    job の environ シグナル**両方**が同時に壊れた場合 (= 将来 Claude Code が
    `CLAUDE_CODE_CHILD_SESSION` と `CLAUDE_CODE_EXECPATH` を同時に改名する、
    という考えにくい複合ドリフト)、job ノードは `CLAUDECODE` だけが残る —
    これは infra ノードも同じく持つので、job と infra が区別できなくなり
    infra 側に倒れる。**この複合欠陥だけは誤検知が増える方向 (idle_process) に
    壊れ、watchdog の kill が増える方向に倒れうる** — 単独の定数破損では
    起きない (`test_breaking_one_job_env_marker_is_still_caught_by_the_other`
    参照) ことをここで対比させ、残存リスクの範囲を実測で固定する。
    """
    monkeypatch.setattr(lib_pane_process, "BASH_TOOL_WRAPPER_MARKER", "/no-such-marker/")
    monkeypatch.setattr(lib_pane_process, "JOB_ENVIRON_MARKERS", ())
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "idle_process"


def test_breaking_the_infra_marker_fails_toward_unknown_not_idle(panes, monkeypatch):
    """受入条件 (4) の対照: `INFRA_ENVIRON_MARKER` (`CLAUDECODE`) の名前が変わると、
    本物の infra ノード (`panes["quiet"]`) はもう infra と確証できなくなり
    unknown に倒れる — job は無いままなので Rule 5 の通知判断は変わらないが
    (unknown も infra も「job ではない」= 通知する)、watchdog は unknown を
    殺さない側に倒すので、**kill は増えず、より安全な方向に壊れる**
    (誤検知が増える側 = 通知寄りに壊れ、kill が増える側には倒れない)。
    """
    monkeypatch.setattr(lib_pane_process, "INFRA_ENVIRON_MARKER", "CLAUDECODE_RENAMED")
    assert lib_pane_process.classify_process_tree(panes["quiet"]) == "unknown"


# ---------------------------------------------------------------------------
# t091 受入条件 (1)(3): `exec` で cmdline のマーカーが消えた後の本物のプロセス
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def execd_panes(tmp_path_factory):
    """{"job_execd": pid, "job_wiped": pid} — どちらも `exec` の**後**の実プロセス
    (cmdline はすでに `sleep 300` に置き換わっている。ラッパーの印は無い)。

    job_execd: environ は `_job_wrapper_cmd(..., with_marker=False)` どおり
               `CLAUDECODE` / `CLAUDE_CODE_CHILD_SESSION` / `CLAUDE_CODE_EXECPATH`
               を持つ (`exec` は envp を明示しない限り継承するので、本物の
               Bash tool job が `exec` した直後と同じ状態)。
    job_wiped: `exec env -i sleep 300` 相当 — cmdline も environ も両方消える
               (受入条件 (3) の「不明」)。
    """
    fake_dir = tmp_path_factory.mktemp("execd-fake-panes")
    snapshot = _write_wrapper_snapshot(fake_dir)
    execd_cmd = _job_wrapper_cmd(snapshot, "/bin/sleep 300", with_marker=False)
    wiped_cmd = f'{_env_prefix({})} bash -c {shlex.quote("exec /bin/sleep 300")}'
    job_execd = subprocess.Popen(["sh", "-c", f'{execd_cmd} & wait'], start_new_session=True)
    job_wiped = subprocess.Popen(["sh", "-c", f'{wiped_cmd} & wait'], start_new_session=True)
    time.sleep(0.5)
    try:
        yield {"job_execd": job_execd.pid, "job_wiped": job_wiped.pid}
    finally:
        for p in (job_execd, job_wiped):
            _kill_pane_tree(p)


def test_the_execd_panes_classify_as_intended(execd_panes):
    """前提: `exec` 後もラッパーの cmdline marker は無いことを確認する
    (無ければこの先の assert は何の証明にもならない)。"""
    execd_cmdline = lib_pane_process._proc_cmdline(
        next(iter(_direct_children(execd_panes["job_execd"]))))
    assert lib_pane_process.BASH_TOOL_WRAPPER_MARKER not in (execd_cmdline or "")


def test_exec_erasing_the_cmdline_marker_is_still_a_job(execd_panes):
    """受入条件 (1) そのもの: `exec sleep 30` 相当 (cmdline のマーカーは無いが
    environ は残る) は `executing` と判定される — cmdline だけを見ていた
    t074 時点の実装ならここは `idle_process` になっていた。"""
    assert lib_pane_process.classify_process_tree(execd_panes["job_execd"]) == "executing"


def test_removing_the_environ_fallback_makes_the_execd_job_look_idle(
    execd_panes, monkeypatch
):
    """赤の実証: environ フォールバックを外した (t074 までの) 欠陥版に戻すと、
    `exec` 済みの本物の job が `idle_process` に化けることを固定する
    (受入条件 (1) の「欠陥版で落ちる」)。"""
    monkeypatch.setattr(lib_pane_process, "JOB_ENVIRON_MARKERS", ())
    assert lib_pane_process.classify_process_tree(execd_panes["job_execd"]) == "idle_process"


def test_watchdog_does_not_terminate_an_exec_erased_job(tmp_path, monkeypatch, execd_panes):
    """受入条件 (1): `exec` 済みの job を watchdog が terminate しないこと
    (実際の `WorkerMonitor.check_detail()` を通す)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, execd_panes["job_execd"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.process_signal) == ("warn", "executing")


def test_watchdog_would_terminate_the_exec_erased_job_without_the_environ_fallback(
    tmp_path, monkeypatch, execd_panes
):
    """同じ赤の実証を watchdog の実際の判定を通して確かめる: environ フォールバック
    を外すと、`exec` 済みの job が hard-idle で terminate されてしまう。"""
    monkeypatch.setattr(lib_pane_process, "JOB_ENVIRON_MARKERS", ())
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, execd_panes["job_execd"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "hard_idle")


def test_watchdog_does_not_terminate_a_fully_wiped_exec_job_either(
    tmp_path, monkeypatch, execd_panes
):
    """受入条件 (3): `exec env -i sleep 30` (cmdline も environ も消える) は
    `unknown` になり、watchdog はやはり terminate しない (warn)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, execd_panes["job_wiped"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason, detail.process_signal) == (
        "warn", "hard_idle_but_process_unknown", "unknown")


def test_rule5_stays_silent_for_an_exec_erased_job(r5, execd_panes):
    """受入条件 (1) の Rule 5 側: `exec` 済みの job がある間は
    idle-with-task を通知しない (job のまま黙る)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", execd_panes["job_execd"]
    r5.age_state()
    assert r5.run() == []


def test_rule5_notifies_for_a_fully_wiped_exec_job(r5, execd_panes):
    """受入条件 (3) の Rule 5 側: cmdline も environ も消えた exec 済みプロセス
    (`unknown`) は「job だと確証できない」ので、Rule 5 は通常どおり通知する
    (unknown を「job だから黙る」に倒さない — 誤検知が増える側)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", execd_panes["job_wiped"]
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


# ---------------------------------------------------------------------------
# P3 (Codex review, PR#238): pane tree の teardown は group ごと
# ---------------------------------------------------------------------------

def test_kill_pane_tree_leaves_no_orphans_behind():
    """root の `sh` だけを kill すると、孫の `sh` / `sleep` が同じ group のまま
    孤児になり、自分の `sleep 300` の寿命 (最大 5分) だけ生き残って標準出力の fd を
    掴み続ける (mutation script を繰り返すたびに残骸が積み上がる)。
    `_kill_pane_tree()` が group ごと落とし、孤児を残さないことを固定する。
    """
    proc = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 孫の裏 job まで出そろうまで
    pgid = os.getpgid(proc.pid)

    # 前提: 木の形が意図どおり (root + MCP 相当 + wrapper + 裏 job の 4 プロセス)
    assert len(_members_of_group(pgid)) == 4

    _kill_pane_tree(proc)
    time.sleep(0.3)  # SIGKILL の反映を待つ
    assert _members_of_group(pgid) == []


# ---------------------------------------------------------------------------
# watchdog — 実プロセス木を通しで (`_process_signal` をスタブしない)
# ---------------------------------------------------------------------------

def test_watchdog_does_not_terminate_a_worker_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """hard idle でも、裏の job が生きている間は terminate しない (warn)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)          # 300 * 2 を大きく超える
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("warn", "hard_idle_but_executing")
    assert detail.process_signal == "executing"


def test_watchdog_still_terminates_the_same_silence_without_a_job(
    tmp_path, monkeypatch, panes
):
    """対照: 同じ無音でも裏の job が無ければ terminate (常に見送る実装を弾く)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["quiet"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "hard_idle")


def test_the_absolute_max_still_applies_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """裏で止まったままの job を持つ Worker が永久に残らない — max は裏の job があっても効く。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300, max_threshold=60)
    _write_activity(tmp_path, age_seconds=3000)
    monitor.started_at = time.time() - 120
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "max_exceeded")


def test_watchdog_and_dispatcher_share_one_classifier():
    """「裏で何かが走っているか」の定義は 1 つ (lib_pane_process) — コピーが生えたら赤。"""
    assert watchdog.classify_process_tree is lib_pane_process.classify_process_tree
    src = (SCRIPTS_DIR / "dispatcher.sh").read_text()
    assert "from lib_pane_process import classify_process_tree" in src
    for name in ("dispatcher.sh", "watchdog.py"):
        text = (SCRIPTS_DIR / name).read_text()
        assert "/proc/" not in text.replace("lib_pane_process", ""), (
            f"{name} が /proc を直接読んでいる — 分類は lib_pane_process に 1 つだけ")
