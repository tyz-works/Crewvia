#!/usr/bin/env python3
"""
tests/test_daemon_backstop_hook.py

Regression テスト: hooks/post-tool-use.sh の同時死 backstop (t008)。

## 背景

dispatcher と watchdog の相互監視 (scripts/lib_daemon_watch.py) は「相手を見る」
仕組みなので、両方が同時に死ぬケース (herdr 再起動、OOM 等) はどちらも互いを
起こせない。この backstop は Director role の PostToolUse hook が
registry/daemons/{dispatcher,watchdog}.heartbeat の mtime だけを見て検知し、
exit code 2 (PostToolUse hook の "stderr を Claude に見せる" 契約) で
Director の文脈に 1 行流し込む。

このテストは **本物の hooks/post-tool-use.sh を subprocess で実行**する。
ロジックを Python で再実装したテストは post-tool-use.sh を直したことを
一切証明しないため使わない (memory: t024 の教訓と同じ形)。
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "post-tool-use.sh"

WORKERS_YAML = """\
workers:
  - name: Sora-director
    role: director
    skills: []
  - name: Wei
    role: worker
    skills: [bash, code, docs]
"""


def _make_registry(tmp_path: Path) -> Path:
    (tmp_path / "registry" / "daemons").mkdir(parents=True, exist_ok=True)
    (tmp_path / "registry" / "workers.yaml").write_text(WORKERS_YAML, encoding="utf-8")
    return tmp_path


def _age(tmp_path: Path, name: str, seconds_ago: float) -> None:
    path = tmp_path / "registry" / "daemons" / f"{name}.heartbeat"
    path.write_text("{}", encoding="utf-8")
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def _run_hook(tmp_path: Path, agent: str = "Sora-director", extra_env: dict | None = None):
    env = dict(os.environ)
    env.update({
        "CREWVIA_REPO_ROOT": str(tmp_path),
        "AGENT_NAME": agent,
        "CREWVIA_TASKVIA": "disabled",
    })
    # 本物のツール承認フロー・pre-tool-use.sh 等を一切経由しない、
    # post-tool-use.sh 単体の subprocess 実行。
    env.pop("TASK_ID", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK)],
        env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10,
    )


# ---------------------------------------------------------------------------
# 両方 stale → exit 2 + stderr に 1 行
# ---------------------------------------------------------------------------

def test_both_daemons_stale_signals_director_via_exit_2(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    result = _run_hook(tmp_path)

    assert result.returncode == 2, (
        f"both stale なのに exit={result.returncode} (expected 2). "
        f"stderr={result.stderr!r}"
    )
    assert "daemon-backstop" in result.stderr
    assert "dispatcher" in result.stderr and "watchdog" in result.stderr
    # クラッシュガードの誤発火が混ざっていないこと (意図的な exit 2 を
    # 「予期しないクラッシュ」と誤認していない)
    assert "crash guard" not in result.stderr


# ---------------------------------------------------------------------------
# throttle: 直後の 2 回目は exit 0 (再通知しない)
# ---------------------------------------------------------------------------

def test_throttle_suppresses_immediate_second_call(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    first = _run_hook(tmp_path)
    assert first.returncode == 2

    second = _run_hook(tmp_path)
    assert second.returncode == 0, (
        f"throttle window 内の 2 回目が exit={second.returncode} "
        f"(expected 0). stderr={second.stderr!r}"
    )
    assert second.stderr == "" or "daemon-backstop" not in second.stderr


# ---------------------------------------------------------------------------
# throttle が経過すれば再度検知できる
# ---------------------------------------------------------------------------

def test_throttle_window_expiry_allows_retrigger(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    throttle = tmp_path / "registry" / "daemons" / "backstop-notify.throttle"
    throttle.write_text("", encoding="utf-8")
    old = time.time() - 120  # throttle 窓 (60s) を過ぎた状態を模す
    os.utime(throttle, (old, old))

    result = _run_hook(tmp_path)
    assert result.returncode == 2, (
        f"throttle 窓経過後は再検知するはず。exit={result.returncode}, "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 片方だけ stale → 検知しない (それは通常の相互監視が respawn する対象)
# ---------------------------------------------------------------------------

def test_only_one_daemon_stale_does_not_trigger(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 0)  # fresh

    result = _run_hook(tmp_path)
    assert result.returncode == 0, (
        f"片方だけ stale なのに exit={result.returncode} (expected 0). "
        f"stderr={result.stderr!r}"
    )
    assert "daemon-backstop" not in result.stderr


def test_dispatcher_heartbeat_missing_entirely_counts_as_stale(tmp_path):
    """heartbeat ファイル自体が無い (一度も beat() されていない) 場合も
    「無限に stale」として扱う — が、もう片方が fresh なら検知しない。"""
    _make_registry(tmp_path)
    _age(tmp_path, "watchdog", 0)  # fresh。dispatcher.heartbeat は作らない

    result = _run_hook(tmp_path)
    assert result.returncode == 0
    assert "daemon-backstop" not in result.stderr


# ---------------------------------------------------------------------------
# director 以外 (Worker) では発火しない
# ---------------------------------------------------------------------------

def test_worker_role_never_triggers(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    result = _run_hook(tmp_path, agent="Wei")
    assert result.returncode == 0, (
        f"Worker (director ではない) で発火してしまった。exit={result.returncode}, "
        f"stderr={result.stderr!r}"
    )
    assert "daemon-backstop" not in result.stderr


# ---------------------------------------------------------------------------
# registry/daemons/ が存在しない (mutual watch が一度も動いていない /
# standalone・inline 運用) → 誤検知しない
# ---------------------------------------------------------------------------

def test_no_daemons_dir_is_not_treated_as_death(tmp_path):
    (tmp_path / "registry").mkdir(parents=True, exist_ok=True)
    (tmp_path / "registry" / "workers.yaml").write_text(WORKERS_YAML, encoding="utf-8")

    result = _run_hook(tmp_path)
    assert result.returncode == 0, (
        f"registry/daemons/ が無い (mutual watch 未使用) 環境で誤検知した。"
        f"exit={result.returncode}, stderr={result.stderr!r}"
    )
    assert "daemon-backstop" not in result.stderr


# ---------------------------------------------------------------------------
# しきい値は env var で上書きできる (lib_daemon_watch.py と同じ変数名)
# ---------------------------------------------------------------------------

def test_stale_threshold_is_overridable_via_env(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 30)  # 既定 60s 未満なので既定では stale ではない
    _age(tmp_path, "watchdog", 30)

    # 既定しきい値では検知しない
    baseline = _run_hook(tmp_path)
    assert baseline.returncode == 0

    throttle = tmp_path / "registry" / "daemons" / "backstop-notify.throttle"
    if throttle.exists():
        throttle.unlink()

    # しきい値を 10s まで下げると 30s 前の heartbeat は stale になる
    overridden = _run_hook(tmp_path, extra_env={
        "CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS": "10",
        "CREWVIA_DAEMON_WATCHDOG_STALE_SECONDS": "10",
    })
    assert overridden.returncode == 2, (
        f"env var でしきい値を下げても検知しなかった。exit={overridden.returncode}, "
        f"stderr={overridden.stderr!r}"
    )


# ---------------------------------------------------------------------------
# F-1: `stat -c` は GNU 専用。BSD/macOS は `stat -f` しか無く、フォールバック
# が無いと mtime が読めず heartbeat 側は「無限に stale」(誤発火)、throttle
# 側は「常に窓経過」(抑制が効かない) の二重故障になる。
# ---------------------------------------------------------------------------

def _write_fake_bsd_stat(tmp_path: Path) -> Path:
    """`-c` を拒否し `-f` だけを解釈する stat シム (実 BSD/macOS の再現)。
    PATH の先頭に置いて `stat -c ... || stat -f ... || ...` の
    フォールバック経路が実際に踏まれることを証明する。"""
    real_stat = which("stat")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    stat_path = bin_dir / "stat"
    stat_path.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  echo 'stat: illegal option -- c' >&2\n"
        "  exit 1\n"
        "fi\n"
        "if [ \"$1\" = \"-f\" ]; then\n"
        "  shift; fmt=\"$1\"; shift\n"
        "  if [ \"$fmt\" = \"%m\" ]; then\n"
        f"    exec {real_stat} -c %Y \"$@\"\n"
        "  fi\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stat_path.chmod(0o755)
    return bin_dir


def test_bsd_only_stat_does_not_false_positive_on_healthy_daemons(tmp_path):
    """フォールバックが無いと、健全 (fresh) な heartbeat すら mtime が読めず
    -1 (無限に stale) 扱いになり、BSD/macOS の Director に常に誤発火する。"""
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 0)
    _age(tmp_path, "watchdog", 0)

    fakebin = _write_fake_bsd_stat(tmp_path)
    result = _run_hook(tmp_path, extra_env={"PATH": f"{fakebin}:{os.environ['PATH']}"})

    assert result.returncode == 0, (
        f"BSD 専用 stat 環境で健全な daemon を stale と誤検知した (F-1 回帰)。"
        f"exit={result.returncode}, stderr={result.stderr!r}"
    )
    assert "daemon-backstop" not in result.stderr


def test_bsd_only_stat_throttle_still_suppresses_second_call(tmp_path):
    """throttle マーカーの mtime 読み取りも同じ `stat -c` 依存だった。
    フォールバックが無いと mtime が常に読めず、窓が「常に経過済み」に
    倒れて 2 回目以降も毎回発火してしまう (要件2 違反)。"""
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    fakebin = _write_fake_bsd_stat(tmp_path)
    env = {"PATH": f"{fakebin}:{os.environ['PATH']}"}

    first = _run_hook(tmp_path, extra_env=env)
    assert first.returncode == 2

    second = _run_hook(tmp_path, extra_env=env)
    assert second.returncode == 0, (
        f"BSD 専用 stat 環境で throttle が効かず 2 回目も発火した (F-1 回帰)。"
        f"exit={second.returncode}, stderr={second.stderr!r}"
    )


# ---------------------------------------------------------------------------
# O-1: 位置依存の `grep -A3` は、director エントリの直前の Worker エントリが
# `- name:` のみ (role/skills 欠落) だと次エントリの role: 行まで読んでしまう。
# 現行 workers.yaml の Ren (Sora-director の直前) がまさにこの形。
# ---------------------------------------------------------------------------

def test_role_lookup_does_not_bleed_into_next_workers_yaml_entry(tmp_path):
    (tmp_path / "registry" / "daemons").mkdir(parents=True, exist_ok=True)
    (tmp_path / "registry" / "workers.yaml").write_text(
        "workers:\n"
        "  - name: Ren\n"
        "  - name: Sora-director\n"
        "    role: director\n"
        "    skills: []\n",
        encoding="utf-8",
    )
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    result = _run_hook(tmp_path, agent="Ren")
    assert result.returncode == 0, (
        f"role フィールドを持たない Worker (Ren) が次エントリの director に "
        f"誤判定された (O-1 回帰)。exit={result.returncode}, stderr={result.stderr!r}"
    )
    assert "daemon-backstop" not in result.stderr


# ---------------------------------------------------------------------------
# O-2: 不正な (非数値の) しきい値 env は lib_daemon_watch.py の load_config()
# と同じく「既定値を保って無視する」べき。以前は `[[ -ge ]]` が非数値を
# 未束縛の変数参照として評価し、set -u で hook 全体が crash guard に握り
# 潰されて、意図した通知が出ないまま exit 0 になっていた。
# ---------------------------------------------------------------------------

def test_invalid_env_threshold_falls_back_to_default_instead_of_aborting(tmp_path):
    _make_registry(tmp_path)
    _age(tmp_path, "dispatcher", 3600)
    _age(tmp_path, "watchdog", 3600)

    result = _run_hook(tmp_path, extra_env={
        "CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS": "not-a-number",
    })
    assert result.returncode == 2, (
        f"不正な env しきい値で既定値へフォールバックせず握り潰された (O-2 回帰)。"
        f"exit={result.returncode}, stderr={result.stderr!r}"
    )
    assert "crash guard" not in result.stderr
    assert "daemon-backstop" in result.stderr


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
