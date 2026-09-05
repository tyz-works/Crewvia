# Dispatcher 通知到達 観測 SOP

**作成**: 2026-09-05, Minjun (mission: 20260905-notify-observation, t007)
**対象**: crewvia Dispatcher の ntfy 通知到達信頼性の継続観測手順

---

## 1. 通知到達確認 SOP — observe-notify.sh の使い方

### インストール

`scripts/observe-notify.sh` は PR #152 でリポジトリに追加された dispatcher ログ集計ツール。
**PR #152 が merge された後に使用可能**（PR #149 は dispatcher ログの永続化機能であり、本スクリプトとは別）。
リポジトリルートの `scripts/` に配置済みのため、追加インストール不要。

```bash
# 基本実行 (今日のログを集計)
bash scripts/observe-notify.sh

# 指定日のログを集計
bash scripts/observe-notify.sh --date 20260905

# 最新 50 件の通知ラインのみ集計
bash scripts/observe-notify.sh --last 50

# JSON 形式で出力して jq でパース
bash scripts/observe-notify.sh --json | jq .stats

# JSON 形式で特定日を集計
bash scripts/observe-notify.sh --date 20260905 --json | jq .stats

# notify cache のパスを明示指定 (デフォルト: /tmp/dispatcher-notify-cache.json)
bash scripts/observe-notify.sh --notify-cache /tmp/dispatcher-notify-cache.json

# ヘルプを表示
bash scripts/observe-notify.sh --help
```

### 出力の読み方

```
=================================================
 Dispatcher 通知到達統計 — 20260905
=================================================

 ①通知送信件数            : 24 件
 ②notify cache ヒット
    - アクティブ (suppress中) : 19 件
    - 全エントリ (履歴)        : 116 件
 ③Director 実受信確認      : 24 件
=================================================
```

| 項目 | 意味 | 正常値の目安 |
|------|------|-------------|
| ①通知送信件数 | ログ内の `→ [target]` 行の総数 | — |
| ②アクティブ cache | TTL (300s) 以内の suppress 中エントリ数 | N/A (監視用) |
| ②全エントリ | 全履歴エントリ数 (suppress 済み含む) | — |
| ③Director 実受信 | `→ [*director*]` の行数 | ① と一致が理想 |

**①=③ であれば、送信した全通知が Director に到達している**ことを示す。
ただし、通常運用では Worker 宛通知 (assign/shutdown/handoff) も ① に含まれるため **①>③ が正常**。
`①−③ = Worker 宛通知件数` として読むこと。
suppress (cache ヒット) があっても Director が正しいタイミングで受信していれば問題なし。

---

## 2. 切り分けフローチャート — 「通知が来ない」場合

```
通知が届いていない / Director が反応しない
         │
         ▼
[Step 1] observe-notify.sh を実行し ①と③を確認
         │
    ┌────┴────────────────────────────────┐
    │ ①=0 (送信件数ゼロ)                  │ ①>0 かつ ③=0 (送信されたが Director 未受信)
    ▼                                     ▼
Dispatcher が発火していない               ntfy 配送問題 / Director の tab が起動していない
    │                                     │
    ▼                                     ▼
[Step 1a] dispatcher プロセス確認         [Step 2a] ntfy サーバー疎通確認
  pgrep -f dispatcher.sh                    curl -s $NTFY_URL/health
  → 起動していない → ./crewvia で再起動     → 応答なし → ntfy 障害
                                          [Step 2b] ntfy トピック確認
[Step 1b] notify cache を確認               echo $NTFY_TOPIC
  cat /tmp/dispatcher-notify-cache.json     → 空 → NTFY_TOPIC 未設定
  | python3 -m json.tool                  [Step 2c] Director tab の存在確認
  → task のエントリがある = suppress 中     herdr list または tmux list-windows -t crewvia で Director tab 確認
    → キャッシュ TTL (300s) 待ち
    → または docker/herdr 再起動で解消
         │
    ┌────┴───────────────────────────────────┐
    │ ①>0 かつ ③=①                          │ ①>0 かつ ③<①
    ▼                                        ▼
正常 — 通知は届いている                   一部通知が欠落 (investigate)
                                            [Step 3a] 欠落した通知のスキルを確認
                                              grep 'skill' queue/missions/*/tasks/*.md
                                            [Step 3b] Director tab の割当状況を確認
                                              plan.sh status (in_progress task がないか)
                                            [Step 3c] PR #150 fix (c) タプル化を確認
                                              dispatcher.sh の assigned_task_ids が
                                              タプル化されているか確認:
                                              grep 'assigned_task_ids' scripts/dispatcher.sh
                                              (cache key 形式の確認は PR #144 検証 → §6 参照)
```

