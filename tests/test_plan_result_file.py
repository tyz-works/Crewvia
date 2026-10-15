#!/usr/bin/env python3
"""plan.sh の Result / 理由 / notes を、ファイルか標準入力で受け取れる (C1 / 運用の穴 #4)。

## 背景

`plan.sh done <id> "<Result>"` の二重引用符の中の **バッククォートと `$(...)` は、plan.sh が
起動する前にシェルが展開する** (コマンド置換)。2026-09-28、Worker (t074) が Result に書いた
`pgrep` 待ちループを実行してしまい 12 分止まった。文書の規則 (「クォート付きヒアドキュメントで
渡せ」) では防げないので、展開の起きない経路 (`--result-file <path>` / `--result-file -`) を仕組みにした。

## このファイルが固定すること

1. 危険の再現 (対照): 二重引用符の位置引数では sentinel が作られる。ファイル / 標準入力では作られず、
   本文がそのまま card に残る (陽性対照がないと「作られない」は何も証明しない)
2. 位置引数との併用・読めない・空・非 UTF-8・通常ファイルでない、は exit 2 で card を 1 バイトも変えない
3. 位置引数の呼び出し (kai-review.sh 等の既存経路) はそのまま通る
4. QA Gate / required_evidence の検査が、ファイル経由の Result でも同じに働く
5. Result を受け取る全サブコマンドを**実装から**列挙し、`--result-file` / `--notes-file` を持つものと
   持たない理由のあるものを表で固定する (新しい本文引数が黙って増えたら赤)

赤の実証は tests/red_proof_c1_result_file.sh。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

from e5_autoname import AutoName

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_SH = pathlib.Path(os.environ.get("PLAN_SH_UNDER_TEST") or REPO_ROOT / "scripts" / "plan.sh")

MISSION = "m-c1"

#: シェルが展開したら sentinel を作る本文。plan.sh に届いた文字列がこのままなら安全。
def hostile_body(sentinel: pathlib.Path) -> str:
    return (
        "PR #1 を作った。\n"
        f"バッククォート: `touch {sentinel}`\n"
        f"コマンド置換: $(touch {sentinel})\n"
        "変数: $HOME と ${AGENT_NAME}\n"
        "二重引用符: \"quoted\" と 'single'\n"
    )


class Sandbox:
    """使い捨ての queue / registry。CREWVIA_QUEUE と CREWVIA_REPO_ROOT の両方を root に向ける。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.queue = root / "queue"
        self.target = root / "work"
        self.target.mkdir()
        self.names = AutoName()                 # E5 PR-2: 自分の pull の execution_id を報告で名乗る

    def env(self, **extra):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "CREWVIA_QUEUE": str(self.queue),
            "CREWVIA_REPO_ROOT": str(self.root),
            "CREWVIA_TASKVIA": "disabled",
            "TARGET_DIR": str(self.target),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(extra)
        return env

    def plan(self, *args, stdin=None, stdin_bytes=None, **env):
        args = self.names.before(args)
        kw = {}
        if stdin_bytes is not None:
            kw["input"] = stdin_bytes
            text = False
        else:
            kw["input"] = stdin if stdin is not None else ""
            text = True
        r = subprocess.run(
            ["bash", str(PLAN_SH), *args], env=self.env(**env), cwd=str(self.root),
            capture_output=True, text=text, timeout=60, **kw,
        )
        if not text:
            r.stdout = r.stdout.decode("utf-8", "replace")
            r.stderr = r.stderr.decode("utf-8", "replace")
        self.names.after(args, r.stdout, r.returncode)
        return r

    def shell(self, script: str):
        """`bash -c` で、人間 / Worker が実際に打つ形 (二重引用符の位置引数) を再現する。"""
        return subprocess.run(
            ["bash", "-c", script], env=self.env(PLAN=str(PLAN_SH)), cwd=str(self.root),
            capture_output=True, text=True, timeout=60,
        )

    def card_path(self, task="t001"):
        return self.queue / "missions" / MISSION / "tasks" / f"{task}.md"

    def card(self, task="t001"):
        return self.card_path(task).read_text()

    def status(self, task="t001"):
        m = re.search(r"^status:\s*(\S+)", self.card(task), re.M)
        return m.group(1).strip("\"'") if m else None

    def frozen(self):
        """queue と registry/workers.yaml の中身 (task-graph の生成物は mtime だけ変わるので除く)。"""
        snap = {}
        for p in sorted(self.root.rglob("*")):
            rel = str(p.relative_to(self.root))
            if rel.startswith("registry/task-graph") or not p.is_file():
                continue
            snap[rel] = p.read_bytes()
        return snap


@pytest.fixture()
def sb(tmp_path):
    return Sandbox(tmp_path)


