# 本番 mux backend を herdr → hc (herdr-core) へ切り替える runbook

対象は**運用手順**（上から順に実行できる形）。`HcBackend` の設計・実測・未決事項は
`knowledge/herdr-core-mux-backend.md`（以下「設計書」）にあり、ここでは重複しない——設計の根拠は
設計書の節番号へのリンクで済ませる。

**新規ファイルにした理由**: 設計書は t001（設計）+ t002（実装）の attempt 履歴で、冒頭に明記のとおり
「設計」がスコープ。本書は「herdr が本番で動いている状態から、どの順でどのコマンドを打てば hc に
変わるか／戻るか」という**運用**で、対象読者（切り替えを実行する Director）も粒度も違うため、
既存ファイルに節を足すより分離した方が両方とも読みやすい。README には「任意機能の使い方」以上の
運用 runbook を置く前例が無く（`knowledge/daemon-authority.md` 等、運用ドキュメントは全部 `knowledge/`
配下）、置き場所として揃えた。

**実測の凡例**: 「実測」= 本書作成中に実際に実行し、出力を貼った。「読んだ限り」= コードを読んだだけで
実行していない。本番の hc server・herdr server・dispatcher/watchdog・稼働中の Worker/Director pane は
本書作成中には一切止めていない（使い捨ての probe terminal 以外は本番に触れていない）。

## 0. 前提

- 対象版: hc 0.1.18（protocol generation 1）。本機 `core.id=972c9056285907f04dfc72170c12ac7b`、
  本書作成時点の `holder.instance=63f6d40f57ba43dd39e40066789098d2`（実測: `hc server status --json`）。
  設計書 §2 が記録した値と同じ holder — 設計書作成以降、hc server は再起動していない。
- 現在の本番設定: `config/crewvia.yaml` の `mode: herdr`（コメント「戻す時は tmux。切替日: 2026-09-04」）。
- `_select_backend()`（`scripts/lib_mux.py:331-355`）は `hc` を認識済み、`start.sh` / `./crewvia` /
  `scripts/crewvia-stop.sh` の 3 入口スクリプトは全部 `hc` ケースを持つ（実装時に判明した脱落は設計書
  §12-7 で既に埋めてある。読んだ限り、3 箇所とも herdr/rex と対称な不在チェック・`available()` チェックが
  入っている）。つまり**コード側に未着手の穴は無い**——本書は「それをどの順で踏むか」だけを決める。
- 本書は**この切り替えを Director が実行する前提**で書く（CLAUDE.md の `CREWVIA_MUX` の項と同じ重さの
  変更 = ユーザー承認必須）。切り替え自体は本書を書いた作業（t002）の範囲外で、**ここでは実行していない**。

## 1. 切り替え前に確認すること（実測、本書作成時点のスナップショット）

1. `./scripts/plan.sh status` で active mission / in-progress task を確認する。本書作成時点では
   mission `20261109-hc-production-switch` の t001（worker Ingrid）・t002（本タスク、worker Mateo）が
   `in_progress`。
2. `registry/mux/*.json` のうち「pane record」の形（`handle`/`backend`/`server` を持つもの。
   `scripts/lib_mux.py:1611` `write_pane_record()` が書く形）を数える。実測、本書作成時点で **5 件**:
   `Sora-director.json`（Director 自身）・`dispatcher.json`・`watchdog.json`・`Mateo-worker.json`・
   `Ingrid-worker.json`。全部 `"backend": "herdr"`。**`*.state.json` / `*.job-since.json`（`Chiara` /
   `Elena` 等）は別物**——agent status のキャッシュ・Rule 5 の job-since マーカーで、pane record ではない
   （`scripts/lib_mux.py:1908-1910` が sweep の対象からも明示的に除外している。`scripts/CLAUDE.md`
   「通知・記録」の項が言う「消してよい」台帳の仲間で、mux backend には無関係——触らない）。
3. hc 側の現状（実測）:
   ```
   $ hc server status --json
   {"core":{"id":"972c9056285907f04dfc72170c12ac7b","name":"desktop-l8nqlib"},
    "holder":{"current":true,"instance":"63f6d40f57ba43dd39e40066789098d2","pid":1246708,"terminals":0}, ...}
   $ hc ls --all --json
   {"hidden_finished":0,"terminals":[]}
   ```
   hc 側には残骸が無い（herdr 専用の probe 以外、まだ誰も hc を使っていない）。