---

## 3. blocked ログ判断基準

### 仕様の確認 (PR #144 実装)

`dispatcher.sh` は blocked task に対して以下の頻度でログを出力する:

- **初回 blocked 検出時**: 即時 1 行
- **以降**: NOTIFY_TTL (300s = 5 分) ごとに 1 行

これは **設計通り** のハートビートログである。

### 実観測データ (t005, 2026-09-05)

```
観測ウィンドウ: 06:37:55 〜 06:48:55 UTC (11 分間、3 サイクル)

| task | Cycle 1 (初回) | Cycle 2      | Cycle 3      | 間隔   |
|------|--------------|-------------|-------------|--------|
| t003 | 06:37:55     | 06:42:56 (+301s) | —        | 301s   |
| t004 | 06:38:05     | 06:43:07 (+302s) | —        | 302s   |
| t006 | 06:38:41     | 06:43:42 (+301s) | 06:48:45 (+303s) | 301/303s |
| t007 | 06:38:51     | 06:43:53 (+302s) | 06:48:55 (+302s) | 302/302s |
```

per-task 出力間隔: **301〜303s** (設計値 300s に対して ±3s 以内)

### flooding 判断基準

| 状況 | 評価 | 対応 |
|------|------|------|
| 5 分で 1 task あたり 1 行以下 | **仕様通り — 問題なし** | 対応不要 |
| 5 分で 1 task あたり複数行 | bug (suppression 不備) | PR で追加 fix |
| 40 task 同時 blocked → 40 行/5min | リスクあるが自然分散で軽減 | NOTIFY_TTL を 600s に伸ばすことを検討 (副作用: 通知 recovery latency も 5min → 10min に増加。この副作用を許容できる場合のみ) |
| 現在規模 (4〜10 task) での flooding | **なし** (4 行/5min, 数十秒スプレッド) | WONTFIX |

> **結論 (2026-09-05)**: 現在の運用規模では PR #144 の blocked ログ抑制は仕様通りに機能しており、追加 fix は不要 (WONTFIX)。40 task 超の mission が増えた場合は再評価すること。

---

## 4. 観測頻度の目安 — 統計的根拠

### N 回観測の意味

通知未達率 p のバイナリ試行において、N 回全て到達 (miss=0) した場合の **95% 片側上限**:

```
95% 片側上限 = 1 - 0.05^(1/N)
```

| N | 95% 上限 (miss=0 の場合) | 適用シーン |
|---|------------------------|-----------|
| 10 | ≈ 25.9% | 最低ライン (精度不足) |
| 15 | ≈ 18.1% | 実用範囲 |
| 20 | ≈ 13.9% | **推奨** |
| 30 | ≈ 9.5% | 高精度 |

### 推奨観測数

- **最低 10 回** で有意な観測とみなす
- **推奨 20 回以上** で 95% 上限 < 14% (= ほぼ信頼できる)
- miss が 1 件でも発生した場合は原因切り分けを優先する (上記 Step 1〜3)

### 実観測例 (t003, 2026-09-05)

`nonexistent_skill_pilot001〜015` (15 task) を順次投入:

```
到達 15 / 投入 15 = 到達率 100.0%
95% 片側上限 ≤ 18.1%  (miss=0, N=15)
全試行 4〜6s で到達 (dispatcher poll 1〜2 cycle 以内)
```