def _ok(r):
    assert r.returncode == 0, f"rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return r


def in_progress_task(sb, *, n=1, extra_frontmatter=""):
    """t001 (と n>1 なら t002...) を in_progress にした mission を作る。"""
    _ok(sb.plan("init", "C1", "--mission", MISSION))
    for i in range(n):
        _ok(sb.plan("add", f"task {i + 1}", "--mission", MISSION, "--skills", "code",
                    "--target-dir", str(sb.target)))
    for i in range(n):
        _ok(sb.plan("pull", "--task", f"t00{i + 1}", "--mission", MISSION,
                    "--agent", f"Ren{i}", "--skills", "code"))
    if extra_frontmatter:
        text = sb.card()
        text = re.sub(r"^(status:.*)$", lambda m: m.group(1) + "\n" + extra_frontmatter,
                      text, count=1, flags=re.M)
        with open(sb.card_path(), "w") as f:
            f.write(text)


# ---------------------------------------------------------------------------
# 1. 危険の再現と、ファイル / 標準入力の安全性
# ---------------------------------------------------------------------------

def test_control_double_quoted_argument_executes_the_command_substitution(sb):
    """対照: 位置引数を二重引用符で渡すと、シェルが sentinel を作る (= 今の危険の再現)。"""
    in_progress_task(sb)
    sentinel = sb.root / "SENTINEL_POSITIONAL"
    r = sb.shell(f'bash "$PLAN" done t001 "PR #1 `touch {sentinel}` $(touch {sentinel}.b)" '
                 f'--mission {MISSION} --no-pr "テスト" --execution {sb.names.ids["t001"]}')
    _ok(r)
    assert sentinel.exists(), "対照が効いていない: 二重引用符でもコマンド置換されなかった"
    assert pathlib.Path(f"{sentinel}.b").exists()


def test_result_file_keeps_the_body_verbatim_and_runs_nothing(sb):
    in_progress_task(sb)
    sentinel = sb.root / "SENTINEL_FILE"
    body = hostile_body(sentinel)
    path = sb.root / "result.md"
    with open(path, "w") as f:
        f.write(body)
    _ok(sb.plan("done", "t001", "--result-file", str(path), "--mission", MISSION,
                "--no-pr", "テスト"))
    assert not sentinel.exists(), "--result-file の本文が実行された"
    assert sb.status() == "done"
    for line in body.strip().splitlines():
        assert line in sb.card(), f"本文が変わった: {line!r}"


def test_result_from_stdin_keeps_the_body_verbatim_and_runs_nothing(sb):
    """`--result-file -` は呼び出し元の標準入力を読む (python 本体は fd 0 のヒアドキュメント)。"""
    in_progress_task(sb)
    sentinel = sb.root / "SENTINEL_STDIN"
    body = hostile_body(sentinel)
    _ok(sb.plan("done", "t001", "--result-file", "-", "--mission", MISSION,
                "--no-pr", "テスト", stdin=body))
    assert not sentinel.exists(), "標準入力の本文が実行された"
    assert sb.status() == "done"
    for line in body.strip().splitlines():
        assert line in sb.card(), f"本文が変わった: {line!r}"


def test_stdin_through_a_quoted_heredoc_in_a_real_shell(sb):
    """Worker が実際に打つ形 (クォート付きヒアドキュメントを標準入力へ)。"""
    in_progress_task(sb)
    sentinel = sb.root / "SENTINEL_HEREDOC"
    r = sb.shell(
        f'bash "$PLAN" done t001 --result-file - --mission {MISSION} --no-pr "テスト" '
        f'--execution {sb.names.ids["t001"]} '
        f"<<'RESULT_EOF'\nPR #1\n`touch {sentinel}` $(touch {sentinel})\nRESULT_EOF\n")
    _ok(r)
    assert not sentinel.exists()
    assert "`touch " in sb.card()


def test_positional_result_still_works(sb):
    """後方互換: kai-review.sh 等の位置引数の呼び出しはそのまま通る。"""
    in_progress_task(sb)
    _ok(sb.plan("done", "t001", "従来の位置引数の Result", "--mission", MISSION,
                "--no-pr", "テスト"))
    assert sb.status() == "done"
    assert "従来の位置引数の Result" in sb.card()


# ---------------------------------------------------------------------------
# 2. 拒否 (exit 2・card は 1 バイトも変わらない)
# ---------------------------------------------------------------------------

