# herdr-core (`hc`) を mux バックエンドにする設計（`scripts/lib_mux.py` の `HcBackend`、仮称）

**実装はしない。これは設計メモの草案**（mission `20261106-herdr-core-backend` t001）。t002 で `HcBackend` を実装する前提の決めごとをまとめる。

- 対象の版: `hc 0.1.18`（`/home/tkadmin/.local/bin/hc`）。upstream `herdrdev/herdr-core`（公開）。OpenRPC schema `info.version` も `0.1.18`、**protocol generation 1**（`hc schema` の `info.description`）
- 書いた人: Worker Minjun（mission `20261106-herdr-core-backend` t001）。Director の事前メモ（card 本文）の実測を自分でも本機（本番の hc server、desktop-l8nqlib、holder pid 1246708 時点）で再現し、JSON Schema（`hc schema`）とライブコマンドの両方で裏を取った
- **改訂（attempt 2）**: Worker Mateo。PR #344 の review task（t006）が P1 1件・P2 2件・P3 2件を指摘して差し戻した（`queue/missions/20261106-herdr-core-backend/history/t006/0001-end-ex-90c5aef661cb4762a10585a1e01aa51d.md`）。全指摘を自分で実機再現してから反映した——P1: 名前の写像（新設 §3.2）。P2: エラー JSON は stderr（§5 冒頭に明記）・`kind` 一覧に `needs_update` が抜けていた（§5 の表を25件に修正）。P3: `hc schema` の schemas 数は34ではなく35（実測で訂正）・`openrpc` のバージョン文字列の意味を訂正・`state`（terminal 生死）と`status.state`（agent status）が同名の別概念である注記を追加（§3 冒頭・§7.1）。「裏が取れた主張」（review 本文に列挙）は取り直していない
- 凡例: 「**実測**」= 自分がこの作業中に確かめた出力。「**Director 実測**」= card に書かれていて自分では別の形で確かめた（同じプロセスは再現していない）事実。「**未確認**」= 確かめていない。「見込み」は推測
- 比較対象は既存の `RexBackend`（`scripts/lib_mux.py:4884-5675`、`knowledge/rex-mux-backend.md`）と `HerdrBackend`（`scripts/lib_mux.py:3352-4883`）。**この文書は両方の既存 backend の挙動を変える提案を 1 点含む（§4）。tmux / herdr / rex は本番で使われているので、その影響範囲を §4.4 に明記した**

## 1. 結論（先に）

