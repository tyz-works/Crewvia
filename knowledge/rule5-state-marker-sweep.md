# registry/mux/*.state.json が窓の消えた Worker で固まる件 (t020 / 20261109-codex-hc)

## 事故

Director が `registry/mux/Hugo.state.json` を見たところ
`{"state": "working", "since": 1793590252}`（2026-11-02 頃）のまま残っていた。Hugo-worker の
pane は存在しない（`lib_mux.py list` に無し）。同じ形の記録が `Chiara.state.json` にも現存した
（2026-11-09 時点）。

Director は `assign-name.sh review` が Seo を返し続けるのを見て「review スキルの Worker は
1 名」と誤認した。実際は Seo と Hugo の 2 名が `registry/workers.yaml` に居た。気づかなければ
レビュー 4 枚を 1 名で直列処理していた。

## 調査（card 要求 #1）: state.json の `working` を誰が読んでいるか

`grep -rn "_load_state_entry\|_save_state_entry" scripts/*.py scripts/*.sh` の結果、
読み書きの双方とも **`scripts/dispatcher.sh` の Rule 5（idle-with-task 通知の grace 計測）
1 か所だけ**（`check_rule5()` → `_load_state_entry` / `_save_state_entry`）。

- `check_rule5()` 自身の `st = _mux.state(target)` は**常にライブに mux へ問い合わせる**
  （スタブの `.state.json` は読まない）。`.state.json` はその結果を「いつから同じ条件が
  続いているか」の grace タイマーとして**追加で**持つだけ
- dispatch() の実際の割り当てループ（`for window in windows: ... if not is_idle: continue`）は
  `tmux_list_worker_windows()` / `registry/workers.yaml` / heartbeat のみを見る。
  `.state.json` を読む経路は無い
- `assign-name.sh`（Director が `review` の Worker 名を引くときに呼ぶ）も `registry/workers.yaml`
  だけを読む。`registry/mux/` には一切触れない

**結論: 割り当て判断には効いていない。** 今回の誤認は「`assign-name.sh` が最初に登録された
同スキルの名前を常に返す」設計（`--fresh` を付けない限り）そのものによるもので、
`.state.json` の値を直接の根拠にしたものではない。ただし「Hugo-worker が消えているのに
working の記録が残る」という**観測事実**自体は、将来 `.state.json` を読む経路が増えたときに
同じ事故を再発させる土台であり、単体でも直す価値がある（card 要求 #2 以降）。card の
「効いていないなら 2 を軽く済ませてよい」のとおり、重い仕組み（pid / generation の付与等）は
見送り、掃除で済ませた。

## 根本原因

`.firstseen`（spawn 猶予マーカー）は 2 つの経路で消える: `tmux_kill_window()`（dispatcher 自身の
kill 直後）と `sweep_spawn_grace_markers()`（窓が消えたかどうかで判定する掃除。毎 cycle）。
一方 `.state.json` / `.job-since.json` / `.usage-limit.json` は**どちらの経路も持たない**:

- `tmux_kill_window()` は `.firstseen` だけを消し、`.state.json` 等には触れない
- `check_rule5()` / `handle_usage_limit()` 自身の自己クリア（`_save_job_since(name, None)` /
  `_retire_usage_limit()`）は、その名前が**今回の cycle でも `tmux_list_worker_windows()` に
  現れた**ときにしか走らない（dispatch() のループが `for window in windows:` の中でしか
  呼ばない）

つまり、名前の窓が消えて二度と現れなくなった瞬間、その名前の `.state.json` /
`.job-since.json` / `.usage-limit.json` は最後に書かれた値のまま永久に凍結される。
`scripts/lib_mux.py` の `reap_stale_pane_records()` 側にも
「`.state.json` / `.firstseen` is the dispatcher's own markers with their own sweeps」という
コメントがあったが、実際には `.firstseen` にしか掃除が実装されていなかった（ドキュメントの
前提が先行し、実装が追いついていなかった）。

## やったこと（card 要求 #2・#3）

`scripts/dispatcher.sh` に `sweep_rule5_state_markers()` を追加し、`sweep_spawn_grace_markers()`
と全く同じ形（窓の不在で判定・例外を外に出さない・ログに 1 行）で、`registry/mux/*.state.json` /
`*.job-since.json` / `*.usage-limit.json` のうち **窓が消えた名前の分**を消す。
`tmux_list_worker_windows()`（dispatch() の割り当てループと同じ「生きている」定義）で現在の
生存 Worker 名の集合を取り、含まれない名前のマーカーだけを落とす。