4. `CREWVIA_MUX=hc python3 scripts/lib_mux.py available` → 実測 `exit=0`（True）。
   `CREWVIA_MUX=hc python3 scripts/lib_mux.py list -worker` → 実測 `exit=0`、出力なし（まだ誰もいない）。
   つまり hc への切り替え自体は版・到達性の面で今すぐ可能——残りは「手順」の問題。

## 2. 決めること: 稼働中の Worker をどう扱うか（ドレインするか、打ち切るか）

`scripts/crewvia-stop.sh --force` は **`*-worker` を無条件に kill する**（mid-task かどうかを見ない。
`scripts/crewvia-stop.sh:123-131`）。読んだ限り、kill された Worker の task card は `in_progress` のまま
（checkpoint は取られない——checkpoint は `plan.sh retire` の退役パスの中でだけ実行され、`crewvia-stop.sh`
はその経路を通らない）。孤立した card は `scripts/CLAUDE.md` の「回復」節の R-1/R-2 や、Director の
`plan.sh update --reset` / `--close-execution` で後から畳む——仕組みとしては壊れないが、**その Worker の
今の作業は失われる**。

2 択。Director がユーザーと決める:

- **A. ドレインする（推奨）**: 新規 `pull` を止め（Worker には伝えない——`pull` 自体は mux と無関係なので
  queue 側の制御は無い。実務的には「全 Worker が今の task を `done`/`fail`/`needs-director` で report し、
  dispatcher が次を assign する前」を待つ）。本書作成時点の t001（Ingrid）・t002（本タスク）が完走・report
  してから進める。
- **B. 打ち切る**: `--force` で今すぐ止める。進行中の task は worker 無し `in_progress` のまま残るので、
  切り替え後に Director が `plan.sh update <id> --status pending --reset`（または `--close-execution`）で
  手当てする。

どちらを選んでも 3 節以降の手順は同じ（A は「3 節の時点で `*-worker` が 0 件」という前提が自然に満たされる
だけ）。

## 3. 切り替え本体（herdr → hc）

### 3.0 Director 自身の扱い（事実として明記する）

**今これを読んでいる Director のセッションは、3.2（全停止）の時点で終わる。** 今の Director は herdr の
pane（`HERDR_PANE_ID=w1:pG` 相当、`CREWVIA_MUX=herdr` を env に持つ、窓名 `Sora-director`）で動いており、
3.2 の `crewvia-stop.sh --force` は worker/dispatcher/watchdog に続けて `*-director` tab も kill する
（`--keep-director` を付けない限り。1 節のとおり、別ターミナルから実行するので herdr 側の自動
`--keep-director` も効かない）。これは事故ではなく**意図した手順**——切り替え後に dispatcher が
`list(suffix='-director')` を hc backend で呼んでも、herdr の pane は hc からは見えない（設計書の
いう「別の backend からは存在しない」）ので、config だけ変えて Director を放置すると、黙って
`director_live=False`（`scripts/dispatcher.sh:2222`）に倒れ、Rule 5 の通知が
「`WARNING: Rule 5 — Director 不在のため通知スキップ`」とログに出るだけで**誰にも届かなくなる**
（`scripts/dispatcher.sh:2231-2235`。読んだ限り、director_live が False でも dispatcher が代わりに
何かを試みることは無い——ただ通知を諦めるだけで、危険側には倒れない）。本書はこれを「後から気付く
サイレント故障」にせず、3.2 の時点で**明示的に**終わらせる。

**立て直す手順**: 3.2〜3.4 を最後まで行った後（config が `mode: hc` になり、全 pane が落ちている状態）、
**ユーザーが**（Director 自身はもう動いていないので、Director には頼めない）別ターミナルから
`./crewvia` を実行する。これが新しい Director セッション（hc backend 下）を起動し、その起動シーケンスの
中で `start.sh` が dispatcher/watchdog も spawn する（3.4）。つまり**誰が**＝ユーザー、**どの順**＝
3.2（全停止、Director 含む）→ 3.3（config）→ 3.4（`./crewvia` 1 回）、**どのコマンド**＝
`scripts/crewvia-stop.sh --force` と `./crewvia` の 2 つだけ。新しい Director は前の Director の
会話コンテキストを引き継がない（別プロセス・別セッション）——引き継ぎたい情報（進行中だった判断・
ユーザーとのやり取り）があれば、3.2 を実行する**前**に Director 自身が knowledge/ や task card に
書き残しておく必要がある（本書の手順そのものには含まれない、切り替えを依頼する側の準備事項）。

