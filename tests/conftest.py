#!/usr/bin/env python3
"""tests/conftest.py — 本番 mux をテストから触れなくする。

## なぜこれが要るのか (2026-09-23 の本番障害)

t036 の赤いテストが、本番の dispatcher ペインを乗っ取って約 4 時間半、
Worker の割り当てを止めた。

    tests/test_daemon_watch_hardening.py::test_restart_cli_exits_nonzero_when_it_refuses

このテストは `lib_daemon_watch.py restart dispatcher --repo-root <pytest tmpdir>`
を **本物の subprocess** で走らせる。CLI は `Mux()` を **周囲の環境変数から**
組み立てるので、手元の shell が herdr・既定ワークスペース `crewvia` を指していれば、
`--repo-root` がどれだけ隔離されていようと、kill と spawn が向かう先は
**本番のペイン `dispatcher`** だった。修正前の `restart()` は pause マーカーの
書き込み失敗を見ずに先へ進む — それがまさにこの赤いテストが証明したかった欠陥
なので、テストは**必ず**その破壊的経路を通る。ペインは pytest の一時ディレクトリを
指すコマンドで置き換えられ、テスト終了後にそのディレクトリが消えて死んだ。

    cd /tmp/pytest-of-tkadmin/pytest-199/test_restart_cli_exits_nonzero0/crewvia \
      && ... bash .../scripts/dispatcher.sh
    bash: /tmp/pytest-of-tkadmin/.../scripts/dispatcher.sh: No such file or directory

教訓は「そのテストを直す」ではない。**赤いテストは定義上、欠陥のある破壊的経路を
通る**のだから、隔離をテストの作法に任せてはいけない。仕組みで禁じる。

## 仕組み

ここで pytest セッション全体の環境変数を書き換え、`lib_mux` 側のガードを起動する:

  - `CREWVIA_MUX_TEST_ISOLATION=1` … `lib_mux` に「今はテスト中」と伝える印。
    `os.environ` に置くので、**subprocess で起動された CLI にも継承される**
    (これが上の障害で効いていなかった唯一のもの)。
  - `CREWVIA_TMUX_SESSION` / `CREWVIA_HERDR_WORKSPACE` … 毎回ユニークな宛先。
    既定値 `crewvia` は使わない。
  - `CREWVIA_MUX_PANE_PREFIX` … ペイン名の名前空間。`dispatcher` を spawn しても
    実際に触るのは `<prefix>dispatcher` になるので、本番の `dispatcher` /
    `watchdog` という名前そのものがテストから到達不能になる。

そのうえで `lib_mux` は、テスト中に宛先が既定値のまま／接頭辞が空のまま破壊的な
verb (spawn / send / kill / attach) を呼ばれたら `MuxTestIsolationError` を投げる。
つまり **env が無ければ落ちる**。黙って本番へ行く経路は残らない。

個別のテストが `monkeypatch.setenv` で自分用のセッション名に差し替えるのは自由
(test_daemon_husk_respawn.py がそうしている)。既定値でさえなければガードは通る。
"""

import os
import re
import uuid

import pytest

#: 本番が使う宛先の既定値。テスト中はここへ到達できない。
PRODUCTION_DESTINATION = "crewvia"

#: このセッションの名前空間。pid まで入れるのは、xdist や複数チェックアウトで
#: 同時に走っても衝突しないようにするため。
_NONCE = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

TEST_DESTINATION = f"crewvia-pytest-{_NONCE}"
TEST_PANE_PREFIX = f"pytest-{_NONCE}-"


def pytest_configure(config):
    """Runs before collection, so even module-import-time mux calls are covered."""
    os.environ["CREWVIA_MUX_TEST_ISOLATION"] = "1"
    os.environ["CREWVIA_TMUX_SESSION"] = TEST_DESTINATION
    os.environ["CREWVIA_HERDR_WORKSPACE"] = TEST_DESTINATION
    os.environ["CREWVIA_MUX_PANE_PREFIX"] = TEST_PANE_PREFIX


def pytest_unconfigure(config):
    """終わるときに、このセッションが作った宛先 (と、死んだ pytest の残骸) を片付ける。

    上の隔離は「本番 `crewvia` を撃たない」ための宛先を毎回作るが、herdr は初回
    アクセスで workspace を自動作成し、以前は誰も閉じなかった (backlog #15: 50 個
    溜まった)。後始末は `tests/pytest_workspace_sweep.py` にある。何があっても
    テストの結果は変えない (例外を出さない)。**隔離の仕組みには触れない。**

    `pytest_sessionfinish` ではなくここなのは、collection エラーや中断でも走るため。
    残骸掃除を「開始時」でなく「終了時」にしたのは、開始時に置くと掃除のぶんだけ
    最初のテストが遅れるから。**このフックは `--collect-only` や `--help` でも走る**
    (一覧の取得だけで、書き込みは無い)。SIGKILL された回の残骸は、次に正常終了した回が拾う。
    """
    import pytest_workspace_sweep
    pytest_workspace_sweep.run_cleanup(TEST_DESTINATION, PRODUCTION_DESTINATION)


