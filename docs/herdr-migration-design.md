# tmux → herdr 移行設計 (mux backend 抽象化)

作成日: 2026-09-04
作成者: Sora (Director)
ステータス: 承認済み設計 (実装前)
関連調査: `~/obsidian/research/20260904_herdr_tmux_replacement.md` (herdr v0.8.2 の primitive 別リファレンス)

---

## 1. 目的と範囲

crewvia が Worker / Director / dispatcher / watchdog の並列実行基盤として使っている tmux を、
agent 向け terminal multiplexer **herdr** (Rust, v0.8.2, pre-1.0) でも動かせるようにする。

- **方針**: backend 抽象化 + tmux 併存。`CREWVIA_MUX=tmux|herdr` で切り替える。tmux は fallback として残す
- **範囲 (第 1 段階)**: tmux と同じ 7 primitive を herdr で実現し、現行ロジック (dispatcher の判定、watchdog の生存判定、kickoff 文面、命名) を **一切変えない**
- **範囲外**: herdr 固有の agent state 検知 (blocked / working / idle) の活用、`agent start` / `agent prompt` API の利用。これらは後続 mission で検討する
- **検証環境**: この WSL2 に herdr をインストールして検証する

## 2. 現状: crewvia の tmux 依存

| # | tmux 操作 | 使用箇所 |
|---|---|---|
| 1 | `new-session` / `new-window -n <name>` + `send-keys "<cmd>" Enter` | `scripts/start.sh` (Worker / Director / dispatcher / watchdog 起動) |
| 2 | `send-keys "<text>"` → 0.1s → `send-keys Enter` (Claude TUI の bracketed paste 対策で 2 段階) | start.sh kickoff / `dispatcher.sh` 通知 / `watchdog.py` terminate / `verifier-dispatcher.sh` |
| 3 | `capture-pane -p` (`❯` 検出で prompt ready 待ち) | start.sh |
| 4 | `list-windows -F '#{window_name}'` (`*-worker` 名で生存 Worker 列挙) | dispatcher / watchdog / verifier |
| 5 | `kill-window` | dispatcher (idle shutdown) |
| 6 | `display-message '#{pane_pid}'` → `pgrep -P` (Claude 稼働中判定) | watchdog |
| 7 | `attach-session` / `switch-client` | start.sh (Director 起動後の自動 attach) |

その他: `review-plan.sh`, `benchmark-ctx.sh`, `setup-new-env.sh` (必須ツール判定)。
window 名 `<Agent>-<role>` (例 `Sora-director`, `Omar-worker`) が registry / heartbeat / dispatcher のフィルタと結びついている。

## 3. herdr との対応

herdr のモデル: background **server** が PTY を保持し、`workspace` > `tab` > `pane` の階層。ID は `w1:p3` 形式 (0.7.0 以降 stable)。CLI は全コマンド JSON を返し、裏は Unix socket (`~/.config/herdr/herdr.sock`, NDJSON)。

| crewvia の tmux 操作 | herdr 等価 | 差分 |
|---|---|---|
| `new-window -n NAME` + `send-keys CMD Enter` | `tab create --workspace $ws --label NAME --cwd DIR --no-focus` → `pane run $pane "CMD"` | 2 コマンド。返ってきた `pane_id` を保持する |
| `send-keys "msg"` → `send-keys Enter` | `pane run $pane "msg"` (text + Enter を bracketed paste 込みで 1 発) | 名前でなく pane_id 指定 |
| `capture-pane -p` | `pane read $pane --source visible` | 等価 |
| `list-windows -F '#{window_name}'` | `tab list --workspace $ws` / `pane list` の `label` | 名前 → ID の逆引きが必要 |
| `kill-window` | `tab close $tab` | 等価 (signal 挙動は未記載) |
| `#{pane_pid}` → `pgrep -P` | `pane process-info --pane $pane` → `.result.process_info.shell_pid` | 等価 |
| `attach-session` / `switch-client` | `herdr` (別ターミナルから) / `tab focus $tab` | herdr pane 内からの nested attach は不可 |

結論: primitive 等価は全て成立する。差分は「名前 → ID 解決」と「attach の作法」の 2 点。

