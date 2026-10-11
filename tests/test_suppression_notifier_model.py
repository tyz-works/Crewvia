"""見送り通知 (`SuppressedIdleNotifier`) の model-based 網羅テスト (PR #278 Codex P2 4 巡目 / t016、§11-13)。

1 巡目 (t009)・2 巡目 (t011)・4 巡目 (t016) の指摘は全部「状態の進み方の一部の経路だけを直した結果、
別の経路が残った」型だった。個別のシナリオを足すのをやめ、出来事の並びを全部流して不変条件を毎ステップ
確かめる。実物の `SuppressedIdleNotifier` と実物の台帳ファイル (tmp) を使う。

出来事 (restart と故障の 3 種を除き、1 つにつき watchdog の 1 サイクルが回る。サイクルの間隔は 32 秒):
  S_unk / S_exe / S_awh  見送りを観測 (process_unknown / executing / awaiting_human)
  recover                見送りでない観測 (alive)。新しい通知ファイルが足される (活動)
  quiet_close            見送りでない観測 (terminate / hard_idle)。ファイルに触らない = 活動なしで閉じる
  restart                watchdog 再起動 (SuppressedIdleNotifier を作り直す。プロセス内の状態を全部失う。
                         止まっている間は観測しない = 再起動後の最初の観測は次の出来事のサイクル)
  write_fail             次のサイクルの台帳への書き込みが失敗する (自分ではサイクルを回さない)
  delete_fail            次のサイクルの台帳の削除が失敗する (False を返す)
  delete_raise           次のサイクルの台帳の削除が例外を投げる
  new_exec               Worker の Execution ID が変わる (新しい通知ファイルが足される)
  vanish                 Worker が監視から外れる (ファイルに触らない)
  tick                   600 秒経つ
  gap                    観測の途切れ: 時計を 700 秒進めるが観測しない。その間に活動 (通知ファイル) が 1 回入る
  signal_lost            いちばん新しいシグナルファイルを実際に消す (heartbeat だけなら消さない) サイクル
  signal_restored        直前に消したファイルを元の (古い) mtime のまま戻すサイクル

起点 (無音区間の始まり) は実物のシグナルファイルから `WorkerMonitor._last_activity_mtime()` (floor 込み) で
求める。CheckResult の idle_seconds は固定のまま。

episode = ある Worker・ある Execution ID の見送りの連続区間 (見送りでない観測 / 消滅 / Execution ID の変化で終わる)。
無音区間 = episode のうち起点が同じ値のまま続く連続区間 (§11-15-11)。起点が変わる、または episode が閉じて
次の episode が始まる (quiet_close を含む) と新しい区間 (§11-15-12 の決定 2)。

不変条件:
  I1  同じ (Execution ID, 無音区間, 理由) の通知は最大 1 回 (台帳への書き込みが失敗した通知は除く)。
      起点が T0 → T1 → T0 と動けば区間は 3 つで、T0 に 2 通出るが別の区間のもの
  I2  無音区間の中である理由の見送りが、プロセスが観測した範囲で 600 秒続き、台帳の書き込み・削除が成功していれば、
      その理由の通知が出ている。例外は「台帳に今の起点と等しい fp が残っているとモデルが見ている」場合だけ
      (整数秒の等値。削除失敗 + 再起動 + 再発、または signal_lost で起点が以前の通知の起点に戻る。§11-15-10)
  I3  回復 (または消滅) のあと、削除の失敗が解けたサイクルには、その Worker の台帳キーは残っていない
  I4  新しい Execution ID は前の試行の episode も通知の記録も引き継がない (I2 が新しい episode で成り立つこと
      + 古い試行のキーがしきい値後の台帳に残らないこと)
  I5  台帳 I/O の失敗はサイクルを止めない (例外が外に出ない)
"""

from __future__ import annotations

import itertools
import os
import random
import shutil
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_daemon_state  # noqa: E402
import watchdog  # noqa: E402
from test_watchdog_idle import AGENT  # noqa: E402

THRESHOLD = 600.0
CYCLE = 32.0
EVENTS = ("S_unk", "S_exe", "S_awh", "recover", "quiet_close", "restart", "write_fail", "delete_fail",
          "delete_raise", "new_exec", "vanish", "tick")
