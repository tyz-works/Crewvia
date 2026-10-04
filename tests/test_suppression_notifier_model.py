"""見送り通知 (`SuppressedIdleNotifier`) の model-based 網羅テスト (PR #278 Codex P2 4 巡目 / t016、§11-13)。

1 巡目 (t009)・2 巡目 (t011)・4 巡目 (t016) の指摘は全部「状態の進み方の一部の経路だけを直した結果、
別の経路が残った」型だった。個別のシナリオを足すのをやめ、出来事の並びを全部流して不変条件を毎ステップ
確かめる。実物の `SuppressedIdleNotifier` と実物の台帳ファイル (tmp) を使う。

出来事 (restart と故障の 3 種を除き、1 つにつき watchdog の 1 サイクルが回る。サイクルの間隔は 32 秒):
  S_unk / S_exe / S_awh  見送りを観測 (process_unknown / executing / awaiting_human)
  recover                見送りでない観測 (alive)
  restart                watchdog 再起動 (SuppressedIdleNotifier を作り直す。プロセス内の状態を全部失う。
                         止まっている間は観測しない = 再起動後の最初の観測は次の出来事のサイクル)
  write_fail             次のサイクルの台帳への書き込みが失敗する (自分ではサイクルを回さない)
  delete_fail            次のサイクルの台帳の削除が失敗する (False を返す)
  delete_raise           次のサイクルの台帳の削除が例外を投げる
  new_exec               Worker の Execution ID が変わる
  vanish                 Worker が監視から外れる
  tick                   600 秒経つ

episode = ある Worker・ある Execution ID の見送りの連続区間 (見送りでない観測 / 消滅 / Execution ID の変化で終わる)。

不変条件:
  I1  同じ (Execution ID, episode, 理由) の通知は最大 1 回 (台帳への書き込みが失敗した通知は除く)
  I2  episode 内である理由の見送りが、プロセスが観測した範囲で 600 秒続き、台帳の書き込み・削除が成功していれば、
      その理由の通知が出ている
  I3  回復 (または消滅) のあと、削除の失敗が解けたサイクルには、その Worker の台帳キーは残っていない
  I4  新しい Execution ID は前の試行の episode も通知の記録も引き継がない (I2 が新しい episode で成り立つこと
      + 古い試行のキーがしきい値後の台帳に残らないこと)
  I5  台帳 I/O の失敗はサイクルを止めない (例外が外に出ない)

既知の穴 (I2 から外す。§11-13): 回復の掃除が失敗したまま watchdog が再起動し、同じ Execution ID で再発した
episode。古いキーが通知を黙らせる (掃除が成功するまで)。
"""

from __future__ import annotations

import itertools
import random
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
EVENTS = ("S_unk", "S_exe", "S_awh", "recover", "restart", "write_fail", "delete_fail",
          "delete_raise", "new_exec", "vanish", "tick")
#: 次の観測のサイクルにだけ効く故障 (自分ではサイクルを回さない。回復・消滅と同じサイクルで失敗させるため)
MODIFIERS = {"write_fail": "write", "delete_fail": "delete", "delete_raise": "raise"}
SUPPRESS_KIND = {"S_unk": "process_unknown", "S_exe": "executing", "S_awh": "awaiting_human"}
REASON = {"process_unknown": "hard_idle_but_process_unknown", "executing": "hard_idle_but_executing",
          "awaiting_human": "hard_idle_but_awaiting_human"}
ALIVE = watchdog.CheckResult("alive", "active", 5.0, "idle_process", False)


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
        self.blocked_episodes: set[tuple] = set()       # 既知の穴 (回復の掃除失敗 + 再起動 + 再発)
        self.restart_since_close = False

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

    # --- 1 ステップ ------------------------------------------------------
    def step(self, ev: str) -> None:
        if ev in MODIFIERS:
            self.pending[MODIFIERS[ev]] = True
            return
        if ev == "restart":
            self.notifier = self._new_notifier()
            self.proc_since = None
            self.restart_since_close = True
            self.t += CYCLE
            return
        self.fail, self.pending = self.pending, {"write": False, "delete": False, "raise": False}
        prev_kind, prev_present, prev_exec = self.kind, self.present, self.exec_n
        if ev in SUPPRESS_KIND:
            self.kind, self.present = SUPPRESS_KIND[ev], True
        elif ev == "recover":
            self.kind, self.present = None, True
        elif ev == "new_exec":
            self.exec_n += 1
            self.present = True
        elif ev == "vanish":
            self.present = False
        elif ev == "tick":
            self.t += THRESHOLD
        self.t += CYCLE

        suppressed = self.present and self.kind is not None
        # --- モデルの episode ---
        if suppressed:
            key = (self.exec_n, self.episode + 1)
            continuing = (prev_present and prev_kind is not None and prev_exec == self.exec_n)
            if not continuing:
                self.episode += 1
                key = (self.exec_n, self.episode)
                self.ep_key = key
                self.proc_since = self.t
                ident = _exec_id(self.exec_n)
                stale_same_exec = any(str(v).startswith(f"{ident}:") for k, v in self._ledger().items()
                                      if (watchdog._split_suppression_key(k) or (None,))[0] == AGENT)
                if stale_same_exec and self.restart_since_close:
                    self.blocked_episodes.add(key)
                self.restart_since_close = False
            elif self.proc_since is None:
                self.proc_since = self.t              # 再起動 (プロセスが episode の途中から観測を始めた)
        else:
            self.ep_key = None
            self.proc_since = None

        # --- 実物を 1 サイクル回す (I5: 例外が外に出ない) ---
        calls_before = len(self.calls)
        monitors = {}
        if self.present:
            mon = self._monitor()
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
        if suppressed:
            for fp, recorded in new_calls:
                exec_s, _, kind = fp.rpartition(":")
                triple = (int(exec_s[3:]), self.ep_key[1], kind)
                self.notified[triple] = self.notified.get(triple, 0) + 1
                if not recorded:
                    self.unrecorded.add(triple)
                # I1
                assert self.notified[triple] == 1 or triple in self.unrecorded, \
                    f"I1: {triple} が {self.notified[triple]} 回通知された"
            # I2
            triple = (self.exec_n, self.ep_key[1], self.kind)
            lasted = self.t - self.proc_since
            if (lasted >= THRESHOLD and not self.fail["write"] and not self.fail["delete"] and not self.fail["raise"]
                    and self.ep_key not in self.blocked_episodes):
                assert self.notified.get(triple, 0) >= 1, \
                    f"I2: {triple} は {lasted:.0f}s 続いたのに通知が出ていない"
            # I4: しきい値を過ぎた台帳に古い試行のキーが残らない
            if lasted >= THRESHOLD and not (self.fail["delete"] or self.fail["raise"] or self.fail["write"]):
                for k in self._ledger_keys():
                    assert str(ledger[k]).startswith(f"{_exec_id(self.exec_n)}:") \
                        or self.ep_key in self.blocked_episodes, \
                        f"I4: 古い試行のキー {k}={ledger[k]} が残っている"
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
        seq = tuple(rng.choice(EVENTS) for _ in range(rng.randint(6, 14)))
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