**使わないもの**: `claude --dangerously-skip-permissions` (herdr ドキュメントの例に登場するが、crewvia は hook ベースの承認モデルのため使用しない)。

## 4. 設計

### 4.1 `scripts/lib_mux.py` — 共通 mux モジュール

責務は「名前付きのエージェント端末を spawn / 送信 / 読む / 列挙 / 殺す / PID / attach」の 7 verb のみ。タスク配布・状態判定は持たない。

```
class Mux:                      # backend 選択: CREWVIA_MUX env > config/crewvia.yaml mode: > 自動 (tmux があれば tmux)
  spawn(name, cmd, cwd, env)    # 名前付き端末を作り cmd を実行。既存名なら何もせず False
  send(name, text)              # text + Enter (tmux: 2-step send-keys / herdr: pane run)
  capture(name) -> str          # 現在画面 (tmux: capture-pane -p / herdr: pane read --source visible)
  list(suffix=None) -> [name]   # 生存端末名 (tmux: list-windows / herdr: tab list の label)
  kill(name)                    # tmux: kill-window / herdr: tab close
  pid(name) -> int|None         # shell PID (tmux: #{pane_pid} / herdr: process-info.shell_pid)
  attach(name)                  # Director 用。挙動は backend 依存 (4.2-E)
  available() -> bool           # backend が使えるか
class TmuxBackend
class HerdrBackend
CLI: python3 lib_mux.py <verb> [args]   # bash からの呼び口。list は 1 行 1 名、capture は生テキスト、他は exit code
```

- 端末名は現行の `<Agent>-<role>` をそのまま使い、`crewvia` session / workspace の下に置く。**名前は変更しない**
- 呼び出し側は名前だけを渡す。`crewvia:Sora-director` のような `session:window` 文字列は廃止し、tmux backend が内部で `crewvia:` を付ける
- bash 側: `scripts/lib_mux.sh` に `mux_spawn` 等の薄い関数 (中身は `python3 "$SCRIPT_DIR/lib_mux.py" ...`)。`start.sh` / `review-plan.sh` / `benchmark-ctx.sh` が source する
- Python 側: dispatcher / watchdog / verifier の埋め込み Python は `sys.path` に `scripts/` を足して `from lib_mux import Mux` (既存 `lib_registry.py` と同じ流儀)
- **tmux backend** は現行の tmux 呼び出し (0.1s sleep 入り 2-step send を含む) をそのまま移す。挙動不変

### 4.2 herdr backend

**A. Server ライフサイクル**
- `available()`: socket `~/.config/herdr/herdr.sock` に `{"id":"1","method":"ping","params":{}}` を投げ `pong` で OK
- 不通なら `setsid nohup herdr server >> ~/.config/herdr/crewvia-server.log 2>&1 &` で起動し最大 10 秒 ping ポーリング。`herdr server` が自前で daemon 化するかは未記載のため Phase 0 で確定し、daemon 化するなら wrapper は不要
- default session を使う (`--session` は使わない)。ユーザーが `herdr` と打つだけで覗ける状態を維持

**B. tmux session ↔ herdr workspace / window ↔ tab**
- tmux `crewvia` session ≡ herdr workspace (label `crewvia`)。`workspace list` で label 検索、無ければ `workspace create --label crewvia --cwd $REPO_ROOT --no-focus`
- tmux window ≡ herdr tab (1 tab = 1 pane)。`tab create --workspace $ws --label <name> --cwd <cwd> --no-focus` の戻り値 `root_pane.pane_id` を保持
- `spawn` は cmd を `pane run $pane "<cmd>"` で流す。現行 `LAUNCH_CMD` (export 群 + cd + claude ...) を文字列のまま渡す。env は `--env` でも渡せるが tmux と同じ「シェルに export を打ち込む」方式に統一して差分を減らす

**C. 名前 → ID 解決とキャッシュ**
- 正: `pane list` で `label == name` の pane を取り `tab_id` / `pane_id` を得る。`tab create --label` が root pane の label に伝播しなければ spawn 直後に `pane rename $pane <name>` を打つ (Phase 0 で確認)
- キャッシュ: `registry/mux/<name>.json` に `{tab_id, pane_id, backend, created_at}`。`send / capture / pid` はキャッシュ優先、herdr 側で pane が消えていれば (`pane get` エラー) キャッシュを捨てて再解決、それでも無ければ「端末なし」
- `list()` は常に herdr に問い合わせる (生存判定の正は herdr 側)。`kill()` 成功時にキャッシュ削除。`registry/mux/` は `.gitignore` 対象

