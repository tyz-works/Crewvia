# Ubuntu 26.04 で CI が落ちた原因と対処 (20261118-ci-ubuntu-2604 t003)

`ubuntu-latest` が 2026-11-18 に 24.04 → 26.04 へ移り、同じ commit (2e5c3c7) で CI が赤になった
(緑: run 43998811051 / 赤: run 44212158641)。`ubuntu:26.04` の docker コンテナで再現して原因を 3 つに分けた。
**テストの前提が古かったもの 1 つ・本番コードが 26.04 で壊れるもの 2 つ**。
本番機は将来 OS を上げれば同じ目に遭うので、本番コード側は直してある。

## 26.04 で変わったもの (実測 `ubuntu:26.04` = 26.04.1)

| 項目 | 24.04 | 26.04 |
|---|---|---|
| coreutils | GNU | **uutils (Rust)** 0.10.0。`/bin/sleep` 等は `/usr/lib/cargo/bin/coreutils/<name>` への symlink の**マルチコール バイナリ** |
| 標準の python3 | 3.12 | **3.14.4**。`python3.12` は既定では入らない |
| bash | 5.2 | 5.3.9 |
| procps | 4.0.4 | 4.0.4 (ps の出力は変わらない。今回の失敗とは無関係) |
| tmux / git | — | CI で入る版で差は出なかった |

## 原因 1: `/bin/sleep` を別名の symlink にして偽装していた (テストの前提・本番コードは無関係)

`ln -s /bin/sleep node` (`tests/watchdog-idle-e2e.sh`) と `node_bin.symlink_to("/bin/sleep")`
(`tests/test_watchdog_idle.py` の 2 箇所) は、argv[0] が `node` で起動される。uutils のマルチコールは
argv[0] で動作を決めるので、

```
Security violation: Requested utility `node` does not match executable name:
  /usr/lib/cargo/bin/coreutils/sleep
```

で起動を断り、偽 node が即死する。watchdog の分類は名前 (comm) を見ない (`lib_pane_process.py`。
構造・exe・環境変数) ので、偽 node に要るのは「長く生きて fork しない子」だけ。
**`#!/bin/sh` + `exec /bin/sleep "$@"` の実体ファイル**にした (exec で environ はそのまま残る)。
症状: `watchdog-idle-e2e.sh` の soft/hard idle の判定行が出ない (偽 node が死に、窓が消えた扱いになる)、
`test_watchdog_idle.py::test_startup_children_are_not_executing` が `idle_process` でなく `no_process`。

**教訓**: コマンドを別名で偽装するときに symlink を使わない。`exec -a` で argv[0] だけ変えるか、sh で包む。

## 原因 2: `scripts/bin/python3` が `exec python3.12` 固定 (**本番コードが壊れる**)

crewvia の文脈では PATH 先頭の `scripts/bin/python3` (shim) が全 `python3` を 3.12 に振り向ける
(`knowledge/python312-runtime-migration.md`)。`python3.12` が無いホストでは `exec python3.12` が
**127** で死に、`start.sh` の trust 検査 (`ERROR: trust の検査が異常終了したので (exit 127)`) をはじめ、
shim 経由の Python 呼び出しがすべて落ちる。bats の約 40 件 (start.sh を起動するもの) がこれ。
CI の pytest job だけは `actions/setup-python` で 3.12 を入れているので、bats / tests-sh job だけが赤になった。

shim の目的は「3.10 の system python では動かない 3.12 以降の構文 (PEP 701) を避ける」ことなので、
**`python3.12` を優先し、無ければ PATH 上の次の `python3` のうち 3.12 以上のもの**に落とすことにした
(shim 自身のディレクトリは飛ばす。無限再帰しない)。3.12 以上が無ければ、黙って古い python3 に落とさず
理由を出して 127 で止める。`/usr/bin/python3` は触らない (不変条件 8)。

本番機 (22.04 + deadsnakes の 3.12) は今まで通り `python3.12` を exec する。OS を上げて `python3.12` が
消えても、3.12 以上の `python3` があれば動く。

## 原因 3: uutils の `timeout --kill-after` は SIGKILL を直接の子にしか送らない (**本番コードが壊れる**)

`scripts/kai-review.sh` は codex / cursor / claude を `timeout --kill-after=K T …` で包んでいる (t009)。
GNU の `timeout` は自分が group leader になって **process group 全体**に信号を送る。uutils 0.10.0 は
SIGTERM 段は group に送るが、`--kill-after` の **SIGKILL は直接の子の pid にしか送らない**ようで、
SIGTERM を無視する子 (`trap '' TERM` → 子孫も継承) の下に孫がいると孫が**孤児で残る**
(実測: bash が死んだ後も `sleep 300` が ppid 1 で生き続けた)。
孤児が `| tee` や `>file` の出力 pipe を握り続けるので、timeout をかけたはずの kai-review が
孫の寿命まで無言で座り続ける (t009 が塞いだ穴の再発)。症状: `test_kai_review_engine_codex_timeout.py` /
`test_kai_review_engine_cursor.py` の「SIGTERM を無視する子」が TimeoutExpired + teardown ERROR。

対処: `_timeout_kill_group` (kai-review.sh) を足し、3 か所の `timeout --kill-after` を置き換えた。
`( echo "$BASHPID" >f; exec timeout … )` で timeout の group id (= その pid) を控え、timeout が
**124 / 137 で戻ったときだけ** `kill -KILL -- -<pgid>` で残りの子孫を落とす。正常終了では何も送らない。
GNU では group が空 (ESRCH) なので何も起きない。

## 確かめ方

```
docker run --rm ubuntu:26.04 …       # uutils / python3.14 の素の環境
bats tests/*.bats                    # start.sh 系 (原因 2)
bash tests/watchdog-idle-e2e.sh      # 原因 1
python3 -m pytest tests/test_watchdog_idle.py tests/test_kai_review_engine_*.py   # 原因 1・3
```

コンテナに入れたもの: tmux / bats / git / python3-yaml / python3-pytest / python3-pytest-xdist / procps / psmisc / jq。
git は `init -b main` (既定の master だと plan.sh pull の worktree 作成が `invalid reference: main` で落ちる。
CI の checkout は main)。

## 後片付け (20261119-ci-2604-unpin-shim-recursion t001)

- ci.yml の `runs-on` は 24.04 の応急固定 (#413) から **`ubuntu-26.04` の明示固定**に替えた（`ubuntu-latest` には戻さない）。
- shim の再帰: PATH に別 checkout の `scripts/bin` (同じ shim) が並ぶと、here だけ飛ばしても A→B→A… と再帰した。
  版検査の候補を `CREWVIA_PY3_SHIM=1` を立てて起こし、立って起動された shim は 127 で即終了する（呼んだ側は次の候補へ）。
  python3.12 がある本番は手前の exec で終わるので挙動は変わらない。テスト: `tests/test_python3_shim_no_recursion.py`。
- **再現テストは必ず別 process group + killpg(STOP→KILL)**。`subprocess.run(timeout=)` は孫を殺さず fork 爆弾になる (9,500 プロセスで rex.service が止まった)。変更前コードで走らせるときは `systemd-run --user --scope -p TasksMax=64 -p RuntimeMaxSec=60 -- …`。

## 関連

- `knowledge/python312-runtime-migration.md` (shim の経緯)
- `scripts/bin/python3` / `scripts/kai-review.sh` `_timeout_kill_group` / `tests/watchdog-idle-e2e.sh`