### 3.1 なぜ「config だけ変えて `lib_daemon_watch.py restart`」では足りないか（読んだ限り。重要）

`restart()`（`scripts/lib_daemon_watch.py:1739-1824`）は `mux = mux if mux is not None else Mux()` で
**1 つの `Mux()` を作り、kill にも直後の spawn にも同じインスタンスを使う**（:1800-1816）。`Mux()` は
構築時に一度だけ `_select_backend()` で backend を解決するので、**kill と spawn は常に同じ backend**
——config を先に `hc` にしてから `restart dispatcher` を呼ぶと、kill は `HcBackend` で
`registry/mux/dispatcher.json`（`"backend":"herdr"`、`handle` が herdr の tab_id 形）を使って
`_inspect_pane()` を試み、hc 側にその id は存在しない（**実測**: `hc get w1:t1V --json` →
`{"error":{"kind":"not_found",...}}`、exit 3）ので `(None, None)` に解決され、`may_destroy_pane()` が
拒否し、`restart()` は `"the mux layer did not destroy that pane, so nothing was started"` と
ログして **spawn せずに `False` を返す**（:1810-1813）。**危険側には倒れない**（古い herdr の
dispatcher/watchdog プロセスはそのまま動き続ける）が、**何も切り替わらない**——これが §5 の「失敗の
見分け方」の主要パターンの 1 つ。

逆順（kill と spawn を橋渡しする）も `restart()` の中では不可能: `spawn_daemon()` は `restart()` が
渡した**同じ** `mux` を使う（:1816）。CLI にも「kill だけ別 backend」を指定するオプションは無い
（`lib_daemon_watch.py` の `restart` サブコマンドは `name` / `--repo-root` / `--force` だけ、:1878-1881）。

CLAUDE.md 不変条件4「デーモンの再起動は `lib_daemon_watch.py restart`。`lib_mux.py kill` / `spawn` を
素で叩かない」は**この手続きをそのまま使って backend をまたぐ**ことを禁じているわけではなく、
「動いている dispatcher/watchdog を respawn するときに、相互監視のペアが取り合いで二重起動しないよう
pause マーカーで直列化する経路を通れ」という意味——読んだ限り、この直列化は `pause`/`resume` の
**別の** CLI サブコマンド（:1871-1877）としても露出しており、`restart()` 自身もこの 2 つ + kill + spawn
の組み合わせでしかない。一方 `scripts/crewvia-stop.sh` は**全停止**（dispatcher も watchdog も Director も
同時に落とす）の正規の入口で、その内部実装（`kill_tab()`、:110-118）は `lib_mux.py kill` を直接呼ぶ
——これは「全部を一度に止めるので、respawn を取り合うペアがそもそも残っていない」という別の安全性の
下で許されている（invariant 4 が守ろうとしている「片方だけ生きている間に起きる競合」が、全停止では
最初から起こり得ない）。**だから backend をまたぐ切り替えは、`restart()` の再利用ではなく
「`crewvia-stop.sh` で全停止 → config を変える → `./crewvia` で新規起動」という、全停止・新規起動の
形にする**（`lib_daemon_watch.py spawn` は `./crewvia` → `start.sh` が二重起動防止込みで内部的に呼ぶので
手で叩く必要もない、§3.4）。

### 3.2 Phase 1 — herdr のまま全停止

**別ターミナルから**実行する（Director 自身の pane 内から実行すると、`HERDR_TAB_ID` を検知して
自動的に `--keep-director` が付き、Director tab が残ってしまう——`scripts/crewvia-stop.sh:56-65`。
今回は Director tab も含めて終わらせる意図なので、別ターミナルを使う）。

```bash
cd /home/tkadmin/workspace/crewvia
scripts/crewvia-stop.sh --force
```

`--full` は**付けない**（herdr server 自体は止めない——herdr-task-graph plugin の画面を見続けたい場合や、
hc への切り替えを後で herdr に戻す可能性のために、server は生かしたまま pane だけ畳む。`--full` は
「herdr 専用」で、hc を使っている間に誤って herdr server を止める事故を防ぐ分岐は既に入っている
——§0 のとおり）。

