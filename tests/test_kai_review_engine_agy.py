"""t080: kai-review.sh のレビュー先 (review.engine = codex|agy|both) と agy の fail-closed。
agy は stub。判定は codex と同じ経路 (findings の JSON) を通る。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kai_review_keeps_full_codex_findings_in_the_card import REPO_ROOT, run_kai_review  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lib_review_engine as lre  # noqa: E402

FAKE_SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
CLEAN = json.dumps({"findings": []})
P1 = json.dumps({"findings": [{"priority": "P1", "title": "agy-found", "body": "B", "file": "README"}]})
CODEX_P1 = json.dumps({"findings": [{"priority": "P1", "title": "codex-found", "body": "B", "file": "README"}]})

# agy stub: AGY_MODE で挙動を変える。argv は ARGV_FILE に残す。worktree (cwd) を AGY_MODE=dirty のときだけ汚す。
AGY_STUB = r'''#!/bin/bash
printf '%s\n' "$@" > "$AGY_ARGV_FILE"
pwd > "$AGY_CWD_FILE"
case "$AGY_MODE" in
  ok)        printf '{"status":"SUCCESS","denied_actions":null,"structured_output":%s}' "$AGY_FINDINGS" ;;
  exit3)     echo 'some preamble' >&2; echo "AGY_ERROR: {\"code\":429,\"message\":\"quota exceeded\",\"api_key\":\"$AGY_FAKE_SECRET\"}" >&2; echo "tail line key=$AGY_FAKE_SECRET" >&2; exit 3 ;;
  status)    printf '{"status":"ERROR","structured_output":%s}' "$AGY_FINDINGS" ;;
  null)      printf '{"status":"SUCCESS","denied_actions":null,"structured_output":null}' ;;
  missing)   printf '{"status":"SUCCESS"}' ;;
  denied)    printf '{"status":"SUCCESS","denied_actions":[{"action":"read_file","path":"/tmp/x/review.diff?token=%s"}],"structured_output":%s}' "$AGY_FAKE_SECRET" "$AGY_FINDINGS" ;;
  dirty)     echo x > injected.txt; printf '{"status":"SUCCESS","denied_actions":[],"structured_output":%s}' "$AGY_FINDINGS" ;;
  garbage)   echo 'not json' ;;
esac
'''


def run(tmp_path, monkeypatch, *, engine=None, agy_mode="ok", agy_findings=CLEAN, codex_out=CLEAN, config_text=None,
        env_engine=None, extra_args=(), with_codex=True):
    monkeypatch.setenv("AGY_MODE", agy_mode)
    monkeypatch.setenv("AGY_FAKE_SECRET", FAKE_SECRET)
    monkeypatch.setenv("AGY_FINDINGS", agy_findings)
    monkeypatch.setenv("AGY_ARGV_FILE", str(tmp_path / "agy_argv.txt"))
    monkeypatch.setenv("AGY_CWD_FILE", str(tmp_path / "agy_cwd.txt"))
    if env_engine is None:
        monkeypatch.delenv("CREWVIA_REVIEW_ENGINE", raising=False)
    else:
        monkeypatch.setenv("CREWVIA_REVIEW_ENGINE", env_engine)
    if config_text is None and engine is not None:
        config_text = f"review:\n  engine: {engine}\n  agy_model: gemini-4-argon-high\n  agy_timeout_seconds: 90\n"
    return run_kai_review(tmp_path, codex_out, extra_args=extra_args, config_text=config_text,
                          extra_exes={"agy": AGY_STUB}, with_codex=with_codex)


def _calls(tmp_path):
    return (tmp_path / "calls.txt").read_text().splitlines()


def _verdict(tmp_path):
    kinds = [c.split()[0] for c in _calls(tmp_path) if c.split()[0] in ("done", "needs-director")]
    assert len(kinds) == 1, _calls(tmp_path)
    return kinds[0]


def _result(capture, kind):
    return (capture / f"{kind}.txt").read_text()


# --- 設定の読み口 ---

@pytest.mark.parametrize("text,expected", [
    ("review:\n  # comment\n  engine: agy\n", "agy"),
    ("review:\n  engine: agy\n  # comment\n  agy_timeout_seconds: 30\n", "agy"),
    ("review:\n\n  engine: both\n\n  agy_model: gemini-4-argon-high\n", "both"),
    ("review:\n    engine: agy\n    agy_timeout_seconds: 30\n", "agy"),
    ("review:\n  engine: agy\n  unknown_key: 1\n", "agy"),
    ("review:\n  engine: 'agy'  # trailing\n", "agy"),
    ("attempts:\n  max_total: 6\nreview:\n  # c\n  engine: both\nescalation:\n  x: 1\n", "both"),
])
def test_valid_yaml_shapes_are_read_not_silently_defaulted(tmp_path, text, expected):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text)
    assert lre.load(cfg, environ={})[0] == expected


@pytest.mark.parametrize("text", [
    "review:\n  engine: 1\n", "review:\n  engine: [agy]\n", "review:\n  engine: true\n",
    "review:\n  agy_model: 5\n", "review: [a]\n", "review:\n  engine: agy\n bad: [\n", "- a\n- b\n",
    "review:\n  engine: agy\n  agy_timeout_seconds: 1.5\n",
    "review:\n  engine:\n", "review: null\n", "review:\n", "review: ~\n",
    "review:\n  engine: agy\n  agy_model:\n", "review:\n  engine: agy\n  agy_timeout_seconds:\n",
    "review:\n  engine: null\n",
])
def test_wrong_types_and_broken_yaml_are_errors_never_codex(tmp_path, text):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text)
    with pytest.raises(lre.ReviewConfigError):
        lre.load(cfg, environ={})


@pytest.mark.parametrize("text", [
    "review:\n  engine: agy\n", '"review":\n  engine: agy\n', "{review: {engine: agy}}\n",
    "attempts:\n  max_total: 6\n", "# nothing\n",
])
def test_without_pyyaml_an_existing_config_is_an_error_never_codex(tmp_path, monkeypatch, text):
    monkeypatch.setattr(lre, "yaml", None)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text)
    with pytest.raises(lre.ReviewConfigError) as e:
        lre.load(cfg, environ={})
    assert e.value.code == "yaml_unavailable"
    assert lre.load(tmp_path / "missing.yaml", environ={})[0] == "codex"   # 無い file は既定


def test_default_engine_is_codex_without_a_review_block(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("attempts:\n  max_total: 6\n")
    assert lre.load(cfg, environ={}) == ("codex", lre.DEFAULT_AGY_MODEL, lre.DEFAULT_AGY_TIMEOUT_SECONDS)
    assert lre.load(tmp_path / "missing.yaml", environ={})[0] == "codex"


def test_the_shipped_config_defaults_to_codex():
    assert lre.load(REPO_ROOT / "config" / "crewvia.yaml", environ={})[0] == "codex"


@pytest.mark.parametrize("text", [
    "review:\n  engine: gemini\n", "review:\n  agy_model: 'a b'\n",
    "review:\n  agy_timeout_seconds: 0\n", "review:\n  agy_timeout_seconds: abc\n", "review: nope\n",
])
def test_bad_values_are_errors_not_defaults(tmp_path, text):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text)
    with pytest.raises(lre.ReviewConfigError):
        lre.load(cfg, environ={})


def test_env_overrides_engine_and_a_bad_or_empty_env_is_an_error(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("review:\n  engine: codex\n")
    assert lre.load(cfg, environ={"CREWVIA_REVIEW_ENGINE": "agy"})[0] == "agy"
    for bad in ("", "AGY", "x"):
        with pytest.raises(lre.ReviewConfigError):
            lre.load(cfg, environ={"CREWVIA_REVIEW_ENGINE": bad})


# --- codex (既定): agy を呼ばない ---

def test_codex_engine_does_not_call_agy(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert not (tmp_path / "agy_argv.txt").exists()


def test_bad_engine_in_config_stops_with_needs_director(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, config_text="review:\n  engine: nope\n")
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert "no fallback to codex" in _result_text(tmp_path)


def _result_text(tmp_path):
    return "\n".join(_calls(tmp_path))


# --- agy ---

def test_agy_clean_review_is_done_and_uses_plan_mode_without_skip_permissions(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", codex_out="NOT USED")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    argv = (tmp_path / "agy_argv.txt").read_text().splitlines()
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "gemini-4-argon-high"
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--print-timeout") + 1] == "90s"
    assert "--json-schema" in argv and "--add-dir" in argv
    pi = argv.index("-p")
    assert "-p" not in argv[pi + 1:] and not any(a.startswith("--") and a in ("--mode", "--model") for a in argv[pi + 1:])
    assert "review.diff" in "\n".join(argv[pi + 1:])               # prompt が diff のファイルを指す
    assert argv.index("--add-dir") < pi                            # -p は最後の option (prompt が続く)
    assert "### agy raw output" in _result(capture, "done")
    assert not list(tmp_path.glob("kai-review-agy*"))


def test_agy_cwd_is_the_review_worktree_and_the_diff_is_not_in_argv(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, engine="agy")
    cwd = (tmp_path / "agy_cwd.txt").read_text().strip()
    assert "kai-review-wt." in cwd
    assert "+change" not in (tmp_path / "agy_argv.txt").read_text()


def test_agy_findings_need_director(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", agy_findings=P1)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert "agy-found" in _result(capture, "needs-director")


@pytest.mark.parametrize("mode,reason", [
    ("exit3", "exit_3"), ("status", "status"), ("null", "no_structured_output"), ("missing", "no_structured_output"),
    ("denied", "denied_actions"), ("dirty", "worktree_modified"), ("garbage", "unparseable_output"),
])
def test_agy_fail_closed_never_lgtm(tmp_path, monkeypatch, mode, reason):
    p, capture = run(tmp_path, monkeypatch, engine="agy", agy_mode=mode)   # findings は空 = 通せば LGTM になる形
    assert _verdict(tmp_path) == "needs-director", (mode, p.stdout, p.stderr)
    assert not (capture / "done.txt").exists()
    assert any(f"AGY FAILURE: {reason}" in c for c in _calls(tmp_path)), (_calls(tmp_path), p.stderr)


def test_env_selects_agy_over_config_codex(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="codex", env_engine="agy", agy_findings=P1)
    assert _verdict(tmp_path) == "needs-director"
    assert (tmp_path / "agy_argv.txt").exists()


def test_agy_engine_does_not_require_codex_to_be_installed(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", with_codex=False)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert (tmp_path / "agy_argv.txt").exists()


@pytest.mark.parametrize("engine", ["codex", "both"])
def test_codex_and_both_still_require_codex(tmp_path, monkeypatch, engine):
    p, capture = run(tmp_path, monkeypatch, engine=engine, with_codex=False)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("codex command not found" in c for c in _calls(tmp_path)), _calls(tmp_path)


# --- both: 判定は codex・agy は併記 ---

def test_both_verdict_follows_codex_and_agy_is_appended(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", codex_out=CLEAN, agy_findings=P1)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)            # agy が P1 でも判定は codex
    text = _result(capture, "done")
    assert "### agy (比較用" in text and "agy-found" in text and "### Codex raw output" in text


def test_both_codex_findings_still_need_director_even_if_agy_is_clean(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", codex_out=CODEX_P1, agy_findings=CLEAN)
    assert _verdict(tmp_path) == "needs-director"
    assert "codex-found" in _result(capture, "needs-director")


def test_both_agy_failure_is_recorded_but_does_not_decide(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", agy_mode="exit3")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert "agy failed: exit_3" in _result(capture, "done")


# --- 失敗の診断は伏せ字を通して Result に残る (一時ファイルは後片付けで消える) ---

def _fail_text(tmp_path):
    return "\n".join(_calls(tmp_path))


def test_exit3_keeps_agy_error_and_stderr_tail_with_secrets_redacted(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", agy_mode="exit3")
    text = _fail_text(tmp_path)
    assert "AGY FAILURE: exit_3" in text and "agy diagnostics" in text
    assert "AGY_ERROR line:" in text and "quota exceeded" in text
    assert "stderr tail:" in text and "tail line" in text
    assert FAKE_SECRET not in text and "REDACTED" in text
    assert not list(tmp_path.glob("kai-review-agy*"))


def test_denied_actions_keep_action_and_path_with_secrets_redacted(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", agy_mode="denied")
    text = _fail_text(tmp_path)
    assert "stdout status: SUCCESS" in text
    assert "denied: action=read_file" in text and "review.diff" in text
    assert FAKE_SECRET not in text


def test_structured_output_null_keeps_status(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="agy", agy_mode="null")
    text = _fail_text(tmp_path)
    assert "AGY FAILURE: no_structured_output" in text and "stdout status: SUCCESS" in text


def test_success_has_no_diagnostics(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, engine="agy")
    assert "agy diagnostics" not in _fail_text(tmp_path)


def test_both_agy_failure_note_carries_redacted_diagnostics(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", agy_mode="exit3")
    text = _result(capture, "done")
    assert "quota exceeded" in text and FAKE_SECRET not in text


def test_diagnose_is_bounded_and_withholds_when_redaction_fails(tmp_path, monkeypatch):
    out, err = tmp_path / "o", tmp_path / "e"
    out.write_text("not json")
    err.write_text("\n".join("line %d %s" % (i, "x" * 1000) for i in range(500)))
    text = lre.diagnose(str(out), str(err))
    assert len(text) <= lre._DIAG_TOTAL_MAX + 1 and text.count("stderr tail:") <= lre._DIAG_TAIL_LINES and "line 499" not in text
    import lib_checkpoint
    def boom(_t):
        raise lib_checkpoint.RedactionError("x")
    monkeypatch.setattr(lib_checkpoint, "redact", boom)
    assert lre.diagnose(str(out), str(err)) == lre._WITHHELD


def test_diagnose_drops_the_partial_first_line_of_a_truncated_window(tmp_path):
    err, out = tmp_path / "e", tmp_path / "o"
    out.write_text("")
    cut = FAKE_SECRET[8:]   # 窓の先頭で token が割れた残り
    err.write_text(cut + "x" * lre._DIAG_READ_BYTES + "\nlast line\n")
    text = lre.diagnose(str(out), str(err))
    assert cut not in text and "last line" in text
