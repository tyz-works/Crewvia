#!/usr/bin/env python3
"""tests/plan_sh_compat_scenario.py — plan.sh の「外から見える挙動」を固定 fixture で 1 通り走らせる (S3 / 原案 §10.6 COMPAT-01)。

`run_scenario(src_root, root)` は隔離した queue で plan.sh の subcommand を決まった順に打ち、各段の
**exit code / stdout / stderr** と、最後の **queue の全ファイルの中身** (card・mission.yaml・state.yaml・
assignments) を正規化して辞書にする。正規化するのは実行ごとに変わる値だけ: 時刻 (ISO 8601)・今日の日付から
作られる slug・一時ディレクトリの絶対パス。**監査ログ (queue/audit/) と `.lock` は含めない** —
監査ログは S3 が足す唯一の新しい出力で、互換性の比較の対象ではない (別のテストが見る)。

golden (`tests/fixtures/plan_sh_compat_s3.golden.json`) は **cutover 前 (a1f6957) の plan.sh** でこれを走らせて
作った。`tests/test_plan_sh_compat_s3.py` が今の plan.sh の出力と比べる。差が出たら、それは外から見える挙動の変化:
意図したものなら `knowledge/state-store.md` §4.1 の表に足して golden を作り直す (作り直し方は下)。

    python3 tests/plan_sh_compat_scenario.py <cutover 前の repo の root> <出力 json>

**意図した変更 (GIT-05 / 01b G1)**: pull の JSON の `worktree_path` は、隔離コピーが stub の git-helpers.sh を持つので
`<ROOT>/.claude/worktrees/<SLUG>/<id>-<task_slug>` になる (cutover 前は helper が無く null)。golden の 6 箇所をこの値に
書き換えた。それ以外の 33 段・queue の全ファイルは cutover 前のまま。

`<root>` は `scripts/plan.sh` と `scripts/lib_*` `scripts/lint_plan.py` を持つ repo
(`git archive <sha> | tar -x -C <dir>`)。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
DATE_SLUG_RE = re.compile(r"\d{8}-compat-mission")


def _normalize(text: str, root: pathlib.Path) -> str:
    text = text.replace(str(root), "<ROOT>")
    text = DATE_SLUG_RE.sub("<SLUG>", text)
    return TS_RE.sub("<TS>", text)


class Runner:
    def __init__(self, plan_sh: pathlib.Path, root: pathlib.Path):
        self.plan = plan_sh
        self.root = root
        self.queue = root / "queue"
        self.log = []

    def env(self, agent):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "C.UTF-8",
               "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(self.root),
               "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}
        if agent:
            env["AGENT_NAME"] = agent
        return env

    def run(self, *args, agent=None, label=None):
        p = subprocess.run([str(self.plan), *args], env=self.env(agent), capture_output=True, text=True,
                           timeout=120)
        self.log.append({
            "cmd": label or " ".join(args),
            "rc": p.returncode,
            "stdout": _normalize(p.stdout, self.root),
            "stderr": _normalize(p.stderr, self.root),
        })
        return p

    def slug(self):
        return sorted(q.name for q in (self.queue / "missions").iterdir())[0]

    def field(self, tid, name):
        text = (self.queue / "missions" / self.slug() / "tasks" / f"{tid}.md").read_text()
        m = re.search(rf"^{name}: (.*)$", text, re.MULTILINE)
        return m.group(1).strip('"') if m else ""


def snapshot_queue(queue: pathlib.Path, root: pathlib.Path) -> dict:
    """queue の全ファイルの正規化した中身。監査ログのディレクトリと、中身の無いロックファイル
    (`.lock` / S5 で足された `.taskvia-map.json.lock`) は除く。"""
    out = {}
    for p in sorted(queue.rglob("*")):
        rel = p.relative_to(queue).as_posix()
        if not p.is_file() or rel.startswith("audit/") or rel in (".lock", ".taskvia-map.json.lock"):
            continue
        out[_normalize(rel, root)] = _normalize(p.read_text(), root)
    return out


def run_scenario(src_root: pathlib.Path, root: pathlib.Path) -> dict:
    """`src_root` (scripts/plan.sh と lib_* を持つ repo) の plan.sh を `root/scripts/` へ写し (fixture_tree の入口)、
    `root/queue` `root/registry` を作って走らせる。"""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from fixture_tree import copy_plan_tree
    plan_sh = copy_plan_tree(root, pathlib.Path(src_root))
    (root / "queue").mkdir(parents=True, exist_ok=True)
    (root / "registry").mkdir(exist_ok=True)
    r = Runner(plan_sh, root)

    r.run("init", "Compat mission")
    specs = [
        ("Plain", ["--skills", "bash"]),
        ("Colon: \"quoted\" 日本語", ["--skills", "code,bash", "--priority", "high",
                                      "--description", "line one # not a comment"]),
        ("Blocked on first", ["--skills", "bash", "--blocked-by", "t001"]),
        ("Deliverable pr", ["--skills", "bash", "--deliverable", "pr", "--priority", "low"]),
        ("Held dep child", ["--skills", "bash", "--blocked-by", "t004"]),
        ("Verified path", ["--skills", "bash", "--priority", "high"]),
    ]
    for title, extra in specs:
        r.run("add", title, *extra)
    r.run("status")
    r.run("lint", "--mission", r.slug(), label="lint")

    # --- pull の task 選択 (priority high の 2 枚: 若い番号が先) --------------------------------
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", label="pull (auto select 1)")
    r.run("pull", "--agent", "Sora", "--skills", "bash,code", label="pull (auto select 2)")
    picked = {a: (r.queue / "assignments" / a).read_text().strip().split(":")[1] for a in ("Ren", "Sora")}
    r.log.append({"cmd": "(picked by pull)", "rc": 0, "stdout": json.dumps(picked, sort_keys=True), "stderr": ""})
    assert picked == {"Ren": "t002", "Sora": "t006"}, picked      # 選択規則が変わったら scenario の前提が崩れる
    r.run("status")

    # --- needs-director → update --reset → 再 pull → done (assignment を撤去する) --------------
    r.run("needs-director", "t002", "long reason: " + "x" * 260 + "\nsecond line", agent="Ren")
    r.run("update", "t002", "--reset")
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", "--task", "t002")
    r.run("done", "t002", "finished: with colon\nand a 2nd line", "--no-pr", "compat", agent="Ren")
    r.run("done", "t002", "again", "--no-pr", "compat", agent="Ren", label="done (already done)")
    r.run("done", "t999", "x", "--no-pr", "compat", label="done (no such task)")

    # --- busy の拒否 / fail / release-dep (失敗した依存の保留の出口) --------------------------
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", "--task", "t004")
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", "--task", "t001", label="pull --task (busy Worker)")
    r.run("fail", "t004", "--no-head", "compat", agent="Ren")
    r.run("fail", "t004", "--no-head", "compat", agent="Ren", label="fail (already failed)")
    r.run("status")
    r.run("release-dep", "t005", "--dep", "t004")

    # --- ready-for-verification → verify-result ------------------------------------------
    r.run("ready-for-verification", "t006", agent="Sora")
    r.run("verify-result", "t006", "pass", "--notes", "looks fine", agent="Sora")

    # --- retire (reset) / reap-orphan-assignment / update ---------------------------------
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", "--task", "t001")
    gen = r.field("t001", "started_at")
    r.run("retire", "t001", "--agent", "Ren", "--started-at", gen, "--outcome", "reset", "--no-wait",
          label="retire (reset)")
    r.run("pull", "--agent", "Ren", "--skills", "bash,code", "--task", "t001")
    r.run("done", "t001", "orphan case", "--no-pr", "compat", label="done (no AGENT_NAME: assignment stays)")
    # 01c E3: done は card の worker (Ren) の枠を AGENT_NAME が無くても外す (execution.md §4.2)。cutover 前の plan.sh は
    # 外さず、孤児の枠 (Ren → t001) を reap が撤去する場面を作っていた。**同じ場面を両方の版で作る**ため、枠を書き直す
    # (cutover 前の版では同じバイトの書き直しで何も変わらない。golden は作り直していない)。
    (r.queue / "assignments" / "Ren").write_text(f"{r.slug()}:t001\n")
    r.run("reap-orphan-assignment", "Ren", "--no-wait")
    r.run("reap-orphan-assignment", "Ren", "--no-wait", label="reap-orphan-assignment (already gone)")
    r.run("update", "t003", "--priority", "medium", "--description", "edited: text")
    r.run("update", "t005", "--status", "skipped")
    r.run("update", "t003", "--status", "cancelled", label="update --status cancelled (refused)")
    r.run("status", "--all")

    before_move = snapshot_queue(r.queue, root)
    r.run("archive", "compat-does-not-exist", label="archive (no such mission)")
    r.run("archive", r.slug(), label="archive")
    return {"steps": r.log, "queue_before_archive": before_move,
            "queue_final": snapshot_queue(r.queue, root)}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src_root = pathlib.Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        result = run_scenario(src_root, root)
    pathlib.Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                         encoding="utf-8")
    print(f"steps={len(result['steps'])} files={len(result['queue_final'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
