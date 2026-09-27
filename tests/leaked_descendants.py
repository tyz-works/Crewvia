#!/usr/bin/env python3
"""tests/leaked_descendants.py — テストが起こした子孫プロセスを、テストの終わりまでに残させない (t029 / backlog #32)。

## 何を止めるか

FIFO のテストは plan.sh を `subprocess.run(timeout=)` で走らせる。返らなければ赤にする作りだが、
タイムアウトで kill されるのは直接の子 (bash) だけで、plan.sh の下の `python3 - <queue> ...` は
孤児になり、書き手のいない FIFO を open したまま **永久に** 待つ (`wchan = wait_for_partner`)。
誰も回収せず、全 pytest を回すたびに数個ずつ溜まった (2026-09-26 に 22 個・287MB)。
WSL のメモリは 6〜8GB でスラッシングの前例がある。`tests/proc_group.py` がその直し方 (グループごと殺す)
で、ここはそれを **仕組みで強制する** 側 — 直し忘れたテストが次に出たら、その 1 本が赤くなる。

## 仕組み

1. セッションの印を `os.environ` に置く (`CREWVIA_PYTEST_SESSION=<pid>-<hex>`)。子孫は継承する。
2. 各テストの前に「いま居るプロセス」の一覧を取り (`snapshot`)、後で **増えていて生きているもの** を探す。
3. 「このセッションの子孫」の判定は 2 つの OR:
   - 環境に印がある (`/proc/<pid>/environ`)
   - cmdline か cwd が、このセッションの pytest 一時ディレクトリ (basetemp) を指している
     (`env -i` 等で環境を捨てた子孫も、sandbox の中で動く限り捕まる)
4. 見つけたら **kill して**、そのテストを ERROR にする (残すと次のテストに溜まる)。メッセージに
   pid・ppid・状態・wchan・cmdline を出す。
5. セッションの終わりに、もう一度印で探す (module / session スコープの fixture が残したもののため)。

## 本番を誤って数えない・殺さない

判定は「祖先が pytest か」を **環境の印 + 自分の basetemp** で行う。本番の plan.sh / dispatcher /
watchdog は、このセッションの環境を継承していないので印を持たず、basetemp も指さない。
別セッションの pytest (同時に走る別の Worker) も、印がセッションごとに違うので数えない。
同じ uid のプロセスだけを見る。ゾンビ (`Z`) は死んでいるので数えない (memory:
killed-subprocess-zombie-looks-alive)。pytest 自身は数えない。

## 倒す向き

- 環境が読めない同 uid のプロセス (`dumpable=0` 等) は **「無い」にしない**: 観測できなかった数として
  メッセージに残す (落とす根拠にはしないが、黙って捨てない)。
- `/proc` が無い環境 (macOS) ではガードは動けない。**動かないことを警告として出す**
  (黙って全部通さない)。
"""

from __future__ import annotations

import os
import pathlib
import signal
import time
import uuid
import warnings

import pytest

import kill_budget

MARKER_VAR = "CREWVIA_PYTEST_SESSION"

#: kill したあと死ぬのを待つ / 増えたプロセスが自然に終わるのを待つ上限 (秒)。
GRACE_SECONDS = 1.5
_POLL_SECONDS = 0.05

_PROC = pathlib.Path("/proc")


def available() -> bool:
    return (_PROC / "self" / "stat").exists()


class Survivor:
    """生き残りの 1 件 (kill する前に読んだ観測)。

    `pidfd` は観測した瞬間 (`scan`) に束縛・同一性確認した破壊用ハンドル (§ `_open_pidfd_verified`)。
    束縛できなかった (取れない / 直後の再確認で starttime が食い違った = pid 再利用の疑い) 場合は
    `None` —— `kill_all` はこれを見て pid 番号へのフォールバックはせず、kill しない。
    """

    def __init__(self, pid: int, ppid: int, state: str, age_s: float, wchan: str, cmdline: str, via: str,
                 pidfd: int | None = None):
        self.pid, self.ppid, self.state, self.age_s = pid, ppid, state, age_s
        self.wchan, self.cmdline, self.via = wchan, cmdline, via
        self.pidfd = pidfd

    def describe(self) -> str:
        return (f"pid={self.pid} ppid={self.ppid} state={self.state} age={self.age_s:.0f}s "
                f"wchan={self.wchan or '-'} via={self.via}\n      {self.cmdline[:300]}")


