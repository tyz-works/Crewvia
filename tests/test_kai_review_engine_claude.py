"""kai-review.sh のレビュー先 `claude` (claude -p・読み取り専用) と `ensemble` (agy + claude の合議) の fail-closed。
claude / agy は stub。判定は codex と同じ経路 (findings の JSON) を通る。設計: knowledge/claude-review.md。"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kai_review_keeps_full_codex_findings_in_the_card import REPO_ROOT, run_kai_review  # noqa: E402
from test_kai_review_engine_agy import AGY_STUB, FAKE_SECRET  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lib_review_engine as lre  # noqa: E402

SCHEMA = json.loads((REPO_ROOT / "config" / "kai-review-findings.schema.json").read_text())
CLEAN = json.dumps({"findings": []})
P2_CLAUDE = json.dumps({"findings": [{"priority": "P2", "title": "claude-found", "body": "repro: run x", "file": "README"}]})
P2_AGY = json.dumps({"findings": [{"priority": "P2", "title": "agy-found", "body": "B", "file": "README"}]})
P3_AGY = json.dumps({"findings": [{"priority": "P3", "title": "agy-nit", "body": "N", "file": None}]})
P3_CLAUDE = json.dumps({"findings": [{"priority": "P3", "title": "claude-nit", "body": "N", "file": None}]})

# claude stub: CLAUDE_MODE で挙動を変える。argv・stdin (prompt)・env・cwd を残す。
# kai-review.sh は子の env を allowlist に絞る (CLAUDE_* も落ちる) ので、指示は $TMPDIR/claude_ctl.sh から読む (TMPDIR は残る)
CLAUDE_STUB = r'''#!/bin/bash
source "$TMPDIR/claude_ctl.sh"
printf '%s\0' "$@" > "$CLAUDE_ARGV_FILE"
cat > "$CLAUDE_STDIN_FILE"
env > "$CLAUDE_ENV_FILE"
pwd > "$CLAUDE_CWD_FILE"
for a in "$@"; do [[ "$prev" == --add-dir ]] && ls "$a" > "$CLAUDE_DIR_FILE"; prev="$a"; done
ok() { printf '{"type":"result","subtype":"success","is_error":false,"permission_denials":[],"result":"x","structured_output":%s}' "$CLAUDE_FINDINGS"; }
case "$CLAUDE_MODE" in
  ok)        ok ;;
  exit3)     echo "boom" >&2; exit 3 ;;
  is_error)  printf '{"type":"result","subtype":"success","is_error":true,"structured_output":%s}' "$CLAUDE_FINDINGS" ;;
  subtype)   printf '{"type":"result","subtype":"error_max_turns","is_error":false,"structured_output":%s}' "$CLAUDE_FINDINGS" ;;
  denied)    printf '{"type":"result","subtype":"success","is_error":false,"permission_denials":[{"tool_name":"Bash","tool_input":{"command":"ls"}}],"structured_output":%s}' "$CLAUDE_FINDINGS" ;;
  noso)      printf '{"type":"result","subtype":"success","is_error":false,"permission_denials":[],"result":"no json here"}' ;;
  nullso)    printf '{"type":"result","subtype":"success","is_error":false,"structured_output":null}' ;;
  schema)    printf '{"type":"result","subtype":"success","is_error":false,"structured_output":{"findings":[{"priority":"P9","title":"t","body":null,"file":null}]}}' ;;
  dupkey)    printf '{"type":"result","subtype":"success","is_error":false,"structured_output":{"findings":[{"priority":"P1","title":"t","body":null,"file":null}]},"structured_output":{"findings":[]}}' ;;
  garbage)   echo 'not json' ;;
  two)       ok; echo; ok ;;
  dirty)     echo x > injected.txt; ok ;;
  modify)    echo y >> README; ok ;;
  commit)    git -c user.email=x@x -c user.name=x commit -q --allow-empty -m sneaky; ok ;;
  sleep)     sleep 30; ok ;;
esac
'''


def run(tmp_path, monkeypatch, *, engine="claude", claude_mode="ok", claude_findings=CLEAN, agy_mode="ok",
        agy_findings=CLEAN, codex_out=CLEAN, config_extra="", with_codex=False, with_claude=True, claude_timeout=60):
    ctl = {"CLAUDE_MODE": claude_mode, "CLAUDE_FINDINGS": claude_findings,
           "CLAUDE_ARGV_FILE": tmp_path / "claude_argv.txt", "CLAUDE_STDIN_FILE": tmp_path / "claude_stdin.txt",
           "CLAUDE_ENV_FILE": tmp_path / "claude_env.txt", "CLAUDE_CWD_FILE": tmp_path / "claude_cwd.txt",
           "CLAUDE_DIR_FILE": tmp_path / "claude_dir.txt"}
    (tmp_path / "claude_ctl.sh").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in ctl.items()))
    for k, v in {"AGY_MODE": agy_mode, "AGY_FINDINGS": agy_findings, "AGY_FAKE_SECRET": "x",
                 "AGY_ARGV_FILE": tmp_path / "agy_argv.txt", "AGY_CWD_FILE": tmp_path / "agy_cwd.txt"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("CREWVIA_REVIEW_ENGINE", raising=False)
    config_text = (f"review:\n  engine: {engine}\n  claude_model: claude-opus-5-5\n  claude_timeout_seconds: {claude_timeout}\n"
                   f"  agy_timeout_seconds: 60\n{config_extra}")
    exes = {"agy": AGY_STUB}
    if with_claude:
        exes["claude"] = CLAUDE_STUB
    return run_kai_review(tmp_path, codex_out, config_text=config_text, extra_exes=exes, with_codex=with_codex)


def _calls(tmp_path):
    return (tmp_path / "calls.txt").read_text().splitlines()


def _verdict(tmp_path):
    kinds = [c.split()[0] for c in _calls(tmp_path) if c.split()[0] in ("done", "needs-director")]
    assert len(kinds) == 1, _calls(tmp_path)
    return kinds[0]


def _result(capture, kind):
    return (capture / f"{kind}.txt").read_text()


def _argv(tmp_path):
    return (tmp_path / "claude_argv.txt").read_text().split("\0")[:-1]


# --- 設定の読み口 ---

def test_get_prints_eight_lines_and_the_shipped_config_is_unchanged():
    c = lre.load_full(REPO_ROOT / "config" / "crewvia.yaml", environ={})
    assert c["engine"] == "codex" and c["compare"] == ["agy"]
    assert c["claude_model"] == "claude-opus-5-5" and c["claude_timeout"] == 900


@pytest.mark.parametrize("engine", ["claude", "ensemble"])
def test_new_engines_are_accepted_by_config_and_env(tmp_path, engine):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  engine: {engine}\n")
    assert lre.load_full(cfg, environ={})["engine"] == engine
    cfg.write_text("review:\n  engine: codex\n")
    assert lre.load_full(cfg, environ={"CREWVIA_REVIEW_ENGINE": engine})["engine"] == engine


@pytest.mark.parametrize("text,code", [
    ("review:\n  claude_model: 'a b'\n", "bad_claude_model"),
    ("review:\n  claude_model: -x\n", "bad_claude_model"),
    ("review:\n  claude_model:\n", "bad_claude_model"),
    ("review:\n  claude_timeout_seconds: 0\n", "bad_claude_timeout"),
    ("review:\n  claude_timeout_seconds: 1.5\n", "bad_claude_timeout"),
    ("review:\n  compare: [claude, claude]\n", "bad_compare"),
    ("review:\n  compare: [codex]\n", "bad_compare"),
])
def test_bad_claude_values_are_errors_not_defaults(tmp_path, text, code):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text)
    with pytest.raises(lre.ReviewConfigError) as e:
        lre.load_full(cfg, environ={})
    assert e.value.code == code


def test_compare_accepts_claude(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("review:\n  engine: both\n  compare: [agy, claude]\n")
    assert lre.load_full(cfg, environ={})["compare"] == ["agy", "claude"]


# --- extract-claude ---

def _doc(**over):
    d = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
         "structured_output": {"findings": []}}
    d.update(over)
    return json.dumps(d)


@pytest.mark.parametrize("text,code", [
    ("not json", "unparseable_output"),
    ("[]", "unparseable_output"),
    (_doc(is_error=True), "is_error"),
    (_doc(is_error=None), "is_error"),
    (_doc(subtype="error_max_turns"), "subtype"),
    (_doc(type="assistant"), "subtype"),
    (_doc(permission_denials=[{"tool_name": "Bash"}]), "permission_denials"),
    (_doc(permission_denials="x"), "unparseable_output"),
    (_doc(structured_output=None), "no_structured_output"),
    (_doc(structured_output=[]), "no_structured_output"),
    (_doc(structured_output={"findings": [{"priority": "P1"}]}), "schema_invalid"),
    (_doc(structured_output={"findings": [], "extra": 1}), "schema_invalid"),
    (_doc(structured_output={}), "schema_invalid"),
])
def test_extract_claude_fails_closed(text, code):
    with pytest.raises(lre.CursorOutputError) as e:
        lre.extract_claude_findings(text, SCHEMA)
    assert e.value.code == code


def test_extract_claude_rejects_duplicate_keys():
    text = ('{"type":"result","subtype":"success","is_error":false,'
            '"structured_output":{"findings":[{"priority":"P1","title":"t","body":null,"file":null}]},'
            '"structured_output":{"findings":[]}}')
    with pytest.raises(lre.CursorOutputError) as e:
        lre.extract_claude_findings(text, SCHEMA)
    assert e.value.code == "duplicate_key"


def test_extract_claude_ok_without_permission_denials_key():
    d = json.loads(_doc())
    del d["permission_denials"]
    assert lre.extract_claude_findings(json.dumps(d), SCHEMA) == {"findings": []}


# --- engine: claude ---

def test_claude_clean_review_is_done_read_only_flags_and_no_codex_needed(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    argv = _argv(tmp_path)
    assert argv[0] == "-p"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--setting-sources") + 1] == "user"         # PR の .claude/ (hooks) を読まない
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert set(allowed) == {"Read", "Grep", "Glob"}                      # Bash は許さない (git diff --output 等で書ける)
    disallowed = argv[argv.index("--disallowedTools") + 1:]
    assert {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"} <= set(disallowed)
    text = _result(capture, "done")
    assert "### claude raw output" in text and "model=claude-opus-5-5" in text.splitlines()[0]
    assert not list(tmp_path.glob("kai-review-claude*"))                 # 一時物は後片付け


def test_claude_cwd_is_the_review_worktree_diff_not_in_argv_and_prompt_on_stdin(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    assert "kai-review-wt." in (tmp_path / "claude_cwd.txt").read_text()
    assert "+change" not in (tmp_path / "claude_argv.txt").read_text()
    prompt = (tmp_path / "claude_stdin.txt").read_text()
    assert "review.diff" in prompt and "adversarial" in prompt
    assert "reproduction steps or the shape of a failing test" in prompt
    assert "knowledge/review-attack-checklist.md" in prompt
    assert "review.log" in prompt and "Shell commands are not available" in prompt
    assert set((tmp_path / "claude_dir.txt").read_text().split()) == {"review.diff", "review.log"}


def test_claude_child_env_is_an_allowlist(tmp_path, monkeypatch):
    for k, v in {"TASK_ID": "t999", "TASK_TITLE": "x", "TASKVIA_TOKEN": "tok", "CREWVIA_MISSION_SLUG": "m",
                 "SOME_API_KEY": "sk-x", "GH_TOKEN": "g"}.items():
        monkeypatch.setenv(k, v)
    p, _ = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    env = dict(ln.split("=", 1) for ln in (tmp_path / "claude_env.txt").read_text().splitlines() if "=" in ln)
    leaked = {n for n in env if n.startswith(("CREWVIA_", "TASKVIA_", "AGY_"))
              or n in ("AGENT_NAME", "TASK_ID", "TASK_TITLE", "SOME_API_KEY", "GH_TOKEN")}
    assert not leaked, leaked
    assert env.get("HOME") and env.get("PATH") and env.get("TMPDIR") == str(tmp_path)


def test_claude_findings_need_director(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, claude_findings=P2_CLAUDE)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert "claude-found" in _result(capture, "needs-director")


@pytest.mark.parametrize("mode,reason", [
    ("exit3", "exit_3"), ("is_error", "is_error"), ("subtype", "subtype"), ("denied", "permission_denials"),
    ("noso", "no_structured_output"), ("nullso", "no_structured_output"), ("schema", "schema_invalid"),
    ("dupkey", "duplicate_key"), ("garbage", "unparseable_output"), ("two", "unparseable_output"),
    ("dirty", "worktree_modified"), ("modify", "worktree_modified"), ("commit", "worktree_modified"),
])
def test_claude_fail_closed_never_lgtm(tmp_path, monkeypatch, mode, reason):
    p, capture = run(tmp_path, monkeypatch, claude_mode=mode)        # findings は空 = 通せば LGTM になる形
    assert _verdict(tmp_path) == "needs-director", (mode, p.stdout, p.stderr)
    assert not (capture / "done.txt").exists()
    assert any(f"CLAUDE FAILURE: {reason}" in c for c in _calls(tmp_path)), (_calls(tmp_path), p.stderr)


def test_claude_timeout_is_fail_closed(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, claude_mode="sleep", claude_timeout=2)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CLAUDE FAILURE: timeout" in c for c in _calls(tmp_path)), (_calls(tmp_path), p.stderr)


def test_claude_not_installed_is_fail_closed(tmp_path, monkeypatch):
    # 実機の claude が見えない PATH (claude の無い machine)
    monkeypatch.setenv("PATH", ":".join(d for d in os.environ["PATH"].split(":")
                                        if not os.access(os.path.join(d, "claude"), os.X_OK)))
    p, capture = run(tmp_path, monkeypatch, with_claude=False)
    assert _verdict(tmp_path) == "needs-director"
    assert any("CLAUDE FAILURE: claude_not_found" in c for c in _calls(tmp_path))


@pytest.mark.parametrize("body", [f"key {FAKE_SECRET}", f'"api_key": "{FAKE_SECRET}"', f'password="{FAKE_SECRET} x"',
                                  f"API_KEY={FAKE_SECRET}"])
@pytest.mark.parametrize("engine,priority,verdict", [
    ("claude", "P2", "needs-director"), ("claude", "P3", "done"), ("ensemble", "P2", "needs-director"),
])
def test_claude_findings_are_redacted_before_the_result(tmp_path, monkeypatch, body, engine, priority, verdict):
    # claude は読んだファイルの中身を body に写しうる。title / body / file の値ごとに伏せ字を通す (Codex #316 r1 P2)
    leak = json.dumps({"findings": [{"priority": priority, "title": f"t {FAKE_SECRET}", "body": body,
                                     "file": f"x/{FAKE_SECRET}"}]})
    p, capture = run(tmp_path, monkeypatch, engine=engine, claude_findings=leak)
    assert _verdict(tmp_path) == verdict, (p.stdout, p.stderr)
    text = _result(capture, verdict)
    assert FAKE_SECRET not in text and "REDACTED" in text
    assert FAKE_SECRET not in "\n".join(_calls(tmp_path))


def test_claude_redaction_failure_withholds_the_findings(tmp_path, monkeypatch, capsys):
    import lib_checkpoint

    def boom(_t):
        raise lib_checkpoint.RedactionError("x")
    monkeypatch.setattr(lib_checkpoint, "redact", boom)
    out = tmp_path / "out.json"
    out.write_text(_doc(structured_output={"findings": [
        {"priority": "P2", "title": f"t {FAKE_SECRET}", "body": None, "file": None}]}))
    rc = lre.main(["extract-claude", "--out", str(out),
                   "--schema", str(REPO_ROOT / "config" / "kai-review-findings.schema.json")])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == "" and "redaction_failed" in cap.err and FAKE_SECRET not in cap.err


def test_claude_failure_diagnostics_name_the_denied_tool_not_its_input(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, claude_mode="denied")
    text = "\n".join(_calls(tmp_path))
    assert "claude diagnostics" in text and "denied tool: Bash" in text
    assert '"command"' not in text


# --- ensemble: agy と claude の合議 ---

@pytest.mark.parametrize("agy_f,claude_f,verdict", [
    (CLEAN, CLEAN, "done"),
    (P2_AGY, CLEAN, "needs-director"),
    (CLEAN, P2_CLAUDE, "needs-director"),
    (P2_AGY, P2_CLAUDE, "needs-director"),
    (P3_AGY, CLEAN, "done"),
    (CLEAN, P3_CLAUDE, "done"),
    (P3_AGY, P3_CLAUDE, "done"),
])
def test_ensemble_verdict_table(tmp_path, monkeypatch, agy_f, claude_f, verdict):
    p, capture = run(tmp_path, monkeypatch, engine="ensemble", agy_findings=agy_f, claude_findings=claude_f)
    assert _verdict(tmp_path) == verdict, (p.stdout, p.stderr)
    text = _result(capture, verdict)
    assert "### agy raw output" in text and "### claude raw output" in text
    for f, eng in ((agy_f, "agy"), (claude_f, "claude")):
        for item in json.loads(f)["findings"]:
            assert item["title"] in text                                  # 両者の findings を併記
    assert "ensemble agy=" in text.splitlines()[0] or verdict == "needs-director"


def test_ensemble_findings_are_tagged_by_engine_and_duplicates_kept(tmp_path, monkeypatch):
    same = json.dumps({"findings": [{"priority": "P2", "title": "same-bug", "body": "B", "file": "README"}]})
    p, capture = run(tmp_path, monkeypatch, engine="ensemble", agy_findings=same, claude_findings=same)
    text = _result(capture, "needs-director")
    assert text.count("#### ") == 2 and "- engine: agy" in text and "- engine: claude" in text
    assert "P2×2" in text.splitlines()[0]


@pytest.mark.parametrize("agy_mode,claude_mode,tag", [
    ("exit3", "ok", "agy=failed:exit_3 claude=ok"),
    ("ok", "exit3", "agy=ok claude=failed:exit_3"),
    ("ok", "dirty", "agy=ok claude=failed:worktree_modified"),
    ("garbage", "is_error", "agy=failed:unparseable_output claude=failed:is_error"),
])
def test_ensemble_any_failure_needs_director_even_if_the_other_is_clean(tmp_path, monkeypatch, agy_mode, claude_mode, tag):
    p, capture = run(tmp_path, monkeypatch, engine="ensemble", agy_mode=agy_mode, claude_mode=claude_mode)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert not (capture / "done.txt").exists()
    assert any(f"ENSEMBLE FAILURE: {tag}" in c for c in _calls(tmp_path)), _calls(tmp_path)
    # agy が失敗しても claude は走る (両方の状態を残す)
    assert (tmp_path / "claude_argv.txt").exists()


def test_ensemble_does_not_need_codex(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="ensemble", with_codex=False)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert (tmp_path / "agy_argv.txt").exists() and (tmp_path / "claude_argv.txt").exists()


# --- both + compare: claude は併記だけ (判定は codex) ---

def test_both_with_claude_in_compare_is_appended_not_judged(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", with_codex=True, claude_findings=P2_CLAUDE,
                     config_extra="  compare: [claude]\n")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    text = _result(capture, "done")
    assert "### claude (比較用" in text and "claude-found" in text
    assert not (tmp_path / "agy_argv.txt").exists()


def test_both_claude_failure_is_recorded_but_does_not_decide(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", with_codex=True, claude_mode="exit3",
                     config_extra="  compare: [claude]\n")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert "claude failed: exit_3" in _result(capture, "done")


def test_default_codex_engine_does_not_call_claude(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="codex", with_codex=True)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert not (tmp_path / "claude_argv.txt").exists()
