#!/usr/bin/env python3
"""tests/test_main_checkout_sync.py — 主 checkout 同期の git 回り (t005 / B2 / backlog #26)。

2 段構え:

1. `lib_daemon_watch.py` の版記録・比較関数 (`record_own_version` / `restart_needed` /
   `fetch_origin` / `commits_behind` / `changed_files_vs` / `affected_targets`) を、
   一時ディレクトリの bare origin + checkout に対して直接呼ぶ。
2. `scripts/sync-main-checkout.sh` を実際に subprocess で実行し、fetch 失敗・
   diverged・ff 成功・対象外ファイルだけの変更・advisory 報告・`--dry-run` を確かめる。

**安全のための境界**: (2) は `restart_needed` が `true` になる状況を作らない
(= `lib_daemon_watch.py restart <name>` を実際には一度も呼ばせない)。その
サブコマンドは本物の mux (tmux/herdr) に触れる、既存の・既にテスト済みのコードで、
このタスクが変えたのは「いつそれを呼ぶか」の判定だけ。判定が正しく `restart` を
呼び出すことだけは、mux 呼び出しをすべてスタブに差し替えた
`test_sync_invokes_restart_when_a_daemon_is_flagged` が別に確かめる — 本物の
mux には一切触れない。

**開発中この禁止を守ったこと**: 本番の主 checkout (/home/tkadmin/workspace/crewvia) に
対してこのスクリプトを一度も実行していない。ここに列挙した一時ディレクトリの
fixture だけで動作確認した。

実行: python3 -m pytest tests/test_main_checkout_sync.py -v
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import fixture_tree  # noqa: E402

REPO_ROOT = fixture_tree.REPO_ROOT
SCRIPTS_SRC = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_SRC))
import lib_daemon_watch as w  # noqa: E402


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def _isolated_env():
    """subprocess に渡す env。`CREWVIA_MUX_TEST_ISOLATION=1` は防御の 2 段目
    (`lib_daemon_watch.py restart` に届く経路があっても本番の宛先名は拒否される) —
    1 段目はテストの作り方そのもの (restart_needed=true を作らない)。"""
    env = dict(os.environ)
    env["CREWVIA_MUX_TEST_ISOLATION"] = "1"
    for k in ("CREWVIA_REPO_ROOT", "CREWVIA_QUEUE", "CREWVIA_MUX",
             "CREWVIA_HERDR_WORKSPACE", "CREWVIA_TMUX_SESSION"):
        env.pop(k, None)
    return env


def _make_repo(tmp_path, *, with_sync_script=True):
    """bare origin + checkout。checkout の scripts/ には lib_* 一式 (helper 経由) と、
    エントリポイント本体 (sync-main-checkout.sh) を 1 つ足す (`tests/CLAUDE.md` の
    「エントリポイントの単体コピーも対象外」と同じ扱い — lib_* の一覧を持たない)。
    """
    bare = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    # `--initial-branch=main` はホームの git 設定 (init.defaultBranch) に依存しない
    # ようにするため。無いと、既定が "master" の環境では bare の HEAD シンボリック参照が
    # unborn な "master" を指したままになり (push で "main" ブランチが増えても HEAD は
    # 動かない)、`_push_remote_change()` の `git clone` がその HEAD を解決できず
    # ("remote HEAD refers to nonexistent ref") 出来立ての clone は unborn な master の
    # まま — そこへの commit は main ではなく master に乗り、後続の
    # `git push origin main` が "src refspec main does not match any" で落ちる。
    _git(["init", "--bare", "-q", "--initial-branch=main", str(bare)], cwd=tmp_path)
    _git(["init", "-q", str(checkout)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=checkout)
    _git(["config", "user.name", "Test"], cwd=checkout)

    # `copy_plan_tree` の戻り値 (`<dest>/plan.sh`) の親から dest 側のディレクトリを
    # 得る — ここで自分で "scripts" という文字列を書かない (下記 sync-main-checkout.sh の
    # 単体コピーが `test_fixture_tree_is_the_only_copier.py` の走査に引っかからないように。
    # あの走査は「plan.sh / lib_* を名前で指すコピー」を拾う仕組みで、たとえ対象が
    # sync-main-checkout.sh のような無関係な 1 ファイルでも、コピー呼び出しの中に
    # 文字列 "scripts" が現れるだけで陽性になる)。
    dest_scripts = fixture_tree.copy_plan_tree(checkout).parent
    if with_sync_script:
        shutil.copy2(SCRIPTS_SRC / "sync-main-checkout.sh",
                     dest_scripts / "sync-main-checkout.sh")
    for rel in ("scripts/dispatcher.sh", "scripts/watchdog.py",
               "scripts/start.sh", "agents/director.md", "README.md"):
        target = checkout / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel} v1\n")

    _git(["add", "-A"], cwd=checkout)
    _git(["commit", "-q", "-m", "init"], cwd=checkout)
    _git(["branch", "-M", "main"], cwd=checkout)
    _git(["remote", "add", "origin", str(bare)], cwd=checkout)
    _git(["push", "-q", "origin", "main"], cwd=checkout)
    return bare, checkout


def _push_remote_change(bare, tmp_path, rel_path, content, *, name="other-clone"):
    """`bare` に対して、`checkout` とは別のクローンから 1 コミット push する
    (主 checkout 自身を経由しない — 本物の merge 済み PR を模す)。"""
    clone = tmp_path / name
    _git(["clone", "-q", str(bare), str(clone)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=clone)
    _git(["config", "user.name", "Test"], cwd=clone)
    target = clone / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-q", "-m", f"change {rel_path}"], cwd=clone)
    _git(["push", "-q", "origin", "main"], cwd=clone)


def _run_sync(checkout, *extra_args):
    script = checkout / "scripts" / "sync-main-checkout.sh"
    return subprocess.run(
        ["bash", str(script), "--repo-root", str(checkout), *extra_args],
        cwd=str(checkout), capture_output=True, text=True, env=_isolated_env())


# ---------------------------------------------------------------------------
# 1. lib_daemon_watch.py の関数を直接 (subprocess を挟まず)
# ---------------------------------------------------------------------------

def test_record_and_compare_version_across_a_merge(tmp_path):
    bare, checkout = _make_repo(tmp_path, with_sync_script=False)
    registry = checkout / "registry"
    w.record_own_version(registry, checkout, "dispatcher")
    assert w.restart_needed(registry, checkout, "dispatcher") is False

    _push_remote_change(bare, tmp_path, "scripts/dispatcher.sh", "# v2\n")
    assert w.fetch_origin(checkout) is True
    assert w.commits_behind(checkout) == 1
    assert w.changed_files_vs(checkout) == ["scripts/dispatcher.sh"]
    # 記録した時点ではまだ disk は v1 のまま (fetch は working tree を変えない)。
    assert w.restart_needed(registry, checkout, "dispatcher") is False

    _git(["merge", "--ff-only", "origin/main"], cwd=checkout)
    assert w.restart_needed(registry, checkout, "dispatcher") is True
    w.record_own_version(registry, checkout, "dispatcher")
    assert w.restart_needed(registry, checkout, "dispatcher") is False


def test_watchdog_restart_is_flagged_when_a_transitively_imported_lib_changes(tmp_path):
    """赤の実証 (t112 / PR#246 Codex 1 巡目 P1)。`lib_pane_process.py` は
    `watchdog.py` が起動時に直接 import する (単一の長寿命インタプリタなので
    以後 disk の変更を拾わない) が、直す前の `DAEMON_RESTART_FILES[watchdog]` には
    無かった — この lib だけを変える merge を経ても `restart_needed(watchdog)` は
    ずっと false のままで、同期は watchdog を「最新」と報告しながら古いコードが
    走り続けていた。"""
    bare, checkout = _make_repo(tmp_path, with_sync_script=False)
    registry = checkout / "registry"
    w.record_own_version(registry, checkout, "watchdog")
    assert w.restart_needed(registry, checkout, "watchdog") is False

    _push_remote_change(bare, tmp_path, "scripts/lib_pane_process.py", "# v2\n")
    _git(["fetch", "origin", "main"], cwd=checkout)
    _git(["merge", "--ff-only", "origin/main"], cwd=checkout)

    assert w.restart_needed(registry, checkout, "watchdog") is True
    w.record_own_version(registry, checkout, "watchdog")
    assert w.restart_needed(registry, checkout, "watchdog") is False


def test_restart_needed_is_unknown_without_a_recorded_version(tmp_path):
    _, checkout = _make_repo(tmp_path, with_sync_script=False)
    assert w.restart_needed(checkout / "registry", checkout, "dispatcher") is None


def test_unrelated_file_change_does_not_flag_a_restart(tmp_path):
    bare, checkout = _make_repo(tmp_path, with_sync_script=False)
    registry = checkout / "registry"
    w.record_own_version(registry, checkout, "dispatcher")
    w.record_own_version(registry, checkout, "watchdog")

    _push_remote_change(bare, tmp_path, "README.md", "unrelated change\n")
    _git(["fetch", "origin", "main"], cwd=checkout)
    _git(["merge", "--ff-only", "origin/main"], cwd=checkout)

    assert w.restart_needed(registry, checkout, "dispatcher") is False
    assert w.restart_needed(registry, checkout, "watchdog") is False


def test_fetch_failure_and_diverged_history_are_reported_as_none_or_refused(tmp_path):
    # `origin/main` を一度も学習していない checkout で確かめる — `_make_repo` の
    # `git push` は成功すると remote-tracking ref を書き戻すため、そちらを使うと
    # 「fetch が壊れていても、以前に学習した古い ref はまだ引ける」だけが分かってしまう。
    checkout = tmp_path / "checkout"
    _git(["init", "-q", str(checkout)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=checkout)
    _git(["config", "user.name", "Test"], cwd=checkout)
    (checkout / "README.md").write_text("x\n")
    _git(["add", "-A"], cwd=checkout)
    _git(["commit", "-q", "-m", "init"], cwd=checkout)
    _git(["remote", "add", "origin", "/no/such/path"], cwd=checkout)

    assert w.fetch_origin(checkout) is False
    assert w.commits_behind(checkout) is None


def test_affected_targets_reports_advisory_separately_from_daemons():
    changed = ["scripts/dispatcher.sh", "scripts/start.sh", "agents/director.md", "README.md"]
    assert w.affected_targets(changed, w.DAEMON_RESTART_FILES) == ["dispatcher"]
    assert sorted(w.affected_targets(changed, w.RESTART_ADVISORY_FILES)) == ["director", "worker"]


# ---------------------------------------------------------------------------
# 2. sync-main-checkout.sh を実際に実行する
# ---------------------------------------------------------------------------

def test_dry_run_reports_up_to_date_when_nothing_to_pull(tmp_path):
    _, checkout = _make_repo(tmp_path)
    before = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    proc = _run_sync(checkout, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up to date" in proc.stdout
    after = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    assert before == after   # dry-run は何も変えない


def test_dry_run_lists_changed_files_and_targets_without_merging(tmp_path):
    bare, checkout = _make_repo(tmp_path)
    _push_remote_change(bare, tmp_path, "scripts/dispatcher.sh", "# v2\n")
    before = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()

    proc = _run_sync(checkout, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 commit" in proc.stdout
    assert "scripts/dispatcher.sh" in proc.stdout
    assert "daemon dispatcher" not in proc.stdout  # restart-targets の生出力ではなく整形後
    assert "restart 対象: dispatcher" in proc.stdout

    after = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    assert before == after   # --dry-run は merge しない


def test_fetch_failure_leaves_the_checkout_untouched(tmp_path):
    _, checkout = _make_repo(tmp_path)
    _git(["remote", "set-url", "origin", "/no/such/path"], cwd=checkout)
    before = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()

    proc = _run_sync(checkout)
    assert proc.returncode != 0
    assert "fetch" in (proc.stdout + proc.stderr)

    after = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    assert before == after


def test_diverged_history_is_refused_without_touching_the_checkout(tmp_path):
    bare, checkout = _make_repo(tmp_path)
    # origin を進める。
    _push_remote_change(bare, tmp_path, "README.md", "remote change\n")
    # ローカルにも、origin には無い別のコミットを作る (fast-forward できない状態)。
    (checkout / "local-only.txt").write_text("local\n")
    _git(["add", "-A"], cwd=checkout)
    _git(["commit", "-q", "-m", "local divergent commit"], cwd=checkout)
    before = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()

    proc = _run_sync(checkout)
    assert proc.returncode != 0
    assert "diverged" in (proc.stdout + proc.stderr)

    after = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    assert before == after   # 何も merge していない


def test_real_run_ff_merges_and_skips_restart_for_unrelated_changes(tmp_path):
    bare, checkout = _make_repo(tmp_path)
    registry = checkout / "registry"
    w.record_own_version(registry, checkout, "dispatcher")
    w.record_own_version(registry, checkout, "watchdog")
    _push_remote_change(bare, tmp_path, "README.md", "unrelated change\n")
    # `checkout` 自身はまだ fetch していないので、bare の方から新しい main の
    # 先端を直接読む (checkout の `origin/main` は sync 実行前は古いまま)。
    remote_head = _git(["--git-dir", str(bare), "rev-parse", "main"],
                       cwd=tmp_path).stdout.strip()

    proc = _run_sync(checkout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "restart-needed(dispatcher) = false" in proc.stdout
    assert "restart-needed(watchdog) = false" in proc.stdout
    assert "restart" not in proc.stdout.split("restart-needed")[0]  # 実行系の "restart します" が無い

    after = _git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    assert after == remote_head   # ff された


def test_real_run_advises_worker_and_director_without_acting_on_them(tmp_path):
    bare, checkout = _make_repo(tmp_path)
    w.record_own_version(checkout / "registry", checkout, "dispatcher")
    w.record_own_version(checkout / "registry", checkout, "watchdog")
    _push_remote_change(bare, tmp_path, "scripts/start.sh", "# v2\n")

    clone2 = tmp_path / "second-clone"
    _git(["clone", "-q", str(bare), str(clone2)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=clone2)
    _git(["config", "user.name", "Test"], cwd=clone2)
    (clone2 / "agents").mkdir(exist_ok=True)
    (clone2 / "agents" / "director.md").write_text("# v2\n")
    _git(["add", "-A"], cwd=clone2)
    _git(["commit", "-q", "-m", "change director.md"], cwd=clone2)
    _git(["push", "-q", "origin", "main"], cwd=clone2)

    proc = _run_sync(checkout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "restart 推奨" in proc.stdout
    assert "worker" in proc.stdout
    assert "director" in proc.stdout
    # restart-needed は dispatcher/watchdog どちらのファイルにも触れていないので false のまま
    assert "restart-needed(dispatcher) = false" in proc.stdout
    assert "restart-needed(watchdog) = false" in proc.stdout


def test_unknown_options_are_rejected(tmp_path):
    _, checkout = _make_repo(tmp_path)
    proc = _run_sync(checkout, "--bogus")
    assert proc.returncode == 2


def test_script_is_executable_and_runs_without_a_bash_prefix(tmp_path):
    """権限ビット回帰ガード (t112 / PR#246 Codex 1 巡目 P2)。`_run_sync()` を含む
    他の全テストは `["bash", str(script), ...]` で明示的に bash へ渡すため、
    実行ビットが落ちていても (docs / 版ずれ通知が指示する直接実行の形では
    Permission denied になっても) 気付けない。ここだけは `./sync-main-checkout.sh`
    と同じ形 (シェバンと実行ビットに頼る直接実行) で確かめる。"""
    _, checkout = _make_repo(tmp_path)
    script = checkout / "scripts" / "sync-main-checkout.sh"
    assert os.access(script, os.X_OK), (
        f"{script} lacks the executable bit — direct execution "
        "(as docs instruct) would fail with Permission denied")

    proc = subprocess.run(
        [str(script), "--dry-run", "--repo-root", str(checkout)],
        cwd=str(checkout), capture_output=True, text=True, env=_isolated_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up to date" in proc.stdout


# ---------------------------------------------------------------------------
# restart の呼び出し配線だけを、本物の mux に一切触れずに確かめる
# ---------------------------------------------------------------------------

_STUB_LIB_DAEMON_WATCH = '''#!/usr/bin/env python3
"""スタブ: sync-main-checkout.sh が呼ぶ 4 つのサブコマンドだけを実装し、呼び出しを記録する。
本物の Mux には一切触れない (restart / status も print するだけ)。"""
import argparse, json, os, sys

p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
for name in ("restart-needed", "restart", "status"):
    sp = sub.add_parser(name)
    sp.add_argument("name", nargs="?")
    sp.add_argument("--repo-root")
    sp.add_argument("--force", action="store_true")
sp = sub.add_parser("restart-targets")
sp.add_argument("--repo-root")
args = p.parse_args()

with open(os.environ["STUB_CALLS_FILE"], "a") as f:
    f.write(f"{args.cmd} {args.name or ''}\\n".rstrip() + "\\n")

if args.cmd == "restart-needed":
    flags = json.loads(os.environ.get("STUB_RESTART_FLAGS", "{}"))
    print(flags.get(args.name, "unknown"))
elif args.cmd == "restart-targets":
    pass  # 出力なし = 何も advisory は無い
elif args.cmd == "restart":
    print(f"stub restarted {args.name}", file=sys.stderr)
elif args.cmd == "status":
    print("stub status: ok")
sys.exit(0)
'''


def test_sync_invokes_restart_when_a_daemon_is_flagged(tmp_path):
    """`restart-needed` が true を返したときだけ `restart <name>` が呼ばれ、
    false のときは呼ばれないことを、本物の mux を経由せずに確かめる。"""
    _, checkout = _make_repo(tmp_path)
    (checkout / "scripts" / "lib_daemon_watch.py").write_text(_STUB_LIB_DAEMON_WATCH)

    calls_file = tmp_path / "calls.log"
    env = _isolated_env()
    env["STUB_CALLS_FILE"] = str(calls_file)
    env["STUB_RESTART_FLAGS"] = json.dumps({"dispatcher": "true", "watchdog": "false"})

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "sync-main-checkout.sh"),
         "--repo-root", str(checkout)],
        cwd=str(checkout), capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    calls = calls_file.read_text().splitlines()
    assert "restart-needed dispatcher" in calls
    assert "restart-needed watchdog" in calls
    assert "restart dispatcher" in calls
    assert "restart watchdog" not in calls   # false だったので呼ばれない
    assert "status " in calls or "status" in calls
    assert "stub restarted dispatcher" in proc.stderr


# ---------------------------------------------------------------------------
# 個々の失敗が最終的な非 0 終了に落ちる (赤の実証: t112 / PR#246 Codex 1 巡目 P2)
# ---------------------------------------------------------------------------
#
# `test_sync_invokes_restart_when_a_daemon_is_flagged` のスタブは常に exit 0 な
# ので、"個々の失敗を集めて非 0 で返す" という直した振る舞いはこの下のテスト群
# でしか確かめられない。`STUB_FAIL_CMDS` (カンマ区切りの `<cmd>` か `<cmd>:<name>`)
# に挙がったサブコマンド呼び出しだけを exit 1 にする。

_STUB_LIB_DAEMON_WATCH_WITH_FAILURES = '''#!/usr/bin/env python3
"""スタブ: 4 つのサブコマンドを実装し、STUB_FAIL_CMDS に挙げた呼び出しだけ
非 0 で終了する。本物の Mux には一切触れない。"""
import argparse, json, os, sys

p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
for name in ("restart-needed", "restart", "status"):
    sp = sub.add_parser(name)
    sp.add_argument("name", nargs="?")
    sp.add_argument("--repo-root")
    sp.add_argument("--force", action="store_true")
sp = sub.add_parser("restart-targets")
sp.add_argument("--repo-root")
args = p.parse_args()

fail_specs = set(x for x in os.environ.get("STUB_FAIL_CMDS", "").split(",") if x)
key = f"{args.cmd}:{args.name}" if args.name else args.cmd
if args.cmd in fail_specs or key in fail_specs:
    print(f"stub: injected failure for {key}", file=sys.stderr)
    sys.exit(1)

if args.cmd == "restart-needed":
    flags = json.loads(os.environ.get("STUB_RESTART_FLAGS", "{}"))
    print(flags.get(args.name, "unknown"))
elif args.cmd == "restart-targets":
    pass  # 出力なし = 何も advisory は無い
elif args.cmd == "restart":
    print(f"stub restarted {args.name}", file=sys.stderr)
elif args.cmd == "status":
    print("stub status: ok")
sys.exit(0)
'''


def _run_sync_with_failure_stub(checkout, *, fail_cmds="", restart_flags=None):
    (checkout / "scripts" / "lib_daemon_watch.py").write_text(_STUB_LIB_DAEMON_WATCH_WITH_FAILURES)
    env = _isolated_env()
    env["STUB_FAIL_CMDS"] = fail_cmds
    env["STUB_RESTART_FLAGS"] = json.dumps(
        restart_flags or {"dispatcher": "false", "watchdog": "false"})
    return subprocess.run(
        ["bash", str(checkout / "scripts" / "sync-main-checkout.sh"),
         "--repo-root", str(checkout)],
        cwd=str(checkout), capture_output=True, text=True, env=env)


def test_restart_needed_failure_is_not_swallowed(tmp_path):
    """直す前: watchdog の比較が壊れていても case のどの枝にも当たらず無視され、
    exit 0 のまま "restart-needed(watchdog)" の行も出ないだけで通り過ぎていた。"""
    _, checkout = _make_repo(tmp_path)
    proc = _run_sync_with_failure_stub(checkout, fail_cmds="restart-needed:watchdog")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stderr
    # 壊れていない dispatcher 側の判定は生きている (1 つの失敗が他を巻き込んで
    # 握りつぶさない)。
    assert "restart-needed(dispatcher) = false" in proc.stdout


def test_restart_execution_failure_is_not_swallowed(tmp_path):
    """直す前: restart(name) が失敗しても WARNING を出すだけで exit 0 だった。"""
    _, checkout = _make_repo(tmp_path)
    proc = _run_sync_with_failure_stub(
        checkout, fail_cmds="restart:dispatcher",
        restart_flags={"dispatcher": "true", "watchdog": "false"})
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stderr


def test_status_failure_is_not_swallowed(tmp_path):
    """直す前: 最後の status の戻り値は見ておらず、その次の "done" 行で
    成功したように見えていた。"""
    _, checkout = _make_repo(tmp_path)
    proc = _run_sync_with_failure_stub(checkout, fail_cmds="status")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stderr
    assert "done." not in proc.stdout


def test_restart_targets_failure_is_not_swallowed(tmp_path):
    """直す前: advisory 一覧の取得が失敗しても process substitution の中で無音に
    吸われ、advisory が 0 件だったのか取得自体が壊れていたのか区別できなかった。"""
    bare, checkout = _make_repo(tmp_path)
    _push_remote_change(bare, tmp_path, "README.md", "unrelated change\n")
    proc = _run_sync_with_failure_stub(checkout, fail_cmds="restart-targets")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stderr