**D. send の Enter 保険**
- 基本は `pane run` 1 発。旧版で「Claude 初期化中は Enter が落ちる」観測があるため、`send` 後に `capture` で入力行に text が残っていれば `pane send-keys $pane enter` を 1 回だけ追送する

**E. attach**
- herdr は pane 内からの nested 起動を禁止している
- `HERDR_ENV=1` (既に herdr クライアント内) → `tab focus <Director の tab_id>`
- herdr 外のシェル → attach しない。「`herdr` を実行すると Director 画面に入れます」と表示して start.sh は制御を返す
- 起動手順は `herdr` → その中で `bash scripts/start.sh director` の順になる (README に追記)

**F. version guard**
- `available()` で `herdr --version` を取り、検証済み 0.8.2 と不一致なら WARNING (停止はしない)
- 叩く herdr サブコマンドは backend 内の定数テーブルに集約し、CLI rename 時は 1 箇所直せば済む形にする

### 4.3 設定・モード選択

`config/crewvia.yaml`:
```yaml
mode: tmux        # inline | tmux | herdr
```
- env 上書き: `CREWVIA_MUX=tmux|herdr` (新設)。既存 `CREWVIA_TMUX=1/0` は互換維持 (`CREWVIA_TMUX=1` かつ `CREWVIA_MUX` 未設定なら tmux)
- Director 起動時プロンプト「tmux を使いますか？」は「並列モードにしますか？」に変え、backend は config / env から決める
- herdr 指定なのに herdr 不在なら tmux にフォールバックせず **エラー停止** (別 mux に散る事故防止)
- Director / Worker / dispatcher / watchdog は `CREWVIA_MUX` env を引き継ぐ (現行 `CREWVIA_TMUX` と同じ仕組み)

### 4.4 呼び出し側の置き換え

| ファイル | 現行 | 置換後 |
|---|---|---|
| `scripts/start.sh` (L456-535) | has-session / new-session / new-window / send-keys / capture-pane / attach | `source lib_mux.sh` → `mux_spawn` / `mux_send` / `mux_capture` / `mux_attach`。dispatcher・watchdog 窓も `mux_spawn` |
| `scripts/dispatcher.sh` (L400-452 + 呼び出し ~10 箇所) | `tmux_list_worker_windows / tmux_send / tmux_kill_window` | 関数名は残し中身を `Mux().list('-worker') / send / kill` に。冒頭の `command -v tmux || exit 0` は `Mux().available() || exit 0` |
| `scripts/watchdog.py` (L258-291, 338-375) | `_tmux_window_target` / pane_pid / send-keys | `Mux().list() / pid / send` |
| `scripts/verifier-dispatcher.sh` (L341-376) | dispatcher と同型 | 同上 |
| `scripts/review-plan.sh` | `tmux new-window` + inline fallback | `mux_spawn`、fallback は維持 |
| `scripts/benchmark-ctx.sh` | tmux 直叩き (tmux 必須) | `mux_*`、必須判定は `mux_available` |
| `scripts/setup-new-env.sh` | 必須ツールに `tmux` | `tmux` or `herdr` のどちらかで可 |

**Director 名の hardcode 解消**: dispatcher / verifier の `'crewvia:Sora-director'` を `list('-director')` で解決する形に直す (同じ箇所を触るため同 PR に含める)。

**変えないもの**: heartbeat ファイル方式、Dispatcher の判定ロジック、Worker の kickoff 文面、命名 `<Agent>-<role>`、`.crewvia-env`、hooks。

### 4.5 ドキュメント更新

- `README.md`: herdr の install 手順と「`herdr` → `bash scripts/start.sh director`」の起動順
- `CLAUDE.md`: 設計原則 1 を「mux 非依存 (tmux / herdr はオプション)」に、環境変数表に `CREWVIA_MUX`
- `agents/director.md` §6 / §16: 「tmux モード必須」→「並列モード (tmux or herdr) 必須」、`tmux attach -t crewvia` に herdr 版を併記
- `agents/worker.md` L89: 「Dispatcher が tmux send-keys で通知」→ mux 経由に