実行順は `crewvia-stop.sh` のコメントどおり: ① `*-worker` ② `dispatcher`/`watchdog` ③ `*-director`。
各 kill は成功すると `registry/mux/<name>.json` を消す（`_forget_record()`。`scripts/lib_mux.py` の
`TmuxBackend.kill()`/`HerdrBackend.kill()` が成功時に呼ぶ、共通の設計——設計書 §2 の
「`write_pane_record()`」のドキュメントが言う「古い記録は次の spawn が置き換える」と同じ仕組みが
ここでは「置き換えずに消える」形で効く）。

**確認（実行後）**:
```bash
ls registry/mux/*.json 2>/dev/null
# .state.json / .job-since.json 以外、pane record（handle/backend を持つ形）が 0 件のはず
grep -l '"backend"' registry/mux/*.json 2>/dev/null   # 何も出なければ OK
```
もし `"backend":"herdr"` の record がまだ残っていれば、その名前の pane が kill に失敗している
（`crewvia-stop.sh` の出力に理由が出る——大抵は「foreign だと判定された」か「herdr に到達できなかった」。
決着のつけ方は 2 つ: ① `scripts/crewvia-stop.sh --force` をもう一度実行する（上で既に打ったのと同じ
コマンド——べき等で、まだ残っている名前だけが対象になる）。② 個別に
`python3 scripts/lib_mux.py kill <name> --force` を打つ（`--force` は `lib_mux.py` 自身のオプションで、
`m.kill(..., allow_foreign=True)` になる——`scripts/lib_mux.py:6433`/`:6438`。実測
（`CREWVIA_MUX_TEST_ISOLATION=1 python3 scripts/lib_mux.py kill`）: `Usage: lib_mux.py kill <name> [--force]`
（`scripts/lib_mux.py:6436`）、exit 2）。`crewvia-stop.sh` が内部で呼ぶ `kill_tab()`（`scripts/crewvia-stop.sh:116`）
は `--force` を**付けない**ので、①と②は非対称——**foreign と判定されて残った pane は①を何度打っても
決着しない（②を使うこと）。①が効くのは「一時的に herdr に到達できなかった」側だけ**。
**`lib_daemon_watch.py restart` はここでは使わない**——理由は 2 つ。(a) `restart` の CLI は
`name` の `choices=DAEMONS`（`dispatcher`/`watchdog` だけ、`:1846`）で、`*-director` / `*-worker` の
名前はそもそも受け付けられず弾かれる。(b) `dispatcher` / `watchdog` 自身に対してでも、この時点では
config がまだ `mode: herdr` のまま（3.3 はまだ先）なので、`restart` を打つと kill の直後に
**同じ `Mux()` インスタンス（= 同じ herdr backend、3.1 のとおり kill と spawn は同じインスタンスを
使う）で spawn し直してしまう**（`:1816`、`spawn_daemon()` は `restart()` が渡した同じ `mux` を使う）。
消したかったはずの herdr の dispatcher/watchdog を自分の手で生き返らせることになり、これは
「全停止したつもりが実は何も消えていない」という、§5 が最も警戒する症状をこの手順自身が作る
——だからこの決着は常に① `crewvia-stop.sh --force` か② `lib_mux.py kill <name> --force` の
どちらかで行い、`restart` には進まない。

**もし herdr の record を残したまま 3.3〜3.4 に進んでしまったら（コードを読んで確認した挙動）**:
危険側には倒れない。`reap_stale_pane_records()`（`scripts/lib_mux.py:1862-`）——dispatcher が毎サイクル
呼ぶ sweep（`CREWVIA_MUX_RECORD_SWEEP=0` で無効化できるが、既定は有効）——は、各 record を
`record.get("backend") != backend.BACKEND_NAME` で**現在の backend と一致しない record を無条件に
スキップする**（`:1917`。読んだ限り、ここが唯一の分岐で、現在の backend が `hc` になった後は
`"backend":"herdr"` の record には一生触らない）。つまり:
- 残った herdr の record は**自動では消えない**（sweep が backend 不一致で素通りする）。
- 対応する herdr 側の pane（まだ生きていれば）も**自動では閉じられない**——hc backend の `kill()` は
  その名前を hc 側で解決しようとして `not_found` になり（実測: `hc get w1:t1V --json` →
  `kind:"not_found"`）、`_inspect_pane()` が `(None, None)` を返すので拒否される（3.1 と同じ理屈）。
  herdr 側の window は人間が herdr 側から直接閉じるしかない（`herdr` CLI、または herdr のタブを
  手で閉じる）。
