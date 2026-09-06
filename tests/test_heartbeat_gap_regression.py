#!/usr/bin/env python3
"""
tests/test_heartbeat_gap_regression.py

Regression テスト: heartbeat gap バグ (OBS-1) の実証

## 背景

dispatcher.sh の `_alive_workers` 構築 (Lines 869-887) は
heartbeat ファイルの mtime のみで Worker 生存を判定する。

Worker が alive (herdr window 存在) でも、heartbeat が AGENT_PRESENCE_TTL (600s)
を超えると `_alive_workers` から除外され `can_handle = False` → 誤通知が発火する。

これを「heartbeat gap バグ」と呼ぶ。

## 修正方針 (次 mission で実装)

`_alive_workers` の判定を「heartbeat fresh OR window exists」の OR 条件にする。

## テストシナリオ

- test_A: heartbeat fresh + window あり → alive ✓ (正常ケース)
- test_B: heartbeat stale (>600s) + window あり → 現在の実装では dead 誤判定 (バグ実証)
  → このテストは FAIL する (バグの実証が目的)
- test_C: heartbeat stale + window なし → dead ✓ (真に dead)
- test_D: heartbeat fresh + window なし (transient) → alive ✓ (PR#154 修正対象の元バグ)

実行方法:
  python3 -m pytest tests/test_heartbeat_gap_regression.py -v
  # test_B が FAIL することを確認

dispatcher.sh 確認箇所:
  Line 481:    AGENT_PRESENCE_TTL = 600
  Lines 869-887: _alive_workers 構築 (heartbeat mtime のみ)
  Lines 1060-1065: can_handle ロジック (_alive_workers 使用)
  Line 542:    publish_agents での AGENT_PRESENCE_TTL 使用
"""

import sys
import os
import time
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

AGENT_PRESENCE_TTL = 600  # seconds (dispatcher.sh Line 481 と同値)

WORKERS = {
    "Haruto": {"skills": ["bash", "code"], "role": "worker"},
    "Minjun": {"skills": ["docs"],          "role": "worker"},
    "Finn":   {"skills": ["qa"],            "role": "worker"},
    "Priya":  {"skills": ["planning"],      "role": "worker"},
    "Sora":   {"skills": [],               "role": "director"},
}


# ---------------------------------------------------------------------------
# MockDispatcherWithWindows
# ---------------------------------------------------------------------------

