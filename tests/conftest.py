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