#: 起点を動かす出来事 (全並びの列挙には入れず、絞った列挙と乱択だけで使う)
SIGNAL_EVENTS = ("gap", "signal_lost", "signal_restored")
#: 次の観測のサイクルにだけ効く故障 (自分ではサイクルを回さない。回復・消滅と同じサイクルで失敗させるため)
MODIFIERS = {"write_fail": "write", "delete_fail": "delete", "delete_raise": "raise"}
SUPPRESS_KIND = {"S_unk": "process_unknown", "S_exe": "executing", "S_awh": "awaiting_human"}
REASON = {"process_unknown": "hard_idle_but_process_unknown", "executing": "hard_idle_but_executing",
          "awaiting_human": "hard_idle_but_awaiting_human"}
ALIVE = watchdog.CheckResult("alive", "active", 5.0, "idle_process", False)
QUIET = watchdog.CheckResult("terminate", "hard_idle", 3700.0, "idle_process", False)
#: 起点の基準時刻。card の started_at (floor になる) より後にして、floor がファイルを落とさないようにする
T0 = watchdog.parse_iso_epoch("2026-10-04T10:00:00Z") + 7200.0


def _exec_id(n: int) -> str:
    return f"ex-{n:032d}"


class Harness:
    """実物の notifier + 実物の台帳ファイルに出来事を流し、不変条件を毎ステップ確かめる。"""

    def __init__(self, base: Path, monkeypatch) -> None:
        self.base = base
        self.told = base / "registry" / "daemons" / "notified-state.json"
        self.told.parent.mkdir(parents=True, exist_ok=True)
        self.told.unlink(missing_ok=True)
        self.t = 1_000_000.0
        self.fail = {"write": False, "delete": False, "raise": False}
        self.pending = dict(self.fail)
        self.sent: list[str] = []
        self.calls: list[tuple[str, str]] = []          # 実際に送った通知の (fp, 台帳に記録できたか)
        real_record = lib_daemon_state.told_record
        monkeypatch.setattr(watchdog, "told_record", lambda path, key, entry, warn=None:
                            False if self.fail["write"] else real_record(path, key, entry, warn=warn))
        self.notifier = self._new_notifier()
        # 世界の状態
        self.exec_n = 1
        self.kind: str | None = None                    # 今の観測 (None = 見送りでない)
        self.present = False                            # Worker が監視されているか
        # モデルの記録
        self.episode = 0
        self.ep_key: tuple | None = None                # 進行中の episode の (exec, episode)
        self.proc_since: float | None = None            # プロセスが観測した episode の開始時刻
        self.notified: dict[tuple, int] = {}            # (exec, episode, kind) -> 回数
        self.unrecorded: set[tuple] = set()             # 送ったが台帳に書けなかった (exec, episode, kind)
        self.interval = 0                               # 無音区間の通し番号 (新しい episode / 起点の変化で増える)
        self.last_origin: int | None = None             # 進行中の episode で最後に観測した起点
        self.origin: int | None = None                  # 今のサイクルの起点
        self.told_fps: set[str] = set()                 # 台帳に記録できた通知の fp (起点つき。モデルの履歴)
        # 実物のシグナルファイル (registry/notifications/<agent>/)。mtime は T0 から 1000 秒ずつ進める
        shutil.rmtree(base / "registry" / "notifications", ignore_errors=True)
        self.notif_dir = base / "registry" / "notifications" / AGENT
        self.sig_files: list[tuple[Path, float]] = []   # (パス, mtime) 古い順
        self.sig_clock = T0
        self.sig_seq = 0
        self.lost: tuple[Path, float] | None = None
        # 初期状態 (§11-15-4): heartbeat (基準 − 100 秒) と通知 1 つ (基準)。signal_lost が最初から起点を後退させられる
        hb = base / "registry" / "heartbeats" / AGENT
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("")
        os.utime(hb, (T0 - 100.0, T0 - 100.0))
        self._add_signal(advance=0.0)

    # --- 実物の組み立て -------------------------------------------------
    def _new_notifier(self):
        nonce = watchdog.make_notify_once(
            self.base, send=lambda msg: self.sent.append(msg) or True, log=lambda m: None,
            now=lambda: self.t)

        def spy(key, fp, kind, slug, task, message):
            before = len(self.sent)
            before_led = dict(self._ledger())
            ok = nonce(key, fp, kind, slug, task, message)
            if len(self.sent) > before:
                recorded = self._ledger().get(key) == fp
                self.calls.append((fp, recorded))
            del before_led
            return ok

        def forget(key):
            if self.fail["raise"]:
                raise OSError("disk")
            if self.fail["delete"]:
                return False
            return lib_daemon_state.told_forget(self.told, key)

        return watchdog.SuppressedIdleNotifier(
            spy, forget, THRESHOLD, now=lambda: self.t,
            read_ledger=lambda: watchdog._told_entries(self.told))

    def _ledger(self) -> dict:
        return watchdog._told_entries(self.told) or {}

    def _monitor(self):
        card = {"worker": AGENT, "timeout": {"idle": 300, "max": 36000},
                "current_execution_id": _exec_id(self.exec_n), "started_at": "2026-10-04T10:00:00Z"}
        return watchdog.WorkerMonitor(task_id="t001", task_card=card, profiles=watchdog.PROFILES,
                                      repo_root=self.base)

    # --- シグナル (起点の元) --------------------------------------------------
    def _add_signal(self, advance: float = 1000.0) -> None:
        self.notif_dir.mkdir(parents=True, exist_ok=True)
        self.sig_clock += advance
        self.sig_seq += 1                               # 名前は使い回さない (消したファイルと衝突しない)
        path = self.notif_dir / f"n{self.sig_seq:04d}.json"
        path.write_text("{}")
        os.utime(path, (self.sig_clock, self.sig_clock))
        self.sig_files.append((path, self.sig_clock))

    def _lose_signal(self) -> None:
        """いちばん新しいシグナルファイルを実際に消す (無ければ何もしない)。"""
        if self.sig_files:
            self.lost = self.sig_files.pop()
            self.lost[0].unlink()

    def _restore_signal(self) -> None:
        """直前に消したファイルを元の (古い) mtime のまま戻す。"""
        if self.lost is not None:
            path, mtime = self.lost
            path.write_text("{}")
            os.utime(path, (mtime, mtime))
            self.sig_files.append((path, mtime))
            self.sig_files.sort(key=lambda pm: pm[1])
            self.lost = None

    # --- 1 ステップ ------------------------------------------------------
    def step(self, ev: str) -> None:
        if ev in MODIFIERS:
            self.pending[MODIFIERS[ev]] = True
            return
        if ev == "restart":
            self.notifier = self._new_notifier()
            self.proc_since = None
            self.t += CYCLE
            return
        if ev == "gap":                                # 観測の途切れ。その間に活動が入る
            self.t += 700.0
            self._add_signal()
            return
        self.fail, self.pending = self.pending, {"write": False, "delete": False, "raise": False}
        prev_kind, prev_present, prev_exec = self.kind, self.present, self.exec_n
        if ev in SUPPRESS_KIND:
            self.kind, self.present = SUPPRESS_KIND[ev], True
        elif ev == "recover":
            self.kind, self.present = None, True
            self._add_signal()
        elif ev == "quiet_close":
            self.kind, self.present = None, True
        elif ev == "new_exec":
            self.exec_n += 1
            self.present = True
            self._add_signal()
        elif ev == "signal_lost":
            self._lose_signal()
        elif ev == "signal_restored":
            self._restore_signal()
        elif ev == "vanish":
            self.present = False
        elif ev == "tick":
            self.t += THRESHOLD
        self.t += CYCLE

        suppressed = self.present and self.kind is not None
        self.origin = int(self._monitor()._last_activity_mtime()) if self.present else None
        # --- モデルの episode と無音区間 ---
        if suppressed:
            continuing = (prev_present and prev_kind is not None and prev_exec == self.exec_n)
            if not continuing:
                self.episode += 1
                self.ep_key = (self.exec_n, self.episode)
                self.proc_since = self.t
                self.interval += 1                    # 新しい episode は新しい区間 (quiet_close を含む)
            else:
                if self.proc_since is None:
                    self.proc_since = self.t          # 再起動 (プロセスが episode の途中から観測を始めた)
                if self.origin != self.last_origin:
                    self.interval += 1                # 起点が変わった = 別の無音区間
            self.last_origin = self.origin
        else:
            self.ep_key = None
            self.proc_since = None
            self.last_origin = None

        # --- 実物を 1 サイクル回す (I5: 例外が外に出ない) ---
        calls_before = len(self.calls)
        monitors = {}
        if self.present:
            mon = self._monitor()
            mon.idle_since = float(self.origin)       # check_detail が idle の計算に使う値 (floor 込み)
            monitors[AGENT] = mon
            detail = (watchdog.CheckResult("warn", REASON[self.kind], 3700.0, "unknown", False)
                      if self.kind else ALIVE)
            self.notifier.observe(mon, detail)
        elif prev_present and ev == "vanish":
            self.notifier.forget(self._monitor())     # forget_monitor 経由
        self.notifier.cycle(set(monitors))

        self._check(ev, suppressed, self.calls[calls_before:])

    def _ledger_keys(self):
        return [k for k in self._ledger()
                if (watchdog._split_suppression_key(k) or (None,))[0] == AGENT]

    # --- 不変条件 --------------------------------------------------------
    def _check(self, ev: str, suppressed: bool, new_calls) -> None:
        ledger = self._ledger()
        clean = not (self.fail["write"] or self.fail["delete"] or self.fail["raise"])
        if suppressed:
            for fp, recorded in new_calls:
                ident, _, rest = fp.rpartition(":")
                kind, _, origin = rest.partition("@")
                assert origin == str(self.origin), f"fp の起点が今の起点と違う: {fp} / {self.origin}"
                triple = (int(ident[3:]), self.ep_key[1], self.interval, kind)
                self.notified[triple] = self.notified.get(triple, 0) + 1
                if recorded:
                    self.told_fps.add(fp)
                else:
                    self.unrecorded.add(triple)
                # I1
                assert self.notified[triple] == 1 or triple in self.unrecorded, \
                    f"I1: {triple} が {self.notified[triple]} 回通知された"
            # I2
            triple = (self.exec_n, self.ep_key[1], self.interval, self.kind)
            lasted = self.t - self.proc_since
            fp_now = f"{_exec_id(self.exec_n)}:{self.kind}@{self.origin}"
            key_now = watchdog.suppression_key(AGENT, self.kind)
            # 例外 (§11-15-10): 今の起点つき fp が、前に記録できた通知のもので、台帳にまだ最新として残っている
            told_before = fp_now in self.told_fps and ledger.get(key_now) == fp_now
            if lasted >= THRESHOLD and clean and not told_before:
                assert self.notified.get(triple, 0) >= 1, \
                    f"I2: {triple} は {lasted:.0f}s 続いたのに通知が出ていない"
            if clean:
                for k in self._ledger_keys():
                    # I4: 古い試行のキーは残らない (しきい値後)
                    if lasted >= THRESHOLD:
                        assert str(ledger[k]).startswith(f"{_exec_id(self.exec_n)}:"), \
                            f"I4: 古い試行のキー {k}={ledger[k]} が残っている"
                    # I6 (§11-15-12 の決定 1): 他の理由のキーは今の起点以外の fp を持たない
                    if k != key_now:
                        assert str(ledger[k]).startswith(f"{_exec_id(self.exec_n)}:") \
                            and str(ledger[k]).endswith(f"@{self.origin}"), \
                            f"I6: 別の理由のキー {k}={ledger[k]} が今の起点 {self.origin} と違う"
        else:
            # I3
            if not (self.fail["delete"] or self.fail["raise"]):
                assert self._ledger_keys() == [], f"I3: 回復/消滅のあとにキーが残っている {self._ledger_keys()}"