- 実害は「registry/mux/ に使われなくなった herdr record が残り続ける」こと（ディスク上のゴミ。
  `Mux` の判断には影響しない——backend が一致しないので、あらゆる verb から見えない）。これを消して
  よいのは、対応する herdr の window を実際に閉じたことを確認した**後**だけ（`drop_pane_record()` は
  `scripts/lib_mux.py` の中で直接呼ぶ関数で、CLI からは露出していない——手で `rm registry/mux/<name>.json`
  してよい。record は「このチェックアウトがこの pane を作った」という主張でしかなく、対応する pane が
  本当に消えていることを先に目で確認していれば、消しても壊れるものは無い）。

### 3.3 Phase 2 — config を切り替える

前例（`config: 本番 mux を herdr に切替 (#142)`、commit `e4dfc66`）と同じ形: PR で
`config/crewvia.yaml` の `mode: herdr` を書き換える。

```diff
-mode: herdr  # 戻す時は tmux。切替日: 2026-09-04
+mode: hc  # 戻す時は herdr。切替日: <実施日>
```

PR を作り、review（このミッションの t004 相当の運用）→ merge → 本番 checkout を
`scripts/sync-main-checkout.sh` で同期する。

**ここが罠（読んだ限り。重要）**: `sync-main-checkout.sh` の自動 restart 判定（`restart-needed`）は
`DAEMON_RESTART_FILES`（`scripts/lib_daemon_watch.py:240-256`）に列挙された**ファイルの内容ダイジェスト**
だけを比較する。`config/crewvia.yaml` はこの一覧に**入っていない**——つまり config だけの変更は
`restart-needed` を `false` のままにし、`sync-main-checkout.sh` は dispatcher/watchdog を**自動では
restart しない**。加えて `_mux = Mux()`（`scripts/dispatcher.sh:277`、`scripts/watchdog.py:65`）は
**モジュールレベルで 1 回だけ**作られる——dispatcher/watchdog は長時間動き続ける 1 プロセスで、内部で
sleep しながらループしているだけなので、config ファイルを書き換えても、そのプロセスが生きている限り
`_mux` は古い backend（herdr）を握ったまま変わらない。**`sync-main-checkout.sh` の出力が
「restart 不要」と言っていても、それは「config 変更は検出対象外」という意味でしかない**——3.2 で
既に dispatcher/watchdog の pane 自体を終わらせてあるので実害は無いが、もし 3.2 を飛ばして
config だけ変えていたら、「切り替えたつもりで何も切り替わっていない」という §5 の失敗パターンに
そのまま当たる。

### 3.4 Phase 3 — hc で新規起動

```bash
cd /home/tkadmin/workspace/crewvia
./crewvia
```

`./crewvia`（`mode: hc` を読む、:114-128）→ `start.sh`（Director role。実効 backend 解決、:232-354）の
経路は、どちらも hc 到達性チェック済みの上で `CREWVIA_MUX=hc` を export して Director を起動する。
Director 起動後、`start.sh` が「Director: dispatcher / watchdog を mux 窓で起動（二重起動防止）」
（`scripts/start.sh:1073`）の節で `lib_daemon_watch.py spawn dispatcher` / `spawn watchdog`
（:1100, 1109）を呼ぶ——**手で dispatcher/watchdog を別途 spawn する必要は無い**。どちらも
`lib_daemon_watch.py` 経由（invariant 4 どおり）。

起動順はコード側で固定されている（Director が先に起動し、その中で dispatcher → watchdog の順に
spawn する）。待つべき操作は無く、`./crewvia` の 1 コマンドで完結する。

## 4. 切り替え後の確認（本書を実際に使う時に行うこと）

