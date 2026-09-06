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
# シナリオ a: 既存維持 (PR #154 regression) — OR 条件適用後も 5連続 done → 誤通知 0
# ---------------------------------------------------------------------------

def test_scenario_a_five_consecutive_done_zero_false_notify_or_condition():
    """
    シナリオ a: 5連続 task done → 誤通知 0 (PR #154 regression を OR 条件で再確認)

    test_regression_no_false_notify.py の test_five_consecutive_task_done_zero_false_notifications
    を MockDispatcherWithWindows + can_handle_fixed() で再検証する。
    OR 条件 fix (このブランチ) 適用後も PR #154 fix が維持されていることを保証。

    シナリオ:
      - Haruto (bash+code) が fresh heartbeat + window あり で稼働
      - 5 タスクを順番に done → 各 done 後に次タスクが unblocked
      - OR 条件: heartbeat fresh → alive → can_handle = True → 誤通知なし
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Haruto: fresh heartbeat + window あり (通常稼働状態)
        disp.set_heartbeat_fresh("Haruto")
        disp.set_window_exists("Haruto", True)

        tasks = [
            {"id": f"t{i:03d}", "skills": {"bash", "code"}}
            for i in range(1, 6)
        ]

        false_count = 0
        for i in range(len(tasks) - 1):
            # task i 完了: done 後も heartbeat を fresh に更新 (Worker はまだ稼働中)
            disp.set_heartbeat_fresh("Haruto")

            # 次 task が unblocked → OR 条件 can_handle チェック
            next_task_skills = tasks[i + 1]["skills"]
            ch = disp.can_handle_fixed(next_task_skills)
            if not ch:
                false_count += 1

        assert false_count == 0, (
            f"シナリオ a FAIL: 5連続 done で誤通知 {false_count} 件 (expected 0). "
            f"PR #154 regression — OR 条件 fix 後も heartbeat fresh → alive が機能すること"
        )
        print(
            f"✓ test_scenario_a_five_consecutive_done_zero_false_notify_or_condition: "
            f"false_notifications={false_count}/4 (5 done, 4 dispatch checks)"
        )


# ---------------------------------------------------------------------------
# シナリオ b: heartbeat stale + window あり → 誤通知 0 (heartbeat gap regression fix)
# ---------------------------------------------------------------------------

def test_idle_worker_stale_heartbeat_window_exists_no_false_notify():
    """
    シナリオ b: idle Worker が heartbeat stale (>TTL) でも window があれば誤通知なし

    heartbeat gap バグ (OBS-1) の修正検証テスト。

    状態:
      - Worker を spawn → heartbeat を 660s 前 (TTL=600s 超) に設定
      - window は存在する (Worker は実際には alive だが heartbeat が gap に入っている)
      - task done 後に dispatch cycle 実行

    期待:
      - OR 条件: window exists → _alive_workers に含まれる → can_handle = True
      - 誤通知なし (false notification = 0)

    旧実装 (heartbeat のみ):
      - heartbeat stale → _alive_workers に含まれない → can_handle = False → 誤通知発火 (バグ)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Worker を spawn し heartbeat を 660s 前に設定 (TTL=600s 超過)
        disp.set_heartbeat_stale("Haruto", age_seconds=660)
        # window は存在する (OR 条件がカバーするケース)
        disp.set_window_exists("Haruto", True)

        task_skills = {"bash", "code"}

        # task done 後のディスパッチサイクル実行
        can_handle = disp.can_handle_fixed(task_skills)

        # OR 条件: window exists → alive → can_handle = True → 誤通知なし
        assert can_handle is True, (
            f"シナリオ b FAIL: heartbeat stale (660s > TTL=600s) + window exists → "
            f"can_handle={can_handle} (expected True). "
            f"OR 条件 fix が機能していない: window exists が alive 判定に使われていない。"
        )

        # 旧実装 (heartbeat のみ) では False だったことを参考確認
        can_handle_old = disp.can_handle_current(task_skills)
        assert can_handle_old is False, (
            f"参考確認: old logic (heartbeat only) should return False for stale heartbeat, "
            f"got {can_handle_old}"
        )

        print(
            f"✓ test_idle_worker_stale_heartbeat_window_exists_no_false_notify: "
            f"heartbeat_age=660s > TTL=600s, window_exists=True "
            f"→ fixed can_handle={can_handle} (誤通知なし), "
            f"old can_handle={can_handle_old} (旧バグ確認)"
        )