def run_sequence(base: Path, monkeypatch, seq) -> None:
    h = Harness(base, monkeypatch)
    for i, ev in enumerate(seq):
        try:
            h.step(ev)
        except AssertionError as e:
            raise AssertionError(f"{e}\n並び: {list(seq[:i + 1])}") from None
        except Exception as e:  # I5
            raise AssertionError(f"I5: 例外が外に出た: {e!r}\n並び: {list(seq[:i + 1])}") from None


def _all_sequences(max_len: int):
    for n in range(1, max_len + 1):
        yield from itertools.product(EVENTS, repeat=n)


def test_every_sequence_up_to_length_5_keeps_the_invariants(tmp_path, monkeypatch):
    for seq in _all_sequences(5):
        run_sequence(tmp_path, monkeypatch, seq)


#: 全 11 種の長さ 6 は 177 万並びで重い。深い並びは「閉じる掃除の失敗 → 再発 → 通知」(長さ 6〜7) のように
#: 特定の出来事の組で起きるので、出来事を絞った全列挙で深さを稼ぐ (全並びの長さ 5 と合わせる)。
REDUCED_ALPHABETS = {
    "cleanup-and-relapse": (("S_exe", "tick", "recover", "delete_fail", "restart"), 7),
    "reasons-and-attempts": (("S_exe", "S_unk", "tick", "vanish", "new_exec", "write_fail"), 6),
    "failures-on-close": (("S_exe", "tick", "recover", "vanish", "delete_fail", "delete_raise"), 6),
    "quiet-close-and-relapse": (("S_exe", "tick", "quiet_close", "delete_fail", "restart"), 7),
    # 起点が動く並び (§11-15-4)。理由の切り替え (S_unk / S_exe) とシグナルの削除・復元・観測の途切れ
    "origin-moves": (("S_unk", "S_exe", "tick", "signal_lost", "signal_restored", "gap"), 6),
    "origin-moves-and-restart": (("S_unk", "tick", "recover", "quiet_close", "signal_lost", "signal_restored",
                                  "delete_fail", "restart"), 5),
}


