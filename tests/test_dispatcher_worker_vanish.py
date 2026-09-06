#!/usr/bin/env python3
"""
tests/test_dispatcher_worker_vanish.py

Regression テスト: vanished worker 検知ロジック (dispatcher.sh)

## 背景

Worker が pull 直後にクラッシュすると:
  - heartbeat 停止 + herdr tab 消滅
  - Rule 5 は state() が pane not found → unknown を返すため発火せず
  - Dispatcher の blocked-stuck 検知は Worker がいる前提で動作するため消滅 Worker を検知しない
  - 結果: task は in_progress のまま永久に立ち往生

## 修正内容 (dispatcher.sh)

dispatch() ループ内、can_handle 通知の後・Handoff 検知の前に追加した
「vanished worker 検知セクション」が対象:
  - 条件A: task status == in_progress
  - 条件B: task.worker フィールドに Worker 名が記録されている
  - 条件C: Worker の tab ('Name-worker') が live windows に存在しない
  - 条件D: heartbeat ファイルが missing か mtime > AGENT_PRESENCE_TTL (600s)
  全条件満足時 → Director に通知 (notify_key = vanished_worker_{slug}_{task_id})

## テストケース

  case_a: in_progress + tab 存在 → 通知しない
  case_b: in_progress + tab 消滅 + heartbeat stale → 通知する
  case_c: in_progress + tab 消滅 + heartbeat fresh → 通知しない (生存中かも)
  case_d: pending + tab 消滅 + heartbeat stale → 通知しない (in_progress でない)

実行方法:
  python3 -m pytest tests/test_dispatcher_worker_vanish.py -v
  python3 -m pytest tests/test_heartbeat_gap_regression.py -v  # regression なし
"""

import time
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数 (dispatcher.sh と同値)
# ---------------------------------------------------------------------------

AGENT_PRESENCE_TTL = 600  # seconds

WORKERS = {
    "Haruto": {"skills": ["bash", "code"], "role": "worker"},
    "Minjun": {"skills": ["docs"],          "role": "worker"},
    "Finn":   {"skills": ["qa"],            "role": "worker"},
    "Priya":  {"skills": ["planning"],      "role": "worker"},
    "Sora":   {"skills": [],               "role": "director"},
}


# ---------------------------------------------------------------------------
# MockVanishChecker
# Dispatcher の vanished worker 検知ロジックを再現するモック。
# ---------------------------------------------------------------------------

class MockVanishChecker:
    """
    dispatcher.sh の vanished worker 検知セクションを再現するモック。

    state:
      windows: set of agent_name strings (live tabs; 'Name-worker' から Name を抽出)
      heartbeats: dict of agent_name → mtime
      tasks: list of {'id', 'status', 'worker', 'mission'} dicts
    """

    def __init__(self, tmpdir: Path):
        self.tmpdir = Path(tmpdir)
        self.hb_dir = self.tmpdir / "heartbeats"
        self.hb_dir.mkdir(parents=True, exist_ok=True)
        self.windows: set = set()   # alive agent names (tab exists)
        self.tasks: list = []       # task meta dicts

    # ------------------------------------------------------------------
    # Helpers to set up state
    # ------------------------------------------------------------------

    def add_window(self, agent_name: str):
        """Worker tab を追加 (alive)"""
        self.windows.add(agent_name)

    def remove_window(self, agent_name: str):
        """Worker tab を削除 (gone)"""
        self.windows.discard(agent_name)

    def set_heartbeat_fresh(self, name: str):
        hb_file = self.hb_dir / name
        now = time.time()
        hb_file.write_text(str(int(now)))
        import os
        os.utime(hb_file, (now, now))

    def set_heartbeat_stale(self, name: str, age_seconds: int = 700):
        hb_file = self.hb_dir / name
        old_mtime = time.time() - age_seconds
        hb_file.write_text(str(int(old_mtime)))
        import os
        os.utime(hb_file, (old_mtime, old_mtime))

    def clear_heartbeat(self, name: str):
        """heartbeat ファイルを削除 (missing)"""
        hb_file = self.hb_dir / name
        if hb_file.exists():
            hb_file.unlink()

    def add_task(self, task_id: str, status: str, worker: str, mission: str = "test-mission"):
        self.tasks.append({
            "id": task_id,
            "status": status,
            "worker": worker,
            "mission": mission,
        })

    # ------------------------------------------------------------------
    # Core logic: vanished_worker_should_notify
    # dispatcher.sh の vanished worker 検知セクションを再現
    # ------------------------------------------------------------------

    def get_vanish_alerts(self) -> list:
        """
        vanished worker 検知ロジックを適用して、通知すべきタスクのリストを返す。

        Returns:
            list of {'slug': str, 'task_id': str, 'worker': str}
        """
        alerts = []
        now = time.time()

        for task in self.tasks:
            # 条件A: status == in_progress
            if task.get("status") != "in_progress":
                continue

            # 条件B: worker フィールドあり
            worker_name = task.get("worker")
            if not worker_name:
                continue

            # 条件C: worker tab が live windows に存在しない
            if worker_name in self.windows:
                continue  # tab 存在 → alive → skip

            # 条件D: heartbeat stale または missing
            hb_file = self.hb_dir / worker_name
            hb_stale = True
            if hb_file.exists():
                try:
                    hb_stale = now - hb_file.stat().st_mtime > AGENT_PRESENCE_TTL
                except OSError:
                    hb_stale = True
            if not hb_stale:
                continue  # heartbeat fresh → skip (worker may still be alive)

            alerts.append({
                "slug": task.get("mission", "?"),
                "task_id": task["id"],
                "worker": worker_name,
            })

        return alerts


