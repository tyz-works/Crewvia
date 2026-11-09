# 退役は process group ごと止める

2026-11-09 の実例（mission `20261109-machine-local-config` t013、Worker Haruto）で、退役が「終わった」と記録したのに claude が 2 時間以上生き残った。

## 何が起きたか

1. `scripts/start.sh` の `LAUNCH_CMD` は `...; cd <WORK_DIR> && claude ...` で、claude は窓の bash の**子**（同じ pgid）だった。記録される `pane_pid`（Rex の spawn 記録の `child.pid` も）は bash。
2. 退役（`lib_retirement.py`）は記録した `pane_pid` にだけ SIGTERM を送り、`_exit_evidence` は「その pid が消えた」を終了の証明にした。
3. bash は SIGTERM で死んだが、SIGTERM を無視する固まった claude は生き残った。退役は `terminated` を書いて task を pending に戻し、SIGKILL の段（`_step_sigterm`）には進まなかった。
4. `lib_mux.py kill` も、Rex backend は記録の child pid（死んだ bash）の pidfd で照合するので断った。

正常な Worker は shutdown の通知で自分から抜けるので、漏れるのは**固まった Worker だけ**（= watchdog が要る場面でだけ効かなかった）。

## 直し方（二重の網）

### A. `exec claude`（`scripts/start.sh` の `LAUNCH_CMD`）

`cd <WORK_DIR> && exec claude ...`。pane の shell が claude に置き換わるので `pane_pid` が claude 本体になる。4 backend（tmux / herdr / rex / hc）とも pane の pid を「窓の最初のプロセス」として記録するだけなので、記録の**意味**は変わらない（pid が引き継がれるだけ）。

- 窓の中で claude が終わると、shell が残らず窓が閉じる（今までは shell のプロンプトが残った）。「窓が消えた」を終了の証拠にしている箇所は、むしろ早く正しく動く。
- `lib_pane_process._classify` は pane root を判定から外し、直接の子を見る。root が claude になると MCP / Bash tool の wrapper が直接の子になるが、判定は `_origin_of`（cmdline / environ）なので結果は同じ。子が 1 つも無い root が**セッション本体**のときは `no_process`（claude が落ちた）ではなく `idle_process`（claude だけが居る）を返す。
- `./crewvia`（Director の起動）と start.sh の inline モード（`exec claude`）は元から exec。ここでは触らない。

### B. 退役の signal は process group に送る（`lib_retirement.py`）

- `_start` が SIGTERM を送る直前、guard が vouched した `pane_pid` が**自分の group の leader**（`os.getpgid(pid) == pid`、自分たちの group ではない、pid > 1）かを `proc_group_leader` で確かめ、そうなら `pgid` を progress marker に記録して `os.killpg(pgid, SIGTERM)` する。証明できなければ `pgid = None` で、今までの `pane_pid` 単体の挙動のまま。
- 終了の証明（`_exit_evidence`）は、`pgid` が記録されているとき「`pane_pid` が消えた **かつ** その group に生きている（zombie でない）プロセスが無い」。group を数えられない（/proc が読めない）ときは終了と見なさない。
- SIGKILL の段:
  - shell がまだ生きている: 今までどおり guard（identity）を通してから `os.killpg(pgid, SIGKILL)`。
  - **shell が死んで group だけ残っている**: identity guard は「記録した pane_pid が今は読めない = 同名の後任が居る」と読んで marker を捨ててしまうので走らせない。代わりに `_start` が guard を通したうえで記録した `pgid` に束縛する。カーネルは group が pgid として使っている間その pid を再利用しないので、空でない group は signal を送った本人の instance であり、後任は別の pgid を持つ。`repo_identity_check`（デーモン側の自己同一性）は引き続き通す。group を数えられないときは**殺さず捨てもしない**（保留、次のサイクルで再確認）。
  - 確認は `pane_pid` が消え、かつ group が空になるまで（`_await_exit(pid, window, pgid)`）。届いても空にならなければ今までどおり `sigkill_unconfirmed`（task の後始末は保留、`SIGKILL_REPORT_AFTER` 回で Director に報告）。
- `pgid` は progress marker の新しい欄（`build_progress` の既定 `None`）。旧 marker・旧コードは読み捨てる（戻し方は PR revert だけ）。

注意: group 単位の signal は pane の中の MCP サーバーや Bash tool の子も巻き込むが、退役は窓ごと Worker を止める操作なので意図どおり。

### C. Rex backend の `kill` について（変更なし・理由）

`RexBackend.kill` は記録した child pid の pidfd を最初に開き、その子孫を**最初の signal の前に**列挙して pidfd で固定してから送る（pid 再利用に対する安全策。`lib_mux.py` の pidfd の節）。root（= 記録した child pid）が既に死んでいると pidfd が開けず、「別のプロセスかもしれないものには送らない」ので断る。残った子孫は init に reparent されていて、記録からは辿れない。

これは安全側の設計で、**ここでは変えない**。代わりに B が退役の経路で group を押さえる:退役は kill の前に group を記録し、root が死んだ後でも `killpg` で片付ける。`lib_mux.py kill` の手動実行で同じ状況を片付けたいときは、`/proc/*/stat` の pgrp で group を確かめて `kill -9 -<pgid>` する（A によって今後の新規 Worker では root = claude なので、root が死んで group だけ残る形は起きにくくなる）。

### `watchdog.py` の hard_idle terminate（スコープ外）

`watchdog.py` の `[terminate]`（`graceful_terminate`、`os.kill(pane_pid, SIGTERM/SIGKILL)`）は退役の phase machine とは別の古い経路。同じ形の穴を持つが、`make_retirement_executor` がある環境では退役が使われる（`knowledge/daemon-authority.md`）。この PR では触らない。

## テスト

`tests/test_retirement_process_group.py`:

- SIGTERM で死ぬ bash（group leader）+ SIGTERM を無視する子 → SIGKILL まで進み、group が空になって初めて `terminated`。
- exec 版（leader 自身と子が SIGTERM を無視）でも同じ。
- SIGKILL が group に届かない間は shell が消えていても `terminated` にならない（task を配り直さない）。再起動後の executor が届かせれば終わる。
- group を観測できないときは終了の証明にしない。
- leader と証明できない pid・自分の group・pid 1 は group 経路を使わない（偽 pid を使う既存の退役テストは変わらない）。
- `LAUNCH_CMD` が `&& exec claude` でなくなったら赤。exec で shell の pid が claude の pid になることの実測。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。progress marker の `pgid` 欄は残ってよい（旧コードは読まない）。
