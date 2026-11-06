# herdr-core (`hc`) を mux バックエンドにする設計（`scripts/lib_mux.py` の `HcBackend`）

設計メモ（mission `20261106-herdr-core-backend` t001、attempt 1-6）+ 実装時の追記（t002）。`HcBackend` は
`scripts/lib_mux.py` に実装済み（`CREWVIA_MUX=hc` / `mode: hc`）。実装で変わった点・t001 が未決のまま残した
点への答えは一番下の「**改訂（t002 実装）**」にまとめた——本文（§1〜§11）は t001 時点の設計のままなので、
実装の最終形との食い違いがあれば最新の記述はそちらを優先すること。

- 対象の版: `hc 0.1.18`（`/home/tkadmin/.local/bin/hc`）。upstream `herdrdev/herdr-core`（公開）。OpenRPC schema `info.version` も `0.1.18`、**protocol generation 1**（`hc schema` の `info.description`）
- 書いた人: Worker Minjun（mission `20261106-herdr-core-backend` t001）。Director の事前メモ（card 本文）の実測を自分でも本機（本番の hc server、desktop-l8nqlib、holder pid 1246708 時点）で再現し、JSON Schema（`hc schema`）とライブコマンドの両方で裏を取った
- **改訂（attempt 2）**: Worker Mateo。PR #344 の review task（t006）が P1 1件・P2 2件・P3 2件を指摘して差し戻した（`queue/missions/20261106-herdr-core-backend/history/t006/0001-end-ex-90c5aef661cb4762a10585a1e01aa51d.md`）。全指摘を自分で実機再現してから反映した——P1: 名前の写像（新設 §3.2）。P2: エラー JSON は stderr（§5 冒頭に明記）・`kind` 一覧に `needs_update` が抜けていた（§5 の表を25件に修正）。P3: `hc schema` の schemas 数は34ではなく35（実測で訂正）・`openrpc` のバージョン文字列の意味を訂正・`state`（terminal 生死）と`status.state`（agent status）が同名の別概念である注記を追加（§3 冒頭・§7.1）。「裏が取れた主張」（review 本文に列挙）は取り直していない
- **改訂（attempt 3）**: Worker Mateo。review t006 の新 P1（`hc ls --json` は `--label` 併用でも `labels` 欄を返さず、attempt 2 の §3.2-2「往復する」は `ListResult.terminals[]` の `$ref` だけを根拠にした未実測の推測だった）を自分で再現して訂正した。出口は Director が提示し自分でも再現した `hc connect`（JSON-RPC API）の `terminal.list`——CLI の `ls` と違いフルの `Terminal`（`labels`/`pid`/`reported`/`status`/`argv`）を返す。`list()` だけこの API 経由にし、他の動詞は CLI のままにする理由を新設 §3.3 に書いた。§3.2-3・§9 をこれに合わせて書き直した
- **改訂（attempt 4）**: Worker Mateo。review t006（attempt 3 完了後）の新 P1（`terminal.list` を `{"all": true}` で呼ぶ設計は `_Backend.list()` の契約——live のみ、`lib_mux.py:2516` Tmux / `:3891` Herdr の docstring——に反する。hc は `finished` を `hc rm` まで残すので、死んだ Worker が `list()` に居座り `dispatcher.sh:2018` の `director_live` 等の生存判定が黙って壊れる）を自分で再現して訂正した。§3（表の list 行）・§3.2-3・§2（再現コマンド）の 3 箇所を `{"all": false}` に直し、`all:false` でも live terminal のフルオブジェクト（`labels`/`pid` 込み）は間引かれないことを実測で確認した。§3.3 の該当箇所も `all:false` に揃えた
- **改訂（attempt 5）**: Worker Mateo。review t006 attempt 4 の新 P2（`pid()` が finished terminal の死んだ pid をそのまま返す。`/proc` に無い。watchdog.py:641-650 の fail-closed の入口を一度も通らなくなる）を自分で再現し、card が追加した「根本原因を 1 つ名指しして、全動詞に当てる」「写像表は 1 動詞 1 行・4 列で既存の契約を保つかを yes/no で書く」の要件に沿って §3 を全面的に書き直した（新 §3.0〜§3.5）。**作業中に attempt 1-4 では見落としていた、同じ根本原因（hc は finished を rm まで残す）の 3 つ目の現れを発見した**: hc は同じ `name` を持つ finished terminal がいくら残っていても新しい spawn を拒否せず（`name_taken` は live な同名terminalにのみ発生）、`name` ベースの単体 lookup（`get`/`kill`/`send`/`keys`/`pid`/`state`）は常に**作成時刻が最も新しい**terminal に解決する——これは crewvia の「名前はポジション」設計（同じ名前を Worker の世代をまたいで再利用する）と直接ぶつかり、`record_existence`（reap-records）が `name` ベースのままでは**死んだ terminal の記録に対して新しい terminal の存在を返してしまい `PANE_GONE` を一度も返せない**という、attempt 4 までの `pid()`/`list()` の事故と同型の穴になる。出口は `_inspect_pane()` の契約どおり `name` ではなく hc の `address`（`t7` 等。spawn ごとに不変・再利用されない）を `handle` として扱うこと（新設 §3.4）。これに伴い `kill`/`pid`/`state`/`record_existence`/`may_destroy` の設計を `address` ベースに訂正した。加えて `Mux.verify_sent()`（:5745-）が `isinstance(self._backend, RexBackend)` でしか backend 固有の実装を呼ばない構造になっていること（`HcBackend` を足すだけでは`verify_sent`は一度も呼ばれない）を発見し §10 に追加した
- **改訂（attempt 6）**: Worker Mateo。review t006 の新 P2（Director も AST で確認: RexBackend は `may_destroy()` を `:5553`、`_inspect_pane_full()` を `:5487` で両方とも上書きしており、TmuxBackend も `_inspect_pane_full()` を `:2735` で定義している——attempt 5 までの §3.1 `may_destroy` 行の「Rex だけ `_inspect_pane_full` を独自定義するが `may_destroy()` 自体は共有のまま」は誤りだった）を自分でもコードを直接読んで確認し、訂正した。Rex が `may_destroy()` 自体を上書きする理由は block がプログラム死後もログインシェルとして残るためで、hc の「finished が `hc rm` まで残る」と同じ構造——これを材料に、§3.1 の `_inspect_pane` が `state != live` なら `pane_pid=None` を返す決め（attempt 5）が共有の `_Backend.may_destroy()` にどう効くかを実際にコードを読んで追い直した（`may_destroy_pane()` の分類は `DAEMON_PANE_NAMES = ("dispatcher", "watchdog")` の 2 つの pane にしか効かないことを実機の python で確認した上で、その範囲では `daemon_pane_owner(None, ...)` が `OWNER_UNKNOWN` になり、finished な terminal への kill は `--force` 無しでは常に拒否される——fail-closed で危険側ではないが、attempt 5 までの「`_inspect_pane` を直せば自動的に保たれる」という結論は誤りだった）。直したのは §3 冒頭の見出し・§3.1 の `may_destroy` 行・§10（t002 への宿題を追加）・§5（`kind` の 25 件が `hc schema` 由来の schema の語彙であって CLI 全体の語彙ではないこと。実測: `hc attach` の `usage` kind は 25 件に無い）の 4 箇所だけで、他は変更していない
- 凡例: 「**実測**」= 自分がこの作業中に確かめた出力。「**Director 実測**」= card に書かれていて自分では別の形で確かめた（同じプロセスは再現していない）事実。「**未確認**」= 確かめていない。「見込み」は推測
- 比較対象は既存の `RexBackend`（`scripts/lib_mux.py:4884-5675`、`knowledge/rex-mux-backend.md`）と `HerdrBackend`（`scripts/lib_mux.py:3352-4883`）。**この文書は両方の既存 backend の挙動を変える提案を 1 点含む（§4）。tmux / herdr / rex は本番で使われているので、その影響範囲を §4.4 に明記した**

## 1. 結論（先に）

1. `hc` で `_Backend` の全メソッドを満たせる。**herdr/tmux よりむしろ楽になる点が 2 つ**: ① `state()` が screen-scraping でなく構造化された OSC 7501 の値を読める（§7）② エラーが `{"error":{"kind":...}}` の構造化形で、exit code も安定している（`hc --skill` に表がある。§5）
2. **満たせない・弱くなる点は無いが、「observed できない」を広めに取る必要がある箇所が 2 つに増えた（attempt 5）**: ① `reported: false`（プログラムが一度も 7501 を報告していない）は `state()` を `"unknown"` にする。`idle` に倒さない（§7.3）。② **（attempt 5 で追加）`name` は hc 内部でも mutable で、同じ `name` を持つ finished terminal が残っている間に新しい spawn が成功しうる——`name` ベースの単体 lookup は常に最新の terminal に解決するため、`kill`/`pid`/`state`/`record_existence` は hc の `address`（`t7` 等。spawn ごとに不変）を `handle` として扱う必要がある（§3.0・§3.4）**
3. **版の固定は Rex と同じ fail-closed にする。既存 `HerdrBackend` の「警告だけで続行」方式はここでは採らない**（§4。Director 指摘の対比を根拠にした判断）
4. PATH は `lib_daemon_watch.spawn_command()` の既存の作法（LAUNCH_CMD に `export PATH=...` を埋める）をそのまま使う。hc 固有の対処は不要（§6）
5. `verify-sent` は screen-scraping を使わず `SendResult.started` / `status` を読む——ただし `reported: false` の対象には answered できないので、既存の `_text_in_input_line` 相当へのフォールバックを残す（§8）。**（attempt 5 で追加）`Mux.verify_sent()` は `RexBackend` への `isinstance` 分岐でしか backend 固有実装を呼ばない構造なので、`HcBackend` を足すだけでは呼ばれない——t002 でこの分岐に `HcBackend` を足す変更も要る（§10）**
6. 実装規模は RexBackend 並み（§4 の表がほぼそのまま、screen-scraping が要らない分 `state()` と `verify_sent()` は短くなり、JSON-RPC 的な構造化パースが増える分 `_Backend._inspect_pane` 系はやや増える）。**1 task（t002）に収まる見込み**（§9）

## 2. 実測の環境と手順（再現できるように）

本番の desktop server（`hc server status --json` の `core.id = 972c9056285907f04dfc72170c12ac7b`）に対して、使い捨ての terminal を `--name` 付きで spawn し、動作確認後に `hc kill` → `hc rm` で後始末した。**`hc server stop` / `restart` は一度も呼んでいない**（CLAUDE.md 不変条件と `hc --skill` の「Never `hc server stop` or restart the user's core unless asked」を守った。本機は人間が普段使う hc server で、止めると無関係な terminal も道連れになる）。