1. **config の値ではなく、実際に書かれた record を見る**（3.3 の罠の裏取り）。`dispatcher.json` /
   `watchdog.json` / `*-director.json` の 3 つだけでは `*-worker.json` を見落とす——**`registry/mux/*.json`
   全件**を対象に、pane record の形（`backend` キーを持つもの。`*.state.json` / `*.job-since.json` は
   持たないので自然に除外される）だけを拾い、**読めなかった record は黙って捨てず明示する**
   （CLAUDE.md 不変条件 1 と同じ理由——`2>/dev/null` で揃って 0 件になる画と「herdr は 0 件」の画を
   区別できないと、後者を前者と誤読しうる）:
   ```bash
   total=0; unreadable=0
   for f in registry/mux/*.json; do
     out=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(sys.argv[1],'->',d.get('backend'),d.get('handle')) if 'backend' in d else None" "$f" 2>&1)
     rc=$?
     if [ $rc -ne 0 ]; then
       unreadable=$((unreadable+1))
       echo "UNREADABLE: $f" >&2
       echo "$out" >&2
     elif [ -n "$out" ]; then
       total=$((total+1))
       echo "$out"
     fi
   done
   echo "pane records: $total, unreadable: $unreadable"
   ```
   全部 `hc` と出て `unreadable: 0` であること。`herdr` の record が混じっていれば 3.1 の罠（Phase 1 を
   飛ばした・Phase 2 だけで止めた）を疑う。`unreadable` が 0 でなければ、その record 自体が壊れている
   か読めない——件数が合っているからといって「herdr record が無い」と即断しない。
   （python を 1 行に収めているのは意図的——この手順は list の継続インデント付きの fenced code block の
   中にあるため、複数行の python ソースを埋め込むと、インデントを保ったまま生テキストをコピーした場合に
   `IndentationError` で壊れる。実測済み: 複数行版を書いて試したところ実際にこの壊れ方をした。1 行に
   畳めば list のインデントは shell の行頭の空白として無害化される）
2. `hc ls --all --json` で `dispatcher` / `watchdog` / `<director 名>-director` / 必要なら
   `<worker 名>-worker` の `name`（小文字化形。設計書 §3.3）が `state:"live"` で見えること。
3. Worker を 1 人 `start.sh` で起動し、`CREWVIA_MUX=hc python3 scripts/lib_mux.py list -worker` に
   その名前が出ること（設計書 §3.3-3 の `all:false` 実装どおり、生きている worker だけが出る）。
4. `registry/daemons/` の heartbeat（`lib_daemon_watch.py status`）が新しい世代で更新されていること。

## 5. 失敗の見分け方

| 症状 | 原因（読んだ限り / 実測） | 見分け方 |
|---|---|---|
| `./crewvia` / `start.sh` が `CREWVIA_MUX=hc が設定されていますが hc バイナリが見つかりません` で止まる | hc 未インストール、または PATH に無い（crewvia 自身の `scripts/bin` 経由では hc を解決しない——hc は別途 `~/.local/bin` 等に入っている前提） | 起動が**最初から始まらない**（claude は exec されない）。危険側の「起動したように見えて死んでいる」には当たらない——エラーで止まるだけ |
| `./crewvia` / `start.sh` が `CREWVIA_MUX=hc が設定されていますが hc server を使えません` で止まる | `available()` が False——`hc server start --json` 自体が失敗、または `hello.generation` が `_HC_GENERATIONS={1}` に無い（設計書 §4.2。版が上がって generation が変わった場合、ここで検出される） | 同上。起動前に止まる。`hc server status --json` / `printf '...hello...' \| hc connect` を手で叩いて `generation` を確認する |
| **config は `mode: hc` なのに、dispatcher/watchdog が実際には herdr のまま動き続ける** | 3.3 の罠。Phase 1（全停止）を飛ばして config だけ変えた、または `sync-main-checkout.sh` の自動 restart に任せて何も起きなかった | `registry/mux/dispatcher.json` / `watchdog.json` の `"backend"` を実際に読む（4節の手順1）。config の値だけを見て「切り替わった」と判断しない——**これが唯一、"起動したように見えて実は(新backendでは)死んでいる"のではなく"何も変わっていないのに変わったように見える"パターン**、本書が一番警戒すべき症状 |
| `restart dispatcher` / `restart watchdog` が `the mux layer did not destroy that pane, so nothing was started` とログして `False` を返す | 3.1 のとおり、config を先に hc にしてから `restart` を呼んだ（kill が新 backend で古い pane を解決できない） | ログの文言そのもの。古い herdr の dispatcher/watchdog はそのまま生き続けている（危険側ではない）。`restart` を使わず 3.2〜3.4 の手順（全停止 → config → 新規起動）をやり直す |
| 新しく spawn した Worker/Director が、起動直後（claude がまだ起動完了していない段階）で次の task を assign される | `reported:false` を `idle` と誤読する regression（設計書 §7.3 で手当て済みのはずの箇所） | `hc get <name> --json` の `reported` が `false` の間に `status.state` を読んでいないか——`state()` の実装（`HcBackend.state()`）が `reported==false` を `"unknown"` に倒しているかを確認する。見えたら退行として扱う（コードの修正が要る。本書の手順の問題ではない） |
| `hc ls --all --json` に `state:"finished"` の dispatcher/watchdog/worker/director が残り続ける | hc は `hc rm` するまで terminal を残す（設計書 §3.0。`kill()` は `hc rm` を呼ばない、§12-8）。**これは失敗ではない** | `hc ls`（`--all` 無し）なら出ない（live だけ）。片付けたいだけなら `hc rm <address>` を任意で実行してよい（掃除であって、切り替えの成否には関係しない） |
| hc 側の `available()` は True なのに `hc connect` 経由の `list()` が空を返し続ける | `hc connect` の JSON-RPC 往復が通っていない（名前の小文字化・`--label crewvia_name=` の不一致、設計書 §3.3） | `hc ls --all --json` を手で叩いて `labels.crewvia_name` が期待どおりかを直接確認する |