def _file(sb, name, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    p = sb.root / name
    with open(p, "wb") as f:
        f.write(data)
    return p


def _bad_inputs(sb):
    """(ラベル, plan.sh に渡す本文引数)。`done` / `needs-director` / `verify-result` で共通。"""
    return [
        ("missing", ["--FLAG", str(sb.root / "does-not-exist.md")]),
        ("empty", ["--FLAG", str(_file(sb, "empty.md", b""))]),
        ("blank", ["--FLAG", str(_file(sb, "blank.md", b" \n\t\n"))]),
        ("non-utf8", ["--FLAG", str(_file(sb, "latin1.md", "結果".encode("cp932")))]),
        ("directory", ["--FLAG", str(sb.root)]),
    ]


@pytest.mark.parametrize("label", ["missing", "empty", "blank", "non-utf8", "directory"])
def test_done_rejects_unusable_result_file_and_writes_nothing(sb, label):
    in_progress_task(sb)
    args = dict(_bad_inputs(sb))[label]
    before = sb.frozen()
    r = sb.plan("done", "t001", *[a.replace("--FLAG", "--result-file") for a in args],
                "--mission", MISSION, "--no-pr", "テスト")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "--result-file" in r.stderr
    assert sb.frozen() == before
    assert sb.status() == "in_progress"


@pytest.mark.parametrize("label", ["empty", "non-utf8"])
def test_stdin_empty_or_non_utf8_is_rejected_and_writes_nothing(sb, label):
    in_progress_task(sb)
    before = sb.frozen()
    data = b"" if label == "empty" else "結果".encode("cp932")
    r = sb.plan("done", "t001", "--result-file", "-", "--mission", MISSION,
                "--no-pr", "テスト", stdin_bytes=data)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert sb.frozen() == before


def test_positional_and_file_together_is_rejected_and_writes_nothing(sb):
    in_progress_task(sb)
    path = _file(sb, "r.md", b"file body")
    before = sb.frozen()
    r = sb.plan("done", "t001", "positional body", "--result-file", str(path),
                "--mission", MISSION, "--no-pr", "テスト")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "同時に指定できません" in r.stderr
    assert sb.frozen() == before


def test_done_without_any_body_is_still_rejected(sb):
    in_progress_task(sb)
    before = sb.frozen()
    r = sb.plan("done", "t001", "--mission", MISSION, "--no-pr", "テスト")
    assert r.returncode != 0
    assert sb.frozen() == before


# ---------------------------------------------------------------------------
# 3. needs-director / verify-result
# ---------------------------------------------------------------------------

def test_needs_director_reads_the_reason_from_file_and_runs_nothing(sb):
    in_progress_task(sb)
    sentinel = sb.root / "SENTINEL_ND"
    reason = "詰まった: `touch " + str(sentinel) + "` $(touch " + str(sentinel) + ")\n" + "詳細 " * 120
    path = _file(sb, "why.md", reason.encode())
    _ok(sb.plan("needs-director", "t001", "--result-file", str(path), "--mission", MISSION))
    assert not sentinel.exists()
    assert sb.status() == "needs_director"
    assert "`touch " in sb.card()     # 長い / 複数行は body の詳細節に全文が残る


def test_needs_director_positional_still_works_and_conflict_is_rejected(sb):
    in_progress_task(sb, n=2)
    path = _file(sb, "why.md", b"x")
    before = sb.frozen()
    r = sb.plan("needs-director", "t001", "reason", "--result-file", str(path), "--mission", MISSION)
    assert r.returncode == 2
    assert sb.frozen() == before
    _ok(sb.plan("needs-director", "t001", "位置引数の理由", "--mission", MISSION))
    assert sb.status() == "needs_director"


@pytest.mark.parametrize("label", ["missing", "empty", "non-utf8"])
def test_needs_director_rejects_unusable_file_and_writes_nothing(sb, label):
    in_progress_task(sb)
    args = dict(_bad_inputs(sb))[label]
    before = sb.frozen()
    r = sb.plan("needs-director", "t001", *[a.replace("--FLAG", "--result-file") for a in args],
                "--mission", MISSION)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert sb.frozen() == before
    assert sb.status() == "in_progress"


def test_verify_result_notes_file(sb):
    in_progress_task(sb)
    # verify-result は検証に出ている task (ready_for_verification / verifying / needs_human_review) だけ (01c E3。execution.md §4.3)
    _ok(sb.plan("ready-for-verification", "t001", "--mission", MISSION))
    sentinel = sb.root / "SENTINEL_VR"
    path = _file(sb, "notes.md", f"確認 `touch {sentinel}` $(touch {sentinel})\n".encode())
    _ok(sb.plan("verify-result", "t001", "needs_human_review", "--notes-file", str(path),
                "--mission", MISSION))
    assert not sentinel.exists()
    assert "`touch " in sb.card()


def test_verify_result_notes_file_conflicts_and_unusable_are_rejected(sb):
    in_progress_task(sb)
    path = _file(sb, "notes.md", b"n")
    before = sb.frozen()
    r = sb.plan("verify-result", "t001", "pass", "--notes", "a", "--notes-file", str(path),
                "--mission", MISSION)
    assert r.returncode == 2
    r = sb.plan("verify-result", "t001", "pass", "--notes-file", str(sb.root / "nope"),
                "--mission", MISSION)
    assert r.returncode == 2
    assert sb.frozen() == before


# ---------------------------------------------------------------------------
# 4. QA Gate / required_evidence はファイル経由でも同じに働く
# ---------------------------------------------------------------------------

def test_required_evidence_applies_to_a_result_read_from_a_file(sb):
    in_progress_task(sb, extra_frontmatter="required_evidence: [EVIDENCE_MARKER_X]")
    lacking = _file(sb, "lacking.md", "証拠の無い Result")
    before = sb.frozen()
    r = sb.plan("done", "t001", "--result-file", str(lacking), "--mission", MISSION,
                "--no-pr", "テスト")
    assert r.returncode != 0
    assert "required_evidence" in r.stderr
    assert sb.frozen() == before
    good = _file(sb, "good.md", "EVIDENCE_MARKER_X を確認した")
    _ok(sb.plan("done", "t001", "--result-file", str(good), "--mission", MISSION,
                "--no-pr", "テスト"))
    assert sb.status() == "done"


def test_qa_gate_applies_to_a_result_read_from_stdin(sb):
    in_progress_task(sb, extra_frontmatter="qa_checkpoints: [cp1]")
    r = sb.plan("done", "t001", "--result-file", "-", "--mission", MISSION, "--no-pr", "テスト",
                stdin="QA Gate セクションの無い Result")
    assert r.returncode != 0
    assert "QA Gate" in r.stderr
    assert sb.status() == "in_progress"
    gate = "## QA Gate\ncheckpoint: cp1 | required: yes | result: observed\n"
    _ok(sb.plan("done", "t001", "--result-file", "-", "--mission", MISSION, "--no-pr", "テスト",
                stdin=gate))
    assert sb.status() == "done"


# ---------------------------------------------------------------------------
# 5. 列挙 (実装から導く): 本文を受け取る引数を持つサブコマンドの表
# ---------------------------------------------------------------------------

#: サブコマンド → (本文を受け取る引数, ファイル / 標準入力で渡す option)。
BODY_SUBCOMMANDS = {
    "done": ("<result>", "--result-file"),
    "needs-director": ("<reason>", "--result-file"),
    "verify-result": ("--notes", "--notes-file"),
}

#: 本文引数を持たない / 持つが対象外で、理由を書いたもの。表に無い自由記述の引数が増えたら赤。
NOT_BODY = {
    "fail": "Result の本文が無い。引数は handoff_path (パス) と --head / --no-head (sha と 1 行の理由)",
}


def _usage_of(sub):
    text = PLAN_SH.read_text()
    block = re.search(r"^USAGE = \{(.*?)^\}", text, re.M | re.S).group(1)
    m = re.search(rf"^\s*'{re.escape(sub)}':\s*(.*?)(?=^\s*'[a-z-]+':|\Z)", block, re.M | re.S)
    assert m, sub
    return m.group(1)


@pytest.mark.parametrize("sub", sorted(BODY_SUBCOMMANDS))
def test_body_subcommands_advertise_the_file_option(sub):
    _, flag = BODY_SUBCOMMANDS[sub]
    assert flag in _usage_of(sub), f"{sub} の usage に {flag} が無い"


def test_every_body_argument_in_the_implementation_is_in_the_table():
    """本文を受け取る option (`--result-file` / `--notes-file`) の宣言を実装から全部拾い、表と一致させる。"""
    text = PLAN_SH.read_text()
    declared = {}
    for m in re.finditer(r"^def (cmd_[a-z_]+)\(args\):(.*?)(?=^def |\Z)", text, re.M | re.S):
        for flag in re.findall(r"'(--(?:result|notes)-file)':\s*'value'", m.group(2)):
            declared.setdefault(m.group(1)[4:].replace("_", "-"), set()).add(flag)
    expected = {sub: {flag} for sub, (_, flag) in BODY_SUBCOMMANDS.items()}
    assert declared == expected, f"実装: {declared} / 表: {expected}"


def test_free_text_options_not_in_the_table_are_only_single_line_reasons():
    """本文を持たないと判断した引数の一覧。増えたらここで気付く (表に理由を足すこと)。"""
    text = PLAN_SH.read_text()
    fn = re.search(r"^def cmd_fail\(args\):(.*?)(?=^def )", text, re.M | re.S).group(1)
    assert set(re.findall(r"'(--[a-z-]+)':\s*'value'", fn)) == {"--mission", "--head", "--no-head", "--execution"}
    assert set(NOT_BODY) == {"fail"}