```bash
hc schema > hc-schema.json                 # OpenRPC 1.3.2 形式、32 methods、35 schemas（P3 対応: attempt 1 は 34 と誤記。`1.3.2` は openrpc フィールドの版であって JSON Schema の版ではない——`info.description` に「Protocol generation 1」とは別の数字として出る）
hc run --bg --name t001probe-bad --json claude        # PATH 依存の名前 → spawn_failed（§6）
hc run --bg --name t001probe-sleep --json /home/tkadmin/.local/bin/hc --version
hc get t001probe-sleep --json              # pid / status / reported / reason を確認
hc send t001probe-sleep "hello" --json     # finished な terminal への send → kind:finished
hc kill tXYZDOESNOTEXIST --json            # 存在しない id → kind:not_found
hc kill t001probe-sleep --json             # 既に finished な terminal への kill → エラーにならず Terminal を返す（冪等）
hc rm t001probe-sleep --json

# attempt 3（§3.2-2・§3.2-3・§3.3）: hc ls --json が labels を返さないこと・hc connect の terminal.list が返すことの再現
hc run --bg --name cvprobet001b --label crewvia_name=Cvprobet001b-worker --json /home/tkadmin/.local/bin/hc --version
hc ls --json                                # live 無し（終了済み）→ terminals:[]
hc ls --all --json                          # labels 欄が無い（間引かれた表示用の形）
hc ls --all --label crewvia_name=Cvprobet001b-worker --json   # --label で絞り込みは効くが labels は返らない
hc get cvprobet001b --json                  # labels.crewvia_name を含むフル Terminal
printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":true}}\n' | hc connect
printf '{"jsonrpc":"2.0","id":1,"method":"hello","params":{}}\n' | hc connect                       # generation:1（§10-1）
printf '{"jsonrpc":"2.0","id":1,"method":"terminal.get","params":{"terminal":"tXYZDOESNOTEXIST"}}\n' | hc connect   # API のエラー封筒は error.data.kind（§3.3）
hc rm cvprobet001b --json

# attempt 4（review t006 attempt 3 の新 P1）: terminal.list は {"all": false} で呼ぶこと（list() の契約は live のみ）の再現
hc run --bg --name cvprobet001c --label crewvia_name=Cvprobet001c-worker --json /home/tkadmin/.local/bin/hc --version
printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":false}}\n' | hc connect   # 即終了済み → hidden_finished:1, terminals:[]（finished は出ない）
printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":true}}\n' | hc connect    # 同じ terminal が finished のまま出る（labels/pid 込みのフル Terminal）
hc rm cvprobet001c --json
hc run --bg --name cvprobet001d --label crewvia_name=Cvprobet001d-worker --json /bin/sh -c 'sleep 5'
printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":false}}\n' | hc connect   # still live → terminals に 1 件（labels/pid 込みのフル Terminal。all:false でも間引かれない）
hc kill cvprobet001d --json
hc rm cvprobet001d --json

# attempt 5（§3.0・§3.4）: 同じ name を持つ finished terminal が残っていても spawn が拒否されないこと、
# name ベースの単体 lookup が「作成時刻が最新」に解決すること、address は不変であることの再現
hc run --bg --name cvpt001e --label crewvia_name=Cvpt001e-worker --json /bin/sh -c 'sleep 60'
hc get cvpt001e --json ; hc read cvpt001e --lines 5 --json ; hc keys cvpt001e enter --json
hc kill cvpt001e --json          # finished になる（address は不変。例 t1d）
hc get cvpt001e --json           # finished のまま返る（not_found にならない）
hc read cvpt001e --lines 5 --json ; hc keys cvpt001e enter --json ; hc send cvpt001e hello --json
  # → keys/send は finished に kind:"finished" で失敗。capture は live/finished とも同形 {"complete":true,"how":"history","text":""}
hc kill cvpt001e --json          # 既に finished への再 kill → エラーにならず Terminal を返す（冪等。再確認）
hc rm cvpt001e --json
hc get cvpt001e --json ; hc kill cvpt001e --json   # rm 後は両方 not_found（exit 3）

# name の衝突（finished とは共存できるが live 同士は拒否される）
hc run --bg --name cvpt001h --label crewvia_name=Cvpt001h-worker --json /bin/sh -c 'sleep 30'
hc kill cvpt001h --json                              # t1h が finished になる（rm しない）
hc run --bg --name cvpt001h --label crewvia_name=Cvpt001h-worker --json /bin/sh -c 'sleep 5'
  # → 拒否されず成功。新しい address（例 t1j）が発行される
hc ls --all --json                                   # t1h(finished) と t1j(live→finished) が同じ name で両方残る
hc get cvpt001h --json                               # 常に作成時刻が新しい方（t1j）を返す。t1h は name では引けない
hc get t1h --json ; hc get t1j --json                # address で直接引けば、それぞれ自分自身だけを正しく返す（曖昧にならない）
hc run --bg --name cvpt001k --label crewvia_name=Cvpt001k-worker --json /bin/sh -c 'sleep 30'
hc run --bg --name cvpt001k --label crewvia_name=Cvpt001k-worker --json /bin/sh -c 'exit 0'
  # → live な同名 terminal が既にあるときだけ kind:"name_taken" で拒否される
hc rm t1h --json ; hc rm t1j --json ; hc kill cvpt001k --json ; hc rm t1k --json  # 後始末

# address は send/keys/read/kill/rm のどの動詞でも name と同じ位置に渡せる（hc attach --help の
# "[TERMINAL] The terminal: `t7`, a name, or `machine/t7`" を他の動詞でも実地確認）
hc run --bg --name cvpt001m --label crewvia_name=Cvpt001m-worker --json /bin/sh -c 'sleep 30'
# ADDR=$(hc get cvpt001m --json の .address)
hc send "$ADDR" hello --json ; hc keys "$ADDR" enter --json ; hc read "$ADDR" --lines 3 --json
hc kill "$ADDR" --json ; hc rm "$ADDR" --json

# attach は tty が無いと使えない（dispatcher/watchdog のような自動化からは呼べない側に倒れる）
hc run --bg --name cvpt001g --label crewvia_name=Cvpt001g-worker --json /bin/sh -c 'sleep 10'
hc attach cvpt001g --json </dev/null >out.txt 2>&1   # → {"error":{"kind":"usage","hint":"use --observe to watch from a pipe","message":"attach needs a terminal"}}
hc attach cvpt001g --observe --json </dev/null       # --observe は非対話でも動く（raw ANSI を吐く。--json は無視される）
hc kill cvpt001g --json ; hc rm cvpt001g --json
```

`/proc/<hc server pid>/environ` を読んで、hc server が実際にどの PATH で子プロセスを起動するかを確認した（§6）。`hc server start --json` を 1 回呼び、既に生きている server に対して**冪等**（pid・uptime が変わらない。再起動しない）であることも確認した——`ensure-server` の候補として安全に呼べる。

## 3. `_Backend` の全メソッドの対応表

hc は JSON-RPC 2.0 の methods（`hc schema` の `methods[]`）を CLI がラップしたもの。32 methods のうち使うのは `terminal.*` と `server.status` が CLI 経由、`list()` と `available()` の版確認だけ `hc connect` 経由で `terminal.list` / `hello` を直接呼ぶ（§3.5・§4.2-4）。`machine.*` / `file.*` / `doc.*` / `events.subscribe` は対象外（§10 で扱う）。`_Backend` クラス docstring（`lib_mux.py:355-361`）「All verbs must: Never raise exceptions / Return False, None, "" on failure / Print a WARNING」は `HcBackend` にもそのまま適用される前提で以下を読む（subprocess 例外・非 JSON 応答は全部この規則で失敗値に握りつぶす）。

**注記（同名の別概念、P3 対応）**: `hc get` の出力にはトップレベルの `state`（`live`/`finished`。terminal プロセスそのものの生死）と、ネストした `status.state`（`idle`/`working`/`done`/`blocked`/`error`。agent status、§7 の対象）という**同名だが別物の 2 つの欄がある**。下表の `kill` 行・§5 の kill 前チェックは前者、`state(name)` 行・§7 は後者を指す。実装時に変数名で区別すること（実測: `hc get` の生出力で両方の欄を確認。§2 の後続コマンドで裏取り済み）。

### 3.0 根本原因を 1 つに絞る（attempt 5 で追加。ここが本題）

差し戻し 3・4 巡目（`list()` の `all:true`・`pid()` の finished 値）はどちらも同じ 1 つの性質が別の動詞に出ただけだった:

> **hc は終わった terminal を `hc rm` するまで残す。tmux / herdr は窓が死ねば一覧からも問い合わせからも消える。**

attempt 5 で実機を叩き直すと、**この根本原因には見落としていた現れがもう 1 つある**: hc は「同じ `name` を持つ finished terminal がいくら残っていても、新しい `spawn` を拒否しない」。実測（本機、使い捨て terminal。後始末済み。§2 に再現コマンドの全文）:

```
$ hc run --bg --name cvpt001h --label crewvia_name=Cvpt001h-worker --json /bin/sh -c 'sleep 30'
{"address":"t1h", ... "state":"live", ...}
$ hc kill cvpt001h --json              # t1h は finished になる（rm はしない）
$ hc run --bg --name cvpt001h --label crewvia_name=Cvpt001h-worker --json /bin/sh -c 'sleep 5'
{"address":"t1j", ... "state":"live", ...}   # 同じ name で新しい address が発行される。拒否されない
$ hc ls --all --json
{"hidden_finished":0,"terminals":[
  {"address":"t1h","name":"cvpt001h","state":"finished", ...},
  {"address":"t1j","name":"cvpt001h","state":"live", ...}]}
$ hc get cvpt001h --json               # 2件が同じ name を持つ。どちらを返す？
{"address":"t1j","name":"cvpt001h","state":"live", ...}   # 新しい方（t1j）
```

**一方、同じ `name` を持つ terminal が 2 つとも live のときは拒否される**（実測）:

```
$ hc run --bg --name cvpt001k --label crewvia_name=Cvpt001k-worker --json /bin/sh -c 'sleep 30'
{"address":"t1k", ..., "state":"live", ...}
$ hc run --bg --name cvpt001k --label crewvia_name=Cvpt001k-worker --json /bin/sh -c 'exit 0'
{"error":{"hint":"pick another name, or kill the one holding it","kind":"name_taken","message":"a live terminal is already named \"cvpt001k\""}}
```

さらに、`t1h`（finished）と `t1j`（live→finished）の両方が finished になった後でも、`hc get cvpt001h --json` は一貫して**作成時刻が新しい方（`t1j`、`created_ms` が大きい方）**を返し続ける（実測。`t1h` は name 経由ではもう引けない——`hc rm t1h` で明示的に消すまで `hc ls --all` には残るが、`hc get <name>` の対象にはならない）。対照に、`address` で直接引けば曖昧さは無い（実測）:

```
$ hc get t1h --json
{"address":"t1h", ... "state":"finished", ...}   # 常に自分自身
$ hc get t1j --json
{"address":"t1j", ... "state":"finished", ...}   # 常に自分自身
```

**これが意味すること**: hc の `name` は、crewvia の既存 3 backend における「呼び出し用の mutable なラベル」の役割（`_inspect_pane` の docstring、`lib_mux.py:367-368`:「`handle` is ... not the caller name, which is mutable and which another checkout can make point somewhere else between the two calls」）**以上の不安定さを持つ**——tmux/herdr/Rex では「名前が指す先が変わる」ことはあっても「同じ名前の死骸が積み上がって、どれが『今の』ものか hc 側の作成時刻で暗黙に決まる」という状態にはならない（tmux の window 名・herdr の pane label は死んだ瞬間に名前空間から外れる）。これは CLAUDE.md の設計原則3「名前はポジション——同スキル Worker は同名前を引き継ぐ」と正面から噛み合う: 本番では同じ hc `name`（`seo-worker` 等）が Worker の世代をまたいで何度も再利用される。**`name` だけで動詞を実装すると、古い Worker の finished terminal が rm されないまま残っている間、新しい Worker の spawn 自体は成功するが、`get`/`kill`/`pid` 等の by-name lookup は常に新しい方に解決される**——これ自体は crewvia が望む挙動（最新の記録が最新の terminal を指す）と偶然一致するが、**`record_existence()`（reap-records）だけは話が別**: これは「過去に記録した特定の terminal がまだ存在するか」を聞く動詞で、同名の新しい terminal に化けて「存在する」と答えてしまうと、死んだ terminal の記録が永遠に `PANE_GONE` にならない（§3.1 の該当行・§3.4 で詳述）。

**この節で名指しする根本原因は 1 つ**: *hc は「終わった」ことと「消えた（`hc rm` 済み）」ことを区別し、前者では record も name も使い回せる状態のまま残す。* 「消えた」だけが唯一の `PANE_GONE` の証拠であることは §5 で既に確定しているが、**この区別は `name` の安定性にも及ぶ**（`name` は「消えた」より前に再利用可能になる）、というのが attempt 3・4 では捉えきれていなかった延長線。下表の全行はこれを踏まえて「保つか」を判定する。

### 3.1 写像表（1 動詞 1 行・4 列。`_Backend`（`lib_mux.py:355-`）の宣言を 1 つも飛ばさない）

`_Backend` が宣言する動詞（`raise NotImplementedError` で宣言、または具体的な既定実装を持つもの）を全部並べる。カードが名指しした 15 動詞に加えて、`_Backend` 自身に宣言があるのに前回までの表に独立行が無かった `server_identity()` / `_inspect_pane()` / `may_destroy()` を追加した（いずれも kill / identity-ok の裏で実際に呼ばれる具体メソッド）。**訂正（attempt 6。review t006 の新 P2）**: `_inspect_pane_full()` は独立行を持たないが `may_destroy` 行の中で扱う——上書きの有無は揃っていない 2 段構造になっている。`_inspect_pane_full()` 自体は TmuxBackend（:2735）・RexBackend（:5487）の両方が上書きする一方、`may_destroy()` 自体を丸ごと上書きするのは RexBackend（:5553）だけ。「Rex だけ `_inspect_pane_full` を独自定義し `may_destroy()` は共有のまま」という attempt 5 までの書き方は、この 2 段構造を 1 段に潰していた誤りだった（下表の `may_destroy` 行で訂正）。