@pytest.mark.parametrize("name", sorted(REDUCED_ALPHABETS))
def test_every_sequence_over_a_reduced_alphabet_keeps_the_invariants(tmp_path, monkeypatch, name):
    alphabet, depth = REDUCED_ALPHABETS[name]
    for n in range(5, depth + 1):          # 長さ 4 以下は全並びの列挙が覆っている
        for seq in itertools.product(alphabet, repeat=n):
            run_sequence(tmp_path, monkeypatch, seq)


@pytest.mark.parametrize("seed", range(6))
def test_seeded_random_sequences_of_length_6_to_14_keep_the_invariants(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    for _ in range(400):
        seq = tuple(rng.choice(EVENTS + SIGNAL_EVENTS) for _ in range(rng.randint(6, 14)))
        run_sequence(tmp_path, monkeypatch, seq)


def test_the_two_sequences_codex_found_are_covered_by_the_model(tmp_path, monkeypatch):
    """4 巡目の指摘の形 (理由が unknown → executing → unknown と往復) を名指しで 1 本。"""
    run_sequence(tmp_path, monkeypatch,
                 ("S_unk", "tick", "S_exe", "S_unk", "S_exe", "S_unk", "tick", "S_unk"))
    # 通知は理由ごとに 1 回ずつ (unknown と executing の 2 通だけ)
    h = Harness(tmp_path, monkeypatch)
    for ev in ("S_unk", "tick", "S_exe", "S_unk", "S_exe", "S_unk", "tick", "S_unk"):
        h.step(ev)
    assert len(h.calls) == 2


def test_a_deletion_that_recovers_after_a_relapse_does_not_renotify_every_cycle(tmp_path, monkeypatch):
    """5 巡目 (t017) の指摘 + t006 で見つかった連投。回復時の削除が失敗し、再発後 600 秒を過ぎてから削除が通ると、
    新しいキーを記録した plan で `_unsettled` が解けず、次のサイクルからそのキーを古いものとして消して再送し続けた。
    この並びは長さ 9 で、長さ 5 の全列挙と絞った列挙 (長さ 6〜7) の外にある。"""
    seq = ("S_unk", "tick", "delete_fail", "recover", "delete_fail", "S_unk", "tick", "S_unk", "S_unk")
    run_sequence(tmp_path, monkeypatch, seq)
    h = Harness(tmp_path, monkeypatch)
    for ev in seq:
        h.step(ev)
    assert len(h.calls) == 2                      # episode 1 と 2 で 1 通ずつ


def _run_and_count(tmp_path, monkeypatch, seq):
    run_sequence(tmp_path, monkeypatch, seq)
    h = Harness(tmp_path, monkeypatch)
    for ev in seq:
        h.step(ev)
    return h


def test_a_reason_switch_with_the_signal_lost_and_restored_does_not_silence_the_new_interval(tmp_path, monkeypatch):
    """§11-15-12 課題 1 の決定。理由 R1 を起点 T0 で通知 → R2 に切り替え → シグナルを消して起点が動く → 戻して
    起点が T0 に → R1 に戻る。別の理由の台帳 (R1@T0) を起点が動いたときに消すので、新しい区間の R1 は通知される。"""
    seq = ("recover", "S_unk", "tick", "S_exe", "signal_lost", "signal_restored", "S_unk", "tick")
    h = _run_and_count(tmp_path, monkeypatch, seq)
    fps = [fp.rpartition(":")[2] for fp, _ in h.calls]
    unk = [f for f in fps if f.startswith("process_unknown@")]
    # 起点 T0 の R1 が 2 通 (別の区間のもの)。途中の起点の変化で R1 の台帳が消えたので 2 通目は黙らない
    assert len(unk) == 2 and unk[0] == unk[1] and fps[-1] == unk[1]


def test_quiet_close_splits_the_interval_so_the_same_reason_may_notify_twice_at_the_same_origin(tmp_path, monkeypatch):
    """§11-15-12 課題 2 の決定。活動なしで閉じた episode (quiet_close) は起点が変わらなくても区間の区切り。
    台帳が正常に消えていれば同じ理由が 2 通出る (I1 は区間ごとに 1 回)。削除が失敗して残っていれば黙る (I2 の例外)。"""
    seq = ("recover", "S_unk", "tick", "quiet_close", "S_unk", "tick")
    h = _run_and_count(tmp_path, monkeypatch, seq)
    assert len(h.calls) == 2 and h.calls[0][0] == h.calls[1][0]
    seq = ("recover", "S_unk", "tick", "delete_fail", "quiet_close", "restart", "S_unk", "tick")
    h = _run_and_count(tmp_path, monkeypatch, seq)
    assert len(h.calls) == 1                      # 11-15-7 / 11-15-10 (i) の意図した動作


def test_the_fingerprint_carries_the_origin_and_a_changed_origin_notifies(tmp_path, monkeypatch):
    h = _run_and_count(tmp_path, monkeypatch, ("recover", "S_unk", "tick", "gap", "S_unk", "S_unk"))
    origins = [fp.rpartition("@")[2] for fp, _ in h.calls]
    assert len(origins) == 2 and origins[0] != origins[1]

#: §11-15-4 の名指しの並びと通知数 (列挙の長さを超える・意味を固定する)。通知数は実行して確かめた値
NAMED_SEQUENCES = {
    "gap-splits-the-interval": ("S_unk tick gap S_unk tick tick", 2),
    "restart-after-failed-cleanup-keeps-notifying": ("S_unk tick delete_fail recover restart S_unk tick S_unk tick S_unk", 2),
    "relapse-cleanup-fails-too": ("S_unk tick delete_fail recover restart delete_fail S_unk tick", 2),
    "vanish-without-activity-is-the-i2-exception": ("S_unk tick delete_fail vanish restart S_unk tick", 1),
    "signal-lost-once": ("S_unk tick signal_lost tick", 2),
    "signal-lost-does-not-repeat": ("S_unk tick signal_lost tick tick tick", 2),
    "signal-lost-and-restored": ("S_unk tick signal_lost signal_restored tick", 3),
    "signal-restored-does-not-repeat": ("S_unk tick signal_lost signal_restored tick tick tick", 3),
    "signal-lost-across-restart": ("S_unk tick restart signal_lost tick", 2),
    # 11-15-10 の既知の例外 (ii): 回復のシグナルを外部操作で消し、起点が初回通知の値に戻る。台帳が @T0 のままなので黙る
    "origin-returns-to-the-told-value": ("S_unk tick delete_fail recover restart S_unk signal_lost tick", 1),
    # 起点が初回通知と等しくならない形は通知が出る (I2 を緩めない)
    "origin-does-not-return": ("S_unk tick delete_fail recover restart S_unk gap S_unk tick", 2),
}


@pytest.mark.parametrize("name", sorted(NAMED_SEQUENCES))
def test_named_sequences_notify_exactly_as_designed(tmp_path, monkeypatch, name):
    text, expected = NAMED_SEQUENCES[name]
    h = _run_and_count(tmp_path, monkeypatch, tuple(text.split()))
    assert len(h.calls) == expected, f"{name}: 通知 {len(h.calls)} 通 (期待 {expected}): {[c[0] for c in h.calls]}"