class MockDispatcherWithWindows:
    """
    dispatcher.sh の _alive_workers / can_handle を再現するモック。

    windows: Worker name → bool のマッピング (herdr window 存在を表す)
    heartbeat: Worker name → mtime のマッピング

    can_handle_current(): 現在の実装 (heartbeat mtime のみ)
    can_handle_fixed():   修正案 (heartbeat fresh OR window exists)
    """

    def __init__(self, tmpdir: Path):
        self.tmpdir = Path(tmpdir)
        self.hb_dir = self.tmpdir / "heartbeats"
        self.hb_dir.mkdir(parents=True, exist_ok=True)
        # windows: worker name → True/False (herdr window の有無)
        self.windows: dict = {}

    # ------------------------------------------------------------------
    # Worker state helpers
    # ------------------------------------------------------------------

    def set_heartbeat_fresh(self, name: str):
        """heartbeat を fresh に設定 (mtime = now)"""
        hb_file = self.hb_dir / name
        hb_file.write_text(str(int(time.time())))
        # mtime を現在時刻に設定 (write_text は通常 now だが明示的に)
        now = time.time()
        os.utime(hb_file, (now, now))

    def set_heartbeat_stale(self, name: str, age_seconds: int = 700):
        """heartbeat を stale に設定 (mtime = now - age_seconds)"""
        hb_file = self.hb_dir / name
        old_mtime = time.time() - age_seconds
        hb_file.write_text(str(int(old_mtime)))
        os.utime(hb_file, (old_mtime, old_mtime))

    def set_window_exists(self, name: str, exists: bool):
        """herdr window の有無を設定"""
        self.windows[name] = exists

    # ------------------------------------------------------------------
    # _alive_workers: 現在の実装 (heartbeat mtime のみ)
    # dispatcher.sh Lines 869-887 の再現
    # ------------------------------------------------------------------

    def _get_alive_workers_current(self) -> set:
        """
        現在の実装 (dispatcher.sh Lines 869-887):
        heartbeat ファイルの mtime が AGENT_PRESENCE_TTL 以内の Worker のみ alive。
        window の有無は考慮しない。
        """
        alive = set()
        now = time.time()
        if self.hb_dir.exists():
            for hb_file in self.hb_dir.iterdir():
                if hb_file.is_file() and not hb_file.name.startswith("."):
                    try:
                        if now - hb_file.stat().st_mtime <= AGENT_PRESENCE_TTL:
                            alive.add(hb_file.name)
                    except OSError:
                        pass
        return alive

    # ------------------------------------------------------------------
    # _alive_workers: 修正案 (heartbeat fresh OR window exists)
    # ------------------------------------------------------------------

    def _get_alive_workers_fixed(self) -> set:
        """
        修正案: heartbeat fresh OR window exists の OR 条件。
        heartbeat gap バグ (OBS-1) を解消する。
        """
        alive = set()
        now = time.time()
        if self.hb_dir.exists():
            for hb_file in self.hb_dir.iterdir():
                if hb_file.is_file() and not hb_file.name.startswith("."):
                    try:
                        hb_fresh = now - hb_file.stat().st_mtime <= AGENT_PRESENCE_TTL
                        window_exists = self.windows.get(hb_file.name, False)
                        if hb_fresh or window_exists:
                            alive.add(hb_file.name)
                    except OSError:
                        pass
        return alive

    # ------------------------------------------------------------------
    # can_handle: 現在の実装 (dispatcher.sh Lines 1060-1065)
    # ------------------------------------------------------------------

    def can_handle_current(self, task_skills: set) -> bool:
        """
        現在の実装 (dispatcher.sh Lines 1060-1065):
        _alive_workers (heartbeat のみ) でスキルが match するか判定。
        """
        alive = self._get_alive_workers_current()
        return any(
            task_skills.issubset(set((WORKERS.get(name) or {}).get("skills") or []))
            for name in alive
            if (WORKERS.get(name) or {}).get("role", "worker") == "worker"
        )

    def can_handle_fixed(self, task_skills: set) -> bool:
        """
        修正案: _alive_workers (heartbeat OR window) でスキルが match するか判定。
        """
        alive = self._get_alive_workers_fixed()
        return any(
            task_skills.issubset(set((WORKERS.get(name) or {}).get("skills") or []))
            for name in alive
            if (WORKERS.get(name) or {}).get("role", "worker") == "worker"
        )


# ---------------------------------------------------------------------------
# test_A: heartbeat fresh + window あり → alive ✓ (正常ケース)
# ---------------------------------------------------------------------------