| 動詞 | 既存の契約（引用 + `file:line`） | hc の挙動（コマンドと生の出力） | 契約を保つか |
|---|---|---|---|
| `available()` | `_Backend.available()`（:543）は `NotImplementedError` のみ。具体例 `HerdrBackend.available()`（:3645）docstring「True if herdr binary exists and server is running (or can be started)」+ 版不一致は warn のみで続行。`RexBackend.available()`（:4911-4927）は版（revision）不一致で `False`（fail-closed） | 実測（本セッションで再実行）: `which hc` → `/home/tkadmin/.local/bin/hc`。`hc server start --json` は既に生きている server に対して**冪等**（pid/uptime 不変、§2）。`printf '{"jsonrpc":"2.0","id":1,"method":"hello","params":{}}\n' \| hc connect` → `generation:1` がトップレベルに入る（§4.2-4で既出） | **Yes**。binary の有無 ∧ `server start`（冪等）成功 ∧ `hello.generation` が許容集合（§4.2 で fail-closed に決定）の AND。個別 terminal を見ない動詞なので §3.0 の根本原因（finished の居座り）は無関係。読み手: `_select_backend()` が選んだ backend の `available()` が `False` なら mux 全体が使用不可（Rex と同じ重さ。CLAUDE.md の `CREWVIA_MUX` 項）——hc 版が generation 不一致のときに静かに `True` を返すと、この安全弁が働かない |
| `server_running()` | `HerdrBackend.server_running()`（:3673）docstring「True if the herdr server answers a ping right now...Unlike available(), this never starts the server」 | 実測（再実行）: 既に動いている server に対し `hc server status --json` は exit 0 で `core`/`holder`/`pid`/`uptime_ms` を返す。**「server が無いときに起動しないこと」自体は本機で未確認**（本番 server を落として試すのは `hc --skill` の「Never `hc server stop` or restart」に反するため見送った）。`hc --skill` の文面（**実測ではない**。`hc schema`/`--help` と同じ扱い）: 「On Windows, `hc server status` never starts a core」——Windows 限定の記述だが、`server status` と `server start` が別サブコマンドであること自体は `hc server status --help` / `hc server start --help` の実測で確認済み（両方とも `--json`/`--help` だけを取るシンプルな引数） | **Yes（見込み）**。`hc server status --json` をそのまま使う。読み手は「起動したか既に動いていたかの区別」だが、crewvia の実装方針（ensure-server は `available()` に一本化、§1-4）はこの区別を使わないので、未確認のまま進めても実害は小さい。**§11 に追加**: 「server が無い状態での `hc server status` の挙動」は未確認 |
| `spawn(name, cmd, cwd)` | `_Backend.spawn()`（:502-509）コメント「No env parameter...Anything the new pane needs has to travel inside cmd」 | 実測（§2・§3.3）: `hc run --bg --name <小文字名> --label crewvia_name=<原綴り> --cwd <cwd> --json <argv...>`。`argv` は配列（§6） | **Yes**。§3.0 の根本原因の影響: 同じ `name` に finished な残骸があっても `name_taken` にはならない（§3.0 で実測）ので、古い Worker を `hc rm` し忘れていても次の Worker の spawn 自体は失敗しない——`spawn()` が返す新しい `address` を記録に書けば、crewvia 側の記録は正しく上書きされる。読み手: `lib_daemon_watch.py restart`（不変条件4）が再 spawn のたびにこの経路を通る |
| `send(name, text)` | `_Backend.send()`（:511-512）は `NotImplementedError` のみ | 実測（§2）: `hc send <t> <text> --json` は live な相手には `{"started":..,"status":..,"turn":..}` を返す。**finished な相手には `{"error":{"kind":"finished","hint":"hc read t1d","message":"t1d has finished"}}`**（本 attempt で再現。既に §5 の `finished` kind と同じ現象） | **Yes（`<t>` に `address` を使う前提で）**。finished への send はエラーになるので「死んでいるのに送れたことにする」事故は起きない。**`<t>` に `name` を使うと §3.0 の曖昧さにより同名の新しい live terminal に送ってしまう理論上の危険**（意図した相手と違う相手に送る、「observed できない」より悪い向き）があるため、`send`/`keys` は spawn 時に得た `address` を使う（§3.4） |
| `keys(name, keys)` | `_Backend.keys()`（:514-524）docstring「Press named keys...Named keys only...Does not clear the input line and does not add an Enter」 | 実測: live には `{"status":null,"turn":0}`。finished には `send` と同じ `{"error":{"kind":"finished",...}}` | **Yes**（`send` と同じ理由・同じ注意: `address` で呼ぶ） |
| `capture(name)` | `_Backend.capture()`（:525-526）は `NotImplementedError` のみ。`Mux.verify_sent()`（:5768-5770）のコメント「`capture()` returns `""` both when the pane is genuinely empty and when it could not be reached at all, and there is no way to tell those apart」——crewvia 自身がこの曖昧さを既に許容している | 実測: `hc read <t> --lines N --json` は live でも finished でも `{"complete":true,"how":"history","text":"..."}`。中身が無ければ `text:""`（live/finished 問わず同形） | **Yes**。hc の方が herdr/tmux より強い情報を返す（`how` で history/screen の別が分かる）が、crewvia の既存契約は「空文字＝読めなかった／本当に空、の両方」を区別しない設計なのでそのまま使える。finished への `capture` はエラーにならず最後の画面を返す——`_require_no_trust_dialog` 系の「空をダイアログなしに倒さない」既存ガードはそのまま効く |
| `list(suffix)` | `TmuxBackend.list()`（:2515-2516）docstring「Return names of live windows」。`HerdrBackend.list()`（:3890-3891）docstring「Return names of live panes...Always queries herdr live (does not use cache) for accurate liveness」 | 実測（§3.5 で確定済み、attempt 4）: `hc connect` の `terminal.list`（**`{"all": false}`**）。`all:false` で live のみ返り、`labels`/`pid` 入りのフルオブジェクトは間引かれない。`all:true` だと finished も残り続ける（§3.0 の根本原因がそのまま出た箇所） | **Yes（`all:false` のときだけ）**。`all:true` のままなら **No**——死んだ Worker が `list()` に居座り、`dispatcher.sh:2018` の `director_live = bool(_mux.list(suffix='-director'))` が「もう死んでいる Director をまだ生きている」と黙って誤認する。attempt 3 まではここが `all:true` だった誤り（review t006 の新 P1。§3.5 に実測の全文） |
| `kill(name, allow_foreign)` | `TmuxBackend.kill()`（:2536-）/ `HerdrBackend.kill()`（:3909-）はどちらも `_refuses_foreign_daemon()` 経由で `may_destroy_pane()` を通してから実際に破壊する——`_inspect_pane_full()` が**その場で**取り直した `handle`/`pid`/`server` を記録と突き合わせる（TOCTOU 対策、`_Backend._refuses_foreign_daemon` docstring :405-419） | 実測: 既に `finished` な terminal への `hc kill` は**エラーにならず Terminal オブジェクトを返す**（冪等。§5・§2 で再現）。`hc kill <name>` を name で呼ぶ設計だと、§3.0 の曖昧さにより**同名の新しい live terminal を殺しうる**理論上の穴が残る（同名 2 件が同時に live な状態は `name_taken` で作れないため完全な再現はできないが、`_inspect_pane_full(name)` が name ベースのままだと kill 直前の再解決が「別の、より新しい terminal」に化ける経路は塞がれていない） | **No（`name` のままでは契約を破る）／Yes（`address` を handle にすれば保つ）**。`_inspect_pane`（:366-376）の契約そのものが「`handle` は caller name ではない」と明記しているのに、attempt 1-4 の表は `kill` を一貫して `name` ベースの `<t>` で設計していた——`_inspect_pane` の契約を読み違えていた。**訂正: `_inspect_pane(name)` は `hc get <name> --json` で得た `address` を `handle` として返し、`kill()` は記録済みの `handle`（＝前回 spawn 時の `address`）を使って `hc kill <address> --json` を呼ぶ**（§3.4）。これなら `may_destroy_pane()` の既存の TOCTOU 対策（記録の handle と今解決した handle の一致を見る）がそのまま効く |
| `pid(name)` | `TmuxBackend.pid()`（:2788-2789）「Return the shell PID of the pane」。`HerdrBackend.pid()`（:3992-3993）同趣旨。契約は**live な pane の shell PID**（死んでいれば `None`）——watchdog.py:641-650 の fail-closed の入口 | 実測（本 attempt で再確認）: `hc get <t> --json` の `pid` は finished でも値を返す（カード記載の Director 実測 `pid:1257222`・`/proc` に無し、を踏襲）。**加えて今回の実測**: `name` で引くと §3.0 の曖昧さにより**別の（より新しい）terminal の pid** を返しうる（同名の finished→live 遷移で実演: `t1h` finished 後に同名で `t1j` を spawn すると、`hc get <name>` は `t1j` の pid を返す） | **Yes（2 つの修正を両方入れれば）**。① attempt 4 で決定済み: `state` が `finished` なら `None` を返す（生きていない pid を watchdog に渡さない）。② 本 attempt で追加: `name` ではなく `address`（handle）で引く（§3.4）——これをしないと、同名の新しい Worker が立った瞬間に古い Worker の pid 問い合わせが新しい Worker の pid を返してしまい、watchdog が**別人のプロセス木を見る**事故になる（カードの retry_reason が挙げた事故の、同根の別パターン） |
| `attach(name)` | `TmuxBackend.attach()`: `$TMUX` が無いときは `False`（attach_cmd + exec 側を使う設計）。`HerdrBackend.attach()`（:3996-）docstring「If HERDR_ENV=1 (already inside herdr), use tab focus...When HERDR_ENV is not set...this path returns False」 | `hc attach <t>` 自体は実在する CLI client だが、**tty が無い（パイプ/リダイレクト）と使えない**ことを実測: `hc attach <t> --json </dev/null >file` は `{"error":{"kind":"usage","hint":"use --observe to watch from a pipe","message":"attach needs a terminal"}}`。`--observe` を付けると非対話でも動く（実測: raw な ANSI エスケープ列を吐いて `[detached from <t>]` で終わる。`--json` は observe の出力形式には影響しない） | **Yes**。常に `False` を返す設計で正しい——hc 自身が「本物の tty から呼べ」という制約を持つ（`herdr` の `HERDR_ENV` 相当の「中に居るかどうか」の概念が無い代わりに、tty の有無で判定される）。crewvia の自動化（dispatcher/watchdog）は tty を持たないプロセスなので、どのみち `attach()` は使えない側に倒れる。これは「見込み」ではなく実測で裏取り済み（attempt 4 までの「未確認」を解消） |
| `attach_cmd(name)` | `RexBackend.attach_cmd()`（:5664-5675）コメント「the `rex` CLI has no terminal client...A person enters a session with Rex.app」→ `None`。`HerdrBackend.attach_cmd()`（:4018-）は `["herdr"]` を返す | 実測: `hc attach --help` の `Usage: hc attach [OPTIONS] [TERMINAL]`。`[TERMINAL]  The terminal: \`t7\`, a name, or \`machine/t7\``——`address` でも `name` でも受け付けることを実地確認（§3.4） | **Yes**。`["hc", "attach", <address または name>]` を返す。Rex と違い hc には本物の CLI client があるので `None` にする必要が無い。exec 後に人間が実際に操作できるかの実地検証は tty が要るため本 task では未実施——**§11 に残す**（attach 自体の tty 制約は実測済みだが別の確認軸） |
| `state(name)` | `HerdrBackend.state()`（:4035-）docstring「Returns one of: blocked/working/idle/done/unknown...Returns unknown on any error (fail-safe)」 | 実測: `hc get <t> --json` の `status.state`。`reported:false` の間は `status` 自体が `null`（live でも finished でも。本 attempt で再確認: `sleep 60` の live 中に `status:null`／`reported:false`） | **Yes（`address` で引く前提で）**。`reported==false` または `status==null` → `"unknown"`（§7.3 で既出）。名前の曖昧さ（§3.0）の影響は `pid`/`kill` と同じ構造なので `address` で引く（§3.4）。`finished` になった後の `status` の保持状況は未確認（`reported:true` の相手を finished にしてから読む実験は本 task では見送った。§11 に追加） |
| `server_identity()` | `_Backend.server_identity()`（:377-391）docstring「`(endpoint, generation)` for the mux server...A pane id is only meaningful inside one (endpoint, generation)」。`TmuxBackend.server_identity()`（:2772-）は `(socket_path, server_pid)`、`RexBackend.server_identity()`（:4905-）は `(endpoint, instance_id)` | 実測: `hc server status --json` の `(core.id, holder.instance)`。`core.id` は desktop の識別子、`holder.instance` は holder（実行プロセス）が変わるたびに別の UUID（実測値 `63f6d40f57ba43dd39e40066789098d2`） | **Yes（§4.3 の前提つき）**。`endpoint = core.id`・`generation = holder.instance` が Rex の `(endpoint, instance_id)` と同じ役割を果たす。**未確認**: holder 再起動をまたいで `core.id` が同じかどうか、かつ `address`（`t7` 等）の採番が holder 再起動後にリセットされ**同じ `address` 文字列が再利用されうるか**（本 attempt の §3.4 の発見により、これが分かっていないと `record_existence` の安全性が holder 世代に依存する——§4.3・§11 に追加） |
| `_inspect_pane(name)` | `_Backend._inspect_pane()`（:366-376）docstring「`(handle, pane_pid)` for `name`...`handle` is the backend's own immutable identity for the thing a kill would destroy...not the caller name, which is mutable」 | 実測（本 attempt、§3.0）: `name` は hc 内部でも mutable（同名の finished→live 遷移で「今の」解決先が変わる）。`hc get <name> --json` の `.address` は**同一 spawn の生涯を通じて不変**（実測: 同じ terminal に対し `get`/`kill` を何度呼んでも `address` は変わらない。`hc rm` されるまで） | **訂正が必要（attempt 1-4 は未定義のまま）。保つ方法: `handle = response["address"]`、`pane_pid = response["pid"] if response["state"]=="live" else None`（finished なら pid 行と同じ理由で `None`）を返す**。これで `handle` が「caller name ではない」という契約を満たす（§3.4） |
| `may_destroy(name)` / identity-ok | `_Backend.may_destroy()`（:466-478、既定実装）: `_inspect_pane_full()` → `may_destroy_pane()`。TmuxBackend（:2735。docstring :393-401 の「Overridden where the backend can answer all three from one query」どおり、効率目的）は `_inspect_pane_full` だけを上書きし `may_destroy()` 自体は共有のまま。**訂正（attempt 6。review t006 の新 P2。「Rex だけ `_inspect_pane_full` を独自定義するが `may_destroy()` 自体は共有のまま」という attempt 5 までの記述は誤りだった）**: RexBackend は `_inspect_pane_full`（:5487）に加えて `may_destroy()` 自体も丸ごと上書きする（:5553-5566）。`_judge()`（:5504-5551）が共有の `may_destroy_pane()` を呼ぶ**前**に record の provenance（`rex_record_ownership`）・generation の一致・プロセスの起動時刻（`rex_worker_provenance`）を調べ、「`normal`（今の世代で生きている）」と「`husk`（サーバー再起動後、死んだプログラムの跡に残った空のログインシェル）」を区別してから、どちらの経路でも最後に共有の `may_destroy_pane()` を呼ぶ（husk でも name が `DAEMON_PANE_NAMES` なら通す、:5545-5550）。Rex がここまで上書きする理由は block が**プログラム死後もログインシェルとして残る**ため——`_inspect_pane` の `(handle, pane_pid)` だけでは「死んだプログラムの跡」と「今も使われている生きたシェル」を区別できない。**注記（attempt 6 で実測して確認）**: `may_destroy_pane()`（:2105-）の分類ロジックは `name in DAEMON_PANE_NAMES`（:916。`("dispatcher", "watchdog")` の 2 つだけ）でないと即 `(True, "not a daemon pane — outside this guard's scope")` に落ちる（:2138-2139。実機の python で確認: `may_destroy_pane("Seo-worker", "hc", "handle", None)` → `(True, 'not a daemon pane — outside this guard's scope')`）——以下の分析は **dispatcher/watchdog 自身の pane を hc で再起動するときだけ**実際に通る経路で、`Seo-worker` のような普通の Worker/Director pane の `kill()` はこの分類をそもそも経由しない（`labels.crewvia_name` 一致確認のような追加の防御だけが Worker にも効く） | hc の `finished` はこれと**同じ構造**の問題——terminal は `hc rm` まで消えず、`hc get` は `state:"finished"` のまま handle（`address`）を返し続ける（§3.0）。§3.4 の決定どおり `_inspect_pane(name)` は `state != "live"` のとき `pane_pid=None` を返す。**dispatcher/watchdog 自身を hc の terminal として spawn している場合**（`lib_daemon_watch.py restart` が自分の pane を `kill()` してから respawn する経路）、これをそのまま共有の `_Backend.may_destroy()` に渡すと何が起きるか実際にコードを読んで追った: `may_destroy_pane()`（:2105-）が呼ぶ `daemon_pane_owner(None, ...)` は `_pane_recognition` の `int(None)` が `TypeError` になるため `problem` 付きで `OWNER_UNKNOWN` を返す（:1300-1303・:1411-1432。実機の python で確認: `daemon_pane_owner(None)` → `('unknown', 'unusable pane pid None')`）。`OWNER_UNKNOWN` は `_OWNERS_THAT_VETO`（:1385）にも `_OWNERS_THAT_PROVE_EMPTY`（:1391、`{OWNER_NONE}`）にも `_OWNERS_THAT_CONFIRM_A_RECORD`（:1408、`{OWNER_MINE, OWNER_NONE}`）にも入らない——record が一致していても `may_destroy_pane()` は常に `False`（「record は一致するが occupant を識別できない……`--force` を使え」、record 不一致なら「何もこの pane を終える根拠にならない」）を返す。Rex の `husk` が「空である」という**積極的な**証拠（generation・provenance を検査した上での判定）なのに対し、`pane_pid=None` は `_pane_recognition` にとって単に「読めなかった」という**消極的な**証拠にしかならない | **No（dispatcher/watchdog の pane に限って素通りでは常に拒否側に倒れる）／Yes（当たらないように運用すれば実害は無い。普通の Worker/Director pane は `DAEMON_PANE_NAMES` の外なのでそもそも当たらない）**。共有の `_Backend.may_destroy()` にそのまま乗せると、**`dispatcher`/`watchdog` という名前で spawn された** finished な hc terminal への `kill()`/`may_destroy()` は `--force` 無しでは**常に拒否**になる——危険側（誤って破壊を許す）には倒れないが、attempt 5 までの kill 行が前提にしていた「`may_destroy_pane()` の既存の TOCTOU 対策がそのまま効く」は、finished な dispatcher/watchdog pane に限っては『効いて許可する』ではなく『効いて常に拒否する』という意味になる。これは crewvia 既存の fail-closed 設計（`UNKNOWN` を `_OWNERS_THAT_CONFIRM_A_RECORD` から意図的に外した 7th round の決定、:1397-1407）どおりに `HcBackend` が上書きしていないことの素直な帰結であって、契約を破ってはいない。**実害は小さい**: finished terminal を `kill` する必要はそもそも無い（hc 自身が `kill` を finished に対して冪等に扱う。§5）——`state()`/`pid()` で finished と分かった時点で `kill()` を呼ばず `record_existence` 経由で記録を畳む経路（§5 末尾で既に決定済み）に進めば、この拒否には当たらない。**Rex のように `may_destroy()` を丸ごと上書きして finished を husk 相当の積極的な空として扱う設計は、この task では決めない**（t002 への宿題として §10 に追加——「`_inspect_pane` を直せば自動的に保たれる」という attempt 5 までの結論は誤りだったので、t002 は dispatcher/watchdog の自己再起動経路に限って『素通りで足りるか、Rex 相当の上書きが要るか』を実装判断する）。`labels.crewvia_name` を `hc get` のレスポンスから読み、`registry/mux/<name>.json` の原綴りと一致するかを確認する追加の防御（実測: `labels.crewvia_name` は `get` に毎回含まれる）は、どちらの設計を採っても `kill()` 内に残してよい |
| `record_existence(name, record, timeout)` / reap-records | `TmuxBackend`（:2690-）/`HerdrBackend`（:3471,3409-）/`RexBackend`（:5037-）の 3 つとも共通して**「記録の id で、記録の generation に束縛して」聞く**契約（`scripts/CLAUDE.md`「mux」:「問い合わせは名前でなく記録の id を、記録の server の世代と同じ接続の上で流す」）。Rex は generation を**前後 2 回**読み直す（`_rex_run(["server","status"...])` を前後に挟む。再起動をまたいだ答えを信用しない） | **訂正（attempt 1-4 の誤り、本 attempt で発見）**: 旧版は `hc get <name>` の `error.kind=="not_found"` だけで判定していたが、これは**名前で引いている**時点で既存契約（id で引く）に反する。§3.0 の実測どおり `name` には複数の過去 spawn が積み重なりうるので、`name` ベースの `get` は**死んだ terminal の記録に対して別の（新しい、生きている）terminal を返してしまい、絶対に `not_found` にならない**——`record_existence` が `PANE_GONE` を一度も返せなくなる、`PANE_EXISTS` を誤って返し続ける事故 | **No（`name` のままでは契約を破る）／Yes（`record["handle"]`＝`address` で引けば保つ）**。実測: `hc get t1h --json`（finished になった古い方の `address`）は常に `t1h` 自身を返す（新しい `t1j` に化けない）。**保つ方法**: `record_existence` は記録の `handle`（`address`、例 `t1h`）で `hc get <address> --json` を呼び、`error.kind=="not_found"` → `PANE_GONE`（`hc rm` された）、成功（`state` が `live` でも `finished` でも）→ **`PANE_EXISTS`**（まだ消えていない、§5 の `finished` 扱いと整合）、他の kind・例外・非 JSON → `PANE_UNOBSERVED`。**Rex 同様 `server_identity()` を前後で読み直す**（holder 再起動をまたいだ `address` 再利用の可能性が未確認——§4.3——なので、Rex と同じ防御を最初から入れる）。読み手: `reap_stale_pane_records()`（`Mux.reap_stale_records()`、:5700-）——誤って `PANE_GONE` を返すと**生きている Worker の記録を消してしまう**、誤って `PANE_EXISTS` を返し続けると registry にゴミが残るだけ（後者の方が安全側） |
| `verify_sent(name, text)` | `Mux.verify_sent()`（:5745-5780）: 既定は `capture()` + `_text_in_input_line()` のヒューリスティック。`isinstance(self._backend, RexBackend)` のときだけ `self._backend.verify_sent(name, text)`（`RexBackend` 独自実装、:5419-）に分岐する——**`_Backend` 自体には `verify_sent` の抽象メソッドが無く、`Mux` 側の `isinstance` 分岐でしか backend 固有の実装を呼べない** | 実測: `hc send` の `SendResult`（`{"started":true\|false\|null,"status":...,"turn":...}`）。`reported:false` の相手には `started` が常に `null`（本 attempt で再確認）。Director 実測（claude-code 相手で `reported:true`）は自分では未再現 | **No（`Mux.verify_sent` を直さない限り保てない）**。`HcBackend.verify_sent()` を実装しても、**`Mux.verify_sent()` の `isinstance(self._backend, RexBackend)` 分岐に `HcBackend` を足さない限り一度も呼ばれない**——これは前回までの表に無かった抜けで、t002 の実装 task に明示的に引き継ぐ必要がある（§10 に追加）。足した上での設計は §8 のまま: `reported==true` かつ `started` が non-null ならそれを採用、それ以外は `capture()` ベースのフォールバック |

