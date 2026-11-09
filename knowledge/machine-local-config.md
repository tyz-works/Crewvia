# マシンごとの設定の上書きファイル (`config/crewvia.local.yaml`) — 設計

状態: **設計のみ (実装なし)**。mission `20261109-machine-local-config` t001。
未決事項は末尾「Director に決めてほしいこと」に選択肢 + 推奨で並べる。

## 1. 背景と目的

共通設定 `config/crewvia.yaml` はコミットされる。ところが `mode:`・`telegram.*`・`irc.*`・
`session_link`・`op_command`・モデル名などは**機ごとに値が違う**。今は各機が未コミットの手元変更で持っている。

- PR #351 が共通の `mode:` を `hc` にした (proserpina が hc に切り替わった記録。意図した merge)。
  WSL 機は rex で動いており、`mode: rex` の手元変更が `git pull` / `sync-main-checkout.sh` で衝突する。
- 手元変更が消える (`git checkout .`・stash の取り違え) と、**黙って別の mux で起動する**。
  mux の取り違えは窓が見えない・Worker が孤児になる事故に直結する (`registry/mux/` の記録は名前ごとに 1 つ)。
- `registry/workers.yaml` は実行時の台帳なのにコミットされ、機ごとにずれる (#351 でも衝突)。§7。

目的: マシン固有の値を **git 管理外の `config/crewvia.local.yaml`** に置き、共通設定に重ねて読む。
共通ファイルは「公開前提の既定」だけを持つ。

## 2. `crewvia.yaml` を読む全箇所 (読み方ごとの分類)

`timeout 120 grep -rn 'crewvia\.yaml' scripts hooks agents crewvia` で洗った。テストが config を
コピーする箇所 (`scripts/test_*.sh`) は §6 で扱う。

### A. Python: `yaml.safe_load` / `lib_task_cards.parse_yaml` で全体を読む (dict が手に入る)

| 読み口 | 取るキー | 読めないときの今の挙動 |
|---|---|---|
| `lib_telegram.py` (`_config_path` / `load_cfg`) | `telegram:` | 既定値 (= 未設定) |
| `lib_irc_relay.py` (`_config_path`) | `irc:` | 既定値 (= 無効) |
| `lib_escalation.py` (`load_cfg`、dispatcher.sh:3329 から) | `escalation:` | 既定値 |
| `lib_review_engine.py` (`load_full`) | `review:` | **止める** (`config_unreadable` / `config_malformed`、exit 1) |
| `lib_history.py` (`load_default_max_attempts`) | `attempts.max_total` | 既定 6 |
| `lib_daemon_watch.py` (`WatchConfig` ローダ) | `daemons:` | 既定値 |
| `lib_model.py resolve` | `worker_model` / `model_per_skill` | 空文字 (呼び側が worker_model → CLI 既定へ) |
| `plan.sh` (3045–3068) | `timeout_max_per_skill` ほか | 既定 |
| `plan.sh` の `load_default_max_attempts` 経由 (7255 / 7359) | `attempts.max_total` | 既定 6 (`lib_history.py` と同じ値を 2 経路で読む。移行は両方) |

### B. Python: 自前の行走査 (yaml を使わない)

| 読み口 | 取るキー | 注意 |
|---|---|---|
| `lib_mux._config_mode()` (:297) | `mode:` | 行頭 `mode:` を探すだけ。インラインコメント除去 (t016) を含め、yaml 構造を知らない。**Python 側の mux 判定の根** (`hooks/post-tool-use.sh` と `watchdog.py` もこれ経由)。ただし起動側の `scripts/start.sh` は別に `mode:` を読む (§2-C)。根は 2 つある |
| `lib_mux.py` rex endpoint (:4155–4205) | `rex.endpoint` | **PyYAML を遅延 import している** (:4172。`import yaml` → `safe_load`)。読めなければ rex を `unavailable` に倒す (fail-closed)。lib_mux は PyYAML に依存済み |

### C. shell: `grep` / `awk` / `sed` で自前に切り出す

| 読み口 | 取るキー |
|---|---|
| `crewvia` の `read_config` (:23) | `mode` / `taskvia` / `taskvia_url` ほか (行頭の top-level scalar)。**`mode` の既定 `"tmux"` を持つ** (:114。D1 (c) を選ぶならここも変える) |
| `scripts/start.sh` (:34 付近) | `wip_limit` / `director_model` / `worker_model` |
| `scripts/start.sh` (:239) | **`mode:`** (`grep '^mode:'`)。Director 起動時の backend (`_EFFECTIVE_BACKEND`・rex / herdr の事前検査) を決める。lib_mux の `_config_mode` とは別の読み口 |
| `scripts/start.sh` (:390–402) | `approval_channel.mode` / `ntfy.*` (`_read_approval_channel_yaml`)。`lib_approval_channel.sh` と同じ 2 段入れ子を別実装で持つ |
| `scripts/start.sh` (:415) | `taskvia` (`grep '^taskvia:'`) |
| `scripts/start.sh` (:102) | `lib_model.py resolve --config` を呼ぶ (A 経由。config のパスを渡す) |
| `scripts/dispatcher.sh` `_read_state_grace` (:55) | `mux.state_grace_seconds` (awk) |
| `hooks/lib_approval_channel.sh` `_read_approval_yaml` (:21) | `approval_channel.mode` / `.ntfy.*` (awk の 2 段入れ子) |
| `scripts/review-plan.sh` (:43) | `worker_model` (grep) + `lib_model.py` |
| `scripts/kai-review.sh` (:576) | `lib_review_engine.py get --config` に渡すだけ (A 経由) |

### D. 存在確認だけ

`crewvia-stop.sh:50` (`config/crewvia.yaml` が無ければ中止)。値は読まない。

### 不変条件 1 との関係

不変条件 1 は「queue / registry / config のファイルを開くコードは `lib_task_cards.py` の入口を通す
(読めない = `Unreadable`。`None` / `{}` / `[]` に潰さない)」。現状 §2-B/C はこれを通っていない (行走査・
grep) し、§2-A も「読めない → 既定値」に潰す読み口が多い。local 重ねを足すと**読み口の数だけ
「local が読めない」の扱いが割れる**ので、次の形にする:

- **重ねる実装は 1 か所**: 新規 `scripts/lib_config.py` (`load_effective(repo_root) -> dict | Unreadable`)。
  ファイルを開くのは `lib_task_cards.read_regular_text_or_unreadable` / `parse_yaml` だけ (不変条件 1)。
  共通 + local の読みと検査をここに集約し、読み口はすべてここを呼ぶ。
- shell 向けに CLI を付ける: `python3 lib_config.py get <dotted.key> [--default X]` と `dump --json`。
  §2-C の grep/awk/sed はすべて `get` に置き換える (awk の 2 段入れ子や行頭 scalar 限定の癖が消える)。
- **`lib_mux._config_mode()`**。lib_mux は rex endpoint の読みで既に PyYAML を遅延 import している (§2-B) ので、
  「PyYAML 無しで動かすため import を増やさない」は理由にならない。`lib_config` を import してよいか
  (`lib_mux` は restart 対象の根なので `DAEMON_RESTART_FILES` が増える)、それとも `mode` だけ
  「local の行走査 → 共通の行走査」の 2 段にするかは §9 (D3) で選ぶ。
- **mux の判定の根は 2 つ**: `lib_mux._config_mode()` と `start.sh:239`。PR-2 は両方を同じコミットで
  `lib_config` 経由にする (片方だけだと、start.sh は共通の mode で backend を選び、lib_mux は local の
  mode を使う = 本件の「別の mux で起動する」事故が残る)。

### E. local の読み先 (repo_root の決め方) — 今は 3 通りある

現状、repo root の決め方が読み口でばらばらである。

| 方式 | 読み口 |
|---|---|
| スクリプトの位置 (`SCRIPT_DIR/..`) | plan.sh / dispatcher.sh / start.sh / kai-review.sh / lib_mux / lib_daemon_watch |
| `CREWVIA_REPO_ROOT` | `lib_telegram.py` (:117) / `lib_irc_relay.py` (:215) |
| `CREWVIA_REPO` | `hooks/lib_approval_channel.sh` (:16) |

`crewvia.local.yaml` は git 管理外なので**主 checkout にしか存在しない**。Worker の hook は
`${CLAUDE_PROJECT_DIR}/hooks/...` (= Worker の worktree)、QA の隔離 clone、worktree 内の kai-review から
動くと、位置基準の読み口は worktree / clone 側の `config/` を見る。そこには local が無く、§3-6
(「local なし = 共通だけ」でエラーにしない) により **mode・approval_channel が黙って共通値になる**。

方針 (推奨):

1. `lib_config.load_effective()` の local の読み先は、**主 checkout の `config/crewvia.local.yaml`** と定義する。
   主 checkout は `git rev-parse --path-format=absolute --git-common-dir` (git 2.31+) の親で求める
   (worktree でも同じ答えになる)。`--path-format=absolute` が要る理由: 付けないと主 checkout の root では
   相対パス `.git` が返り (worktree では絶対パス)、「親」を取る結果が cwd に依存してしまう。
   git が使えない場所 (git 管理外の配置) では**スクリプトの位置だけ**に落とす。`CREWVIA_REPO_ROOT` は
   落とし先に使わない: Worker のセッションでは `.crewvia-env` が `CREWVIA_REPO_ROOT` = 主 checkout を入れるので、
   git 管理外にコピーしたテスト (script test / bats) がそこから実機の local を読んでしまう (CI には env が無い
   ので手元でしか起きず気づきにくい)。本番の主 checkout は常に git 管理下なので、この落とし先で困ることはない。
2. **共通 yaml は従来どおり呼び出し側の位置から読む** (worktree の `config/crewvia.yaml` は Worker のブランチの版。
   既存の挙動を変えない)。local だけ主 checkout から読む。
3. QA の隔離 clone は別 repo (`git-common-dir` が clone 自身) なので local は見えない。ここは意図どおり
   (隔離のため)。ただし §6 のとおり、隔離 clone で「local なし」を黙って通す経路を、テストでは
   固定の挙動として検査する。
4. **黙って共通値になる経路を塞ぐ**: (a) 主 checkout に local があるのに worktree / hook から読めなかった
   場合を検知できるよう、`lib_config` は `effective mode=… (source: local|common)` を返し、start.sh と
   dispatcher の初回ログに出す。(b) `mode` と `approval_channel` は、`registry/mux/` の記録や稼働中のデーモンと
   食い違う source=common のときに警告する (§5 の緩和と同じ仕組み)。(c) 読み口が env で repo root を
   上書きできる形 (`CREWVIA_REPO` / `CREWVIA_REPO_ROOT`) は、`lib_config` に統一して**読み先の決め方を 1 か所**にする
   (新しい config 位置 env は作らない — §8-5)。
5. hook (`lib_approval_channel.sh`) は shell なので `lib_config.py get` を呼ぶ。読めないとき (§3 の表) は
   承認できない側に倒す。worktree から呼んでも 1 の定義で主 checkout の local を見る。

## 3. 重ね方の仕様

1. 読む順: 共通 `config/crewvia.yaml` → `config/crewvia.local.yaml`。後ろが勝つ。
2. **深いマージ**: dict は再帰してキー単位で上書き、**list と scalar は置き換え** (`allowed_targets` は
   local の list が丸ごと勝つ。list の追記マージはしない — 何が足されたか読めなくなる)。
3. `key: null` (または `key:` だけ) は **null で置き換える** (「共通の値を消す」ができる)。
   ファイル全体が空 (0 byte・コメントのみ) は「上書きなし」。
4. 型の食い違い (共通は dict・local は scalar など) は置き換えず**エラー** (typo の兆候)。
5. 環境変数 > local > 共通 の順は変えない (`CREWVIA_MUX` など env が最優先という既存の規則はそのまま)。
6. local が**無い** = 共通だけ (公開前提の既定。エラーにしない)。
7. local が**ある**のに読めない / yaml でない / トップが dict でない / 通常ファイルでない (FIFO・symlink 先が
   通常ファイルでない) / 許可されないキーを含む = **`Unreadable` (fail-closed)**。共通の値に黙って戻さない
   (戻すと黙って別の mux で起動する、という本件の事故そのもの)。
8. エラーは**固定コード + 位置 (行・キー名) だけ**を出す。元の行を載せない
   (`knowledge` 内「解析エラーに元の行が漏れる族」。local は token_ref / session_link を含みうる)。

### fail-closed を consumer ごとにどう出すか

| consumer | local が壊れているとき |
|---|---|
| `start.sh` / `crewvia` (起動) | **起動しない**。固定コードと対処 (local を直す / 一時的に退避) を出して exit 1 |
| `plan.sh` | config を読む subcommand は `UsageExit` で何も書かず拒否 (task-graph も再生成しない)。読まない subcommand は影響なし |
| dispatcher | そのサイクルは割り当て・通知判定を**スキップ**し、「local config unreadable」を Director に**一度だけ**通知 (回復時に clear — `_daemon の一度だけ通知には回復時の clear が要る`)。Worker は殺さない・起こさない |
| watchdog | **何も終了させない** (判定材料が壊れているので retire / kill をしない) |
| `lib_daemon_watch` (相互監視・respawn) | respawn **しない** (保留。flap ガードと同じ「確証が無いときは動かない」) |
| `lib_mux._config_mode()` | `None` を返すと `TmuxBackend` に落ちる (t016 の事故の形)。local が壊れているときは **None ではなく例外/unavailable** にする。mux verb は拒否 |
| `hooks/pre-tool-use.sh` (承認) | 承認経路の `approval_channel` が読めない → 既存の「承認できない」側 (拒否または ask) に倒す。承認を黙って通さない |
| `hooks/post-tool-use.sh` | ログ投稿は best effort でスキップ可。デーモン同時死の backstop は `_config_mode` 失敗時に動作しない方へ (誤って殺さない) |
| `lib_telegram` / `lib_irc_relay` / `lib_escalation` | 今は「読めない → 既定 = 無効」。local が壊れた場合に**無効へ黙って落とす**と「通知が来ない」になる → 無効に落としつつ dispatcher の一度だけ通知に載せる |

## 4. 上書きを許すキー: 全キー vs allowlist

**推奨: allowlist (トップキー単位)。許可外のキーは local にあると `Unreadable`。**

サブキー単位の許可・除外は初期案では持たない (表はトップキーだけ)。`daemons` や `review` の中を細かく塞ぎたくなったら、
表に `daemons.mutual_watch` のような行を足す形で後から広げる (広げる側は安全、狭める側は local を壊す)。

理由:

- typo (`mdoe:`) が黙って無視されるのが最も悪い失敗。allowlist なら「知らないキー」としてエラーにできる。
- 「共有規則」(不変条件 5): dispatcher と plan.sh で答えが割れてはいけない値は機ごとに変えてはいけない。
  `attempts.*` (総試行数の上限) と `timeout_max_per_skill` (card 生成時に値が焼かれる) は機でずれると
  同じ mission で挙動が割れる。**除外**する (`attempts.max_total` は `lib_history.py` と plan.sh の 2 経路が読む — §2-A)。
- 公開リポジトリとしては「local に何を書いてよいか」が文書化される。

初期の allowlist (案):

| 許可 | キー |
|---|---|
| ○ | `mode` / `rex` / `mux` (state_grace_seconds 等) |
| ○ | `director_model` / `worker_model` / `model_per_skill` |
| ○ | `taskvia` / `taskvia_url` / `approval_channel` |
| ○ | `telegram` / `irc` / `escalation` |
| ○ | `review` (engine・各 model・timeout) |
| ○ | `wip_limit` (メモリ 6GB の WSL と他機で違う) / `daemons` (しきい値。mutual_watch の ON/OFF も含む) |
| × | `attempts` / `timeout_max_per_skill` (共有規則) |

allowlist は `lib_config.py` に定数で持ち、テストが共通 yaml の top-level キーと突き合わせる
(共通に新キーが増えたとき、allowlist に入れる / 入れないの判断を強制する)。

全キー許可の利点は「機能追加のたびに allowlist を更新しなくてよい」だけで、上の 3 点を全部失う。

## 5. 共通設定の `mode:` の既定値

選択肢:

- **(a) hc のまま** — proserpina は何もしなくてよい。WSL は local に `mode: rex`。
  公開前提としては hc は herdr-core という特殊な依存を要求する。
- **(b) tmux に戻す (推奨)** — 公開前提の既定として最も依存が少ない (§CLAUDE.md 設計原則 1)。
  両機が local で `mode:` を持つ。**欠点**: local を作り忘れた機が黙って tmux で起動する。
  → 緩和: 起動時・dispatcher の初回に `effective mode=hc (source: local)` を 1 行ログに出す。
  `registry/mux/` に別 backend の記録があるのに mode が違うときは起動を止める (既存の
  「mux 切替の前に crewvia を止める」の機械化。別 PR でよい)。
- (c) 共通は `mode:` を持たない (キー無し = 起動拒否) — 最も安全だが、clone 直後に動かない。

移行手順 (**順序が要る**):

1. 各機で **先に** `config/crewvia.local.yaml` を作る (旧コードは存在を無視するので無害)。
   - proserpina: `mode: hc` ほか手元の値。
   - WSL: `mode: rex` / `telegram:` / `irc:` / `session_link` / `op_command` / モデル名。
2. `git diff config/crewvia.yaml` の手元変更を local に写し終えたら `git checkout config/crewvia.yaml`。
   (**写す前に checkout しない**。値は `git diff` を保存してから。秘密は `op://` の参照のみで、値は元々無い)
3. 実装 PR を merge → `sync-main-checkout.sh`。restart は §8 の判定で走る。
4. `./crewvia status` 相当で effective mode と source を確認する。

(b) を選ぶ場合、共通の `mode:` を tmux にする変更は**実装 PR とは別の最後の PR**にする
(local が各機に揃ったことを確かめてから。揃う前に merge すると proserpina が tmux で起動する)。

## 6. 実装 PR の分け方・テスト方針

PR を 3 本に分ける (積み上げは merge commit — stacked-pr-squash の教訓):

1. **PR-1 `lib_config.py` + 単体テスト** — 重ねと検査・allowlist・CLI (`get` / `dump`)。読み口はまだ変えない。
   `.gitignore` に `config/crewvia.local.yaml`、`config/crewvia.local.yaml.example` を追加。
2. **PR-2 読み口の移行** — §2-A (python) と §2-C (shell → `lib_config.py get`)、`lib_mux._config_mode`。
   読み口ごとに 1 コミット。`DAEMON_RESTART_FILES` に `lib_config.py` を追加 (§8)。
3. **PR-3 運用** — workers.yaml の追跡解除 (§7)、共通 `mode:` の変更 (§5)、docs (README・file-map・CLAUDE.md の表)。

テスト: 読み口ごとに **local なし / あり / 壊れ** の 3 通り。

| 場面 | なし | あり | 壊れ (yaml でない・dict でない・FIFO・許可外キー・型違い) |
|---|---|---|---|
| `lib_config` 単体 | 共通と同じ | 深いマージ・list 置換・null | `Unreadable` + 固定コード、元の行が出ない (secret テスト) |
| python 読み口 (A, B) | 既存テストがそのまま通る | local の値が勝つ | 既定値に戻らず §3 の表の挙動 |
| shell 読み口 (C) | 既存 bats | `get` 経由で local が勝つ | exit 1 と固定コード |
| デーモン | 既存 | 値が反映 | スキップ + 一度だけ通知 + 回復で clear |

既存テストが config をコピーする箇所 (`test_review_plan_*.sh` ほか 6 件) は、コピー先に local を置かない限り
挙動不変。**テスト中は実機の local を読まない**ことを保証する。「隔離 repo には local が無い」はスクリプトを
コピーするテストにしか当てはまらない。主 checkout で実リポジトリの scripts を直接走らせるテスト
(lib_mux の既定 config_path・plan.sh の REPO_ROOT など) は、何もしないと実機の local を読み、手元の pytest
だけ結果が変わる。仕組み:

- `lib_config` は **`CREWVIA_MUX_TEST_ISOLATION` が立っている間は local を読まない** (共通だけ)。この印は
  `tests/conftest.py` が置く既存のもので、本番では立たない (CLAUDE.md の env 表)。local を読む挙動を
  検査するテストだけが、明示的に `local_path=` を `lib_config` に渡す (env では上書きしない)。
- bats / `scripts/test_*.sh` は env に印が無い。コピー先 (`tests/fixture_tree.py` は repo を初期化せず、
  `scripts/test_*.sh` 26 本のうち repo を初期化するのは 2 本だけ。つまりふつう git 管理外) では、§2-E の 1 により
  読み先が**スクリプトの位置 (コピー先自身)** に落ちる。`CREWVIA_REPO_ROOT` には落とさないので、Worker の
  セッションで直接走らせても実機の local は読まない。コピー先に local を置いたテストだけが読む。
  主 checkout を指す形のテストは作らない。
- 保険として conftest が「実機の local が存在する」ことを検知しても**失敗はさせず**、印の効きを
  確かめるテスト 1 本 (印あり = 実機に local があっても共通値) を PR-1 に入れる。

## 7. `registry/workers.yaml` の扱い

実態: 実行時の台帳 (Worker 名 → skills・task_count・last_active)。`assign-name.sh` / `start.sh` /
`lib_registry.py` が書く。コミットされていて、機ごとの稼働で必ずずれる。

選択肢:

- **(a) 追跡解除 + `.gitignore` (推奨)** — `registry/workers/`・`registry/heartbeats/` と同じ扱いにする。
  `git rm --cached registry/workers.yaml`。ファイルが無い場合は `lib_registry.py` が空の名簿から作る
  (`FileNotFoundError` 経路が既にある。:108)。ヘッダコメント (各欄の説明) は
  `registry/workers.yaml.example` か knowledge に移す。
  **注意**: 「名前はポジション・歴代継承」(設計原則 3) があるので、名簿が消えると名前の継承が切れる。
  `.gitignore` には既に `registry/workers.yaml` の行がある (追跡中なので効いていないだけ)。必要なのは
  `git rm --cached registry/workers.yaml` のコミット 1 つ。

  **機ごとに挙動が違う**:
  - 追跡解除を**実行する機**: `git rm --cached` はファイルを消さない (index のみ)。
  - 追跡解除のコミットを **pull で受け取る機**: `git pull` が**ワーキングツリーのファイルを削除する**
    (手元に未コミット変更があれば衝突して pull が止まる)。放っておくと名簿が消え、名前の継承が切れる。
  - 手順 (受け取る機): **退避 → pull → 退避から戻す**。**前提: crewvia を止めて (Worker なしで) 行う。**
    手順 3 と 4 の間に plan.sh / lib_registry が名簿を書くと、空の名簿から新しく作られ、そこで振られた名前が
    4 の上書きで消える (または退避と食い違う)。`sync-main-checkout.sh` は止めたデーモンも restart で起動しうる
    (`lib_daemon_watch.py:386-399` の `restart_needed` と kill→spawn) ので、この手順では使わない。
    1. `cp registry/workers.yaml "$(mktemp -d)/workers.yaml"` で退避先を決めて退避する (退避先のパスを控える)。
    2. 手元変更を `git checkout registry/workers.yaml` で捨てる (追跡中の最後の 1 回。退避済みなので失われない)。
    3. `git pull`。ファイルが消える。
    4. 退避から `registry/workers.yaml` に戻す。以後は `.gitignore` で無視される。
  - この手順は `sync-main-checkout.sh` の移行メモにも入れる (PR-3)。
- (b) 追跡のまま `skip-worktree` — 機ごとの設定で脆い (pull で外れる)。非推奨。
- (c) 追跡のまま・共通は雛形だけ (名簿を空にして commit) — 機ごとの差は消えるが、今度は
  稼働のたびに diff が出る。解決していない。

(a) は PR-3 で。台帳をコードが読む箇所 (`scripts/dispatcher.sh`・`plan.sh`・`kai-review.sh` ほか) は
ファイル欠如を `Unreadable` ではなく「空」で扱ってよいか (初回起動で必ず起きる) を PR-3 の最初に洗う。

## 8. デーモンの respawn・`sync-main-checkout.sh` の restart 判定

現状: `restart_needed()` は `DAEMON_RESTART_FILES[name]` の**ファイル内容の digest** を、デーモン起動時に
記録した `files_digest` と比べる。**`config/crewvia.yaml` はこの一覧に入っていない**。つまり今も
config を変えても restart は要求されない (手で restart している)。デーモンが config を毎サイクル読む
(dispatcher は cycle ごとに新しい python) 読み口と、起動時に 1 回だけ読む読み口 (start.sh の `wip_limit` 等、
telegram/IRC の認証情報の取り出し) が混在している。

方針:

1. **local は digest の対象に入れない** (推奨)。理由: (a) `DAEMON_RESTART_FILES` の AST 突き合わせテスト
   (`test_daemon_restart_files_match_load_model.py`) は `.py` / `.sh` の import を前提にしている。
   (b) local は git 管理外なので、`sync-main-checkout.sh` の「変わったファイル一覧」にも出ない。
   (c) 値の変更で restart が自動で走ると、telegram/IRC の認証情報を変えたつもりのない restart が増える。
2. 代わりに、**起動時 1 回読みの値だけ**が問題になる。dispatcher は respawn のたびに認証情報を取り出す
   (CLAUDE.md の Telegram/IRC の項) ので、local を直したら `lib_daemon_watch.py restart dispatcher` を
   手で実行すれば反映される。これを手順書に 1 行書く。
3. 「local の内容が変わったのに restart していない」を**検知だけ**する案 (任意・PR-3 以降):
   record_own_version に `local_config_digest` を足し、`restart-needed` が `true` ではなく
   別の値 `config-changed` (advisory) を返す。自動 restart はしない。
4. `lib_config.py` / `lib_mux.py` を import する側は `DAEMON_RESTART_FILES` に `lib_config.py` を足す
   (PR-2 の必須作業。推移 import の追加は構造ガードに引っかかる — `new-lib-trips-single-definition-guards`)。
5. respawn の `spawn_command()` が運ぶ env allowlist は変えない。local の読み先は repo_root から決まるので
   env を増やさない (`CREWVIA_*` の config 位置 env は作らない — 作ると env 停止スイッチと同じ割れ方をする)。

## 9. Director に決めてほしいこと

| # | 内容 | 選択肢 | 推奨 |
|---|---|---|---|
| D1 | 共通 `mode:` の既定 | (a) hc のまま / (b) tmux に戻す / (c) キー無し = 起動拒否 | **(b)**。local 配布が揃ってから最後の PR で変更 |
| D2 | 上書きキーの範囲 | 全キー / allowlist | **allowlist** (`attempts` / `timeout_max_per_skill` を除外、許可外キーはエラー) |
| D3 | local が壊れたときの `lib_mux._config_mode()` | `lib_config` を import (restart 対象が増える) / mode だけ行走査 2 段 | **`lib_config` を import**。lib_mux は rex endpoint の読みで PyYAML を既に遅延 import している (:4172) ので依存は増えない。2 段にすると「壊れた local を共通に戻す」穴を 2 か所で守ることになる |
| D4 | `registry/workers.yaml` | (a) 追跡解除 / (b) skip-worktree / (c) 雛形のみ | **(a)**。移行時は各機のファイルを退避してから pull |
| D5 | local 変更の restart | digest 対象に含める / 含めず手動 + 検知(advisory) | **含めず手動**、検知は PR-3 以降の任意 |
| D6 | PR 分割 | 1 本 / 3 本 | **3 本** (PR-1 lib、PR-2 移行、PR-3 運用)。積み上げは merge commit |
| D7 | `null` の意味 | 共通の値を消す / 無視 | **消す** (§3-3) |

## 10. 実装時の落とし穴 (過去の教訓の写し)

- 1 つの読み口を変えて他の読み口が旧挙動のまま残ると、同じ値を 2 通りに読む。§2 の表を PR-2 の
  チェックリストにして、`grep -rn 'crewvia\.yaml'` が 0 件 (テストのコピーを除く) になるまで閉じない。
- 行走査 (`_config_mode`) はインラインコメント・クォートを自前で処理していた (t016)。`lib_config.get` に
  寄せると消せるが、`mode: "hc"  # コメント` の形を回帰テストに残す。
- シェルの `awk '/^mux:/…'` は 2 段入れ子しか見られず、`mux:` の次の top-level キーでも止まらない読み口があった。
  `get` に置き換えたら該当テストを `get` 経由に書き直す。
- 秘密は local にも**値を書かない** (`op://` 参照・ファイルパスだけ。既存の規則どおり)。`.example` にも値は入れない。
- `.gitignore` の追加は `config/crewvia.local.yaml` の 1 行 (`registry/workers.yaml` は既に載っている — §7) (パターンで広げない — `config/*.yaml` を巻き込むと共通も外れる)。