def test_A_heartbeat_fresh_window_exists_alive():
    """
    test_A: heartbeat fresh + window あり → alive ✓

    両方の条件が揃っている正常ケース。
    現在の実装でも修正案でも can_handle = True になる。

    dispatcher.sh 参照:
      Line 481: AGENT_PRESENCE_TTL = 600
      Lines 869-887: heartbeat mtime が AGENT_PRESENCE_TTL 以内 → alive
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: heartbeat fresh + window あり
        disp.set_heartbeat_fresh("Haruto")
        disp.set_window_exists("Haruto", True)

        task_skills = {"bash", "code"}

        # 現在の実装: heartbeat fresh → alive → can_handle = True
        result_current = disp.can_handle_current(task_skills)
        alive_current = disp._get_alive_workers_current()

        # 修正案: heartbeat fresh OR window → alive → can_handle = True
        result_fixed = disp.can_handle_fixed(task_skills)
        alive_fixed = disp._get_alive_workers_fixed()

        assert result_current is True, (
            f"test_A FAIL: heartbeat fresh + window exists → current: "
            f"can_handle={result_current}, alive={alive_current}"
        )
        assert result_fixed is True, (
            f"test_A FAIL: heartbeat fresh + window exists → fixed: "
            f"can_handle={result_fixed}, alive={alive_fixed}"
        )

        print(
            f"✓ test_A: heartbeat_fresh=True, window_exists=True "
            f"→ current can_handle={result_current}, fixed can_handle={result_fixed}"
        )


# ---------------------------------------------------------------------------
# test_B: heartbeat stale + window あり → バグ実証 (FAIL 期待)
# ---------------------------------------------------------------------------

def test_B_heartbeat_stale_window_exists_bug():
    """
    test_B: heartbeat stale (>600s) + window あり → 現在の実装では dead 誤判定

    *** このテストは FAIL することが目的 ***

    Worker は alive (herdr window が存在する) にもかかわらず、
    heartbeat が 600s 超えたために `_alive_workers` から除外され、
    `can_handle = False` → 誤通知が発火するバグを実証する。

    dispatcher.sh 参照:
      Lines 869-887: heartbeat mtime > AGENT_PRESENCE_TTL → _alive_workers に含まれない
      Lines 1060-1065: _alive_workers が空 → can_handle = False

    期待される挙動 (バグが修正された場合):
      window が存在するので alive → can_handle = True
    現在の挙動 (バグあり):
      heartbeat stale → alive なし → can_handle = False ← 誤り
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: heartbeat stale (700s 前) + window あり (実際には alive)
        disp.set_heartbeat_stale("Haruto", age_seconds=700)
        disp.set_window_exists("Haruto", True)

        task_skills = {"bash", "code"}

        # 現在の実装: heartbeat stale → _alive_workers 空 → can_handle = False
        result_current = disp.can_handle_current(task_skills)
        alive_current = disp._get_alive_workers_current()

        # 修正案: window あり → alive → can_handle = True
        result_fixed = disp.can_handle_fixed(task_skills)
        alive_fixed = disp._get_alive_workers_fixed()

        print(
            f"  test_B: heartbeat_stale=True (700s), window_exists=True "
            f"→ current can_handle={result_current} (alive={alive_current}), "
            f"fixed can_handle={result_fixed} (alive={alive_fixed})"
        )

        # 修正案では True になることを確認 (参考)
        assert result_fixed is True, (
            f"test_B sanity check: fixed logic should return True "
            f"(window exists), but got {result_fixed}"
        )

        # 現在の実装では True を期待する (= Worker alive なので)
        # ← しかし実際は False → このアサーションが FAIL する (バグ実証)
        assert result_current is True, (
            f"BUG CONFIRMED: heartbeat stale (700s) + window exists "
            f"→ current can_handle={result_current} (expected True, got False). "
            f"alive_workers={alive_current}. "
            f"Worker IS running (window exists) but heartbeat gap causes false 'dead' detection. "
            f"Fix: use 'heartbeat fresh OR window exists' OR condition."
        )


# ---------------------------------------------------------------------------
# test_C: heartbeat stale + window なし → dead ✓ (真に dead)
# ---------------------------------------------------------------------------

