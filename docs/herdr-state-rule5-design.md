# herdr agent state による blocked / idle-with-task 検知 (Rule 5) 設計

作成日: 2026-09-04
作成者: Sora (Director)
ステータス: 承認済み設計 (実装前)
前提: `docs/herdr-migration-design.md` (mux 抽象化、Phase 0-3 完了)、`knowledge/worker-shutdown-rules.md` (Rule 1-4)
参考: `~/obsidian/research/20260904_herdr_tmux_replacement.md` §9 (herdr の agent state 検知)

---

## 1. 目的と範囲

Worker が **承認待ち・質問待ちで止まっている** ことを Director が数十秒以内に知れるようにする。

- 現行の watchdog は heartbeat mtime の無音時間しか見ておらず、「考え中」「質問待ち」「本当に idle」を区別できない。2026-09-04 に review Worker (Seo) が permission classifier の質問待ちで 19 分停止し、Director は定期 wakeup まで気づけなかった
- herdr は pane の画面を regex で判定し `blocked / working / idle / done / unknown` を返す。これを Dispatcher の 5 秒ポーリングに取り込む
- **範囲**: 検知と Director への通知のみ。自動対処 (Worker への自動回答・自動 kill) はしない。Director が通知を見て判断する
- **tmux モードでは何もしない** (state が取れないため判定を skip)。本番が tmux のままでも無害

### herdr の判定について (docs より)

- `blocked`: 承認ダイアログや `Enter to confirm · Esc to cancel` 等の UI を regex で認識した時
- `working`: spinner / `esc to interrupt` 等
- `idle` / `done`: ターン終了して `❯` で待機 (`done` は UI で tab を見ていない状態)。**ターン終了時にテキストで質問して待っている Claude も `idle`/`done` になる** (docs: idle ≠ task complete)
- `unknown`: agent は居るが分類不能

したがって「質問待ち」は `blocked` だけでは拾えず、**「idle なのに task を持っている」** を別途判定する必要がある。

## 2. 検知ルール (Rule 5)

各生存 Worker について、以下のどちらかが **grace 秒 (既定 60) 連続**で成立したら Director へ通知する。

| 名前 | 条件 | 意味 |
|---|---|---|
| **A: blocked** | `state == blocked` | 承認ダイアログ等で止まっている |
| **B: idle-with-task** | `state ∈ {idle, done}` かつ `queue/assignments/<name>` が存在 | task を持ったまま手が止まっている (質問待ち / hang 後の停止 / done 忘れ) |

- `working` / `unknown` は何もしない (`unknown` は safe side = 通知しない)
- 連続時間は `registry/mux/<name>.state.json` に `{state, since}` を保持して測る。5 秒ごとの単発観測でノイズ通知しないため。state が変わったら `since` をリセット
- `working` に戻ったら通知 dedup key をクリアし、再発時に再通知できるようにする
- Rule 2 (blocked-stuck: 全 matching task が 600 秒 blocked) とは独立。Rule 2 は「割り当てる task が無い idle」、Rule 5 B は「task を持っているのに止まっている」

## 3. 設計

### 3.1 `lib_mux.py` に `state` verb を追加

```
state(name) -> "blocked" | "working" | "idle" | "done" | "unknown"
  herdr: pane get <pane_id> の .result.pane.agent_status (キャッシュ経由、他 verb と同じ再解決)
  tmux : 常に "unknown"
  失敗時: "unknown" + stderr WARNING (例外を上に投げない、既存方針)
CLI: python3 lib_mux.py state <name>   # 文字列を 1 行出力
```

### 3.2 Dispatcher (`scripts/dispatcher.sh` 埋め込み Python)

- 既存の Worker 列挙ループ (assignment ファイルの有無を見ている箇所) に Rule 5 判定を追加
- `Mux().state(name)` を各 Worker につき 1 回/サイクル呼ぶ (≤ 8 Worker × 5 秒なら負荷は無視できる)
- 通知経路は既存の `tmux_send(_director_name(), msg)` + `NOTIFY_TTL` (300 秒) の dedup。key は `blocked_<name>` / `idle_with_task_<name>`
- 通知文面:
  ```
  [Rule 5] Worker <name> が <blocked|idle-with-task> です (task <id>, mission=<slug>, <n>秒継続)。
  画面末尾:
  <capture(name) の末尾 5 行>
  ```
  Director が pane を開かずに「質問か / ダイアログか / 終わっているか」を判断できるようにする