## 5. エラー処理

- `lib_mux.py` は例外を上に投げない。各 verb は失敗時 `False` / `None` / `""` を返し stderr に `[mux:<backend>] WARNING: ...` (現行 tmux helper の流儀)
- 停止するのは 2 ケースのみ: (1) `CREWVIA_MUX=herdr` なのに herdr バイナリ不在 (2) server 起動を 10 秒待っても ping 不通。start.sh 冒頭で検出して exit 1
- herdr CLI の JSON は stdout、エラーは stderr の JSON。`exit 2` (usage error = CLI rename の兆候) は「herdr CLI の引数が変わった可能性」と明示ログ
- キャッシュ不整合は 4.2-C の通り再解決 → 無ければ「端末なし」。watchdog はこれを従来の "kill (window gone)" と同じ扱い

## 6. テスト

| レイヤ | 内容 | 方法 |
|---|---|---|
| unit (bats) | tmux / herdr backend を **fake CLI** で検証: PATH 先頭に偽 `tmux` / `herdr` を置き呼び出し引数を記録、7 verb の期待引数と戻り値を assert。名前 → ID 解決とキャッシュ再解決も fake で再現 | `tests/lib-mux.bats` (新規) |
| regression | 既存 bats (dispatcher-*, worker-idle-shutdown, target-dir-*) 全 pass | 変更なし |
| E2E (tmux) | 現行運用と同じ mission を tmux backend で流し挙動不変を確認 | 手動 (QA task) |
| E2E (herdr) | Director + Worker 1 名で init → planning → pull → done → archive。dispatcher 通知・watchdog 生存判定・idle shutdown (`tab close`) を観察 | 手動 (QA task) |

## 7. ロールアウト (各 Phase = 1 crewvia mission、PR は Phase ごと)

| Phase | 内容 | 出口条件 |
|---|---|---|
| **0. Spike** (throwaway) | herdr install → 未確定 5 点を確認。結果を `docs/herdr-spike-results.md` に | 5 点の Yes/No が出る。No があれば 4.2 の該当設計を修正 |
| **1. 抽象化 (挙動不変)** | `lib_mux.py` + `lib_mux.sh` + tmux backend、全呼び出し側を置換、Director 名 hardcode 解消、`tests/lib-mux.bats` | 既存 bats 全 pass + tmux E2E で現行と同じ動き |
| **2. herdr backend** | `HerdrBackend`、`mode: herdr` / `CREWVIA_MUX`、version guard、bats に herdr fake ケース | bats 全 pass |
| **3. E2E + docs** | herdr 上で mission 1 本完走、4.5 のドキュメント更新 | herdr E2E pass、ドキュメント merge |

Phase 1 を herdr と切り離すことで、Phase 1 で壊れたら herdr のせいではないと切り分けられる。Phase 2 の mission は Phase 0 完了後に起票する。

### Phase 0 で確認する未確定事項

1. `herdr server` が daemon 化するか (しないなら 4.2-A の wrapper が必要)
2. `tab create --label` が root pane の `label` に伝播するか (しないなら `pane rename`)
3. `pane run` の Enter が Claude Code TUI で確実に submit されるか (4.2-D の保険が要るか)
4. `tab close` で claude プロセスが正しく終了するか (孤児プロセスが残らないか)
5. WSL2 上で socket / ConPTY 周りに問題がないか

## 8. リスクと逃げ道

| リスク | 対処 |
|---|---|
| herdr の CLI rename (3 ヶ月で 2 回実績) | backend の定数テーブル 1 箇所修正 (4.2-F) |
| herdr が実運用で不安定 | `mode: tmux` に戻すだけ (tmux backend は Phase 1 で検証済み) |
| WSL2 で socket / ConPTY 問題 | Phase 0 で判明するので実装前に止められる |
| Claude の状態検知が画面 regex 依存 | 第 1 段階では使わないので影響なし |
| Director 起動体験の変化 (自動 attach 不可) | README で `herdr` → `start.sh director` の順を案内 |