### 3.2 `_Backend` インタフェースに対応が無い動詞

- タスク本文が明記した 15 動詞（available / server-running / ensure-server / spawn / send / keys / capture / list / kill / pid / attach / attach-cmd / state / verify-sent / reap-records / identity-ok）は**全部対応がある**（上表）。「対応するものが無い」はゼロ件——hc が RexBackend より API が広い（`terminal.get` が構造化 JSON を返すため、herdr の「pane_list を舐めて label で探す」のような間接参照が要らない）ためで、無理にこじつけた対応は無い
- 逆に hc にあって `_Backend` に無いもの: `hc put`（ファイル転送）・`hc interrupt`（stop key。kill とは別）・`hc wait --children`（複数 terminal の待ち合わせ）・`doc.get/set`（共有ドキュメント）。これらは crewvia の既存動詞に出番が無いので**対象外**（§10）
- **ただし §3.3 の前提条件が 1 つ抜けていた（attempt 1 時点。review t006 の P1）**: 「対応がある」は hc の `--name` が crewvia のペイン名（`Seo-worker` / `Sora-director` のような大文字始まり）をそのまま受け付けることを**暗黙の前提**にしていたが、この前提が成り立たない（§3.3）。動詞の対応自体は変わらないが、`spawn`/`list`/`kill`/`pid`/identity-ok の実装は名前の変換を経由する必要がある——この節を書いた時点では見落としていた

### 3.3 名前の写像（P1 対応。review t006 で指摘・自分で再現して確定）

**事実（実測）**: `hc run --help` の `--name` は「1-32 of a-z 0-9 - _ . starting with a letter or digit」。実際に使い捨て名で確認した:

```
$ hc run --bg --name Cvprobet001-worker --json /home/tkadmin/.local/bin/hc --version
{"error":{"hint":"see `hc schema`","kind":"invalid_params",
  "message":"bad name \"Cvprobet001-worker\": use 1-32 of a-z 0-9 - _ . starting with a letter or digit,
  not shaped like an id (t7)"}}
exit=6
```

一方 crewvia のペイン名は `config/worker-names.yaml` の名前プール（`Hana` / `Seo` / `Minjun` …、全50件とも大文字始まりの ASCII のみ、小文字化しても衝突する組は無いことを確認済み）+ `-worker` / `-director` のロール接尾辞で構成され、**全部大文字始まり**（`lib_mux.py:17-27` のdocstring・`lib_daemon_watch.py` の director 既定値 `"Sora-director"`・稼働中の `registry/mux/Seo-worker.json` 等）。この構成のまま `hc run --name` に渡すと `HcBackend.spawn()` は**1 本目の Worker を起動した瞬間に `invalid_params` で落ちる**。

**決定（採用する方式）**: hc 側には `--label K=V`（`run`/`ls` 双方にあることを実測）がある。

1. `spawn(name, cmd, cwd)`: crewvia の `name`（原綴り、例 `Seo-worker`）を**小文字化しただけ**（例 `seo-worker`）の文字列を hc の `--name` に渡す。ロール接尾辞 `-worker`/`-director` はそのまま hc の許容文字集合（`a-z 0-9 - _ .`）に収まるので、小文字化だけで制約を満たす。**名前プールに大小文字違いの衝突が無いこと（確認済み）と、最長の名前（`Haruto`/`Mathis` 等 6-7 文字）+ 接尾辞でも 32 文字上限に収まること（最長でも `Astrid-director` で 15 文字）から、小文字化は一意性を壊さない**。同時に `--label crewvia_name=<原綴りの name>` を付け、原綴りを hc 側にも残す
2. 実測で裏取り: `hc run --bg --name cvprobet001b --label crewvia_name=Cvprobet001b-worker --json ...` は成功（`state:"live"`）。**訂正（attempt 3。review t006 の新 P1）**: attempt 2 は「続く `hc get` / `hc ls --all --label crewvia_name=...` の両方で `labels.crewvia_name` が往復する」と書いたが、これは誤りだった——`hc schema` の `ListResult.terminals[]` が `Terminal` への `$ref` であることだけを根拠に「`ls` も `get` と同じ形で返るはず」と推測し、それを実測と書いてしまった（attempt 2 で `needs_update` を見落としたのと同じ系統のミス: schema の $ref は「あり得る形」であって「実際にこのコマンドが返す形」ではない）。本当に `hc ls` を叩いて確かめ直すと:

   ```
   $ hc ls --json
   {"hidden_finished":1,"terminals":[]}
   $ hc ls --all --json
   {"hidden_finished":0,"terminals":[{"address":"t14","agent":"hc","cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","exit_code":0,"name":"cvprobet001b","reason":"exited","state":"finished","turn":0}]}
   $ hc ls --all --label crewvia_name=Cvprobet001b-worker --json
   {"hidden_finished":0,"terminals":[{"address":"t14","agent":"hc","cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","exit_code":0,"name":"cvprobet001b","reason":"exited","state":"finished","turn":0}]}
   ```

   `--label` による絞り込みは効く（指定した label を持たない terminal は出てこない）が、**返ってくる `terminals[]` の各要素に `labels` 欄そのものが無い**。つまり `hc ls`（`--all`・`--label` の有無を問わず）では `labels.crewvia_name` を読み戻せない——CLI の `ls` が `Terminal` を間引いた表示用の形に落としているため（`hc get` は同じ terminal に対して `labels` を含むフル `Terminal` を返す。実測: 同じ `t14` に対する `hc get cvprobet001b --json` は `"labels":{"crewvia_name":"Cvprobet001b-worker"}` を含む）
