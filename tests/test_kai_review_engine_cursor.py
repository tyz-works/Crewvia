"""kai-review.sh のレビュー先 cursor (Cursor CLI: cursor-agent) と compare の一覧。cursor-agent は stub。
判定は codex と同じ経路 (findings の JSON) を通る。設計・実測: knowledge/cursor-review.md"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kai_review_keeps_full_codex_findings_in_the_card import REPO_ROOT, run_kai_review  # noqa: E402
from test_kai_review_engine_agy import AGY_STUB, CLEAN, CODEX_P1, FAKE_SECRET  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lib_review_engine as lre  # noqa: E402

CURSOR_P1 = json.dumps({"findings": [{"priority": "P1", "title": "cursor-found", "body": "B", "file": "README"}]})
SCHEMA = json.loads((REPO_ROOT / "config" / "kai-review-findings.schema.json").read_text())


def _envelope(result, *, subtype="success", is_error=False):
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": result,
                       "usage": {"inputTokens": 1}})


# cursor-agent stub: CURSOR_MODE で挙動を変える。argv・cwd を残す。both の順序を見るため、
# 呼ばれた時点で TMPDIR に codex の一時ファイル (出力・stderr。空でも) / agy の一時物が残っていたか、
# agy が既に走ったかを CURSOR_SEEN_FILE に残す。
CURSOR_STUB = r'''#!/bin/bash
printf '%s\n' "$@" > "$CURSOR_ARGV_FILE"
pwd > "$CURSOR_CWD_FILE"
{ for f in "$TMPDIR"/kai-review-output.* "$TMPDIR"/kai-review-stderr.*; do [[ -e "$f" ]] && echo "codex-tmp:$f"; done
  ls -d "$TMPDIR"/kai-review-agy* 2>/dev/null | sed 's/^/agy:/'
  [[ -e "$AGY_ARGV_FILE" ]] && echo agy-ran; } > "$CURSOR_SEEN_FILE"
envelope() { python3 -c 'import json,sys; print(json.dumps({"type":"result","subtype":sys.argv[2],"is_error":sys.argv[3]=="1","result":sys.argv[1],"usage":{}}))' "$1" "${2:-success}" "${3:-0}"; }
case "$CURSOR_MODE" in
  ok)        envelope "前置きの文です。差分を突き合わせます。$CURSOR_FINDINGS" ;;
  fenced)    envelope $'Here is the result:\n```json\n'"$CURSOR_FINDINGS"$'\n```\n' ;;
  bare)      envelope "$CURSOR_FINDINGS" ;;
  action)    echo "ActionRequiredError: gpt-5.6-sol-high is not included in your weekly usage. token=$CURSOR_FAKE_SECRET" >&2; exit 1 ;;
  sleep)     sleep 30 ;;
  is_error)  envelope "$CURSOR_FINDINGS" success 1 ;;
  subtype)   envelope "$CURSOR_FINDINGS" error_max_turns ;;
  nojson)    envelope "LGTM, no issues." ;;
  trailing)  envelope "$CURSOR_FINDINGS 以上です。" ;;
  schema)    envelope '{"findings":[{"priority":"P9","title":"x","body":null,"file":null}]}' ;;
  extra)     envelope '{"findings":[],"verdict":"ok"}' ;;
  garbage)   echo 'not json' ;;
  dirty)     echo x > injected.txt; envelope "$CURSOR_FINDINGS" ;;
  dirty_fail) echo x > injected.txt; exit 3 ;;
  escape)    for d in "$TMPDIR"/kai-review-wt.*; do echo x > "$d/escaped.txt"; done; envelope "$CURSOR_FINDINGS" ;;
  ignore_term) trap '' TERM; sleep 300 ;;
  ignored_write) echo secret.local >> "$(git rev-parse --git-path info/exclude)"; echo x > secret.local; envelope "$CURSOR_FINDINGS" ;;
  escape_ignored) for d in "$TMPDIR"/kai-review-wt.*; do echo x > "$d/secret.local"; done
             echo secret.local >> "$(git rev-parse --git-path info/exclude)"; envelope "$CURSOR_FINDINGS" ;;
  commit)    echo x >> README; git -c user.email=a@b -c user.name=a commit -qam x; envelope "$CURSOR_FINDINGS" ;;
  checkout)  git checkout -q --detach HEAD~1; envelope "$CURSOR_FINDINGS" ;;
  pretty)    envelope "結果です。"$'\n'"$(printf '%s' "$CURSOR_FINDINGS" | python3 -m json.tool --indent 2)" ;;
esac
'''


def run(tmp_path, monkeypatch, *, engine="cursor", cursor_mode="ok", cursor_findings=CLEAN, codex_out=CLEAN,
        agy_mode="ok", agy_findings=CLEAN, config_text=None, extra_config="", with_codex=True, timeout=60):
    for k, v in {"CURSOR_MODE": cursor_mode, "CURSOR_FINDINGS": cursor_findings, "CURSOR_FAKE_SECRET": FAKE_SECRET,
                 "CURSOR_ARGV_FILE": str(tmp_path / "cursor_argv.txt"),
                 "CURSOR_CWD_FILE": str(tmp_path / "cursor_cwd.txt"),
                 "CURSOR_SEEN_FILE": str(tmp_path / "cursor_seen.txt"),
                 "AGY_MODE": agy_mode, "AGY_FINDINGS": agy_findings, "AGY_FAKE_SECRET": FAKE_SECRET,
                 "AGY_ARGV_FILE": str(tmp_path / "agy_argv.txt"), "AGY_CWD_FILE": str(tmp_path / "agy_cwd.txt")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CREWVIA_REVIEW_ENGINE", raising=False)
    if config_text is None:
        config_text = (f"review:\n  engine: {engine}\n  cursor_model: grok-4.7-high\n"
                       f"  cursor_timeout_seconds: {timeout}\n  agy_timeout_seconds: 90\n{extra_config}")
    return run_kai_review(tmp_path, codex_out, config_text=config_text,
                          extra_exes={"cursor-agent": CURSOR_STUB, "agy": AGY_STUB}, with_codex=with_codex)


def _calls(tmp_path):
    return (tmp_path / "calls.txt").read_text().splitlines()


def _verdict(tmp_path):
    kinds = [c.split()[0] for c in _calls(tmp_path) if c.split()[0] in ("done", "needs-director")]
    assert len(kinds) == 1, _calls(tmp_path)
    return kinds[0]


def _result(capture, kind):
    return (capture / f"{kind}.txt").read_text()


def _text(tmp_path):
    return "\n".join(_calls(tmp_path))


# --- 設定の読み口 ---

def test_defaults_keep_the_current_behaviour(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("review:\n  engine: both\n")
    c = lre.load_full(cfg, environ={})
    assert c["compare"] == ["agy"]                                   # compare 無し = 今の both (agy だけ併記)
    assert c["cursor_model"] == "grok-4.7-high" and c["cursor_timeout"] == 900
    assert lre.load(cfg, environ={}) == ("both", lre.DEFAULT_AGY_MODEL, lre.DEFAULT_AGY_TIMEOUT_SECONDS)


def test_shipped_config_is_codex_with_compare_agy():
    c = lre.load_full(REPO_ROOT / "config" / "crewvia.yaml", environ={})
    assert c["engine"] == "codex" and c["compare"] == ["agy"] and c["cursor_model"] == "grok-4.7-high"


@pytest.mark.parametrize("value,expected", [
    ("[agy, cursor]", ["agy", "cursor"]), ("[cursor]", ["cursor"]), ("[cursor, agy]", ["cursor", "agy"]),
])
def test_compare_lists_are_read(tmp_path, value, expected):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  engine: both\n  compare: {value}\n")
    assert lre.load_full(cfg, environ={})["compare"] == expected


@pytest.mark.parametrize("value", ["[]", "[codex]", "[both]", "[agy, agy]", "agy", "[1]", "", "null", "[agy, gemini]",
                                   "[[agy]]", "[{a: 1}]", "[[agy], agy]"])
def test_bad_compare_is_an_error(tmp_path, value):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  engine: both\n  compare: {value}\n")
    with pytest.raises(lre.ReviewConfigError) as e:
        lre.load_full(cfg, environ={})
    assert e.value.code == "bad_compare"


@pytest.mark.parametrize("model", ["grok-4.7-high", "gpt-5.6-sol-high", "grok-4.7[effort=high,fast=false]", "composer-2"])
def test_cursor_model_accepts_names_and_bracket_parameters(tmp_path, model):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  cursor_model: '{model}'\n")
    assert lre.load_full(cfg, environ={})["cursor_model"] == model


@pytest.mark.parametrize("model", ["claude-fable-5-1", "Claude-Fable-5-1-thinking", "-p", "a b", "x;rm", "", "null"])
def test_cursor_model_rejects_no_zdr_and_bad_names(tmp_path, model):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  cursor_model: {model if model == 'null' else repr(model)}\n")
    with pytest.raises(lre.ReviewConfigError) as e:
        lre.load_full(cfg, environ={})
    assert e.value.code == "bad_cursor_model"


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "true", "null"])
def test_bad_cursor_timeout_is_an_error(tmp_path, value):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"review:\n  cursor_timeout_seconds: {value}\n")
    with pytest.raises(lre.ReviewConfigError) as e:
        lre.load_full(cfg, environ={})
    assert e.value.code == "bad_cursor_timeout"


def test_env_selects_cursor(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("review:\n  engine: codex\n")
    assert lre.load_full(cfg, environ={"CREWVIA_REVIEW_ENGINE": "cursor"})["engine"] == "cursor"


# --- findings の取り出し (lib) ---

@pytest.mark.parametrize("result", [
    'preamble. {"findings":[]}', '{"findings":[]}', 'x\n```json\n{"findings":[]}\n```', '{"findings": []}  \n',
    # 前置きの中に {"findings" が出ても、末尾の JSON を採る
    'I will output {"findings" ...} at the end. {"findings":[]}',
])
def test_extract_takes_the_trailing_findings_json(result):
    assert lre.extract_cursor_findings(_envelope(result), SCHEMA) == {"findings": []}


@pytest.mark.parametrize("result", [
    '前置き\n{\n  "findings": []\n}', '{ "findings" : [] }', 'x\n```json\n{\n\t"findings": []\n}\n```\n',
    '{"findings" ...} は最後に出します。\n{\n  "findings": []\n}',
])
def test_extract_accepts_pretty_printed_json(result):
    """整形された JSON (`{` の後に改行・空白) も取り出す (Codex #313 r1 P2)。"""
    assert lre.extract_cursor_findings(_envelope(result), SCHEMA) == {"findings": []}


@pytest.mark.parametrize("out,code", [
    ("not json", "unparseable_output"), ("[1]", "unparseable_output"),
    (_envelope('{"findings":[]}', is_error=True), "is_error"),
    (json.dumps({"type": "result", "subtype": "success", "result": '{"findings":[]}'}), "is_error"),   # is_error 欠け
    (_envelope('{"findings":[]}', subtype="error"), "subtype"),
    (json.dumps({"type": "assistant", "subtype": "success", "is_error": False, "result": '{"findings":[]}'}), "subtype"),
    (_envelope("no json here"), "no_findings_json"),
    (_envelope('{"findings":[]} trailing words'), "no_findings_json"),
    (_envelope('{"findings":[{"priority":"P9","title":"t","body":null,"file":null}]}'), "schema_invalid"),
    (_envelope('{"findings":[],"extra":1}'), "schema_invalid"),
    (_envelope('{"findings":[{"priority":"P1","title":"t","body":null}]}'), "schema_invalid"),
    (json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": None}), "no_findings_json"),
])
def test_extract_is_fail_closed(out, code):
    with pytest.raises(lre.CursorOutputError) as e:
        lre.extract_cursor_findings(out, SCHEMA)
    assert e.value.code == code


# --- cursor 単独 ---

def test_cursor_clean_review_is_done_with_read_only_flags(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, codex_out="NOT USED")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    argv = (tmp_path / "cursor_argv.txt").read_text().splitlines()
    assert argv[0] == "-p"
    assert argv[argv.index("--mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "grok-4.7-high"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--trust" in argv and "--add-dir" in argv
    for banned in ("--force", "--yolo", "--approve-mcps", "--sandbox", "-f"):
        assert banned not in argv
    prompt = argv[argv.index("--add-dir") + 2:]                      # --add-dir <dir> の後が prompt (最後の位置引数)
    assert "review.diff" in "\n".join(prompt) and not any(a.startswith("--") for a in prompt[:1])
    assert "Do not run commands" in "\n".join(prompt)
    assert "+change" not in "\n".join(argv)                          # diff は argv に載せない
    assert "kai-review-cursor-wt." in (tmp_path / "cursor_cwd.txt").read_text()   # REVIEW_WT ではない専用の worktree
    assert "### cursor raw output" in _result(capture, "done")
    assert not list(tmp_path.glob("kai-review-cursor*"))              # 一時物 (専用 worktree を含む) は後片付けで消える


@pytest.mark.parametrize("mode", ["ok", "fenced", "bare", "pretty"])
def test_cursor_findings_need_director(tmp_path, monkeypatch, mode):
    p, capture = run(tmp_path, monkeypatch, cursor_mode=mode, cursor_findings=CURSOR_P1)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert "cursor-found" in _result(capture, "needs-director")


@pytest.mark.parametrize("mode,reason", [
    ("action", "exit_1"), ("is_error", "is_error"), ("subtype", "subtype"), ("nojson", "no_findings_json"),
    ("trailing", "no_findings_json"), ("schema", "schema_invalid"), ("extra", "schema_invalid"),
    ("garbage", "unparseable_output"), ("dirty", "worktree_modified"),
])
def test_cursor_fail_closed_never_lgtm(tmp_path, monkeypatch, mode, reason):
    p, capture = run(tmp_path, monkeypatch, cursor_mode=mode)          # findings は空 = 通せば LGTM になる形
    assert _verdict(tmp_path) == "needs-director", (mode, p.stdout, p.stderr)
    assert not (capture / "done.txt").exists()
    assert any(f"CURSOR FAILURE: {reason}" in c for c in _calls(tmp_path)), (_calls(tmp_path), p.stderr)


def test_cursor_timeout_is_fail_closed(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, cursor_mode="sleep", timeout=1)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: timeout" in c for c in _calls(tmp_path)), _calls(tmp_path)


def test_weekly_quota_refusal_keeps_one_redacted_line(tmp_path, monkeypatch):
    """枠外のモデル: 約 9 秒で exit 1・stdout 空・stderr に ActionRequiredError (2026-10-27 実測の形)。"""
    p, capture = run(tmp_path, monkeypatch, cursor_mode="action")
    text = _text(tmp_path)
    assert "CURSOR FAILURE: exit_1" in text and "cursor diagnostics" in text
    assert "ActionRequiredError: gpt-5.6-sol-high is not included in your weekly usage" in text
    assert FAKE_SECRET not in text and "REDACTED" in text


def test_failure_diagnostics_do_not_echo_the_result_text(tmp_path, monkeypatch):
    """is_error の stdout の result (レビュー本文・コード片) は診断に出さない。型・subtype・is_error だけ。"""
    p, capture = run(tmp_path, monkeypatch, cursor_mode="is_error",
                     cursor_findings=json.dumps({"findings": [], "note": "SECRET-BODY-TEXT"}))
    text = _text(tmp_path)
    assert "stdout is_error: True" in text and "stdout subtype: success" in text
    assert "SECRET-BODY-TEXT" not in text


@pytest.mark.parametrize("body", [f"key {FAKE_SECRET}", f'"api_key": "{FAKE_SECRET}"', f'password="{FAKE_SECRET} x"'])
def test_findings_are_redacted_before_the_result(tmp_path, monkeypatch, body):
    leak = json.dumps({"findings": [{"priority": "P2", "title": f"t {FAKE_SECRET}", "body": body, "file": None}]})
    p, capture = run(tmp_path, monkeypatch, cursor_findings=leak)
    text = _result(capture, "needs-director")
    assert FAKE_SECRET not in text and "REDACTED" in text


def test_redaction_failure_withholds_the_findings(monkeypatch):
    import lib_checkpoint

    def boom(_t):
        raise lib_checkpoint.RedactionError("x")
    monkeypatch.setattr(lib_checkpoint, "redact", boom)
    with pytest.raises(lib_checkpoint.RedactionError):
        lre.redact_findings({"findings": [{"priority": "P1", "title": "t", "body": None, "file": None}]})


@pytest.mark.parametrize("rel", [".cursor/mcp.json", "sub/dir/.cursor/rules/x.mdc", ".cursorrules", "pkg/.cursorrules",
                                 ".cursorignore"])
def test_cursor_does_not_run_when_the_checkout_carries_cursor_config(tmp_path, monkeypatch, rel):
    """--trust で PR の中身を信頼するので、Cursor の設定 (.cursor/ = MCP・権限・hook・rules、.cursorrules) を
    木のどこかに持ち込んだ PR では走らせない。"""
    import subprocess
    orig = run_kai_review.__globals__["_git"]

    def git_with_cursor_dir(root, *args):
        if args[:2] == ("commit", "-qam") and args[2] == "change":
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("{}")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return orig(root, *args)
    monkeypatch.setitem(run_kai_review.__globals__, "_git", git_with_cursor_dir)
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: workspace_has_cursor_config" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not (tmp_path / "cursor_argv.txt").exists()


def test_cursor_engine_does_not_require_codex(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, with_codex=False)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)


def test_cursor_missing_is_fail_closed(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("PATH", ":".join(d for d in os.environ["PATH"].split(":")
                                        if not os.access(os.path.join(d, "cursor-agent"), os.X_OK)))
    p, capture = run_kai_review(tmp_path, CLEAN, config_text="review:\n  engine: cursor\n")
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: cursor_agent_not_found" in c for c in _calls(tmp_path)), _calls(tmp_path)


# --- both + compare ---

def test_both_default_compare_is_agy_only(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", agy_findings=CURSOR_P1.replace("cursor", "agy"))
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    text = _result(capture, "done")
    assert "### agy (比較用 — 判定は Codex。model=gemini-4-argon-high)" in text and "agy-found" in text
    assert "### cursor" not in text
    assert not (tmp_path / "cursor_argv.txt").exists()


def test_both_with_agy_and_cursor_appends_both_in_order_and_codex_decides(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [agy, cursor]\n",
                     cursor_findings=CURSOR_P1)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)            # cursor が P1 でも判定は codex
    text = _result(capture, "done")
    a = text.index("### agy (比較用 — 判定は Codex。model=gemini-4-argon-high)")
    c = text.index("### cursor (比較用 — 判定は Codex。model=grok-4.7-high)")
    assert text.index("### Codex raw output") < a < c                  # 併記は compare の順
    assert "cursor-found" in text and "worktree の外も読める" in text


@pytest.mark.parametrize("compare", ["[agy, cursor]", "[cursor, agy]"])
def test_both_runs_cursor_last_after_the_other_temp_files_are_gone(tmp_path, monkeypatch, compare):
    """cursor は worktree の外を読み書きできうるので、codex と agy が走り終えて判定に要る結果をシェル変数に取り、
    一時ファイルを消した後に走らせる (Director #313 r3)。compare の順に関わらず cursor は最後・併記は compare の順。"""
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config=f"  compare: {compare}\n",
                     codex_out=CODEX_P1, cursor_findings=CURSOR_P1)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert (tmp_path / "cursor_seen.txt").read_text() == "agy-ran\n"
    text = _result(capture, "needs-director")
    a, c = text.index("### agy (比較用"), text.index("### cursor (比較用")
    assert (a < c) == (compare == "[agy, cursor]") and "cursor-found" in text


def test_both_cursor_failure_is_recorded_but_does_not_decide(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [cursor]\n", cursor_mode="action")
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    text = _result(capture, "done")
    assert "cursor failed: exit_1" in text and "ActionRequiredError" in text and FAKE_SECRET not in text
    assert not (tmp_path / "agy_argv.txt").exists()


def test_bad_compare_in_config_stops_the_review(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [codex]\n")
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert "bad_compare" in _text(tmp_path)


@pytest.mark.parametrize("rel", [".claude/settings.json", "sub/.claude/hooks/x.sh", ".mcp.json", ".codex/config.toml",
                                 ".vscode/settings.json", "AGENTS.md", "pkg/AGENTS.md"])
def test_cursor_does_not_run_when_the_pr_changes_agent_config(tmp_path, monkeypatch, rel):
    """crewvia 自身が .claude/ を持つので「ある」では拒否しない。PR が agent の設定を変えたら --trust で読ませない。"""
    import subprocess
    orig = run_kai_review.__globals__["_git"]

    def git_with_change(root, *args):
        if args[:2] == ("commit", "-qam") and args[2] == "change":
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return orig(root, *args)
    monkeypatch.setitem(run_kai_review.__globals__, "_git", git_with_change)
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: pr_changes_agent_config" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not (tmp_path / "cursor_argv.txt").exists()


def test_agent_config_already_on_base_does_not_block(tmp_path, monkeypatch):
    """base にある .claude/ (crewvia 自身の設定) は PR の変更ではないので走らせる。"""
    import subprocess
    orig = run_kai_review.__globals__["_git"]

    def git_with_base_config(root, *args):
        if args[:2] == ("commit", "-q") and args[2:] == ("-m", "base"):
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text("{}")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return orig(root, *args)
    monkeypatch.setitem(run_kai_review.__globals__, "_git", git_with_base_config)
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert (tmp_path / "cursor_argv.txt").exists()


@pytest.mark.parametrize("name,target", [(".claude", "/tmp"), ("docs/link", "../.claude"), ("innocent", "README")])
def test_cursor_does_not_run_when_the_pr_adds_a_symlink(tmp_path, monkeypatch, name, target):
    """symlink は行き先を問わず拒否 (名前が .claude で終わる / 設定の場所へ向ける)。"""
    import os
    import subprocess
    orig = run_kai_review.__globals__["_git"]

    def git_with_link(root, *args):
        if args[:2] == ("commit", "-qam") and args[2] == "change":
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, root / name)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return orig(root, *args)
    monkeypatch.setitem(run_kai_review.__globals__, "_git", git_with_link)
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: pr_changes_agent_config" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not (tmp_path / "cursor_argv.txt").exists()


# --- cursor は専用の使い捨て worktree で走る (Codex #313 r1 P1) ---

@pytest.mark.parametrize("mode", ["dirty", "dirty_fail"])
def test_both_cursor_dirtying_its_worktree_does_not_reach_codex(tmp_path, monkeypatch, mode):
    """cursor が自分の checkout を書き換えても (成功・途中失敗とも)、codex / agy の REVIEW_WT は汚れない。"""
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [agy, cursor]\n", cursor_mode=mode)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)               # 判定は codex のまま
    text = _result(capture, "done")
    assert "cursor failed: " in text
    agy_cwd = (tmp_path / "agy_cwd.txt").read_text().strip()
    cursor_cwd = (tmp_path / "cursor_cwd.txt").read_text().strip()
    assert "kai-review-wt." in agy_cwd and "kai-review-cursor-wt." in cursor_cwd and agy_cwd != cursor_cwd
    assert not list(tmp_path.glob("kai-review-cursor*"))                      # 専用 worktree は消えている


@pytest.mark.parametrize("mode", ["escape", "escape_ignored"])
@pytest.mark.parametrize("codex_out,verdict", [(CLEAN, "done"), (CODEX_P1, "needs-director")])
def test_both_cursor_writing_into_the_review_worktree_cannot_change_the_verdict(tmp_path, monkeypatch, mode,
                                                                                 codex_out, verdict):
    """cursor は判定の後に走るので、専用 worktree の外 (REVIEW_WT) を書き換えても判定は codex の結果のまま
    (脅威モデル: cursor の誤った書き込みから判定を守る。knowledge/cursor-review.md)。"""
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [cursor]\n", cursor_mode=mode,
                     codex_out=codex_out)
    assert _verdict(tmp_path) == verdict, (p.stdout, p.stderr)
    assert "REVIEW WORKTREE MODIFIED" not in _text(tmp_path)
    assert (tmp_path / "cursor_seen.txt").read_text() == ""          # codex の一時ファイルは先に消えている


def test_cursor_engine_dirty_worktree_is_removed_after_failure(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, cursor_mode="dirty_fail")
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: exit_3" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not list(tmp_path.glob("kai-review-cursor*"))


# --- ignored のファイルも検査する・SIGTERM を無視しても止める (Codex #313 r2 P2x2) ---

def test_cursor_writing_an_ignored_file_is_worktree_modified(tmp_path, monkeypatch):
    """`status --porcelain` には出ない ignored のファイルを足しても、前後の指紋で拾う。"""
    p, capture = run(tmp_path, monkeypatch, cursor_mode="ignored_write")
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: worktree_modified" in c for c in _calls(tmp_path)), _calls(tmp_path)


@pytest.mark.parametrize("mode", ["commit", "checkout"])
def test_cursor_moving_head_is_worktree_modified(tmp_path, monkeypatch, mode):
    """commit・checkout は status を空のまま中身を変える。指紋の HEAD で拾う (Codex #313 r3)。"""
    p, capture = run(tmp_path, monkeypatch, cursor_mode=mode)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: worktree_modified" in c for c in _calls(tmp_path)), _calls(tmp_path)


def test_cursor_does_not_run_when_the_pr_renames_agent_config_away(tmp_path, monkeypatch):
    """base の .claude/ の設定を外へ rename (= 消す) する PR も止める。rename 検出だと元のパスが出ない (agy #313 r3)。"""
    import subprocess
    orig = run_kai_review.__globals__["_git"]

    def git_with_rename(root, *args):
        if args[:2] == ("commit", "-q") and args[2:] == ("-m", "base"):
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text('{"hooks": {"PreToolUse": ["guard"]}}\n' * 5)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        if args[:2] == ("commit", "-qam") and args[2] == "change":
            subprocess.run(["git", "-C", str(root), "mv", ".claude/settings.json", "old-settings.json"], check=True)
        return orig(root, *args)
    monkeypatch.setitem(run_kai_review.__globals__, "_git", git_with_rename)
    p, capture = run(tmp_path, monkeypatch)
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: pr_changes_agent_config" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not (tmp_path / "cursor_argv.txt").exists()


def test_fingerprint_sees_a_rewritten_ignored_file(tmp_path):
    """既にある ignored のファイルの中身の書き換え (status の出力は前後で同じ) も指紋が変わる。"""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("local.json\n")
    (tmp_path / "local.json").write_text("{}\n")

    def fp():
        st = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z", "--ignored=matching",
                             "--untracked-files=all"], check=True, capture_output=True).stdout
        return st, lre.tree_fingerprint(st, str(tmp_path))
    st1, before = fp()
    (tmp_path / "local.json").write_text('{"hooks": "evil"}\n')
    st2, after = fp()
    assert st1 == st2 and b"local.json" in st1            # status だけでは区別できない
    assert before != after


def test_cursor_ignoring_sigterm_is_killed_and_fail_closed(tmp_path, monkeypatch):
    """SIGTERM を無視する cursor-agent でも --kill-after で止まり、needs-director (LGTM に倒さない)。"""
    import time
    t0 = time.monotonic()
    p, capture = run(tmp_path, monkeypatch, cursor_mode="ignore_term", timeout=1)
    assert time.monotonic() - t0 < 60, "timeout が SIGKILL に進まず待ち続けた"
    assert _verdict(tmp_path) == "needs-director", (p.stdout, p.stderr)
    assert any("CURSOR FAILURE: timeout_killed" in c for c in _calls(tmp_path)), _calls(tmp_path)
    assert not list(tmp_path.glob("kai-review-cursor*"))


def test_both_cursor_ignoring_sigterm_still_lets_codex_decide(tmp_path, monkeypatch):
    p, capture = run(tmp_path, monkeypatch, engine="both", extra_config="  compare: [cursor]\n",
                     cursor_mode="ignore_term", timeout=1)
    assert _verdict(tmp_path) == "done", (p.stdout, p.stderr)
    assert "cursor failed: timeout_killed" in _result(capture, "done")


def test_fingerprint_sees_inside_a_collapsed_ignored_dir_even_with_mtime_restored(tmp_path):
    """git は ignored の dir を `!! cache/` 1 行に畳む。中のファイルを書き換えて mtime を戻しても指紋が変わる。"""
    import os
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("cache/\n")
    (tmp_path / "cache" / "sub").mkdir(parents=True)
    f = tmp_path / "cache" / "sub" / "hook.json"
    f.write_text("{}\n")

    def fp():
        st = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z", "--ignored=matching",
                             "--untracked-files=all"], check=True, capture_output=True).stdout
        return st, lre.tree_fingerprint(st, str(tmp_path))
    st1, before = fp()
    assert b"!! cache/" in st1 and b"hook.json" not in st1          # 畳まれている
    old = f.stat()
    f.write_text("{1}\n")                                            # 同じ大きさ
    os.utime(f, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.utime(f.parent, ns=(f.parent.stat().st_atime_ns, f.parent.stat().st_mtime_ns))
    st2, after = fp()
    assert st1 == st2 and before != after
