"""kai-review.sh の codex 本体の timeout (t009)。

t007/Seo (PR #352 レビュー) が発見した穴: run_codex_review() だけが他の 3 エンジン
(agy / cursor / claude) と違って timeout に包まれていなかったため、codex 本体が応答しない・
無言で座り込む場合に kai-review 側の上限が無く、watchdog の timeout.max (review card は
7200 秒) まで無言で座り続けていた。ここでは codex を stub にして、timeout --kill-after で
包んだことで (1) 無言のハングにならず短時間で終わる、(2) 非ゼロ (exit=124 / --kill-after は
137) が CODEX FAILURE 経由で fail_needs_director (fail-closed) に伝わる、(3) 正常系は
今までどおり done になる、ことを確認する。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kai_review_keeps_full_codex_findings_in_the_card import REPO_ROOT, run_kai_review  # noqa: E402,F401

CLEAN = json.dumps({"findings": []})

# codex stub: CODEX_MODE で挙動を変える。run_codex_review() は diff を stdin で渡すので、
# 必ず読み切ってから動く (読まずに終わると printf 側が SIGPIPE で落ちて誤検知するおそれがある)。
CODEX_STUB = r'''#!/bin/bash
cat >/dev/null
out=""
prev=""
for a in "$@"; do
  [[ "$prev" == "-o" ]] && out="$a"
  prev="$a"
done
case "$CODEX_MODE" in
  ok)          cp "$TMPDIR/codex_out.txt" "$out" ;;
  sleep)       sleep 30 ;;
  ignore_term) trap '' TERM; sleep 300 ;;
esac
'''


def run(tmp_path, monkeypatch, *, codex_mode="ok", codex_timeout=60, codex_out=CLEAN):
    monkeypatch.setenv("CODEX_MODE", codex_mode)
    monkeypatch.delenv("CREWVIA_REVIEW_ENGINE", raising=False)
    config_text = f"review:\n  engine: codex\n  codex_timeout_seconds: {codex_timeout}\n"
    return run_kai_review(tmp_path, codex_out, config_text=config_text,
                           extra_exes={"codex": CODEX_STUB}, with_codex=False)


def _calls(tmp_path):
    return (tmp_path / "calls.txt").read_text().splitlines()


def _verdict(tmp_path):
    kinds = [c.split()[0] for c in _calls(tmp_path) if c.split()[0] in ("done", "needs-director")]
    assert len(kinds) == 1, _calls(tmp_path)
    return kinds[0]


def test_codex_clean_review_still_works_under_the_timeout_wrap(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, codex_mode="ok", codex_timeout=60)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)


def test_codex_timeout_is_fail_closed_not_a_silent_hang(tmp_path, monkeypatch):
    t0 = time.monotonic()
    p, capture = run(tmp_path, monkeypatch, codex_mode="sleep", codex_timeout=1)
    elapsed = time.monotonic() - t0
    assert elapsed < 60, "codex の timeout が効かず無言で座り続けた"
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CODEX FAILURE: exit=124" in c for c in _calls(tmp_path)), _calls(tmp_path)


def test_codex_ignoring_sigterm_is_killed_and_fail_closed(tmp_path, monkeypatch):
    """SIGTERM を無視する codex でも --kill-after で止まり、needs-director に倒れる (無言の無限ハングにしない)。"""
    t0 = time.monotonic()
    p, capture = run(tmp_path, monkeypatch, codex_mode="ignore_term", codex_timeout=1)
    elapsed = time.monotonic() - t0
    assert elapsed < 60, "timeout が SIGKILL に進まず待ち続けた"
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CODEX FAILURE: exit=137" in c for c in _calls(tmp_path)), _calls(tmp_path)