- Director が居ない (`-director` tab なし) 場合は WARNING ログのみ (既存挙動と同じ)

### 3.3 設定

- `config/crewvia.yaml`:
  ```yaml
  mux:
    state_grace_seconds: 60   # Rule 5 の連続時間しきい値
  ```
- env `CREWVIA_STATE_GRACE` で上書き (dispatcher.sh の引数経由で埋め込み Python に渡す、`NOTIFY_TTL` と同じ流儀)

### 3.4 Director 側 SOP (`agents/director.md`)

- §16 の通知表に Rule 5 の行を追加:
  | `[Rule 5] Worker X が blocked / idle-with-task です …` | 承認待ち・質問待ち・停止 | 画面末尾を読み (1) 質問なら `python3 scripts/lib_mux.py send X-worker "<回答>"` (2) 承認ダイアログなら user へエスカレーション (3) 回復不能なら kill + `plan.sh update --reset` |
- §17 に Rule 5 の節を追加 (自動対処しないこと、tmux モードでは発火しないことを明記)
- `knowledge/worker-shutdown-rules.md` に Rule 5 を追記

## 4. Spike (実装前、throwaway)

herdr が Claude Code の各状況を何と判定するかを実機で確認し、Rule 5 の A/B 定義を確定する。結果は `docs/herdr-state-spike-results.md`。

| # | 状況 | 予想 |
|---|---|---|
| 1 | 思考中 (spinner) | `working` |
| 2 | task 完了後に `❯` で待機 | `idle` / `done` |
| 3 | テキストで質問して `❯` で待機 (Seo 型) | `idle` / `done` → B で拾える |
| 4 | AskUserQuestion の選択 UI | 不明 (`blocked` か `idle`) |
| 5 | PreToolUse 承認ダイアログ (`Enter to confirm · Esc to cancel`) | `blocked` |
| 6 | `done` → `idle` の遷移 (tab を見ていない時に `done` に留まるか) | 記録のみ (B は両方を含む) |

4 が `idle` なら B で拾えるので問題なし。`unknown` になる状況があれば、その状況は検知対象外として記録する。

## 5. エラー処理

- `state()` 失敗 → `unknown` → 判定 skip (通知しない)。herdr の画面 regex が Claude の UI 変更で崩れた場合も同じ (安全側)
- `capture()` 失敗 → 画面末尾なしで通知 (通知自体は出す)
- `registry/mux/<name>.state.json` が壊れていたら削除して作り直す

## 6. テスト

| レイヤ | 内容 |
|---|---|
| `tests/lib-mux.bats` | fake herdr の `pane get` に `agent_status` を持たせ、`state` verb の 5 値を assert。tmux backend で `unknown` |
| `tests/dispatcher-state-rule5.bats` (新規) | dispatcher の判定を fake state で検証: A / B の成立、`working` で不成立、grace 未満で通知しない、grace 超過で 1 回通知、`NOTIFY_TTL` 内の再通知抑制、`working` 復帰で key クリア、tmux (`unknown`) で完全 skip、assignment 無しの `idle` は B にならない |
| QA (実機、隔離) | Phase 3 と同じ手法 (`CREWVIA_MUX=herdr` + `CREWVIA_HERDR_WORKSPACE=crewvia-qa` + 一時 queue + herdr 上の dispatcher)。Worker に「AskUserQuestion で質問して待て」「承認が要る操作をせよ」という task を積み、A/B 通知が dispatcher.log (Director tab 不在時) に出ること、grace 未満では出ないこと、本番 tmux 側に影響ゼロを確認 |

## 7. ロールアウト (1 mission、直列)

planning → spike → 実装 (`lib_mux` state verb + dispatcher Rule 5 + config + bats + docs) → QA (隔離 E2E) → review。docs は実装と同じ PR で可。

## 8. リスクと逃げ道

| リスク | 対処 |
|---|---|
| herdr の画面 regex が Claude UI 変更で崩れる | `unknown` → 判定 skip。remote manifest 更新で追随 |
| B が「done 直後の一瞬」を拾う誤通知 | grace 60s + `working` 復帰クリア。閾値は config で調整 |
| 通知が多すぎる | `NOTIFY_TTL` (300s) の dedup。必要なら Rule 5 専用 TTL を config 化 |
| tmux モードへの影響 | `unknown` 固定で判定 skip、挙動不変 (bats で担保) |
| 将来 push 化したい | `events.subscribe` への差し替えは dispatcher 内部に閉じる (interface は `state` verb のまま) |