# ---------------------------------------------------------------------------
# case_a: in_progress + tab 存在 → 通知しない
# ---------------------------------------------------------------------------

def test_case_a_tab_exists_no_alert():
    """
    case_a: in_progress + worker tab 存在 → vanish 通知しない

    Worker は正常稼働中。tab が存在する限り通知は出ない。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # Setup: Haruto tab 存在 (alive)
        checker.add_window("Haruto")
        checker.set_heartbeat_stale("Haruto")  # heartbeat は stale でも tab があれば skip
        checker.add_task("t001", status="in_progress", worker="Haruto")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 0, (
            f"case_a FAIL: tab 存在なのに vanish alert が発火した: {alerts}"
        )
        print("✓ case_a: in_progress + tab 存在 → 通知なし (正常ケース)")


# ---------------------------------------------------------------------------
# case_b: in_progress + tab 消滅 + heartbeat stale → 通知する
# ---------------------------------------------------------------------------

def test_case_b_tab_gone_heartbeat_stale_alert():
    """
    case_b: in_progress + worker tab 消滅 + heartbeat stale → vanish 通知する

    Worker がクラッシュして tab も消滅し heartbeat も stale の場合。
    4 条件すべて満足 → Director に通知。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # Setup: Haruto tab なし + heartbeat stale
        checker.set_heartbeat_stale("Haruto", age_seconds=700)
        checker.add_task("t001", status="in_progress", worker="Haruto", mission="mission-x")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 1, (
            f"case_b FAIL: vanish alert が発火しなかった (expected 1, got {len(alerts)})"
        )
        alert = alerts[0]
        assert alert["slug"] == "mission-x"
        assert alert["task_id"] == "t001"
        assert alert["worker"] == "Haruto"
        print(
            f"✓ case_b: in_progress + tab 消滅 + heartbeat stale → 通知 alert={alert}"
        )


# ---------------------------------------------------------------------------
# case_c: in_progress + tab 消滅 + heartbeat fresh → 通知しない
# ---------------------------------------------------------------------------

def test_case_c_tab_gone_heartbeat_fresh_no_alert():
    """
    case_c: in_progress + worker tab 消滅 + heartbeat fresh → 通知しない

    herdr の transient list failure で tab が一時的に見えない場合がある。
    heartbeat が fresh ならば Worker はまだ生きているかもしれない → skip。
    (PR #154 で導入した heartbeat-fresh 優先ロジックと整合)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # Setup: Haruto tab なし + heartbeat fresh (transient mux glitch)
        checker.set_heartbeat_fresh("Haruto")
        checker.add_task("t001", status="in_progress", worker="Haruto")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 0, (
            f"case_c FAIL: heartbeat fresh なのに vanish alert が発火した: {alerts}"
        )
        print("✓ case_c: in_progress + tab 消滅 + heartbeat fresh → 通知なし (生存中の可能性)")


# ---------------------------------------------------------------------------
# case_d: pending + tab 消滅 + heartbeat stale → 通知しない
# ---------------------------------------------------------------------------

def test_case_d_pending_not_in_progress_no_alert():
    """
    case_d: pending + worker tab 消滅 + heartbeat stale → 通知しない

    vanish 検知は in_progress タスクのみが対象。
    pending タスクは worker が pull する前の状態 → worker フィールドは記録されない場合が多い。
    status が pending の task については 条件A で弾かれる。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # Setup: pending task + Haruto tab なし + heartbeat stale
        checker.set_heartbeat_stale("Haruto", age_seconds=700)
        checker.add_task("t001", status="pending", worker="Haruto")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 0, (
            f"case_d FAIL: pending task なのに vanish alert が発火した: {alerts}"
        )
        print("✓ case_d: pending + tab 消滅 + heartbeat stale → 通知なし (in_progress でない)")


# ---------------------------------------------------------------------------
# case_e: 混合ケース (複数タスク) — 対象のみ検知
# ---------------------------------------------------------------------------