3. **出口は `hc get` の N+1 呼び出しでも、hc 名からの逆変換でもない**（Director が本番で再現・提示した経路を自分でも再現して採用）: `hc connect`（stdin/stdout で JSON-RPC 2.0 を話す。`hc schema` の `info.description` が明記する唯一のプロトコル窓口）の `terminal.list` メソッドは、CLI の `ls` と同じ `ListResult` を返すが、**各 `terminals[]` 要素が `Terminal` を間引かないフルオブジェクト**になる。実測:

   ```
   $ printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":true}}\n' | hc connect
   {"id":1,"jsonrpc":"2.0","result":{"hidden_finished":0,"terminals":[{"address":"t14","argv":["/home/tkadmin/.local/bin/hc","--version"],"cols":200,"created_ms":1793952220052,"cwd":"...","die_with_parent":false,"exit_code":0,"finished_ms":1793952220054,"identity":"ghostty","keep":false,"labels":{"crewvia_name":"Cvprobet001b-worker"},"name":"cvprobet001b","notify":true,"parent":null,"pid":1297336,"reason":"exited","reported":false,"rm":false,"rows":120,"screen":"exact","signal":null,"state":"finished","status":null,"status_ms":null,"template":null,"title":"","turn":0}]}}
   ```

   `labels` / `pid` / `reported` / `status` / `argv` が `list` 1 回で全部そろう。

   **訂正（attempt 4。review t006 attempt 3 の新 P1）**: 上の実測は `{"all": true}` で呼んだものだが、attempt 3 の§3.2-3・§3.3・§2 はこれをそのまま `list()` の実装方式として採用していた。これは `_Backend.list()` の既存の契約（`lib_mux.py:2516` Tmux「Return names of live windows」・`:3891` Herdr「Return names of live panes…Always queries herdr live」）に反する——**`list()` は live のみを返す**契約で、hc は `finished` な terminal を `hc rm` で明示的に消すまで残し続ける（§5 の `finished` kind と同じ現象）。`{"all": true}` のままだと、終了した Worker の terminal が `list()` に居座り続け、`dispatcher.sh:2018` の `director_live = bool(_mux.list(suffix='-director'))` のような生存判定が「もう死んでいる Worker をまだ生きている」と黙って誤認する——`not_found` を見逃す §5 の事故と同じ形（「動くが黙って誤る穴」）。正しくは `{"all": false}` で呼ぶこと。実測で両方の挙動を取り直した:

   ```
   $ hc run --bg --name cvprobet001c --label crewvia_name=Cvprobet001c-worker --json /home/tkadmin/.local/bin/hc --version
   {"address":"t18","agent":"hc","cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","name":"cvprobet001c","state":"live","turn":0}
   $ printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":false}}\n' | hc connect
   {"id":1,"jsonrpc":"2.0","result":{"hidden_finished":1,"terminals":[]}}
   $ printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":true}}\n' | hc connect
   {"id":1,"jsonrpc":"2.0","result":{"hidden_finished":0,"terminals":[{"address":"t18","argv":["/home/tkadmin/.local/bin/hc","--version"],"cols":200,"created_ms":1793953199876,"cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","die_with_parent":false,"exit_code":0,"finished_ms":1793953199878,"identity":"ghostty","keep":false,"labels":{"crewvia_name":"Cvprobet001c-worker"},"name":"cvprobet001c","notify":true,"parent":null,"pid":1312180,"reason":"exited","reported":false,"rm":false,"rows":120,"screen":"exact","signal":null,"state":"finished","status":null,"status_ms":null,"template":null,"title":"","turn":0}]}}
   ```

   （`--version` は一瞬で終わるので `terminal.run` が返った時点で既に `finished`。`all:false` はこれを `hidden_finished` に数えて隠し、`all:true` だけが同じ terminal を `labels`/`pid` 込みのフルオブジェクトで返す。）続けて、まだ生きている terminal では `all:false` でも間引かれないことも確認した:

   ```
   $ hc run --bg --name cvprobet001d --label crewvia_name=Cvprobet001d-worker --json /bin/sh -c 'sleep 5'
   {"address":"t19","agent":"sh","cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","name":"cvprobet001d","state":"live","turn":0}
   $ printf '{"jsonrpc":"2.0","id":1,"method":"terminal.list","params":{"all":false}}\n' | hc connect
   {"id":1,"jsonrpc":"2.0","result":{"hidden_finished":0,"terminals":[{"address":"t19","argv":["/bin/sh","-c","sleep 5"],"cols":200,"created_ms":1793953206375,"cwd":"/home/tkadmin/workspace/crewvia/.claude/worktrees/20261106-herdr-core-backend/t001-herdr-core-backend","die_with_parent":false,"exit_code":null,"finished_ms":null,"identity":"ghostty","keep":false,"labels":{"crewvia_name":"Cvprobet001d-worker"},"name":"cvprobet001d","notify":true,"parent":null,"pid":1312429,"reason":null,"reported":false,"rm":false,"rows":120,"screen":"exact","signal":null,"state":"live","status":null,"status_ms":null,"template":null,"title":"","turn":0}]}}
   ```

   **決定: `list(suffix)` は CLI の `hc ls` を使わず、`hc connect` 経由で `terminal.list`（**`{"all": false}`**）を 1 回呼ぶ**。live の terminal だけが返り、`labels`/`pid` 等のフルオブジェクトは `all:false` でも間引かれない（間引かれるのは表示用の CLI `ls` の方——上の 2 項）ので、「live のみ」の契約と「フルオブジェクトが要る」の両方を同時に満たせる。各要素の `labels.crewvia_name` を crewvia の `name` として返し、`suffix`（`"-worker"` 等）の絞り込みは `crewvia_name` に対して行う。hc 自身の `name` 欄は内部実装の詳細として呼び出し元には見せない。実装は「`hc connect` を subprocess で起動 → 1 行の JSON-RPC リクエストを書く → 1 行読んで `json.loads` → stdin を閉じてプロセスを終了させる」という**呼び出しごとに使い捨てる**形にする（他の動詞と同じ「1 回の呼び出しに 1 回の subprocess」という既存の形を崩さない。常駐接続にして複数リクエストを使い回す案は §3.5 で退ける）
4. **訂正（attempt 5）**: 「`kill` / `pid` / `record_existence` / identity-ok など、crewvia 側が `name` を渡してくる動詞は全部 `name` を小文字化して `hc` を呼ぶ」と attempt 1-4 は書いていたが、これは §3.0・§3.1・§3.4 の発見により**部分的に誤り**だった。`spawn()` の `--name` 引数と `list()` の絞り込みキー（`crewvia_name` ラベル）には小文字化した `name` をそのまま使ってよいが、**`kill`/`pid`/`state`/`record_existence`/identity-ok は `name` ではなく `_inspect_pane()` が返す `address`（handle）を使う**（§3.4）。`send`/`keys`/`capture` は実害が小さい（最新の生きている terminal にしか解決しない limitであり、そもそも同時に 2 つの live は作れないため）が、安全側に倒すなら同様に `address` を使ってよい（§3.4 末尾）
5. `registry/mux/<name>.json` のファイル名・記録 id は**従来どおり crewvia の原綴り**（`Seo-worker.json`）のまま変えない。record の中身に hc の `address`（handle）と `labels.crewvia_name` の両方を保存する（§3.4）

**この方式を採らなかった代替案**: crewvia 側の名前プール自体を小文字に変える案は、`registry/mux/*.json` の既存ファイル名・`config/worker-names.yaml`・ドキュメント中の表記（`Omar-worker` 等）を全部変更する必要があり、既存 backend（tmux/herdr/Rex）の挙動には影響しないが crewvia 全体の表記を変える大きい変更になるため**対象外**（CLAUDE.md 設計原則3「名前はポジション」の表記を崩さない）。

### 3.4 handle は `address` であって `name` ではない（attempt 5 で追加。§3.0 の是正）

§3.0 の実測が示すとおり、hc の `name` は「spawn 時に指定した文字列」であって「この 1 回の spawn を一意に指す不変な識別子」ではない——finished のまま放置された同名の過去 terminal が居る間、`name` ベースの単体 lookup は常に**作成時刻が最も新しい** terminal に解決する（§3.0 で `t1h`/`t1j` を使って実証済み）。一方 hc の `address`（`t7` 等）は spawn ごとに一意に発行され、`hc rm` されるまで**その terminal だけ**を指し続ける（実測: `hc get t1h --json` と `hc get t1j --json` は、両方 finished になった後も互いに取り違えない）。

これは `_Backend._inspect_pane()` の契約（`lib_mux.py:366-376`）がまさに言っていること——「`handle` is the backend's own immutable identity for the thing a kill would destroy...not the caller name, which is mutable」——の hc 版の具体化である。attempt 1-4 は `_inspect_pane` をどの行にも明示的に対応させておらず、`kill`/`pid`/`record_existence` の表の行は暗黙に `name`（小文字化したもの）を `<t>` として使う設計のまま書かれていた。これは tmux の `@window_id` / herdr の `tab_id` / rex の `block_id` に相当するものを**一度も導入していなかった**ということで、§3.0 で見つかった `name` の使い回しと組み合わさると、記録済みの `record_existence` が常に偽陽性（`PANE_EXISTS`）を返し続ける具体的な穴になる（§3.1 の `record_existence` 行）。

**決定**:

1. `spawn(name, cmd, cwd)` は `hc run` の応答 `RunResult.address` を記録し、`registry/mux/<name>.json` に `handle = address`（例 `"t1h"`）を保存する（従来の tmux の `@window_id`・herdr の `tab_id` と同じ扱い）
2. `_inspect_pane(name)` は `hc get <name> --json`（name で引く——spawn 直後や、記録が無い初回解決はこれしかできない）の `.address` を `handle`、`.pid`（`state=="live"` のときだけ）を `pane_pid` として返す
3. **識別が要る動詞（`kill`/`pid`/`state`/`record_existence`/`may_destroy`）は、記録済みの `handle`（`address`）が分かっているときはそれを使って `hc <cmd> <address> --json` を呼ぶ**。記録が無い・初回の解決時だけ `name` を使う。これにより、§3.0 の「`name` は新しい方に解決される」という性質が実害にならない——古い `address` を使う限り、同名の新しい terminal が居ても取り違えない
4. `send`/`keys`/`capture`/`attach`/`attach_cmd` は、呼び出し元が「今まさに自分が spawn した、あるいは `list()` で見つけた生きている terminal」に対して呼ぶのが通常の使い方で、かつ同じ `name` の live terminal は hc 側が `name_taken` で重複を防ぐため、`name` のままでも実害は小さい——ただし §3.1 の表で述べたとおり、安全側に倒すならこれらも `address` を使ってよい（追加コストはほぼ無い。`_inspect_pane` が既に `address` を持っている）
5. hc 自身は `address` と `name` を同じ位置の引数として受け付ける（`hc attach --help` の「`t7`、a name、or `machine/t7`」。実測で `get`/`kill`/`send`/`keys`/`read`/`rm` の全部について確認済み、§2）ので、`HcBackend` 内部で「`<t>` に何を渡すか」を出し分けるだけで済み、hc 呼び出しの骨格（1 verb = 1 subprocess）は変わらない

### 3.5 `HcBackend` 全体を CLI でなく `hc connect` の API で話すべきか（review t006 の新 P1、§3.3-3 の論点）

**決定: 全体を API 化しない。`list()` だけ API（`hc connect`）に逃がし、残り 14 動詞は CLI のままにする。**

API 化の動機は「list / pid / state が 1 回で揃う」こと——`terminal.list`（`{"all": false}`。§3.3-3 で `all:false` に訂正済み）の 1 呼び出しで、live な terminal の `labels`・`pid`・`status.state`（agent status）・`argv` が全terminal 分まとめて返る（§3.3-3 で実測）。これは**`list()` にだけ当てはまる利得**であって、他の動詞には当てはまらない:

1. **`pid(name)` / `state(name)` は単体 lookup**（crewvia 側が `name` または `address` を 1 件指定してくる）なので「1 回で揃う」の利得が無い。CLI `hc get <t> --json` が既に §3.3-2 の実測どおり `labels` / `pid` / `status` を**フルオブジェクトで 1 回**返している——API に切り替えても往復回数は 1→1 のままで、得られる情報も変わらない。変わるのは JSON-RPC の封筒を自分で組み立てる・剥がすコストが増えるだけ
2. **`kill` / `rm` / `send` / `keys` のような単発アクション動詞**は、CLI がすでに「1 回の subprocess 起動で `--json` の結果（または stderr のエラー）を返して終了する」という crewvia の他 3 backend と同じ形を提供している。API 経由にしても呼び出し形は変わらない（`hc connect` を起動して 1 行書いて 1 行読んで閉じる、という骨格は CLI サブコマンドの subprocess 起動と同じ）のに、**CLI が無料でやってくれているリクエスト ID の発行・相関・JSON-RPC envelope の組み立てを自前でやる義務だけが増える**
3. **エラーの形が CLI と API で違う**（実測）。CLI の `hc <cmd> --json` は stderr に `{"error":{"kind":...,"hint":...,"message":...}}` を直接返す（§5）。`hc connect` 経由だと JSON-RPC 2.0 の封筒に包まれ、`kind` は 1 段深い `error.data.kind` に入る。実測（存在しない terminal への `terminal.get`）:

   ```
   $ printf '{"jsonrpc":"2.0","id":1,"method":"terminal.get","params":{"terminal":"tXYZDOESNOTEXIST"}}\n' | hc connect
   {"error":{"code":-32000,"data":{"hint":"see `hc ls --all`","kind":"not_found"},"message":"no terminal \"tXYZDOESNOTEXIST\""},"id":1,"jsonrpc":"2.0"}
   ```

   `kind` の語彙（§5 の 25 件）自体は同じだが、**CLI 用の「エラーかどうかは exit code、kind は `error.kind`」という §5 のパース規約を API 経路にそのまま使い回すと `data.kind` を見落として全部 catch-all（`PANE_UNOBSERVED`）に落ちる**——これは attempt 1〜2 で踏んだのと同じ「2 つの違う形を 1 つのつもりでパースする」事故の形なので、`list()` 用の JSON-RPC ヘルパーは**独立した小さいパーサ**（`error.data.kind` を読む）にし、CLI 用のエラーパーサ（§5）と混在させない設計にする
