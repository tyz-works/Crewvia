#!/usr/bin/env python3
"""ci.yml の ci-ok 集約 job と nightly の構造ガード (ci-wait-operations §5.2・PR-E)。

- ci-ok は ci-ok 以外の全 job を needs に持つ（足した job が黙って門の外に残らない）
- if: always()（無いと needs 失敗時に skipped になる）
- success 以外（skipped 含む）を落とす step がある
- schedule と workflow_dispatch がある。paths の絞りは無い
"""
import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_ci_ok_needs_every_other_job():
    jobs = _doc()["jobs"]
    others = set(jobs) - {"ci-ok"}
    assert len(others) >= 6, others
    assert set(jobs["ci-ok"]["needs"]) == others


def test_ci_ok_runs_always_and_rejects_non_success():
    job = _doc()["jobs"]["ci-ok"]
    assert "always()" in str(job["if"])
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert '!= "success"' in script and "sys.exit(1)" in script


def test_nightly_and_no_path_filter():
    on = _doc()[True] if True in _doc() else _doc()["on"]
    assert on["schedule"] and on["schedule"][0]["cron"]
    assert "workflow_dispatch" in on
    for trig in ("pull_request", "push"):
        assert "paths" not in on[trig] and "paths-ignore" not in on[trig]