- 掃除する側は **1 者（dispatcher）に限定**（CLAUDE.md 不変条件 4 / 7）。watchdog 側には何も
  足していない
- 本番は hc で稼働中。生きている Worker の記録を誤って消さない方向に倒した: 判定は
  「`tmux_list_worker_windows()` に名前が無い」ことだけを根拠にし（`sweep_spawn_grace_markers()`
  と同じ判定関数を再利用）、観測できない（mux が答えない等）ケースは `_mux.list()` 自体が
  空配列を返す実装（各 backend の `list()` は例外を内部で握り潰して `[]` を返す）なので、
  「安全側」（= 掃除しない方向ではなく掃除を先送りするだけ。次の cycle で mux が答えれば
  正しく再評価される）に自然と倒れる
- 消してよい根拠: これらのマーカーは Rule 5 の通知 debounce 用タイマーに過ぎず、
  `may_destroy_pane()`（kill の認可）の証拠である `registry/mux/<name>.json` /
  `<name>-worker.json` とは別物（`_save_job_since` / `_save_usage_limit` が既に同じ理由で
  自己クリアしている。t074 / C2 / t005）。消えても「次にその名前の窓が現れたとき grace
  タイマーが最初からになる」だけで、安全側
- 呼び出し位置は `sweep_spawn_grace_markers()` / `sweep_stale_pane_records()` の隣
  （`dispatch()` の早期 return する cycle でも毎 cycle 走る）

## 検討して見送ったもの（card 要求 #4・任意）

Director が「同じスキルで空いている Worker」を知る手段を足すかどうかを検討した。
`plan.sh status` は `in_progress` の task に `worker` 名を出すので、「この review task は
Seo が持っている」は既に見える。残っている開示だけ無い情報は「registry/workers.yaml に
review スキルで**他に誰が登録されているか**」で、これは `registry/workers.yaml` を直接読めば
わかる（Director は読み取り専用でそのファイルにアクセスできる）。新しい表示コマンドを
`plan.sh status` や `assign-name.sh` に足すのは、この card の主題（stale な `.state.json`）
より一段重い変更になる（表示フォーマットの設計・既存出力との整合性の検討が要る）ため、
**今回は見送った**。今回の誤認の直接の原因は `.state.json` ではなく
`assign-name.sh`（`--fresh` 無しでは最初に登録された同スキル名を常に返す）の設計そのものに
あり、別 card として Director に報告する方が筋が良いと判断した。

## 回帰テスト（card 要求 #5）

`tests/test_stale_pane_record_sweep.py` に追加（`sweep_spawn_grace_markers()` のテストと
同じ `_rule5_sweep_function()` 抽出パターン）:

- `test_the_dispatcher_cycle_calls_the_rule5_sweep` — cycle の呼び出し表に載っている
- `test_rule5_sweep_drops_state_marker_for_a_vanished_worker` — 事故の再現（窓が無い名前の
  `.state.json` は消える）
- `test_rule5_sweep_keeps_the_marker_for_a_live_worker` — 同じ名前でも窓が生きていれば消さない
- `test_rule5_sweep_drops_job_since_and_usage_limit_markers_too` — 3 種類とも掃除の対象
- `test_rule5_sweep_leaves_unrelated_registry_files_alone` — `.firstseen` / spawn 記録は触らない
- `test_rule5_sweep_logs_what_it_dropped` / `test_rule5_sweep_can_never_fail_a_cycle`

赤の実証: `scripts/dispatcher.sh` だけを変更前に戻して同じ 7 本を走らせ、全部
`StopIteration`（関数が存在しない）で落ちることを確認済み。

構造ガードの表を 2 つ更新した（新しい `unlink` 呼び出し・新しい「registry への書き込み」が
1 件増えたのを理由つきで許可する側）:

- `tests/test_stale_pane_record_sweep.py` の `ALLOWED_DELETIONS`
- `tests/test_queue_writes_go_through_the_store.py` の `ALLOWED_WRITES`

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。`sweep_rule5_state_markers()` が消えるだけで、
`.state.json` 等は以前と同じ「窓が消えたら永久に残る」挙動に戻る（card 要求 #1 の調査のとおり
割り当て判断には影響しない）。本番の `registry/mux/*.state.json` に残っている既存の凍結記録は
revert 後も残り続けるが、実害は無い（復旧は手で消すだけでよい。CLAUDE.md 不変条件 7 と同じ
「消してよい」側の性質）。