4. **常駐接続（`hc connect` を 1 プロセス起動したまま複数リクエストを使い回す）にする案**は、1 回の `hc connect` 呼び出しの中で複数行の JSON-RPC リクエストを送って複数行の応答を読めること自体は実測で確認した（2 リクエストを同じ stdin に書いて 2 応答を得た）が、**採らない**。理由: (a) dispatcher / watchdog / start.sh など複数のプロセスが同じ `HcBackend` インスタンスを跨いで動詞を呼ぶわけではなく、**動詞呼び出しごとに新しい python プロセスが `lib_mux.py` を import する**（dispatcher はサイクルごとに新しい python。CLAUDE.md 不変条件 8 と同じ前提）ので、常駐接続を維持する主体が無い。(b) 常駐接続を維持するには「接続が死んでいたら再接続する」「複数スレッド/呼び出し元からの同時リクエストをどう直列化するか」という、tmux/herdr/Rex のどの backend にも無い新しいライフサイクル管理が要る。**1 task（t002）に収めるにはこの複雑さは釣り合わない**——`list()` の低頻度な呼び出し（dispatcher の 1 サイクルに 1 回程度）に対して、呼び出しごとに `hc connect` を起動して 1 往復だけして閉じる（他の CLI 動詞と同じ「使い捨て subprocess」の形）で十分

結論として、`HcBackend` は「14 動詞は CLI サブコマンドの subprocess 呼び出し、`list()` だけ `hc connect` への JSON-RPC 1 往復」という**混在**になる。これは既存 backend のどれとも違う形だが、`list()` という 1 箇所に閉じているので実装の見通しは悪化しない（§9）。

## 4. 版の固定

### 4.1 既存 2 backend の対比（Director 指摘）

今このリポジトリで `review-plan.sh` を走らせると `[mux:herdr] WARNING: herdr version mismatch: expected 0.9.0, got '0.9.5'. CLI interface may have changed.` が出る——これを自分で `scripts/lib_mux.py` を読んで裏取りした。

- **HerdrBackend（`lib_mux.py:3645-3671`）**: `_HERDR_VERIFIED_VERSION = "0.9.0"`（`lib_mux.py:2839`）と食い違っても `self._warn(...)` で `stderr` に警告するだけで、**`available()` はそのまま `self._ensure_server()` の結果を返す**（版が違っても使い続ける）。警告の文面は `_warn()`（`lib_mux.py:479-480`: `f"[mux:{self.BACKEND_NAME}] WARNING: {msg}"`）が組み立てるので、`f"herdr version mismatch: expected {_HERDR_VERIFIED_VERSION}, got {ver!r}. CLI interface may have changed."` と合わせると、報告されたメッセージとコードの形が一致する（実測: 自分ではこのメッセージを実際に出る状態（herdr 0.9.5 が入った環境）では再現していない——コードパスの確認であって実行結果の再現ではない）
- **RexBackend（`lib_mux.py:4911-4927`）**: `_REX_API_REVISIONS = frozenset({9})`（`lib_mux.py:4087`）と不一致なら `available()` が `self._warn(...)` の後 **`False` を返す**。`_select_backend()`（`lib_mux.py:328-348`）は `CREWVIA_MUX` / config の `mode:` で明示選択した backend クラスをそのまま使い、他の backend に切り替えるロジックは無い——つまり「rex を選んだのに available() が False」は**他の backend への自動フォールバックではなく、mux の機能そのものが使えなくなる**という意味での「失敗」

両者は版が違っても使うか、使うのをやめるかで**正反対**。

### 4.2 hc はどちらの作法を採るべきか

**hc は RexBackend と同じ fail-closed にする。** 理由:

1. **hc は 0.1.x**（`hc --version` → `hc 0.1.18`）。`herdr` の 0.9.0 → 0.9.5 のような「マイナーパッチ内でも CLI の挙動が変わった」事例が既に本番で実際に起きている一方、0.1.x は 0.9.x よりさらに若く、**後方互換の建前が無い**（1.0 未満は破壊的変更があり得るのが一般的な版付け規約で、hc 自身も `hc schema` に `"Protocol generation"` という明示のプロトコル世代番号を持つ——これは「黙って変わりうる」ことを作者自身が前提にしている設計の表れ）
2. **herdr の「警告だけで続行」は、実際に CLI の出力形が変わったときに検出できない**。`_warn()` は stderr に 1 行出すだけで、呼び出し元の `available()` の戻り値には影響しない——つまり `herdr 0.9.5` で `pane_list` の JSON の欄が増減していても、HerdrBackend は気づかずに動き続け、パースが通れば偶然動き、通らなければ `None` 扱いで「observed できなかった」側に静かに倒れる（警告は出るが、動作は変わらない）。これは「気づける」設計ではなく「気づいても何もしない」設計で、CLAUDE.md の `CREWVIA_MUX` の項が Rex について明記する「不一致・読めないときは他の backend に落とさず失敗する」という方針と矛盾している
3. **hc には generation という一次概念がある**ので、Rex のような「数値 1 個を集合と比較する」方式がそのまま使える。`hello` の `generation`（uint32）または `server.status` の `version`（semver 文字列）のどちらで固定するか: **`hello.generation`（protocol generation。実測で `1`）を主、`version` の major（`0`）を補助**にする。generation は JSON-RPC の形そのものの互換性を表す値で、`hc schema` の `info.description` が「Errors carry a stable `data.kind`」と明記するような contract の単位。`version` は実装のビルド番号で、同じ generation の中でコマンドの細部（新しい option の追加等）が変わりうる——Rex の `api_revision` と同じ位置づけなのは `generation` の方
4. `_HC_GENERATIONS = frozenset({1})` のような定数にし、`available()` で `hello` を叩いて `generation` を確かめる。**決定（attempt 3。§10-1 で解消）**: `server.status` は generation を持たず、CLI にも `hello` 相当のサブコマンドが無いので、`hc connect`（API を stdin/stdout に出す）経由で `hello` を 1 回叩く。これは `list()`（§3.3-3・§3.5）のために用意する JSON-RPC ヘルパー（subprocess 起動 → 1 行書く → 1 行読む → 閉じる）をそのまま使い回せる——`hc schema` の `info.version` 文字列比較で代用する必要は無い（実測: `hello` の戻りに `generation:1` がトップレベルで入っている。§2 の再現コマンド）

### 4.3 identity（世代）の対応

| | Rex | hc |
|---|---|---|
| endpoint 相当 | `rex server status` の `endpoint` | `server.status` の `core.id`（desktop インストールの識別子） |
| generation 相当 | `instance_id`（server 再起動ごとに変わる。実測） | `holder.instance`（holder＝実行プロセスが変わるごとに変わる。実測で `63f6d40f57ba43dd39e40066789098d2`） |
| 再起動後の ID 再利用 | block_id/window_id は UUIDv7 で保たれる（実測）が中身は login shell に化ける | **未確認（attempt 5 で論点を追加）**。`hc update`（`restarted: true`）は「terminals keep running」と `hc --skill` にあるので、tmux/herdr のような「server 死亡で全部消える」より Rex に近い見込みだが、`hc server stop/restart` を本番で試すのは避けた（CLAUDE.md・`hc --skill` 共に明記の禁止事項）ので**未検証**。**§3.4 の発見により重要度が上がった**: `address`（`t7` 等）が holder 再起動後も連番を引き継ぐのか、`t1` から採番し直すのか（＝同じ `address` 文字列が別の terminal を指すようになりうるか）が分からないと、`record_existence`/`may_destroy` が `address` だけで安全と言い切れない。**保険として、§3.1 の `record_existence` 行で決定したとおり `server_identity()`（`core.id`+`holder.instance`）を前後で読み直す Rex 方式を最初から採用する**（holder が変わっていれば `address` の一致を信用しない） |

`holder.instance` は `server.status` の `holders[]` に複数残る（アップデート後、古い holder は自分の terminals を完走させる、と `StatusResult.installed_by`/`holders` の descriptionにある）。これは RexBackend の「サーバーが入れ替わった後の古い記録は `PANE_UNOBSERVED` 扱いにする」設計をそのまま持ち込める形——**ただし実際に holder が切り替わる瞬間（アップデート）は未確認**（§11）。

### 4.4 既存 backend への影響

**この設計は HerdrBackend・RexBackend のコードを 1 行も変えない。** hc は 3 つ目でなく 4 つ目の backend として独立したクラスで足すだけで、`_select_backend()`（§4.2 で読んだ `lib_mux.py:328-348`）の分岐に `hc` を 1 本足すだけなので、tmux / herdr / rex の既存の選択・挙動には影響しない。**ただし `Mux.verify_sent()`（:5745-）の `isinstance(self._backend, RexBackend)` 分岐には `HcBackend` を足す変更が要る**（§3.1 の `verify_sent` 行・§10）——これは `RexBackend`/`HerdrBackend`/`TmuxBackend` 自体のコードは変えないが、3 backend 共通の `Mux` クラスを 1 行変える。

**ただし Director の指摘は「HerdrBackend 自体の方針を変えるべきでは」という論点を含んでいる。これは t001 のスコープ外（対象外節の「本番の backend 切り替え」に準ずる既存 backend の変更）として、ここでは提案だけ残す**: HerdrBackend の `available()`（`lib_mux.py:3645`）を Rex と同じ fail-closed にする変更は、**本番で herdr を使っている環境すべてに影響する**——`_HERDR_VERIFIED_VERSION` 以外の版（今の本番がまさにそう、0.9.5）で `available()` が `False` になれば、`CREWVIA_MUX=herdr` / `mode: herdr` を選んでいる本番は mux が丸ごと使えなくなる（tmux へのフォールバックは `_select_backend()` に無い）。**やるなら別 PR・ユーザー承認必須**（CLAUDE.md `CREWVIA_MUX` の項と同じ重さの変更）。この文書はそれを決めない。

## 5. エラーの写像（`record_existence` / reap-records）

**読み口（P2 対応。review t006 で指摘・自分で再現して確定）**: `hc <cmd> --json` のエラー JSON は**stderr に出る。stdout は空**。実測:

```
$ hc kill cvprobet001nope --json 2>/dev/null   # stdout のみ → 空
$ hc kill cvprobet001nope --json 2>&1 1>/dev/null  # stderr のみ → {"error":{"hint":"see `hc ls --all`","kind":"not_found","message":"no terminal \"cvprobet001nope\""}}
```

`hc --skill` にも「Errors are `{error: {kind, message, hint}}` on stderr.」と明記されている。**stdout だけ読む実装にすると、エラー時の stdout は常に空なので全エラーが「JSON でない応答」＝`PANE_UNOBSERVED` に落ち、`record_existence` が `PANE_GONE` を一度も返せなくなる**（`registry/mux/<name>.json` が永久に畳まれない）。`HcBackend` の JSON パース共通ヘルパーは、**exit code が非 0 のときは stderr を、0 のときは stdout を読む**設計にすること。

`hc schema` の 32 methods の `errors[]` を全部書き出した（§2 の `hc-schema.json`）。**これは schema の語彙であって CLI 全体が返しうる `kind` の全集合ではない（訂正・attempt 6。review t006 の新 P2 の一部）**——下は `components` 配下の `data.kind` の `const` を機械的に抽出した 25 件（**attempt 1 は `needs_update` を見落として 24 個と書いていたが、正しくは 25 個**）:

```
already_added, blocked, config, conflict, edit_failed, finished, hc_missing,
incompatible, internal, invalid_params, invalid_template, io, live,
machine_unknown, name_taken, needs_framed, needs_target, needs_update, not_found,
not_text, spawn_failed, status_mismatch, unavailable, unreachable, unsupported_generation
```

**CLI はこれ以外の `kind` も返しうる**（実測: `usage`）。`hc attach` を tty の無いパイプから呼んだときのエラー（§2）:

```
$ hc attach cvpt001g --json </dev/null >out.txt 2>&1
{"error":{"kind":"usage","hint":"use --observe to watch from a pipe","message":"attach needs a terminal"}}
```

`usage` は上の 25 件に無い——`attach` は本物の tty クライアントを操作する CLI 固有の機能で `terminal.*` の JSON-RPC メソッドに 1 対 1 で対応しないため、`hc schema` の `errors[]` の集計に現れない。`hc schema`・`--help` の記述から機械的に数え上げた語彙は「schema が明記する最小集合」であって、CLI 全体の上限ではないという前提で読むこと。設計上の結論には影響しない——`usage` も含めて `not_found` 以外は全部、下表の「それ以外すべて」の catch-all に落ち `PANE_UNOBSERVED` になる。`needs_update` も同じ catch-all に含まれ、`PANE_UNOBSERVED` に落ちる（`not_found` 以外という条件に変わりはない）。**`name_taken`（attempt 5 で実機再現。§3.0）も同じ catch-all**: 「live terminal がその名前を既に持っている」であって pane の有無の答えではない。

`terminal.get` / `terminal.kill` の `errors[]` は `not_found` と `machine_unknown` の 2 つだけ（実測、schema より）。