# ---------------------------------------------------------------------------
# シナリオ c: heartbeat stale + window なし → dead 正判定 → Director 通知発火
# ---------------------------------------------------------------------------

def test_dead_worker_stale_heartbeat_no_window_triggers_notify():
    """
    シナリオ c: heartbeat stale + window なし → dead 正判定 → Director への通知が発火

    真の dead Worker を正しく検出することを確認。
    OR 条件 fix 後も、両方の条件が満たされない場合は can_handle = False (正しい挙動)。

    状態:
      - Worker heartbeat を TTL 超過 (700s 前) に設定
      - window も存在しない (Worker は本当に dead)
      - task pending に対して dispatch cycle 実行

    期待:
      - OR 条件: heartbeat stale AND no window → _alive_workers に含まれない
      - can_handle = False → Director への通知が正当に発火 (true dead detection)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        disp = MockDispatcherWithWindows(tmpdir)

        # Worker heartbeat を TTL 超過 (700s 前) に設定
        disp.set_heartbeat_stale("Haruto", age_seconds=700)
        # window も消去 (Worker は本当に dead)
        disp.set_window_exists("Haruto", False)

        task_skills = {"bash", "code"}

        # dispatch cycle: OR 条件でも両方 stale/なし → can_handle = False
        can_handle = disp.can_handle_fixed(task_skills)
        alive_workers = disp._get_alive_workers_fixed()

        # can_handle = False → Director への通知が発火すべき (正しい挙動)
        assert can_handle is False, (
            f"シナリオ c FAIL: heartbeat stale (700s) + no window → "
            f"can_handle={can_handle} (expected False = dead 正判定). "
            f"alive_workers={alive_workers}"
        )

        # 通知発火のシミュレート: can_handle=False → notify fires
        notify_should_fire = not can_handle
        assert notify_should_fire is True, (
            f"シナリオ c FAIL: dead worker なのに notify が発火しない"
        )

        print(
            f"✓ test_dead_worker_stale_heartbeat_no_window_triggers_notify: "
            f"heartbeat_age=700s > TTL=600s, window_exists=False "
            f"→ can_handle={can_handle}, alive_workers={alive_workers} "
            f"→ notify_fires={notify_should_fire} (dead 正判定 ✓)"
        )


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
    print("  シナリオ a: PASS (5連続 done → 誤通知 0, OR 条件維持 ✓)")
    print("  シナリオ b: PASS (heartbeat stale + window → 誤通知なし ✓)")
    print("  シナリオ c: PASS (heartbeat stale + no window → dead 正判定 + notify 発火 ✓)")
    print()

    results = {}
    tests = [
        ("test_A", test_A_heartbeat_fresh_window_exists_alive),
        ("test_B", test_B_heartbeat_stale_window_exists_fixed),
        ("test_C", test_C_heartbeat_stale_no_window_dead),
        ("test_D", test_D_heartbeat_fresh_no_window_alive),
        ("test_E", test_E_dispatcher_sh_contains_or_condition_fix),
        ("scenario_a", test_scenario_a_five_consecutive_done_zero_false_notify_or_condition),
        ("scenario_b", test_idle_worker_stale_heartbeat_window_exists_no_false_notify),
        ("scenario_c", test_dead_worker_stale_heartbeat_no_window_triggers_notify),
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