## 6. 戻す手順（hc → herdr）

3 節と対称の「全停止 → config → 新規起動」。密度を落とさず同じ粒度で書く。

### 6.1 Phase 1' — hc のまま全停止

```bash
cd /home/tkadmin/workspace/crewvia
scripts/crewvia-stop.sh --force
```
この時点で `_select_backend()` は `hc`（config がまだ `mode: hc` なので）を返すため、
`scripts/lib_mux.py list`（`crewvia-stop.sh:95`）は hc の live terminal を列挙し、`kill_tab()` が
`lib_mux.py kill <name>` で順に落とす——3.2 と同じ理由で、Director 自身の pane も含めて落ちる
（別ターミナルから実行する前提も同じ）。

`--full` は付けない（`hc server` はユーザーのデスクトップ全体の server で、crewvia 以外の terminal も
持つ——rex と同じ理由。設計書 §4.2 冒頭、`crewvia-stop.sh:177-181` の分岐どおり止めない）。

**確認**:
```bash
hc ls --all --json   # state:"live" の dispatcher/watchdog/*-director/*-worker が 0 件
ls registry/mux/*.json 2>/dev/null
grep -l '"backend": "hc"' registry/mux/*.json 2>/dev/null   # 何も出なければ record 側もクリーン
```

### 6.2 Phase 2' — config を herdr に戻す

```diff
-mode: hc  # 戻す時は herdr。切替日: <実施日>
+mode: herdr  # 戻す時は tmux。切替日: 2026-09-04
```
PR → review → merge → `sync-main-checkout.sh`。**3.3 と同じ罠がそのまま戻り方向にも当てはまる**——
`config/crewvia.yaml` は `DAEMON_RESTART_FILES` に無いので自動 restart は起きない。6.1 で dispatcher/
watchdog は既に全停止済みなので実害は無いが、6.1 を飛ばして config だけ戻すと、hc のまま dispatcher/
watchdog が動き続ける（§5 の表の該当行と対称の事故）。

### 6.3 Phase 3' — herdr で新規起動

```bash
cd /home/tkadmin/workspace/crewvia
./crewvia
```
herdr server が本書の間ずっと止めていないので（3.2・6.1 ともに `--full` を付けていない）、`./crewvia`
の herdr 到達チェック（`crewvia:141-172`）はほぼ即座に通る見込み（読んだ限り。herdr server 自体を
本当に落とした状態からの起動は未検証——落とす操作を本書の作成中に行っていないため）。

### 6.4 戻せないほど進んでしまう操作（名指し）

- **`hc server stop` / `hc server restart`**: 本書・設計書ともに「頼まれていないのに本番の hc server を
  止めるな」が前提（CLAUDE.md 不変条件 8 と同種の重さ）。万一止めてしまった場合、holder が入れ替わり
  `address` の採番がどうなるかは**未確認**（設計書 §11 の未確認事項そのまま）——戻せるかどうかも
  分からない操作なので、本書の手順には一度も含めていない。
- **`registry/mux/*.json` を手で編集 / 削除して `"backend"` を偽る**: record は kill の認可の唯一の証拠
  （`scripts/CLAUDE.md` 「mux」の項）。空の `--force` 無しで偽の record を作ると、その名前への次の
  `kill()` が「記録と一致しない」で fail-closed に拒否する側に倒れるだけ（危険側ではない）が、
  整合性を手作業で壊すこと自体を本書は推奨しない——3.2/6.1 の「全停止」だけが record を正しく畳む
  唯一の経路。