| kind | 観測事実 | `record_existence` の答え |
|---|---|---|
| `not_found`（`terminal.get`/`kill`/`send`/`keys`等） | **実測**: `hc kill tXYZDOESNOTEXIST --json` → `{"error":{"hint":"see \`hc ls --all\`","kind":"not_found","message":"no terminal \"tXYZDOESNOTEXIST\""}}`。exit code は `hc --skill` の一覧で **3**。**attempt 5 で確認**: `record_existence` が `<t>` に渡すのは記録の `address`（§3.4）であって `name` ではないので、ここでの `not_found` は「この `address` を持つ terminal はもう `hc rm` された」という、名前の使い回しに惑わされない確実な証拠になる | **`PANE_GONE`**（CLAUDE.md の規則どおり「その id は無い」と明確に答えた唯一のケース） |
| `machine_unknown` | 未実測（`--on <machine>` を使っていないため）。意味は「リモート機の指定が存在しない」で、pane の有無とは無関係 | `PANE_UNOBSERVED`（この terminal について何も言っていない） |
| それ以外すべて（`finished` / `unavailable` / `blocked` / `status_mismatch` / `config` / `invalid_params` / `spawn_failed` / `name_taken` / `invalid_template` / `needs_target` / `needs_framed` / `needs_update` / `io` / `not_text` / `edit_failed` / `conflict` / `internal` / `unreachable` / `hc_missing` / `incompatible` / `already_added` / `live` / `unsupported_generation`） | `finished` は**実測**（§2 の send/keys テスト。「存在するが終わっている」——これは `PANE_GONE` ではない。herdr/tmux に無い状態で、hc 独自の「死んではいるが record は消していい」にはまだ当たらない） | **`PANE_UNOBSERVED`**（CLAUDE.md の規則: `not_found` 以外は「観測できなかった」側に倒す。`finished` も同様——終わった terminal は `hc rm` で明示的に消すまで `ls --all` / `get` に残り続けるので、「消えた」と「終わった」は別の事実） |
| JSON でない応答・subprocess 失敗・timeout | — | `PANE_UNOBSERVED` |

**hc の `kill` は herdr/tmux と違う挙動がある（実測、§2）**: 既に `finished` な terminal に `kill` しても**エラーにならずTerminal オブジェクトを返す**（冪等。本 attempt でも再現）。これは `record_existence` の話ではなく `kill()` 本体の話だが、`may_destroy_pane` の判定後に hc の `kill` を呼ぶ設計では「`kill` が失敗しなかった」を「生きていたものを殺せた」の意味に使わないよう注意が要る——`kill()` の前に `hc get <address>` で `state` を見て、`finished` ならそもそも kill を呼ばず `record_existence` 経由で記録を畳む経路にする。

`terminal.wait` の exit code 対応（`hc --skill` より）: `3 not_found` / `7 needs_target` / `9 blocked` / `10 status_mismatch` / `11 finished`。CLI の exit code だけで判定せず、**`--json` の `error.kind` を主、exit code は保険**にする（herdr が exit code の意味を変えた前例（`lib_mux.py:2958` 付近の「herdr CLI の引数が変わった可能性」コメント）があるため、文字列の `kind` の方が構造として安定している）。

## 6. PATH