### 定期観測の推奨サイクル

crewvia の本番運用では以下のタイミングで観測を推奨する:

1. **重要な Dispatcher 関連 PR マージ後** — PR #144/149/150 相当の変更後に N=15 以上で検証
2. **ntfy / herdr 設定変更後** — 設定ミスによる silent failure を早期検出
3. **長期 session 開始前** — 観測用 mission を軽量に走らせる

---

## 5. 参照リンク

### 関連 memory ファイル

| ファイル名 | 内容 |
|-----------|------|
| `crewvia-dispatcher-notify-cache-pitfalls.md` | notify cache の切り分け手順 + tmux_send 握り潰し欠陥 |
| `session-20260905-notify-reliability-complete.md` | mission 20260905-dispatcher-notify-reliability 完了レポート (PR #149/#150) |
| `crewvia-dispatcher-director-only-bug.md` | director-only スキル filter バグ (PR #125 で修正) |
| `herdr-migration-phase0-handoff.md` | tmux → herdr 移行完了レポート (PR #130-#142) |

### 関連 PR

| PR | 概要 |
|----|------|
| **#144** | blocked ログ抑制 (NOTIFY_TTL ごとに 1 行に制限) |
| **#149** | dispatcher stdout/stderr を `logs/dispatcher/YYYYMMDD.log` に保存 |
| **#150** | herdr 通知未達バグ 3 件修正 — ❯ 待機ループ / 長文 Enter 保険 / assigned_task_ids タプル化 |

### 関連 knowledge ファイル

| ファイル | 内容 |
|----------|------|
| `knowledge/dispatcher-duplicate-notify-observation-20260902.md` | 2026-09-02 の重複通知観測ログ |
| `knowledge/dispatcher-blocked-by-analysis.md` | Dispatcher blocked_by 厳密化の調査レポート |
| `knowledge/qa-gate-design.md` | QA ゲート機構の設計 Spec |

---

## 6. よくある問題と対処

### 通知が届かないが cache に suppress 中エントリがある

```bash
# cache の全エントリとその age を確認
cat /tmp/dispatcher-notify-cache.json | python3 -c "
import json, sys, time
cache = json.load(sys.stdin)
now = time.time()
for k, ts in sorted(
    [(k, ts) for k, ts in cache.items() if isinstance(ts, (int, float))],
    key=lambda x: x[1], reverse=True
):
    age = int(now - ts)
    remaining = max(0, 300 - age)
    print(f'{k}: age={age}s, next_in={remaining}s')
" | head -20
```

TTL (300s) が明けるまで待つか、`/tmp/dispatcher-notify-cache.json` を削除して cache をリセットする。

### 2 mission 並走で片方の通知が届かない

PR #150 fix (c) 以降、`assigned_task_ids` は `(slug, task_id)` タプルで一意化されている。
fix (c) が適用されているか確認:

```bash
grep 'assigned_task_ids' scripts/dispatcher.sh
# 期待: assigned_task_ids.add((slug, task_id)) と (slug, meta['id']) in assigned_task_ids
```

**notify cache のキー名確認 (PR #144 導入の機能)**:

```bash
# cache キーの確認
cat /tmp/dispatcher-notify-cache.json | python3 -m json.tool | grep "no_worker"
```

期待形式: `"no_worker_<mission-slug>_<task-id>"` — PR #144 で導入されたキー名形式

### observe-notify.sh が「ログファイルが見つかりません」と表示する

```bash
# 今日のログファイルが存在するか確認
ls logs/dispatcher/dispatcher-$(date +%Y%m%d).log

# CREWVIA_REPO_ROOT が設定されているか確認 (worktree 内作業時に必要)
echo $CREWVIA_REPO_ROOT

# worktree 内からは CREWVIA_REPO_ROOT を設定して実行
CREWVIA_REPO_ROOT=/home/tkadmin/workspace/crewvia bash scripts/observe-notify.sh
```