1. `hc` で `_Backend` の全メソッドを満たせる。**herdr/tmux よりむしろ楽になる点が 2 つ**: ① `state()` が screen-scraping でなく構造化された OSC 7501 の値を読める（§7）② エラーが `{"error":{"kind":...}}` の構造化形で、exit code も安定している（`hc --skill` に表がある。§5）
2. **満たせない・弱くなる点は無いが、「observed できない」を広めに取る必要がある箇所が 1 つ**: `reported: false`（プログラムが一度も 7501 を報告していない）は `state()` を `"unknown"` にする。`idle` に倒さない（§7.3）。herdr の `state()` は常に本物の agent_status を返す前提だったが、hc は「報告しないプログラムが居る」ことを型で表現している分、この区別を設計に組み込みやすい
3. **版の固定は Rex と同じ fail-closed にする。既存 `HerdrBackend` の「警告だけで続行」方式はここでは採らない**（§4。Director 指摘の対比を根拠にした判断）
4. PATH は `lib_daemon_watch.spawn_command()` の既存の作法（LAUNCH_CMD に `export PATH=...` を埋める）をそのまま使う。hc 固有の対処は不要（§6）
5. `verify-sent` は screen-scraping を使わず `SendResult.started` / `status` を読む——ただし `reported: false` の対象には answered できないので、既存の `_text_in_input_line` 相当へのフォールバックを残す（§8）
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
```

`/proc/<hc server pid>/environ` を読んで、hc server が実際にどの PATH で子プロセスを起動するかを確認した（§6）。`hc server start --json` を 1 回呼び、既に生きている server に対して**冪等**（pid・uptime が変わらない。再起動しない）であることも確認した——`ensure-server` の候補として安全に呼べる。

## 3. `_Backend` の全メソッドの対応表

hc は JSON-RPC 2.0 の methods（`hc schema` の `methods[]`）を CLI がラップしたもの。32 methods のうち使うのは `terminal.*` と `server.status` だけで、`machine.*` / `file.*` / `doc.*` / `events.subscribe` は対象外（§10 で扱う）。

**注記（同名の別概念、P3 対応）**: `hc get` の出力にはトップレベルの `state`（`live`/`finished`。terminal プロセスそのものの生死）と、ネストした `status.state`（`idle`/`working`/`done`/`blocked`/`error`。agent status、§7 の対象）という**同名だが別物の 2 つの欄がある**。下表の `kill` 行・§5 の kill 前チェックは前者、§3 の `state(name)` 行・§7 は後者を指す。実装時に変数名で区別すること（実測: `hc get` の生出力で両方の欄を確認。§2 の後続コマンドで裏取り済み）。

| `_Backend` のメソッド | hc での満たし方 | 根拠 | 満たせるか |
|---|---|---|---|
| `available()` | `shutil.which("hc")` ∧ `hc server start --json`（冪等。§2）が成功 ∧ `hello`/`server.status` の版が許容範囲（§4） | 実測 | 満たせる |
| `server_running()` | `hc server status --json` が exit 0 で返る（起動はしない） | 実測 | 満たせる |
| `ensure-server`（`available()` の一部） | `hc server start --json`。既に生きている server には**冪等**（pid・uptime 不変。実測） | 実測 | 満たせる |
| `server_identity()` | `(core.id, holder.instance)`。`core.id` は desktop の識別子（再起動をまたいで同じ見込み・未確認）、`holder.instance` は **holder（実行プロセス）が変わるたびに別の UUID**（`server.status` の `holder.instance`。アップデート後は旧 holder が `holders[]` に残り、新しい terminal は新しい holder に行く——RexBackend の `instance_id` と同じ役割） | 実測（`server.status` の形。再起動そのものは未確認 — §11） | 満たせる（§4.3） |
| `spawn(name, cmd, cwd)` | `hc run --bg --name <hc名> --label crewvia_name=<name> --cwd <cwd> --json <argv...>` → `RunResult.address` を記録。`<hc名>` は crewvia の `name` を小文字化したもの（**§3.2、P1 対応**）。**`argv` は配列**なので、herdr/tmux のような「1 本の shell 文字列を pane に type する」のではなく argv をそのまま渡せる。ただし crewvia の LAUNCH_CMD は `export ...; cd ... && claude ...` という**shell 文字列**なので、`bash -c '<LAUNCH_CMD>'` 経由にする（§6 で PATH を埋めた後の文字列） | 実測（`terminal.run` の params・§3.2 の名前変換） | 満たせる |
| `send(name, text)` | `hc send <t> <text> --json`（3 秒の入力休止待ちの後に paste + Enter。`--force` は blocked 相手への強制、crewvia は使わない） | 実測・`hc send --help` | 満たせる |
| `keys(name, keys)` | `hc keys <t> <keys...> --json`。名前は `enter` / `esc` / `tab` / `up` / `down` / `ctrl+c` / `alt+x` / `f5` など——`translate_keys()` の hc 列を足す。choice dialog のカーソル移動に使う想定は herdr/tmux と同じ | 実測・`hc keys --help` | 満たせる |
| `capture(name)` | `hc read <t> --lines N --json`。画面 + history。**作業中/blocked/error の間は history 読み出しでスクロールしない**（画面だけ・`why` に理由) という制約が `hc --skill` にある | `hc --skill`（未実行で検証、文面のみ） | 満たせる |
| `list(suffix)` | `hc ls --json`（live のみ）/ `--all`（finished 含む）。hc の `name` でなく **`labels.crewvia_name` を読み、それを crewvia の `name` として返す**（§3.2）。`suffix` 絞り込みは `crewvia_name` に対して行う（hc 側は `--label` でも絞れるが、crewvia 名は label 値に入っているため `--label` の値一致では使えず、`ls --all --json` を全件取ってから crewvia 側で `crewvia_name` を見てフィルタする） | 実測（§3.2） | 満たせる |
| `kill(name, allow_foreign)` | `name`（crewvia 名）を §3.2 で小文字化した `<t>` に変換してから `hc kill <t> --json`。**`may_destroy_pane` を先に通す**のは herdr/tmux と同じ（§4.3 の identity で束縛）。**実測: 既に finished な terminal への kill はエラーにならず Terminal を返す**（冪等。herdr/tmux の「無いものを kill した」との扱いの違いに注意——hc は「もう死んでいる」を失敗にしない） | 実測 | 満たせる |
| `pid(name)` | `name` を §3.2 で変換した `<t>` で `hc get <t> --json` の `pid`（Director 実測値 1257222 を確認済みの前提どおり、`hc get` は `pid` を持つ。実測では `exit_code:0` の `hc --version` 実行でも `pid: 1283891` が入っていた） | 実測 | 満たせる |
| `attach(name)` | 常に `False`（tmux の「$TMUX が無いときは attach_cmd を使う」側と同じ扱い。hc には「中に居る」状態からの `switch-client` に当たる概念が無い見込み） | 見込み（未確認） | 満たせる（見込み） |
| `attach_cmd(name)` | `["hc", "attach", name]`（Rex と違い hc には本物の CLI client がある。`hc attach [t]` で ctrl+\ 離脱）| `hc attach --help` | 満たせる（exec の実地検証は未確認） |
| `state(name)` | `hc get <t> --json` の `status.state`（`idle`/`working`/`done`/`blocked`/`error`）を Rule 5 の語彙に写す。`reported: false` は `"unknown"` に倒す（§7） | 実測 + Director 実測 | 満たせる |
| `record_existence` / reap-records | `hc get <t> --json`（`<t>` は §3.2 で変換した hc 名）の `error.kind == "not_found"` だけ `PANE_GONE`。他の kind・例外・非 JSON は `PANE_UNOBSERVED`（§5） | 実測（`kill`/`get` の `not_found` 文言と `hint: "see \`hc ls --all\`"` を確認） | 満たせる |
| `may_destroy` / identity-ok | `server_identity()`（`core.id` + `holder.instance`）に加えて、**`hc get` が返す `labels.crewvia_name` が `registry/mux/<name>.json` の記録名（crewvia 名、原綴り）と一致することを確認**してから destroy を許可する（§3.2）。`core.id`/`holder.instance` は `pane_record_status()` の `_server_binding_problem` にそのまま載せ、herdr/Rex と同じ 2 軸（endpoint, generation）の型にはまる | 実測（`server.status` の形・`labels` の往復を確認。実際の record 書き込みは未実装） | 満たせる |
| `verify_sent(name, text)` | `SendResult.started`（bool\|null）と `status`（送信時点の word）を読む。`reported:false` の相手（`started` が常に `null`）は既存の `_text_in_input_line` 風のフォールバックへ（§8） | 実測（`SendResult` スキーマ）。実機の claude-code 相手での確認は Director 実測を根拠にし自分では再現していない | 満たせる（フォールバック込み） |

### 3.1 `_Backend` インタフェースに対応が無い動詞

- タスク本文が明記した 15 動詞（available / server-running / ensure-server / spawn / send / keys / capture / list / kill / pid / attach / attach-cmd / state / verify-sent / reap-records / identity-ok）は**全部対応がある**（上表）。「対応するものが無い」はゼロ件——hc が RexBackend より API が広い（`terminal.get` が構造化 JSON を返すため、herdr の「pane_list を舐めて label で探す」のような間接参照が要らない）ためで、無理にこじつけた対応は無い
- 逆に hc にあって `_Backend` に無いもの: `hc put`（ファイル転送）・`hc interrupt`（stop key。kill とは別）・`hc wait --children`（複数 terminal の待ち合わせ）・`doc.get/set`（共有ドキュメント）。これらは crewvia の既存動詞に出番が無いので**対象外**（§10）
- **ただし §3.2 の前提条件が 1 つ抜けていた（attempt 1 時点。review t006 の P1）**: 「対応がある」は hc の `--name` が crewvia のペイン名（`Seo-worker` / `Sora-director` のような大文字始まり）をそのまま受け付けることを**暗黙の前提**にしていたが、この前提が成り立たない（§3.2）。動詞の対応自体は変わらないが、`spawn`/`list`/`kill`/`pid`/identity-ok の実装は名前の変換を経由する必要がある——この節を書いた時点では見落としていた

### 3.2 名前の写像（P1 対応。review t006 で指摘・自分で再現して確定）

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
2. 実測で裏取り: `hc run --bg --name cvprobet001-worker --label crewvia_name=Cvprobet001-worker --json ...` は成功（`state:"live"`）。続く `hc get` / `hc ls --all --label crewvia_name=...` の両方で `labels.crewvia_name` が原綴りのまま往復することを確認した
3. `list(suffix)` は `hc ls --all --json` の全件を取り、各要素の `labels.crewvia_name` を読んで crewvia の `name` として返す（hc 自身の `name` 欄は内部実装の詳細として扱い、呼び出し元には見せない）。`suffix`（`"-worker"` 等）での絞り込みは `crewvia_name` に対して行う
4. `kill` / `pid` / `record_existence` / identity-ok など、crewvia 側が `name` を渡してくる動詞は全部「§3.2-1 と同じ小文字化を通してから hc を呼ぶ」。逆に hc からの戻り（`list` や `get` の結果）を crewvia 側に見せるときは必ず `labels.crewvia_name` 経由で原綴りに戻す
5. `registry/mux/<name>.json` のファイル名・記録 id は**従来どおり crewvia の原綴り**（`Seo-worker.json`）のまま変えない。`may_destroy_pane` の照合（CLAUDE.md 不変条件外だが `scripts/CLAUDE.md` の mux 契約）は、記録の id と `hc get` が返す `labels.crewvia_name` を比較する——hc 自身の小文字 `name` 欄とは比較しない（大文字小文字変換は非可逆な情報ではないが、変換ロジックを 2 箇所に書くと食い違いの元になるため、原綴りを持ち回る label を正とする）

**この方式を採らなかった代替案**: crewvia 側の名前プール自体を小文字に変える案は、`registry/mux/*.json` の既存ファイル名・`config/worker-names.yaml`・ドキュメント中の表記（`Omar-worker` 等）を全部変更する必要があり、既存 backend（tmux/herdr/Rex）の挙動には影響しないが crewvia 全体の表記を変える大きい変更になるため**対象外**（CLAUDE.md 設計原則3「名前はポジション」の表記を崩さない）。

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
4. `_HC_GENERATIONS = frozenset({1})` のような定数にし、`available()` で `hello`（または `server.status` が generation を持たないので `hc --version` 経由でなく **JSON-RPC の `hello` を呼ぶ経路が要る**——CLI には `hello` 相当のサブコマンドが無いため、`hc schema` から `info.version` の major（`0.1.x` → major 0 は 1.0 未満なので毎回 minor も見る）を見るか、`hc connect`（API を stdin/stdout に出す）を使って `hello` を 1 回叩くかのどちらかになる。**これは決めること**（§10）——CLI 経由でどう generation を取るかが未確定なので、同じ frozenset パターンに落とし込めることは確認済みだが取得コマンドは次の task で決める

### 4.3 identity（世代）の対応

| | Rex | hc |
|---|---|---|
| endpoint 相当 | `rex server status` の `endpoint` | `server.status` の `core.id`（desktop インストールの識別子） |
| generation 相当 | `instance_id`（server 再起動ごとに変わる。実測） | `holder.instance`（holder＝実行プロセスが変わるごとに変わる。実測で `63f6d40f57ba43dd39e40066789098d2`） |
| 再起動後の ID 再利用 | block_id/window_id は UUIDv7 で保たれる（実測）が中身は login shell に化ける | **未確認**。`hc update`（`restarted: true`）は「terminals keep running」と `hc --skill` にあるので、tmux/herdr のような「server 死亡で全部消える」より Rex に近い見込みだが、`hc server stop/restart` を本番で試すのは避けた（CLAUDE.md・`hc --skill` 共に明記の禁止事項）ので**未検証** |

`holder.instance` は `server.status` の `holders[]` に複数残る（アップデート後、古い holder は自分の terminals を完走させる、と `StatusResult.installed_by`/`holders` の descriptionにある）。これは RexBackend の「サーバーが入れ替わった後の古い記録は `PANE_UNOBSERVED` 扱いにする」設計をそのまま持ち込める形——**ただし実際に holder が切り替わる瞬間（アップデート）は未確認**（§11）。

### 4.4 既存 backend への影響

**この設計は HerdrBackend・RexBackend のコードを 1 行も変えない。** hc は 3 つ目でなく 4 つ目の backend として独立したクラスで足すだけで、`_select_backend()`（§4.2 で読んだ `lib_mux.py:328-348`）の分岐に `hc` を 1 本足すだけなので、tmux / herdr / rex の既存の選択・挙動には影響しない。

**ただし Director の指摘は「HerdrBackend 自体の方針を変えるべきでは」という論点を含んでいる。これは t001 のスコープ外（対象外節の「本番の backend 切り替え」に準ずる既存 backend の変更）として、ここでは提案だけ残す**: HerdrBackend の `available()`（`lib_mux.py:3645`）を Rex と同じ fail-closed にする変更は、**本番で herdr を使っている環境すべてに影響する**——`_HERDR_VERIFIED_VERSION` 以外の版（今の本番がまさにそう、0.9.5）で `available()` が `False` になれば、`CREWVIA_MUX=herdr` / `mode: herdr` を選んでいる本番は mux が丸ごと使えなくなる（tmux へのフォールバックは `_select_backend()` に無い）。**やるなら別 PR・ユーザー承認必須**（CLAUDE.md `CREWVIA_MUX` の項と同じ重さの変更）。この文書はそれを決めない。

## 5. エラーの写像（`record_existence` / reap-records）

**読み口（P2 対応。review t006 で指摘・自分で再現して確定）**: `hc <cmd> --json` のエラー JSON は**stderr に出る。stdout は空**。実測:

```
$ hc kill cvprobet001nope --json 2>/dev/null   # stdout のみ → 空
$ hc kill cvprobet001nope --json 2>&1 1>/dev/null  # stderr のみ → {"error":{"hint":"see `hc ls --all`","kind":"not_found","message":"no terminal \"cvprobet001nope\""}}
```

`hc --skill` にも「Errors are `{error: {kind, message, hint}}` on stderr.」と明記されている。**stdout だけ読む実装にすると、エラー時の stdout は常に空なので全エラーが「JSON でない応答」＝`PANE_UNOBSERVED` に落ち、`record_existence` が `PANE_GONE` を一度も返せなくなる**（`registry/mux/<name>.json` が永久に畳まれない）。`HcBackend` の JSON パース共通ヘルパーは、**exit code が非 0 のときは stderr を、0 のときは stdout を読む**設計にすること。

`hc schema` の 32 methods の `errors[]` を全部書き出した（§2 の `hc-schema.json`）。`kind` の語彙（実測、`components` 配下の `data.kind` の `const` を機械的に抽出。**attempt 1 は `needs_update` を見落として 24 個と書いていたが、正しくは 25 個**）:

```
already_added, blocked, config, conflict, edit_failed, finished, hc_missing,
incompatible, internal, invalid_params, invalid_template, io, live,
machine_unknown, name_taken, needs_framed, needs_target, needs_update, not_found,
not_text, spawn_failed, status_mismatch, unavailable, unreachable, unsupported_generation
```

`needs_update` は設計上の結論には影響しない——下表の「それ以外すべて」の catch-all に含まれ、`PANE_UNOBSERVED` に落ちる（`not_found` 以外という条件に変わりはない）。

`terminal.get` / `terminal.kill` の `errors[]` は `not_found` と `machine_unknown` の 2 つだけ（実測、schema より）。

| kind | 観測事実 | `record_existence` の答え |
|---|---|---|
| `not_found`（`terminal.get`/`kill`/`send`/`keys`等） | **実測**: `hc kill tXYZDOESNOTEXIST --json` → `{"error":{"hint":"see \`hc ls --all\`","kind":"not_found","message":"no terminal \"tXYZDOESNOTEXIST\""}}`。exit code は `hc --skill` の一覧で **3** | **`PANE_GONE`**（CLAUDE.md の規則どおり「その id は無い」と明確に答えた唯一のケース） |
| `machine_unknown` | 未実測（`--on <machine>` を使っていないため）。意味は「リモート機の指定が存在しない」で、pane の有無とは無関係 | `PANE_UNOBSERVED`（この terminal について何も言っていない） |
| それ以外すべて（`finished` / `unavailable` / `blocked` / `status_mismatch` / `config` / `invalid_params` / `spawn_failed` / `name_taken` / `invalid_template` / `needs_target` / `needs_framed` / `needs_update` / `io` / `not_text` / `edit_failed` / `conflict` / `internal` / `unreachable` / `hc_missing` / `incompatible` / `already_added` / `live` / `unsupported_generation`） | `finished` は**実測**（§2 の send テスト。「存在するが終わっている」——これは `PANE_GONE` ではない。herdr/tmux に無い状態で、hc 独自の「死んではいるが record は消していい」にはまだ当たらない） | **`PANE_UNOBSERVED`**（CLAUDE.md の規則: `not_found` 以外は「観測できなかった」側に倒す。`finished` も同様——終わった terminal は `hc rm` で明示的に消すまで `ls --all` / `get` に残り続けるので、「消えた」と「終わった」は別の事実） |
| JSON でない応答・subprocess 失敗・timeout | — | `PANE_UNOBSERVED` |

**hc の `kill` は herdr/tmux と違う挙動がある（実測、§2）**: 既に `finished` な terminal に `kill` しても**エラーにならず Terminal オブジェクトを返す**（冪等）。これは `record_existence` の話ではなく `kill()` 本体の話だが、`may_destroy_pane` の判定後に hc の `kill` を呼ぶ設計では「`kill` が失敗しなかった」を「生きていたものを殺せた」の意味に使わないよう注意が要る——`kill()` の前に `hc get` で `state` を見て、`finished` ならそもそも kill を呼ばず `record_existence` 経由で記録を畳む経路にする。

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
- **自分の実測**: `sh -c 'sleep 90'`相当（`hc --version` の一瞬の実行）は `status: null` / `reported: false`。Director の「`sh -c 'sleep 90'` は `status: null` / `reported: false`」と型として一致（終了済みプロセスでの確認だが、報告しないプログラムの扱いという点で同じ現象）

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

**これは Director 実測（claude-code は reported:true）を自分では再現していないため、1 の経路は未検証**（§11）。2 の経路（screen-scraping）は既存コードの移植なので新規リスクは無い。

## 9. 実装規模の見積もり

- `RexBackend` は `lib_mux.py:4884-5675`（約 790 行）。`HerdrBackend` は `3352-4883`（約 1530 行）。`TmuxBackend` は `2204-3351`（約 1150 行）
- hc 版は Rex 規模に近いと見積もる。**増える要因**: JSON-RPC 的な構造化パース（`hc <cmd> --json` の出力を dataclass 的に正規化するヘルパーが要る——ただし herdr/Rex も同種のラッパー（`_herdr_run` / `_rex_run`）を既に持っているので新規パターンではない）。**減る要因**: `state()` と `verify_sent()` の主経路が screen-scraping を使わない分、Rex の `rex_screen_state()` / `_rex_input_box()` / `_rex_dialog` 系の正規表現群（`lib_mux.py:4779-4843` 相当、60行強）が要らない
- 版固定（§4）・identity（§4.3）・エラー写像（§5）・PATH 埋め込み（§6）は RexBackend で確立済みのパターンをなぞるだけで、設計判断としての新規性は低い（コード量としては通常の backend 1 つ分）
- **1 task（t002）に収まる見込み**。分割するとすれば「`HcBackend` 本体 + 隔離テスト」と「start.sh / dispatcher / watchdog の配線」の 2 PR（Rex の PR-1 / PR-2 と同じ割り方）だが、Rex はこれを 1 つの mission の 2 task で収めているので、t002 を同様に 2 分割するかは実装 task 側の判断に委ねる

## 10. 決めること（ユーザー判断 / 実装 task 側で詰める点）

**attempt 1 からの差分**: 名前の写像（hc の `--name` が crewvia の大文字始まりペイン名を受け付けない問題）は review t006 の P1 指摘を受けて §3.2 で決定済みにしたので、この節からは除外した（attempt 1 の時点でこの節にも §11 にも載っておらず、それ自体が review の指摘事項だった）。

1. **generation の取得経路**（§4.2-4.4）: CLI に `hello`相当のサブコマンドが無い。`hc connect`（stdin/stdout で API を直接叩く）を使うか、`hc schema` の `info.version` の文字列比較で代用するかは t002 で決める
2. **`error` state の Rule 5 対応**（§7.2）: A/B のどちらにも無い新しい値。Director への別枠通知にするか B に含めるか
3. **holder 切り替わり時の挙動**（§4.3）: `hc update` 後に「terminals keep running」が実際にどう見えるか（旧 holder の記録をどう `PANE_UNOBSERVED` に倒すか）は、本番の hc を実際にアップデートする機会がないと検証できない——未検証のまま「Rex と同じ設計で安全側に倒す」ことにして進めるか、検証を待つか
4. **`attach()` / `attach_cmd()` の実地検証**（§3 の「見込み」2件）: 実際に `hc attach` を exec してみる確認は t002 で行う

## 11. 未確認（実装前に確かめる一覧）

- `claude`（claude-code）を hc の terminal で実際に起動し、`reported: true` / `status.state` の遷移を自分で観測する（Director 実測の再現。コストと本機への影響を考え本 task では見送った——claude-code のネストしたセッションを本番 hc server 上に作ることになるため）
- `hc server` の再起動（`hc update` の `restarted: true` を含む）で terminal の ID・holder.instance がどう変わるか（§4.3・§10-3）。本番の hc server を止める操作はこの task の方針（CLAUDE.md・`hc --skill` 共通の「頼まれていないのに止めるな」）に反するため未実施
- `hc attach <t>` / `attach_cmd` の実地検証（§3・§10-4）
- `machine_unknown` kind の実測（`--on` を使うリモート機能は対象外のため）
- generation の取得経路（`hello` を CLI からどう叩くか。§10-1）