**実測**: `hc server status --json` の `pid`（本機では 1246519）の `/proc/<pid>/environ` を読むと、

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin
```

`~/.local/bin`（hc 自身・claude の実体）も crewvia の `scripts/bin`（python3.12 shim）も**両方とも入っていない**。Director の前提（`hc run --bg claude` が `spawn_failed`）を再現した:

```
$ hc run --bg --name t001probe-bad --json claude
{"error":{"hint":"check the program name and cwd","kind":"spawn_failed","message":"cannot start \"claude\": No such file or directory (os error 2)"}}
```

絶対パス（`/home/tkadmin/.local/bin/hc --version` を argv に渡す）では成功する（実測、§2）ことも確認した。

**対処**: `lib_daemon_watch.spawn_command()`（`scripts/lib_daemon_watch.py:858-`）が既にやっている「起動コマンドに `export PATH=...` を埋め込む」パターンをそのまま使う。hc の `terminal.run` は `argv` が配列だが、crewvia の LAUNCH_CMD は元々 1 本のシェル文字列（`export FOO=...; cd ... && claude ...`）なので、

```
argv = ["bash", "-lc", "export PATH=\"$CREWVIA_REPO_ROOT/scripts/bin:$HOME/.local/bin:$PATH\"; " + LAUNCH_CMD]
```

の形にする——**argv の要素として渡すので、herdr/tmux の「pane に文字列を type する」ときのような引用符のエスケープ地獄（`start.sh` の `_shq` が族 D でまとめて対処している問題）が起きない**。hc 側で PATH を解く設定（`~/.config/herdr-core/config.toml` の `shell = [...]`）もあるが、それは**新しい terminal の既定シェルを変える本番設定**（CLAUDE.md 不変条件 8 の「本番機の `/usr/bin/python3` は触らない」と同種の、ユーザーの環境全体に効く設定）なので、crewvia 側だけで完結する LAUNCH_CMD への埋め込みの方が安全——Rex 設計書の §8 が言う「`env=` 引数は廃止、PATH は起動コマンドに埋める」の方針をそのまま踏襲する。

## 7. agent status の写像（Rule 5）

### 7.1 hc の語彙

`hc get <t> --json` の `status`（`Status` schema、実測 + `hc schema`）: `state` は `idle` / `working` / `done` / `blocked` / `error`。トップレベルの `reported`（bool、デフォルト `false`）が「一度でも 7501 を報告したか」を持つ。

- **Director 実測（自分では再現していない）**: `claude` を起動すると `{"app":"claude-code","state":"idle"}` / `reported: true`
- **自分の実測**: `sh -c 'sleep 90'`相当（`hc --version` の一瞬の実行、本 attempt では `sh -c 'sleep 60'` の live 中）は `status: null` / `reported: false`。Director の「`sh -c 'sleep 90'` は `status: null` / `reported: false`」と型として一致（終了済みプロセスでの確認だが、報告しないプログラムの扱いという点で同じ現象）

### 7.2 Rule 5 の条件への対応

`agents/director.md` §Rule 5: A（`blocked`）/ B（`idle` または `done` かつ assignment あり）。

| hc の `status.state` | `reported` | Rule 5 への対応 |
|---|---|---|
| `blocked` | true | A（dispatcher が permission/question の通知を出す対象） |
| `idle` | true | B（assignment があれば idle-with-task） |
| `done` | true | B |
| `working` | true | 対応なし（動いている。Rule 5 対象外） |
| `error` | true | 新規の第3カテゴリ——A/B のどちらでもない。Rex/herdr には無い値。**Director への通知を別枠で立てるか B 相当に含めるかは決めること**（§10） |
| （何でも） | **false** | **`"unknown"` に倒す**（§7.3） |

### 7.3 `reported: false` を `idle` にしない

タスク本文の警告どおり、これは設計上の核心。`reported: false` は「このプログラムは 7501 を一度も報告していない」で、「手が止まっている」とは別の事実——`claude` のように起動直後は `reported: false` で、起動が終わって最初の 7501 を出した瞬間に `true` になる見込み（**未確認**: 起動シーケンスの途中経過は観測していない）。この遷移中に `idle` 扱いにすると、起動がまだ終わっていない Worker を「手が空いている」と誤認して次のタスクを投げる事故になる。

`state()` の実装方針: `reported == false` → **`"unknown"`**（RexBackend の `rex_screen_state()` が `unknown` のときに「通知しない側」に倒れるのと同じ fail-safe）。`reported == true` のときだけ `state.state` をそのまま返す。

### 7.4 `mux.state_grace_seconds` への影響

`config/crewvia.yaml` のコメント「tmux モードでは agent_status が取得できないためこの設定は使用されない」は hc には当てはまらない——hc は herdr 同様 `state()` が本物の値を返せるので、**`state_grace_seconds` は hc でも効く設定になる**。これは config のコメントを直す対象（実装 PR 側の作業、この文書では指摘のみ）。

## 8. `verify-sent`

### 8.1 既存の意味（自分で読んで確認）

`lib_mux.py:5419-5437`（RexBackend.verify_sent）のdocstring: 「`text` が Claude の*入力欄*にもう無いこと」を確かめる screen-scraping。`lib_mux.py:2437` 付近のコメントが言う「verify_sent が前回の送信を『届いていない』と報告する」問題を踏まえ、リトライ前に必ず呼ぶ設計。IRC 中継（`knowledge/irc-mention-relay.md`、CLAUDE.md の記述）は「`lib_mux.verify_sent` は会話記録の `❯` エコーに一致して再送するので使わない」と明記——つまり既存の `verify_sent` は**完璧ではなく、画面上の見た目に依存する弱いヒューリスティック**だと crewvia 自身が認識している。

### 8.2 hc で使える信号

`hc send` の `SendResult`（`hc schema`、実測）:

```json
{"started": true|false|null, "status": "idle"|"working"|...|null, "turn": N, "wait": {...}|null}
```

`started` の定義（schema の description、実測）: 「数秒以内に `working` になったか。報告しないプログラムには `null`」。`reported: true` の相手（claude-code 想定）であれば、**`started: true` は「画面から消えた」より強い証拠**——プログラム自身が「取り込んで動き出した」と言っている。`send --wait` を使えば `SendResult.wait`（`WaitResult`）に turn の終わり方（`reason: status/exit/output/timeout/not_started`）まで入る。

### 8.3 `reported: false` の相手への対応

`started` が常に `null` になる（報告しないプログラムには判定できないと schema が明記）。この場合は**既存の `_text_in_input_line` 相当にフォールバック**——`hc read <t> --lines N` で画面を取り、§8.1 の screen-scraping ロジックをそのまま流用する。つまり `verify_sent()` の実装は:

1. `reported == true` の対象 → `SendResult.started`（`null` でなければそれを採用。`true`/`false` をそのまま返す）
2. `reported == false` または `started is null` → `hc read` ベースの screen-scraping（herdr/Rex と同じ入力欄ヒューリスティック）

**これは Director 実測（claude-code は reported:true）を自分では再現していないため、1 の経路は未検証**（§11）。2 の経路（screen-scraping）は既存コードの移植なので新規リスクは無い。**加えて（attempt 5）**: `HcBackend.verify_sent()` をこの設計で実装しても、`Mux.verify_sent()`（:5745-）の `isinstance(self._backend, RexBackend)` 分岐に `HcBackend` を足さなければ一度も呼ばれず、常に既定の `capture()` + `_text_in_input_line()` 経路に落ちる（§3.1 の `verify_sent` 行・§10）。

## 9. 実装規模の見積もり

- `RexBackend` は `lib_mux.py:4884-5675`（約 790 行）。`HerdrBackend` は `3352-4883`（約 1530 行）。`TmuxBackend` は `2204-3351`（約 1150 行）
- hc 版は Rex 規模に近いと見積もる。**増える要因**: ① JSON-RPC 的な構造化パース（`hc <cmd> --json` の出力を dataclass 的に正規化するヘルパーが要る——ただし herdr/Rex も同種のラッパー（`_herdr_run` / `_rex_run`）を既に持っているので新規パターンではない）。② `list()` 用の小さい JSON-RPC ヘルパーが別途要る（§3.3-3・§3.5）: `hc connect` を subprocess で起動 → 1 行の JSON-RPC リクエストを書く → 1 行読んで `json.loads` → 閉じる、という往復と、`error.data.kind`（§3.5 で実測した API 固有のエラー封筒）を読む専用の小さいパーサ。CLI 用のパーサ（`error.kind` を直接読む、§5）とは意図的に共有しない設計なので、**パーサが 1 つでなく 2 つ**になる。③ **（attempt 5 で追加）`handle`（`address`）を記録・引き回す配線**: `spawn()` が `registry/mux/<name>.json` に `address` を書き、`kill`/`pid`/`state`/`record_existence`/`may_destroy` がそれを読んで `name` の代わりに使う分岐——ただし既存 3 backend も「handle を記録して突き合わせる」設計は`_inspect_pane`/`may_destroy_pane` の共有フレームワークが既に持っているので、`HcBackend` 側で書く量は `_inspect_pane()` の中身（`hc get` を呼んで `.address`/`.pid` を取り出すだけ）に収まる。ただし呼び出し箇所は `list()`（と §10-1 の `hello`）の 1 箇所に閉じるので、合わせても 30-50 行程度と見積もる（Rex の `rex_screen_state()` 系正規表現 1 個分より小さい）。**減る要因**: `state()` と `verify_sent()` の主経路が screen-scraping を使わない分、Rex の `rex_screen_state()` / `_rex_input_box()` / `_rex_dialog` 系の正規表現群（`lib_mux.py:4779-4843` 相当、60行強）が要らない
- 版固定（§4）・identity（§4.3）・エラー写像（§5）・PATH 埋め込み（§6）は RexBackend で確立済みのパターンをなぞるだけで、設計判断としての新規性は低い（コード量としては通常の backend 1 つ分）。`list()` の API 化（§3.5）・`address` ベースの handle 配線（attempt 5 で追加）で増える分は上の見積もりに織り込み済みで、全体の規模感（Rex 並み）を変えるほどではない
- **1 task（t002）に収まる見込み**。分割するとすれば「`HcBackend` 本体 + 隔離テスト」と「start.sh / dispatcher / watchdog の配線 + `Mux.verify_sent()` の isinstance 分岐追加」の 2 PR（Rex の PR-1 / PR-2 と同じ割り方）だが、Rex はこれを 1 つの mission の 2 task で収めているので、t002 を同様に 2 分割するかは実装 task 側の判断に委ねる

## 10. 決めること（ユーザー判断 / 実装 task 側で詰める点）

**attempt 1 からの差分**: 名前の写像（hc の `--name` が crewvia の大文字始まりペイン名を受け付けない問題）は review t006 の P1 指摘を受けて §3.3 で決定済みにしたので、この節からは除外した（attempt 1 の時点でこの節にも §11 にも載っておらず、それ自体が review の指摘事項だった）。

1. **generation の取得経路**（§4.2-4.4）: **attempt 3 で解消**。CLI に `hello` 相当のサブコマンドは無いが、`list()`（§3.3-3・§3.5）で導入する `hc connect` の JSON-RPC ヘルパーに相乗りして `hello` メソッドを呼べばよい。実測:

   ```
   $ printf '{"jsonrpc":"2.0","id":1,"method":"hello","params":{}}\n' | hc connect
   {"id":1,"jsonrpc":"2.0","result":{"core":{...},"features":[...],"framed":false,"generation":1,"methods":[...],"version":"0.1.18"}}
   ```

   トップレベルに `generation`（実測で `1`）がそのまま入っている。`hc schema` の `info.version` 文字列比較で代用する案は不要——`list()` 用に用意する JSON-RPC ヘルパーをそのまま使い回せるので、実装側で新しい経路を増やす必要もない
2. **`error` state の Rule 5 対応**（§7.2）: A/B のどちらにも無い新しい値。Director への別枠通知にするか B に含めるか
3. **holder 切り替わり時の挙動**（§4.3）: `hc update` 後に「terminals keep running」が実際にどう見えるか、特に**`address` の採番が holder をまたいで連続するか、`t1` からリセットされるか**（§3.4 で重要度が上がった論点）は、本番の hc を実際にアップデートする機会がないと検証できない——未検証のまま「Rex と同じ設計で安全側に倒す（`server_identity()` を前後で読み直す）」ことにして進めるか、検証を待つか
4. **`attach()` / `attach_cmd()` の実地検証**（§3.1 の「tty 制約は実測済みだが exec 後に人間が操作できるかは別」の件）: 実際に `hc attach` を exec してみる確認は t002 で行う
5. **（attempt 5 で追加）`Mux.verify_sent()`（:5745-）の `isinstance(self._backend, RexBackend)` 分岐に `isinstance(self._backend, (RexBackend, HcBackend))` のように `HcBackend` を足す変更**: `HcBackend.verify_sent()` を実装するだけでは呼ばれないので、t002 の実装 task はこの 1 行の変更を**明示的にスコープに含める**こと（§3.1・§8.3・§9）
6. **（attempt 6 で追加）`may_destroy()`/`kill()` の finished terminal 判定（`dispatcher`/`watchdog` の自己再起動経路だけ）**: `may_destroy_pane()` の分類ロジックは `name in DAEMON_PANE_NAMES`（`("dispatcher", "watchdog")`）でなければ素通りで許可されるので、影響するのはこの 2 つの pane だけ。`_inspect_pane` が `state != "live"` で `pane_pid=None` を返す決め（§3.4）を、dispatcher/watchdog 自身の pane に対して共有の `_Backend.may_destroy()` に素通りさせると `daemon_pane_owner(None, ...)` が `OWNER_UNKNOWN` になり、finished な terminal への `kill`/`may_destroy` を `--force` 無しでは常に拒否する（§3.1 の `may_destroy` 行）。fail-closed なので危険側には倒れないが、「`kill()` の前に `state` を見て finished なら `record_existence` 経由に回す」運用で避ける（素通りのまま）か、Rex と同じく `may_destroy()` 自体を上書きして finished を husk 相当の積極的な空として許可する設計にするかは、t002 の実装 task の判断に委ねる

## 11. 未確認（実装前に確かめる一覧）

- `claude`（claude-code）を hc の terminal で実際に起動し、`reported: true` / `status.state` の遷移を自分で観測する（Director 実測の再現。コストと本機への影響を考え本 task では見送った——claude-code のネストしたセッションを本番 hc server 上に作ることになるため）
- `hc server` の再起動（`hc update` の `restarted: true` を含む）で terminal の ID・holder.instance がどう変わるか、**特に `address`（`t7` 等）の採番が holder をまたいで連続するか**（§4.3・§3.4・§10-3）。本番の hc server を止める操作はこの task の方針（CLAUDE.md・`hc --skill` 共通の「頼まれていないのに止めるな」）に反するため未実施
- `hc attach <t>` を実際に exec して人間が操作できるかの実地検証（tty 制約自体は実測済み。§3.1・§10-4）。t002 でも対話的な tty が無く未実施のまま
- `machine_unknown` kind の実測（`--on` を使うリモート機能は対象外のため）
- `server` が無い状態での `hc server status --json` の挙動（起動するのか、エラーになるのか）。本番 server を落として試すのは禁止事項に反するため未実施（§3.1 の `server_running()` 行）
- `finished` になった後も `status`（agent status）欄が保持されるかどうか（§3.1 の `state(name)` 行）。`reported:true` の相手を finished にしてから `status` を読む実験は本 task では見送った

## 12. 改訂（t002 実装）

Worker Ingrid（mission `20261106-herdr-core-backend` t002）。`HcBackend` を `scripts/lib_mux.py` に実装した。
本機（§2 と同じ本番 desktop server、hc 0.1.18）で使い捨て terminal（`Cvt002probe*-worker`、後始末済み）を実際に
spawn/state/pid/capture/list/attach/kill/record_existence/`hc rm` の順で叩き、下の各点を**実測**で裏取りした
うえで実装した。設計 §10 の未決事項への答えもここにまとめる。

1. **§10-1（generation の取得経路）**: 設計どおり `hc connect` の `hello` を使った。`available()` は
   `hc server start --json`（冪等。実測: 既に生きている server に対して pid/uptime 不変）→ `hello.generation`
   が `_HC_GENERATIONS = frozenset({1})` に無ければ False。**実測**: 本機で `available()` → `True`、
   `server_identity()` → `("972c9056285907f04dfc72170c12ac7b", "63f6d40f57ba43dd39e40066789098d2")`
   （`core.id` / `holder.instance`、§4.3 のとおり）。
2. **§10-2（`error` state の Rule 5 対応）**: この task では決めなかった。`state()` は `status.state == "error"` を
   そのまま返す（他の4値と同じ関数内で、idle/working/done/blocked と並べて許可）。dispatcher.sh の Rule 5 には
   まだ `"error"` の分岐が無いので、今のところ A（blocked）にも B（idle/done）にも当たらず**通知されない**
   （危険側ではない——Rule 5 が無視するだけで、watchdog の max 等の既存の安全弁は別に効く）。dispatcher.sh 側の
   配線は別タスクにした（lib_mux.py の外に出る変更で、今回の実装スコープ外と判断した）。
3. **§10-3（holder 切り替わり時の `address` 再利用）**: 本番 server を再起動する検証はこの task でも実施して
   いない（禁止事項どおり）。設計の決定（`record_existence()` が `server_identity()` を前後で読み直す。Rex と
   同じ防御）をそのまま実装した——未確認のリスクは据え置きだが、保険は入っている。
4. **§10-4（`attach()` / `attach_cmd()` の実地検証）**: `attach_cmd()` が返す `["hc", "attach", <target>]` の
   `hc attach` 自体が tty を要求すること（`usage` kind）は t001 時点で実測済み（§3.1 を再確認し、本 task でも
   変更していない）。**ただし「exec 後に人間が実際に操作できるか」は、このセッションにも対話的な tty が無く
   本 task でも検証できていない**——§11 の該当項目は未確認のまま残す。
5. **§10-5（`Mux.verify_sent()` の isinstance 分岐）**: 設計どおり `isinstance(self._backend, (RexBackend,
   HcBackend))` に変更した（`lib_mux.py` の `Mux.verify_sent()`）。ただし `HcBackend.verify_sent()` 自体は
   §8.3 の案 1（`reported: true` の相手に `SendResult.started` を使う）を実装していない——`started` は
   `send()` のその場の呼び出しでしか手に入らず、`verify_sent()` は（`send()` と同じ python プロセスとは
   限らない）**別の呼び出し**なので、読み戻す手段が無い（CLAUDE.md 不変条件 8: dispatcher はサイクルごとに
   新しい python）。設計が想定していた「同一プロセス内で送信直後に確認する」経路はこの構造と噛み合わない
   ため、この task では screen-scraping（§8.3 案 2）だけを実装し、`Mux.verify_sent()` の既定経路と同じ挙動に
   した（isinstance への追加自体は、将来 backend 固有の改善を入れる置き場として残す）。§11 の「`started` が
   実際に使えるか」の検証は、この構造上の制約が先に解消されないと意味を持たない。
6. **§10-6（`may_destroy()`/`kill()` の finished terminal 判定）**: Rex 相当の上書きはせず、共有の
   `_Backend.may_destroy()` に素通りさせた（fail-closed のまま。`dispatcher`/`watchdog` 自身の hc pane が
   finished のときの `kill()`/`may_destroy()` は `--force` 無しでは常に拒否）。理由は設計の見立てどおり実害が
   小さいこと——finished と `state()`/`pid()` で分かった時点で `kill()` を呼ばず `record_existence` 経由で
   記録を畳む経路に進めば、この拒否には当たらない。
7. **（実装時に判明。設計書には無い）入口スクリプト 3 つが `hc` を知らないと事故る**:
   - `scripts/start.sh` の director 専用 backend 解決の「env (`CREWVIA_MUX`) と config (`mode:`) の食い違いを
     揃える」ロジック（`case "$_ENV_MUX" in tmux|herdr|rex)`）は `hc` を含んでいなかった。`CREWVIA_MUX=hc` を
     env で指定しても、config の `mode: herdr`（本番の既定）がそのまま `herdr` の不在チェック・
     `export CREWVIA_MUX=herdr` に流れ、**env の `hc` 指定を黙って上書きする**——ちょうどこのコメントが警告
     している事故そのもの。`hc` をケース文に足し、herdr と対称な不在チェック・`available()` チェック・
     `mode: hc` の分岐を追加した（実測で再現・修正の両方を確認。テスト:
     `tests/test_rex_pr2_wiring.py::test_start_sh_env_hc_is_not_silently_overridden_by_config_mode_herdr`）。
   - トップレベルの `./crewvia` 自身の `mode:` 読み取り（`case "$MODE" in tmux|herdr|rex|inline|*)`）にも
     `hc)` が無く、`mode: hc` は `*)` の catch-all に落ちて「不明な mode」警告つきで `tmux` に強制される
     ——これは env での上書きが無い、**config だけで `mode: hc` を選ぶ一番普通の経路**で踏む。`hc)` の分岐と、
     herdr/rex と対称な不在チェック・`available()` チェックのブロックを追加した（実測で再現・修正を確認。
     テスト: `tests/test_hc_crewvia_launcher_wiring.py`）。
   - `scripts/crewvia-stop.sh --full` の backend 判定（`lib_mux._select_backend().BACKEND_NAME`。これ自体は
     汎用なので `hc` も正しく返る）の後の分岐は `rex` だけ専用メッセージを持ち、それ以外は無条件に
     `herdr server stop` を実行する——`hc` を選んでいても、インストールされていれば**無関係な herdr server
     を止めてしまう**。rex と同じ理由（利用者のデスクトップ全体の server で、crewvia 以外の terminal も
     持つ）で `hc` 専用の「止めません」分岐を足した。
   - `scripts/setup-new-env.sh` の mux 検出（情報表示のみ、分岐の事故ではない）にも `hc` を足した。
   いずれも `lib_mux.py` 自体の変更ではなく、入口スクリプト側が新しい backend 名を知らないことによる
   脱落——`_select_backend()` のような汎用の判定を通さない箇所ほど、同じクラスの事故を踏みやすいことが
   分かった（3 箇所とも「文字列リテラルの一覧に `hc` を足し忘れる」という同型のミス）。
8. **（実装時に判明）`kill()` は `hc rm` を呼ばない**: finished terminal を `hc rm` するのは運用（手動確認の
   後始末・将来の gc task）の仕事のままにし、`kill()` は crewvia 側の記録を畳むところまで。**実測で確認**:
   `kill()` 後も `hc ls --all` に `state:"finished"` として残り続け、`record_existence()` はその record に対して
   引き続き `PANE_EXISTS` を返す（設計 §3.1 の決定どおり。「消えた」と「終わった」が別概念であることと整合）。
9. **（実装時に判明。実測で確認）`spawn()` の PATH 補完が実際に効くことを確認した**: `hc run` に生の
   `claude`/`hc` を argv で渡すと `spawn_failed`（§6 のとおり、再現済み）になるのに対し、`HcBackend.spawn()`
   が組み立てる `bash -lc "export PATH=...; " + cmd` 経由で bare `hc --version` を実行すると成功する
   （本機での実測。`$HOME/.local/bin` が無いと即死する再現と、有ると直ることの両方を確認した）。
10. **（実装時に判明。実測で確認）`name_taken` の挙動**: 同じ名前へ live な terminal がある間の 2 回目の
    `spawn()` は `last_spawn_refusal == SPAWN_OCCUPIED` で False になることを実機で確認した（§3.0 の記述と
    一致）。
11. テスト: `tests/test_hc_backend.py`（偽の `hc` を PATH に置く。`hc` が本物の CLI/`hc connect` の両方の形で
    応答できるよう `tests/hc_harness.py` の `FakeHc` を新設——`tests/rex_harness.py` の `FakeRex` と同じ立て付け）。
    CI で常に走る（本物の hc は入っていない環境を想定）。mutation 観点で特に厚くしたのは、設計のここまでの
    attempt が繰り返し壊した 4 点——① kill の認可（`not_found` 以外は `PANE_GONE` にしない。`finished` /
    `name_taken` / `usage` / `needs_update` をパラメタ化してテスト）② 版の固定（`hello.generation` 不一致で
    `available()` False）③ `reported: false` を `idle` に倒さない④ `spawn()` の argv に PATH export
    （`scripts/bin` と `$HOME/.local/bin`）が入っていること。Rex 自身のテスト（`test_rex_backend_stub_protocol.py`）
    にも独立した `red_proof_*` スクリプトは無く、偽の CLI に対する直接的な assertion がその役を果たしている
    ——`HcBackend` も同じ作法に揃えた（別立ての mutation-injection スクリプトは作っていない）。