@pytest.fixture
def production_destination(monkeypatch):
    """Put the environment back the way the 2026-09-23 incident found it.

    ガード自身を検証するテスト専用。これを使うテストは「本番を狙ったら止まるか」
    を確かめるものなので、**止まらなかったら本番に当たる**。必ず tmux / herdr の
    実行層を差し替えたうえで使うこと (下の `recording_tmux` がその役)。
    """
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", PRODUCTION_DESTINATION)
    monkeypatch.setenv("CREWVIA_HERDR_WORKSPACE", PRODUCTION_DESTINATION)
    monkeypatch.setenv("CREWVIA_MUX_PANE_PREFIX", "")
    return PRODUCTION_DESTINATION


# ---------------------------------------------------------------------------
# 束縛された破壊 (`tmux if-shell -F`) を偽 tmux で解く
# ---------------------------------------------------------------------------
#
# t043 以降、保護されたペインの kill は
#
#     tmux -S <endpoint> if-shell -F '#{==:#{pid},<世代>}' \
#          'kill-window -t @7' 'display-message -p -- <印>'
#
# という **1 回の呼び出し** になる。検証と破壊が同じサーバーで起きることが
# 安全性の中身なので、偽 tmux も「条件が外れたら then 側は実行されない」を
# 模さなければならない。条件を無視して then 側を実行する偽物にすると、束縛を
# 外した欠陥版でも緑になる。
#
# 偽 tmux は 4 つのテストファイルに散っているので、条件の解き方はここに 1 つ
# だけ置く。3 つの写しが少しずつずれる、というのがこのリポジトリが何度も踏んだ
# 壊れ方である。

def fake_tmux_if_shell(argv, answers):
    """`(then_argv, stdout)` — tmux と同じ順で `if-shell` を解く。

    `then_argv` が None なら条件が外れたので then 側は実行されない。
    `answers` は書式トークン → 値 (`{"#{pid}": "900"}` 等)。
    """
    at = argv.index("-F")
    condition = argv[at + 1]
    for token, value in answers.items():
        condition = condition.replace(token, str(value))
    m = re.fullmatch(r"#\{==:([^,}]*),([^,}]*)\}", condition)
    if m is None:
        raise AssertionError(
            f"fake tmux が評価できない条件式: {argv[at + 1]!r} → {condition!r}")
    branches = argv[at + 2:]
    if m.group(1) == m.group(2):
        return ["tmux"] + branches[0].split(), ""
    if len(branches) > 1:
        # `display-message -p -- <印>` の最後の語が印。
        return None, branches[1].split()[-1] + "\n"
    return None, ""


# ---------------------------------------------------------------------------
# 本物の「空のペイン」 — idle なシェルを pty の上に立てる
# ---------------------------------------------------------------------------
#
# t041 (Codex 6 巡目 P1-1) で、ペインが空であることは**積極的に示す**ものになった:
# root プロセスがシェルで、argv が 1 語で、端末を持ち、その端末の前景グループで、
# 眠っていて、子が居ない。それ以前は「中の人を認識できなかった」だけで空と
# 見なされていたので、テストは *python プロセス自身* を husk の代わりに使えていた。
# python は idle なシェルではないので、もう代わりにならない。
#
# 代わりにここで本物を 1 つ用意する。fake が返す pid がこれになることで、
# 「husk は今まで通り片付けられる」というテストが、**本物の husk** に対して
# 行われるようになる。

@pytest.fixture
def idle_pane_shell(tmp_path):
    """実在する idle なペインシェルの pid を返す。"""
    import os
    import pty
    import signal
    import sys
    import time

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    import lib_mux

    # 素の HOME。実機の ~/.bashrc は自前でプロセスを起こす (tmux auto-attach 等) ので、
    # それを読むと「子の居ないシェル」ではなくなる。
    home = tmp_path / "pane-home"
    home.mkdir(exist_ok=True)

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.environ["HOME"] = str(home)
            os.environ["PS1"] = "$ "
            for leak in ("BASH_ENV", "ENV"):
                os.environ.pop(leak, None)
            os.execvp("bash", ["bash"])     # argv 1 語、tty 上で対話
        except BaseException:
            os._exit(127)

    # idle で、かつ**少し後もまだ idle**であること。起動ファイルにまだ取りかかって
    # いないシェルは、読み終えて待っているシェルと見分けがつかない。
    deadline = time.time() + 10.0
    stable = 0
    while time.time() < deadline:
        if lib_mux._pane_shell_state(pid) == lib_mux.PANE_IDLE:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(fd)
        pytest.skip("could not bring up an idle shell on a pty")

    try:
        yield pid
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(fd)