- **`herdr server stop` を hc 稼働中に実行**: herdr-task-graph plugin（CLAUDE.md の節）は herdr server の
  上で動くので、hc に切り替えた後も herdr server を落とすと plugin の画面が使えなくなる。tasks.json の
  生成自体は `CREWVIA_TASK_GRAPH`（mux backend と無関係。`plan.sh` が queue 書き換えのたびに生成する
  だけ）には影響しないが、**見る手段**（plugin の pane）が herdr server に依存するので、hc 移行後も
  herdr server は生かしておく方針を本書では一貫して採った（3.2・6.1 の `--full` 省略はこのため）。

## 7. タスクグラフ (herdr-task-graph) への影響

`registry/task-graph/tasks.json` の生成（`plan.sh` が queue 書き換えのたびに行う）は mux backend と
無関係——`CREWVIA_TASK_GRAPH` の ON/OFF だけで決まる（CLAUDE.md）。**切り替えに伴って触る必要は無い**。
plugin 自体（`herdr plugin action invoke open-task-graph ...`）は herdr server の上で動く別物で、
hc への切り替え後も「herdr server が生きていれば」そのまま使える（6.4 のとおり、本書は herdr server を
一度も落とさない設計にしている）。plugin の config dir への symlink（`knowledge/task-graph.md` §1）も
`registry/task-graph/tasks.json` という同じパスを指し続けるので、張り直す必要は無い。

## 8. 実行結果（2026-11-09）

- **実行日**: 2026-11-09。herdr → hc への本番切り替えを実施した（mission
  `20261109-hc-production-switch`）。
- **切り替え後に backend を確認する正しい手順**（4節の手順1を改変して使った——当時の4節は
  `dispatcher.json`/`watchdog.json`/`*-director.json` の3件だけを見る形で、`*-worker.json` を
  含む全件確認ではなかった。この差分は後に4節側へ反映した）:
  `config/crewvia.yaml` の `mode:` の値では判断しない——**`registry/mux/*.json` の
  `"backend"` を実際に読む**。本書作成時点と同じ複数行の python で、pane record（`handle`/
  `backend`/`server` を持つ形）全件を確認する:
  ```bash
  for f in registry/mux/*.json; do
    python3 -c "
  import json,sys
  d=json.load(open(sys.argv[1]))
  if 'backend' in d: print(sys.argv[1], '->', d.get('backend'), d.get('handle'))
  " "$f" 2>/dev/null
  done
  ```
  実測（本 task 実行時点）: `dispatcher.json` / `watchdog.json` / `Sora-director.json` /
  各 `*-worker.json`（例 `Mateo-worker.json`、`Hana-worker.json`）、すべて `"backend": "hc"`。
  `"backend": "herdr"` の record は 0 件——3.2 の全停止が正しく完了し、§3.2 末尾が警告する
  「herdr record が残り続ける」症状は今回は起きていない。
- **手順書が想定していた挙動との差分**:
  - **mux backend の切り替え自体（3〜4節の手順）は差分なし**。4節の確認はそのまま通った。
  - **ただし 3.3「config を切り替える」の実務（PR → review → merge → 本番 checkout 同期）が
    手順どおりに完結していなかった**: 切り替え後、`config/crewvia.yaml` の `mode: hc` と
    `registry/workers.yaml` の通常更新が、本番 checkout の working tree に**未コミットのまま**
    残っていた（本 task=t003 の発端）。本書の 3.3 は「PR を作り…」とだけ書いており、
    「コミットし忘れると config は hc のままでも次に `git checkout` / `reset` が走ったときに
    herdr へ巻き戻るリスクがある」という後片付けの注意までは書いていなかった——運用上の
    ギャップとして本節に記録する（本書の手順自体を書き直す必要があるほどではない。やること
    リストの最後に「commit → push → PR → merge」まで明示的に含めるべき、という教訓）。
  - **`.claude/settings.json` の失効 worktree permission 行**: t003 の発端になった Director の
    一次確認では「既に消えた worktree への permission 行」とされていたが、実行時点
    （2026-11-09）で該当する全ディレクトリ（`.claude/worktrees/20261102-pr-task-line/t002-t001`
    ほか計 5 件）を `git worktree list` と `[ -d ...]` で 1 件ずつ確認したところ、**全て実在して
    いた**（他 task が継続使用中）。そのため本 task では該当行を削除していない——
    「消す前に実在しないことを確認する」が実際に差分を防いだ例として記録する。stale 行の
    削除は、該当 worktree が実際に `git worktree remove` された後の別 task に委ねる。