def test_C_heartbeat_stale_no_window_dead():
    """
    test_C: heartbeat stale + window なし → dead ✓

    heartbeat が stale で window も存在しない場合、Worker は本当に dead。
    現在の実装・修正案ともに can_handle = False → 通知は正当。

    dispatcher.sh 参照:
      Lines 869-887: heartbeat stale → _alive_workers に含まれない
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: heartbeat stale + window なし (本当に dead)
        disp.set_heartbeat_stale("Haruto", age_seconds=700)
        disp.set_window_exists("Haruto", False)

        task_skills = {"bash", "code"}

        result_current = disp.can_handle_current(task_skills)
        alive_current = disp._get_alive_workers_current()

        result_fixed = disp.can_handle_fixed(task_skills)
        alive_fixed = disp._get_alive_workers_fixed()

        # 両方とも False (正当な dead 判定)
        assert result_current is False, (
            f"test_C FAIL: heartbeat stale + no window → current should be False, "
            f"got {result_current}, alive={alive_current}"
        )
        assert result_fixed is False, (
            f"test_C FAIL: heartbeat stale + no window → fixed should be False, "
            f"got {result_fixed}, alive={alive_fixed}"
        )

        print(
            f"✓ test_C: heartbeat_stale=True (700s), window_exists=False "
            f"→ current can_handle={result_current}, fixed can_handle={result_fixed} "
            f"(both correctly: dead)"
        )


# ---------------------------------------------------------------------------
# test_D: heartbeat fresh + window なし → alive ✓ (PR#154 修正対象の元バグ)
# ---------------------------------------------------------------------------

def test_D_heartbeat_fresh_no_window_alive():
    """
    test_D: heartbeat fresh + window なし (transient) → alive ✓

    herdr が pane_list を transient で返さない場合でも、
    heartbeat が fresh であれば Worker は alive とみなす。
    これは PR#154 で修正した元バグ (window のみで判定していた旧実装) への対処。

    dispatcher.sh 参照:
      Lines 869-887: heartbeat fresh → _alive_workers に追加
      コメント: "herdr pane_list can transiently return [] ...
               Heartbeat files are written by Workers every HEARTBEAT_INTERVAL seconds
               and are independent of herdr's runtime state."
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: heartbeat fresh + window なし (herdr の transient failure)
        disp.set_heartbeat_fresh("Haruto")
        disp.set_window_exists("Haruto", False)

        task_skills = {"bash", "code"}

        result_current = disp.can_handle_current(task_skills)
        alive_current = disp._get_alive_workers_current()

        result_fixed = disp.can_handle_fixed(task_skills)
        alive_fixed = disp._get_alive_workers_fixed()

        # 現在の実装: heartbeat fresh → alive → can_handle = True ✓
        assert result_current is True, (
            f"test_D FAIL: heartbeat fresh + no window → current should be True "
            f"(PR#154 fix), got {result_current}, alive={alive_current}"
        )
        # 修正案も: heartbeat fresh → alive → can_handle = True ✓
        assert result_fixed is True, (
            f"test_D FAIL: heartbeat fresh + no window → fixed should be True, "
            f"got {result_fixed}, alive={alive_fixed}"
        )

        print(
            f"✓ test_D: heartbeat_fresh=True, window_exists=False "
            f"→ current can_handle={result_current}, fixed can_handle={result_fixed} "
            f"(both correctly: alive via heartbeat)"
        )


# ---------------------------------------------------------------------------
# エントリポイント (直接実行用)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("Heartbeat Gap Regression Test (OBS-1)")
    print("dispatcher.sh Lines: 481, 869-887, 1060-1065, 542")
    print("=" * 70)
    print()
    print("期待される結果:")
    print("  test_A: PASS (heartbeat fresh + window → alive ✓)")
    print("  test_B: FAIL (heartbeat stale + window → current: dead 誤判定, BUG)")
    print("  test_C: PASS (heartbeat stale + no window → dead ✓)")
    print("  test_D: PASS (heartbeat fresh + no window → alive via heartbeat ✓)")
    print()

    results = {}
    tests = [
        ("test_A", test_A_heartbeat_fresh_window_exists_alive),
        ("test_B", test_B_heartbeat_stale_window_exists_bug),
        ("test_C", test_C_heartbeat_stale_no_window_dead),
        ("test_D", test_D_heartbeat_fresh_no_window_alive),
    ]

    for name, fn in tests:
        try:
            fn()
            results[name] = "PASS"
        except AssertionError as e:
            print(f"✗ {name}: FAIL — {e}")
            results[name] = "FAIL"
        except Exception as e:
            import traceback
            print(f"✗ {name}: ERROR — {e}")
            traceback.print_exc()
            results[name] = "ERROR"

    print()
    print("=" * 70)
    print("結果サマリ:")
    for name, result in results.items():
        mark = "✓" if result == "PASS" else "✗"
        print(f"  {mark} {name}: {result}")

    print()
    if results.get("test_B") == "FAIL":
        print("✓ test_B が FAIL → heartbeat gap バグ (OBS-1) を実証成功")
        print()
        print("CONCLUSION:")
        print("  dispatcher.sh Lines 869-887 は heartbeat mtime のみ判定。")
        print("  Worker が alive (window あり) でも heartbeat が 600s 超えると")
        print("  _alive_workers から除外され can_handle = False → 誤通知発火。")
        print()
        print("  修正: 'heartbeat fresh OR window exists' の OR 条件にすること。")
    else:
        print("WARNING: test_B が FAIL しなかった。バグが既に修正済みか、")
        print("         テストの前提条件に問題がある可能性がある。")

    sys.exit(0)  # バグ実証が目的なので非ゼロ終了はしない
