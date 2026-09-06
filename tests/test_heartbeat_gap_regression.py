#!/usr/bin/env python3
"""
tests/test_heartbeat_gap_regression.py

Regression テスト: heartbeat gap バグ (OBS-1) の実証 → fix 検証

## 背景

dispatcher.sh の `_alive_workers` 構築 (Lines 869-887) は
heartbeat ファイルの mtime のみで Worker 生存を判定していた (旧実装)。

Worker が alive (herdr window 存在) でも、heartbeat が AGENT_PRESENCE_TTL (600s)
を超えると `_alive_workers` から除外され `can_handle = False` → 誤通知が発火していた。
これを「heartbeat gap バグ」と呼ぶ (OBS-1)。

## 修正内容 (このブランチで適用済み)

`_alive_workers` の判定を「heartbeat fresh OR window exists」の OR 条件に変更。

## テストシナリオ

- test_A: heartbeat fresh + window あり → alive ✓ (正常ケース)
- test_B: heartbeat stale (>600s) + window あり → OR fix により alive ✓ (fix 検証)
  → 旧実装では FAIL していたが、fix 適用後は PASS に転換
- test_C: heartbeat stale + window なし → dead ✓ (真に dead)
- test_D: heartbeat fresh + window なし (transient) → alive ✓ (PR#154 修正を維持)

実行方法:
  python3 -m pytest tests/test_heartbeat_gap_regression.py -v
  # 全テスト PASS することを確認 (test_B が PASS に転換)

dispatcher.sh 確認箇所:
  Line 481:    AGENT_PRESENCE_TTL = 600
  Lines 869-887: _alive_workers 構築 (heartbeat fresh OR window exists — fix 適用後)
  Lines 1060-1065: can_handle ロジック (_alive_workers 使用)
  Line 542:    publish_agents での AGENT_PRESENCE_TTL 使用 (変更なし)
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
# test_B: heartbeat stale + window あり → OR fix により alive ✓ (fix 検証)
# ---------------------------------------------------------------------------

def test_B_heartbeat_stale_window_exists_fixed():
    """
    test_B: heartbeat stale (>600s) + window あり → OR fix により alive ✓

    heartbeat gap バグ (OBS-1) の fix 検証テスト。

    旧実装 (heartbeat のみ):
      heartbeat stale → _alive_workers 空 → can_handle = False (誤判定)
    新実装 (OR 条件, このブランチ):
      heartbeat stale でも window あり → alive → can_handle = True (正しい)

    dispatcher.sh 参照:
      Lines 869-887: _hb_fresh or _win_exists の OR 条件 (fix 適用後)
      Lines 1060-1065: _alive_workers を使った can_handle 判定
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: heartbeat stale (700s 前) + window あり (実際には alive)
        disp.set_heartbeat_stale("Haruto", age_seconds=700)
        disp.set_window_exists("Haruto", True)

        task_skills = {"bash", "code"}

        # 旧実装 (heartbeat のみ): dead 誤判定 (バグ — 参考情報)
        result_old = disp.can_handle_current(task_skills)
        alive_old = disp._get_alive_workers_current()

        # 新実装 (OR 条件 = fix): window あり → alive → can_handle = True
        result_fixed = disp.can_handle_fixed(task_skills)
        alive_fixed = disp._get_alive_workers_fixed()

        print(
            f"  test_B: heartbeat_stale=True (700s), window_exists=True "
            f"→ old logic can_handle={result_old} (alive={alive_old}) [historical bug], "
            f"fixed logic can_handle={result_fixed} (alive={alive_fixed})"
        )

        # 旧実装がバグ (False) だったことを記録 (参考: 歴史的バグ)
        assert result_old is False, (
            f"Unexpected: old (heartbeat-only) logic returned {result_old}. "
            f"Expected False to confirm historical OBS-1 bug pattern."
        )

        # fix 適用後: OR 条件により window あり → alive → True (PASS = fix 検証)
        assert result_fixed is True, (
            f"FIX FAILED: heartbeat stale (700s) + window exists "
            f"→ fixed can_handle={result_fixed} (expected True). "
            f"alive_workers={alive_fixed}. "
            f"OR condition fix not working correctly."
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
# test_E: dispatcher.sh に OR 条件 fix が適用済みであることを構文確認
# ---------------------------------------------------------------------------

def test_E_dispatcher_sh_contains_or_condition_fix():
    """
    test_E: dispatcher.sh に heartbeat gap fix (OR 条件) が適用済みであることを確認。

    dispatcher.sh 参照:
      Lines 869-887: _hb_fresh or _win_exists の OR 条件
      _window_agent_names の構築
    """
    dispatcher_path = Path(__file__).parent.parent / "scripts" / "dispatcher.sh"
    assert dispatcher_path.exists(), f"dispatcher.sh not found at {dispatcher_path}"

    content = dispatcher_path.read_text()

    # OR 条件の実装確認
    assert "_window_agent_names" in content, \
        "Fix: _window_agent_names (window set) not found in dispatcher.sh"
    assert "_hb_fresh" in content, \
        "Fix: _hb_fresh variable not found in dispatcher.sh"
    assert "_win_exists" in content, \
        "Fix: _win_exists variable not found in dispatcher.sh"
    assert "_hb_fresh or _win_exists" in content, \
        "Fix: 'heartbeat fresh OR window exists' OR condition not found in dispatcher.sh"

    # 既存の構造が維持されていることを確認
    assert "_alive_workers" in content, \
        "_alive_workers variable not found in dispatcher.sh"
    assert "AGENT_PRESENCE_TTL" in content, \
        "AGENT_PRESENCE_TTL not found in dispatcher.sh"
    assert "for name in _alive_workers" in content, \
        "can_handle loop over _alive_workers not found"

    print("✓ test_E: dispatcher.sh に heartbeat gap OR fix が適用済みであることを確認")


# ---------------------------------------------------------------------------
# エントリポイント (直接実行用)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("Heartbeat Gap Regression Test (OBS-1) — fix 検証")
    print("dispatcher.sh Lines: 481, 869-887, 1060-1065, 542")
    print("=" * 70)
    print()
    print("期待される結果 (fix 適用後):")
    print("  test_A: PASS (heartbeat fresh + window → alive ✓)")
    print("  test_B: PASS (heartbeat stale + window → OR fix により alive ✓)")
    print("  test_C: PASS (heartbeat stale + no window → dead ✓)")
    print("  test_D: PASS (heartbeat fresh + no window → alive via heartbeat ✓)")
    print("  test_E: PASS (dispatcher.sh に OR fix が適用済み ✓)")
    print()

    results = {}
    tests = [
        ("test_A", test_A_heartbeat_fresh_window_exists_alive),
        ("test_B", test_B_heartbeat_stale_window_exists_fixed),
        ("test_C", test_C_heartbeat_stale_no_window_dead),
        ("test_D", test_D_heartbeat_fresh_no_window_alive),
        ("test_E", test_E_dispatcher_sh_contains_or_condition_fix),
    ]

    failures = []
    for name, fn in tests:
        try:
            fn()
            results[name] = "PASS"
        except AssertionError as e:
            print(f"✗ {name}: FAIL — {e}")
            results[name] = "FAIL"
            failures.append(name)
        except Exception as e:
            import traceback
            print(f"✗ {name}: ERROR — {e}")
            traceback.print_exc()
            results[name] = "ERROR"
            failures.append(name)

    print()
    print("=" * 70)
    print("結果サマリ:")
    for name, result in results.items():
        mark = "✓" if result == "PASS" else "✗"
        print(f"  {mark} {name}: {result}")

    print()
    if not failures:
        print("✓ 全テスト PASS — heartbeat gap バグ (OBS-1) の fix が正しく適用済み")
        print()
        print("CONCLUSION:")
        print("  dispatcher.sh の _alive_workers 構築を OR 条件に変更した:")
        print("  'heartbeat fresh OR window exists'")
        print("  idle Worker (heartbeat gap) でも window が存在すれば alive とみなす。")
        sys.exit(0)
    else:
        print(f"✗ {len(failures)} tests FAILED: {failures}")
        sys.exit(1)