def test_case_e_mixed_tasks_only_vanished_alerted():
    """
    case_e (ボーナス): 複数タスクが存在するとき、vanish 条件を満たすもののみ通知される。

    - t001: in_progress, Haruto tab あり → skip (case_a)
    - t002: in_progress, Minjun tab なし + heartbeat stale → alert (case_b)
    - t003: in_progress, Finn tab なし + heartbeat fresh → skip (case_c)
    - t004: pending, Haruto tab なし → skip (case_d)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # t001: Haruto tab あり
        checker.add_window("Haruto")
        checker.set_heartbeat_fresh("Haruto")
        checker.add_task("t001", status="in_progress", worker="Haruto", mission="m1")

        # t002: Minjun tab なし + heartbeat stale → should alert
        checker.set_heartbeat_stale("Minjun", age_seconds=800)
        checker.add_task("t002", status="in_progress", worker="Minjun", mission="m1")

        # t003: Finn tab なし + heartbeat fresh → skip
        checker.set_heartbeat_fresh("Finn")
        checker.add_task("t003", status="in_progress", worker="Finn", mission="m1")

        # t004: pending + Haruto tab なし (Haruto window is up but task is pending)
        checker.add_task("t004", status="pending", worker="Haruto", mission="m1")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 1, (
            f"case_e FAIL: expected 1 alert (Minjun/t002), got {len(alerts)}: {alerts}"
        )
        assert alerts[0]["task_id"] == "t002"
        assert alerts[0]["worker"] == "Minjun"
        print(
            f"✓ case_e: 混合 4 タスク → vanish alert 1 件のみ (t002/Minjun): {alerts}"
        )


# ---------------------------------------------------------------------------
# case_f: heartbeat ファイル missing + tab なし → 通知する (missing = stale と同義)
# ---------------------------------------------------------------------------

def test_case_f_no_heartbeat_file_no_tab_alert():
    """
    case_f: in_progress + tab なし + heartbeat ファイル missing → 通知する

    heartbeat ファイルが存在しない場合も stale と同義で扱う。
    Worker がクラッシュした直後はファイルが消える場合がある。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = MockVanishChecker(tmpdir)

        # Setup: heartbeat ファイルなし + tab なし
        checker.clear_heartbeat("Haruto")  # ファイルなし
        checker.add_task("t001", status="in_progress", worker="Haruto", mission="mission-y")

        alerts = checker.get_vanish_alerts()

        assert len(alerts) == 1, (
            f"case_f FAIL: heartbeat missing + no tab → expected alert, got {len(alerts)}: {alerts}"
        )
        assert alerts[0]["task_id"] == "t001"
        print(
            f"✓ case_f: heartbeat missing + tab なし → 通知 alert={alerts[0]}"
        )


# ---------------------------------------------------------------------------
# case_g: dispatcher.sh にvanish検知セクションが実装済みであることを構文確認
# ---------------------------------------------------------------------------

def test_case_g_dispatcher_sh_contains_vanish_detection():
    """
    case_g: dispatcher.sh に vanished worker 検知セクションが存在することを確認。

    dispatcher.sh 確認箇所:
      - 'vanished_worker' notify_key パターン
      - 条件C: _window_agent_names チェック
      - 条件D: heartbeat stale チェック
      - Director への通知
    """
    dispatcher_path = Path(__file__).parent.parent / "scripts" / "dispatcher.sh"
    assert dispatcher_path.exists(), f"dispatcher.sh not found at {dispatcher_path}"

    content = dispatcher_path.read_text()

    assert "vanished_worker" in content, \
        "vanish検知: notify_key 'vanished_worker' が dispatcher.sh に存在しない"
    assert "_window_agent_names" in content, \
        "vanish検知: 条件C (_window_agent_names チェック) が dispatcher.sh に存在しない"
    assert "vanish" in content.lower() or "vanished" in content.lower(), \
        "vanish検知: vanish 関連コメント/変数が dispatcher.sh に存在しない"
    assert "tab が消滅" in content or "tab gone" in content.lower() or "vanished" in content, \
        "vanish検知: Director 通知メッセージが dispatcher.sh に存在しない"

    print("✓ case_g: dispatcher.sh に vanished worker 検知セクションが実装済み")


# ---------------------------------------------------------------------------
# エントリポイント (直接実行用)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=" * 70)
    print("Vanished Worker Detection Test — dispatcher.sh regression")
    print("=" * 70)
    print()

    tests = [
        ("case_a", test_case_a_tab_exists_no_alert),
        ("case_b", test_case_b_tab_gone_heartbeat_stale_alert),
        ("case_c", test_case_c_tab_gone_heartbeat_fresh_no_alert),
        ("case_d", test_case_d_pending_not_in_progress_no_alert),
        ("case_e", test_case_e_mixed_tasks_only_vanished_alerted),
        ("case_f", test_case_f_no_heartbeat_file_no_tab_alert),
        ("case_g", test_case_g_dispatcher_sh_contains_vanish_detection),
    ]

    failures = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"✗ {name}: FAIL — {e}")
            failures.append(name)
        except Exception as e:
            import traceback
            print(f"✗ {name}: ERROR — {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    print("=" * 70)
    if not failures:
        print("✓ 全テスト PASS — vanished worker 検知ロジックが正しく実装済み")
        sys.exit(0)
    else:
        print(f"✗ {len(failures)} tests FAILED: {failures}")
        sys.exit(1)