class Scan:
    """1 回の走査の結果。`unobservable` は「同 uid なのに環境が読めなかった」pid の数。"""

    def __init__(self):
        self.survivors: list[Survivor] = []
        self.unobservable = 0


def _read_stat(pid: int):
    """(state, ppid, starttime_ticks) — 読めなければ None。`comm` は括弧の中に空白を含みうるほか、
    Linux のプロセス名 (`comm`、`prctl(PR_SET_NAME)` 等で設定) は NUL と `/` を除く任意のバイト列を
    取れる。`read_text()` (str) はそれを UTF-8 としてデコードするため、無関係な 1 プロセスが
    `b'bad-\xff'` のような不正なバイト列の名前を持つだけで `UnicodeDecodeError` を投げる ——
    これは `OSError` のサブクラスではないので、旧実装の `except OSError` では捕まえられず、
    走査 (`scan()`) がそのプロセスの手前で丸ごと落ちる (4巡目 codex review P2-1)。
    ここで要るのは `comm` の中身ではなく、その後ろの state/ppid/starttime だけなので、
    バイト列のまま `rfind` / `split` して `comm` を一切デコードしない。
    """
    try:
        raw = (_PROC / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    rp = raw.rfind(b")")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        state = rest[0].decode("ascii", errors="replace")
        return state, int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def _read_bytes(path: pathlib.Path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def pids() -> set[int]:
    out = set()
    try:
        names = os.listdir(_PROC)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            out.add(int(name))
    return out


def snapshot() -> set[int]:
    """この時点で居るプロセスの pid 一覧 (pid の再利用は 1 テストの長さでは起きない前提)。"""
    return pids()


def pidfd_supported() -> bool:
    """`os.pidfd_open` と `signal.pidfd_send_signal` が**このカーネルで実際に動くか**
    (関数として存在するだけでは分からない。そもそも属性として存在しない環境もある)。

    `hasattr` を先に見ないと、この関数自体が `pytest.mark.skipif` の引数として
    **収集の段階で**評価されるモジュール (`test_leaked_descendants_guard.py` /
    `test_leak_guard_self_preservation.py`) で `AttributeError` を投げ、`/proc` が
    無い場合の skip マークが効く前に収集そのものを落とす (3巡目 codex review finding 1)。
    属性が無ければ `except OSError` は捕まえない。

    Python は Linux 以外でも `os.pidfd_open` を定義しうるし、対応カーネル (5.3+) でなければ
    `OSError` (ENOSYS 等) になる。動かない環境で `install()` が黙っていると、「検出はするが
    kill フォールバックを廃止したので実際には殺せない」という族C の劣化状態に誰も気づけない。
    """
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        fd = os.pidfd_open(os.getpid())
    except OSError:
        return False
    os.close(fd)
    return True


def _open_pidfd_verified(pid: int, expected_start: int) -> int | None:
    """観測した瞬間に pidfd を取り、直後に starttime を読み直して同一プロセスであることを確かめる。

    `pidfd_open` はカーネルがその瞬間に `pid` を持つプロセスへ fd を束縛する動作なので、
    一度取れれば以降その pid 番号がどれだけ再利用されても、この fd は**取った瞬間のプロセス
    インスタンス**にしか届かない (memory: verify-and-destroy-must-share-one-connection)。
    残る隙は「scan が stat を読んでからここに来るまで」と「pidfd_open してから直後の
    再読みまで」の 2 箇所だけで、後者はここで閉じる: 直後の starttime が渡された
    `expected_start` と食い違えば、その pid は既に再利用されているので束縛を捨てて
    `None` を返す (kill_all はこれを「束縛できなかった」として扱い、pid 番号への
    フォールバックはしない —— 観測失敗を許可に倒さない)。

    `os.pidfd_open` / `signal.pidfd_send_signal` が属性として無い環境 (macOS 等、あるいは
    `os.pidfd_open` はあっても `signal.pidfd_send_signal` が無い構成) では `except OSError` は
    `AttributeError` を捕まえない。ここは `scan()` から**無条件に**呼ばれる
    (pidfd_supported() のチェックを経由しない) ので、属性が無ければ「束縛できなかった」
    と同じ `None` を返して fail closed のまま抜ける (族A: 使えない → kill しない、を維持)。

    両方の属性を見るのが要る: `pidfd_supported()` は既に両方をゲートしているが (3巡目
    codex review finding 1)、ここは `pidfd_supported()` を経由せず `scan()` から直接呼ばれる
    **別の呼び出し経路**なので、同じ判定をここにも個別に置かないと届かない
    (4巡目 codex review P2-2 — 3巡目の fix は判定関数自体しか直しておらず、使う側の
    この関数には届いていなかった。`os.pidfd_open` だけ有って `signal.pidfd_send_signal` が
    無い場合、ここでゲートしないと束縛だけは成功して `pidfd` が非 None になり、後段の
    `_default_kill` が `signal.pidfd_send_signal` を素で呼んで `AttributeError` を漏らす ——
    「検出のみ」に倒すはずが、その `AttributeError` で `kill_all` ごと止まり、残りの
    survivors の後片付けと pidfd のクローズが飛ばされる)。
    """
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return None
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return None
    recheck = _read_stat(pid)
    if recheck is None or recheck[2] != expected_start:
        os.close(fd)
        return None
    return fd


def _close_survivor_pidfds(survivors: list["Survivor"]) -> None:
    """束縛した pidfd を全部閉じる (fd リークを避ける。破壊の成否は問わない)。"""
    for s in survivors:
        if s.pidfd is not None:
            try:
                os.close(s.pidfd)
            except OSError:
                pass
            s.pidfd = None


def _uptime() -> float:
    try:
        return float((_PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _dir_in_cmdline(haystack: bytes, dirpath: bytes) -> bool:
    """`dirpath` を、`haystack` (NUL 区切りの cmdline) の**引数ごとに正規化した所有**として持つか。

    旧実装は一致の「後ろ」の境界 (直後が `/` / NUL / 終端) しか見ておらず、2 つの誤認を通した
    (2 巡目 codex review finding 2、直接呼び出しで再現済み):

    - **前の境界を見ない**: `/backup/tmp/pytest-1/job.py` は `/tmp/pytest-1` を含むが、
      実際に所有しているのは `/backup/tmp/pytest-1` という別ディレクトリ (末尾がたまたま
      一致するだけ)。
    - **`..` を正規化しない**: `/tmp/pytest-1/../pytest-2/job.py` は文字面に basetemp を
      含むが実体は隣の pytest-2 配下 (逆に `/tmp/pytest-2/../pytest-1/x` は文字面には
      含まないが実体は basetemp 配下 —— 見逃す向きの誤りも起こりうる)。

    引数を **1 単位** として扱い (`--rootdir=<path>` のような `=` 付き引数は値側も見る)、
    `os.path.normpath` で構文的に正規化してから「等しい」か「区切り付きで前方一致する」かを
    見る (`os.path.commonpath` 相当。末尾スラッシュの有無もこれで吸収する)。

    シンボリックリンクは解決しない —— 相手プロセスの cmdline に現れた任意の文字列を
    `realpath` すると、こちらが制御できないファイルシステム (遅い network mount 等) への
    stat を pid ごとに発生させることになり、走査そのものが固まりうる (残る既知のギャップ。
    Result 参照)。`basetemp` 側のシンボリックリンクは呼び出し元 (`scan`) が一度だけ
    `os.path.realpath` して吸収する。
    """
    if not dirpath:
        return False
    dirpath = os.path.normpath(dirpath)
    for arg in haystack.split(b"\0"):
        if not arg:
            continue
        candidates = [arg]
        eq = arg.find(b"=")
        if eq >= 0 and arg[eq + 1:eq + 2] == b"/":
            candidates.append(arg[eq + 1:])
        for candidate in candidates:
            if not candidate.startswith(b"/"):
                continue
            normalized = os.path.normpath(candidate)
            if normalized == dirpath or normalized.startswith(dirpath + b"/"):
                return True
    return False


def _belongs(pid: int, marker: bytes, basetemp: bytes):
    """(所属する理由 | None, 観測できたか)。3 つの検出手段のうち、どれか 1 つでも読めなければ
    観測できていない (`observed=False`) —— environ だけの話ではない。

    **0 バイトで読めたのも観測の失敗** (`knowledge/empty-vs-unobservable.md` の O)。exec の
    最中のプロセスは environ が空で読める —— 孤児を起こした直後の 1 読みで 300 回中 8 回
    (2.7%)、印が現れるまでは 1ms 未満だった。これを「読めた・印が無い」に潰すと、印を継承した
    子孫が survivors にも unobservable にも入らず **黙って消える**。残骸を見逃さないのが
    仕事のガードとしては倒す向きが逆なので、空は「観測できなかった」に倒す。

    同じ exec 遷移の隙は cmdline / cwd の読み取りにも起こりうる (どちらも同じ `/proc/<pid>/`
    以下で、同じ瞬間に切り替わる)。environ は読めて印が無かった (= env を捨てた子孫かもしれない)
    のに、その先の cmdline / cwd の読み取りが失敗すると、旧実装はそれを黙って「一致しなかった」
    にしていた —— 一時的な観測失敗を「この signal では確認できなかった」ではなく
    「basetemp 配下ではないと確認できた」に倒しており、env を捨てて basetemp で動く子孫を
    黙って見逃しうる。3 つの signal のうち 1 つでも読めなければ `observed=False` にする。
    """
    base = _PROC / str(pid)
    observed = True

    environ = _read_bytes(base / "environ")
    if not environ:
        observed = False
    elif marker in environ.split(b"\0"):
        return "env-marker", True

    cmdline = _read_bytes(base / "cmdline")
    if not cmdline:
        observed = False
    elif basetemp and _dir_in_cmdline(cmdline, basetemp):
        return "cmdline-in-basetemp", True

    try:
        cwd = os.readlink(base / "cwd").encode()
    except OSError:
        observed = False
    else:
        if basetemp and (cwd == basetemp or cwd.startswith(basetemp + b"/")):
            return "cwd-in-basetemp", True

    return None, observed


def scan(exclude: set[int], marker_value: str, basetemp: str) -> Scan:
    """`exclude` に無い、生きていて、このセッションの子孫と判定できるプロセスを集める。

    `basetemp` は一度だけ `os.path.realpath` する。`/proc/<pid>/cwd` はカーネルが常に
    シンボリックリンクを解決した正準パスを返すので、呼び出し元から渡された `basetemp`
    自体が (`TMPDIR` の設定等で) シンボリックリンク経由の表記だと、正規化しないまま
    比較すると cwd 側と文字面が食い違って黙って見逃す (族B: 観測対象と比較対象の
    同一性がずれる)。`basetemp` は自分の既知のディレクトリなので、ここで 1 回
    realpath するのは安全 (候補側の cmdline パスは解決しない — 上の `_dir_in_cmdline`
    参照)。
    """
    result = Scan()
    marker = f"{MARKER_VAR}={marker_value}".encode()
    base = os.path.realpath(basetemp).encode() if basetemp else basetemp.encode()
    me, uid = os.getpid(), os.getuid()
    boot_now = _uptime()
    hz = os.sysconf("SC_CLK_TCK")
    for pid in sorted(pids() - exclude):
        if pid == me:
            continue
        stat = _read_stat(pid)
        if stat is None:
            if (_PROC / str(pid)).exists():
                result.unobservable += 1   # 居るのに stat が読めない / 読み取れない形 —— 「無い」にしない
            continue                       # (居なければ走査中に死んだだけ)
        state, ppid, start = stat
        if state in ("Z", "X"):
            continue                       # 死んでいる (回収待ちのゾンビ)
        try:
            if (_PROC / str(pid)).stat().st_uid != uid:
                continue                   # 別ユーザーのプロセスは見ない
        except OSError:
            continue
        why, observed = _belongs(pid, marker, base)
        if why is None:
            if not observed:
                result.unobservable += 1
            continue
        # 観測した瞬間に破壊用の pidfd を束縛する (finding 3)。ここより後で pid が
        # 再利用されても、kill_all は必ずこの fd 経由で送るので誤った相手には届かない。
        pidfd = _open_pidfd_verified(pid, start)
        cmd = _read_bytes(_PROC / str(pid) / "cmdline") or b""
        wchan = (_read_bytes(_PROC / str(pid) / "wchan") or b"").decode(errors="replace")
        result.survivors.append(Survivor(
            pid, ppid, state, max(0.0, boot_now - start / hz), wchan,
            cmd.replace(b"\0", b" ").decode(errors="replace").strip(), why, pidfd=pidfd))
    return result


class KillReport:
    """`kill_all` の結果。`refused` は関門が断った件、`fatal` は 1 件も殺さなかった理由。"""

    def __init__(self, killed: list[int], refused: list, fatal: str | None):
        self.killed, self.refused, self.fatal = killed, refused, fatal

    def describe(self) -> str:
        lines = []
        if self.fatal:
            lines.append(f"  !! kill を全件見送った (ガードの判定が壊れている疑い): {self.fatal}")
        lines += [f"  - kill しなかった {r.describe()}" for r in self.refused]
        return "\n".join(lines)


def _default_kill(survivor: "Survivor", sig: int) -> None:
    """本物の破壊経路。`survivor.pidfd` **限定**で送る —— pid 番号では送らない (finding 3)。

    `pidfd is None` (束縛できなかった / 直後の再確認で弾かれた) 呼び出しは、この関数を
    呼ぶ前に `kill_all` 側で refused に回すので、ここに来る時点で必ず束縛済みのはず。
    それでも呼ばれた場合に備えて防御的に拒否する (観測失敗を許可に倒さない、をここでも)。
    """
    if survivor.pidfd is None:
        raise ProcessLookupError("no verified pidfd bound — refusing to signal by bare pid")
    signal.pidfd_send_signal(survivor.pidfd, sig)


def kill_all(survivors: list[Survivor], kill=None) -> KillReport:
    """生き残りのうち `kill_budget` が許し、かつ pidfd を束縛できた pid だけを SIGKILL する。

    判定 (`_belongs` / `scan`) を壊す変異が入っても、自分・祖先・自分より古いプロセスは
    ここで落とせない。件数が上限を超えたら 1 件も殺さない (2026-09-27 の自爆の再発防止)。
    観測時 (`scan`) に pidfd で同一性を束縛できなかった survivor は、pid 番号への
    フォールバックをせず kill しない (finding 3: 観測から破壊までの間に pid が再利用
    されても、無関係な新しいプロセスを殺さない)。
    `kill` は差し替え口 (既定は `_default_kill`) — 変異テストとこのガード自身のテストは
    本物のシグナルを送らない。
    """
    if kill is None:
        kill = _default_kill
    by_pid = {s.pid: s for s in survivors}
    budget_allowed, refused, fatal = kill_budget.partition([s.pid for s in survivors])
    if fatal is not None:
        _close_survivor_pidfds(survivors)
        return KillReport([], refused, fatal)

    attempted: list[int] = []
    for pid in budget_allowed:
        survivor = by_pid[pid]
        if survivor.pidfd is None:
            refused.append(kill_budget.Refusal(
                pid, "観測時に pidfd で同一性を束縛できなかった (pid 再利用の疑い) — kill しない"))
            continue
        attempted.append(pid)
        try:
            kill(survivor, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        alive = [pid for pid in attempted if (_read_stat(pid) or ("Z",))[0] not in ("Z", "X")]
        if not alive:
            break
        time.sleep(_POLL_SECONDS)
    _close_survivor_pidfds(survivors)
    return KillReport(attempted, refused, None)


def settle(exclude: set[int], marker_value: str, basetemp: str) -> Scan:
    """増えたプロセスが自然に終わるのを `GRACE_SECONDS` まで待ってから、残ったものを返す。

    テストの後片付けで殺されたばかりのプロセスが、まだ終わり切っていないだけの場合を残りに数えない。
    `unobservable` (environ が読めない同 uid のプロセス) がある間も再試行する —— exec 直後の
    空 environ (2.7%、1ms 未満で解消) のような一過性の観測失敗を、1 回読めなかっただけで
    「クリーンな結果」に潰さないため。survivors が 0 でも unobservable が残ったままなら、
    それは「無い」ではなく「確認できなかった」であり、呼び出し側が別に報告する。
    """
    deadline = time.monotonic() + GRACE_SECONDS
    result = scan(exclude, marker_value, basetemp)
    while (result.survivors or result.unobservable) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        _close_survivor_pidfds(result.survivors)   # 破棄する走査の分。fd リークを避ける
        result = scan(exclude, marker_value, basetemp)
    return result


def format_failure(where: str, result: Scan, report: "KillReport | None" = None) -> str:
    lines = [f"{where}: このセッションのテストが子孫プロセスを {len(result.survivors)} 個残した (kill 済み)。",
             "  テストの後片付け漏れ。plan.sh のような bash の下の子孫は `subprocess.run(timeout=)` では "
             "bash しか殺されない —— `tests/proc_group.py` の `run_in_own_group()` / `kill_group()` を使う。"]
    lines += [f"  - {s.describe()}" for s in result.survivors]
    if report is not None:
        detail = report.describe()
        if detail:
            lines.append(detail)
    if result.unobservable:
        lines.append(f"  (環境が読めず観測できなかった同 uid のプロセス: {result.unobservable} 個。数には入れていない)")
    return "\n".join(lines)


class LeakGuard:
    """pytest プラグイン。`install()` が登録する。fixture と sessionfinish を持つ。"""

    def __init__(self, marker_value: str):
        self.marker_value = marker_value
        self.basetemp = ""
        self.checked_tests = 0          # 実際に走査したテストの数 (0 件で PASS にしない)
        self.leaks: list[str] = []
        #: survivors は 0 だが unobservable が残ったまま (無いとは言えない) だったテストの数。
        #: 落とす根拠にはしない (誤検出の可能性がある観測失敗で赤にしない) が、黙って消さない。
        self.unobservable_only = 0

    def _report_unobservable_only(self, where: str, count: int) -> None:
        warnings.warn(
            f"{where}: 子孫プロセスとは確認できなかったが、環境が読めず観測できなかった同 uid の"
            f"プロセスが {count} 個残っている (『無い』とは言えない。数には入れていない)",
            pytest.PytestWarning)

    @pytest.fixture(autouse=True)
    def _crewvia_no_leaked_descendants(self, request, tmp_path_factory):
        # autouse は関数スコープの fixture の中で最初に立つので、最後に降ろされる —— テストの
        # 他の fixture (worker_pane など) が自分で片付けたあとの状態を見る。
        self.basetemp = str(tmp_path_factory.getbasetemp())
        before = snapshot()
        yield
        result = settle(before, self.marker_value, self.basetemp)
        self.checked_tests += 1
        if result.survivors:
            report = kill_all(result.survivors)
            message = format_failure(request.node.nodeid, result, report)
            self.leaks.append(message)
            pytest.fail(message, pytrace=False)
        elif result.unobservable:
            self.unobservable_only += 1
            self._report_unobservable_only(request.node.nodeid, result.unobservable)

    def pytest_terminal_summary(self, terminalreporter):
        """検査した件数を毎回出す（0 件で PASS にしない）。"""
        terminalreporter.write_line(
            f"[leaked-descendants] 検査したテスト: {self.checked_tests} 件 / "
            f"残された子孫を検出したテスト: {len(self.leaks)} 件 / "
            f"観測できず不確かなまま終えたテスト: {self.unobservable_only} 件")

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        """module / session スコープの fixture が残したもののための最後の網。

        fixture の `_crewvia_no_leaked_descendants` と同じ扱い (`settle` で再試行し、
        survivors が無くても unobservable が残れば報告する) をここにも入れる。
        """
        if not available():
            return
        result = settle(set(), self.marker_value, self.basetemp)
        if result.survivors:
            report = kill_all(result.survivors)
            message = format_failure("session finish", result, report)
            self.leaks.append(message)
            print("\n" + message)
            session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)
        elif result.unobservable:
            self.unobservable_only += 1
            print(f"\n[leaked-descendants] session finish: 子孫プロセスとは確認できなかったが、"
                  f"環境が読めず観測できなかった同 uid のプロセスが {result.unobservable} 個残っている "
                  f"(『無い』とは言えない。数には入れていない)")


def install(config) -> LeakGuard | None:
    """conftest の `pytest_configure` から呼ぶ。`/proc` が無ければ警告して何もしない。"""
    if not available():
        warnings.warn(
            "tests/leaked_descendants.py: /proc が無いので、テストが残した子孫プロセスの検査は動かない "
            "(黙って通さないためにこの警告を出す)", pytest.PytestWarning)
        return None
    if not pidfd_supported():
        warnings.warn(
            "tests/leaked_descendants.py: このカーネルは pidfd_open が使えない。残った子孫プロセスの"
            "検出 (テストを ERROR にする) は動くが、kill は観測時に束縛した pidfd 限定でしか行わない"
            "設計 (finding 3) のため、実際には殺せない (検出だけになる。黙って劣化させないための警告)",
            pytest.PytestWarning)
    marker_value = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    os.environ[MARKER_VAR] = marker_value
    guard = LeakGuard(marker_value)
    config.pluginmanager.register(guard, "crewvia-leak-guard")
    return guard
