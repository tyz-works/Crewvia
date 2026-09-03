# Dispatcher 重複通知観測ログ (2026-09-02)

## 状況

Mission `20260902-crewvia-improvements` の Phase 1 起動中に、Dispatcher が **既に Worker が pull 済み task に対しても Worker 起動要求通知を送信する** 現象を観測。

## 観測タイムライン (2026-09-02)

| 時刻 | イベント |
|------|----------|
| 08:09 頃 | Director (Sora) が mission init + t001-t014 登録 |
| 08:12 頃 | Priya (planning) が t001 pull → done |
| 08:14 頃 | Omar/Yasmin/Jiwon/Tariq を起動 (Phase 1 用 4 Worker) |
| 08:14 頃 | plan.sh status で全 Phase 1 task が pull 完了 (Omar:t002, Yasmin:t003, Tariq:t004, Jiwon:t005) 確認 |
| 08:14+ | Dispatcher から連続 4 件の通知:<br>1. `要求スキル ['bash', 'research'] の Worker を起動してください (task t002)` — 最初の Worker 起動前<br>2. `要求スキル ['bash', 'research'] の Worker を起動してください (task t003)` — Yasmin pull 後<br>3. `要求スキル ['docs', 'research'] の Worker を起動してください (task t004)` — Tariq pull 後<br>4. `要求スキル ['research'] の Worker を起動してください (task t005)` — Jiwon pull 後 |

## 疑わしい原因 (仮説)

1. **Dispatcher の busy 判定不備**: pull 済み task を pending として扱っている
2. **NOTIFY_TTL の抑制対象**: 同一スキル通知の抑制が効いていない (task_id が異なるため別通知扱い)
3. **Pull 検知タイミング**: dispatcher poll (5秒) vs pull 完了の race condition

## 影響

- Director が新規 Worker 起動要求を誤って処理すると WIP 上限を超過する
- 実運用では前回 session でも Phase B v4 で同種の "director-only 通知" が発火した (in_progress 手動化で回避)

## 本 mission との関連

- **t002 (Dispatcher director-only)**: skill filter だけでなく busy Worker 判定の側面もあるかもしれない → Omar 調査時に本ファイル参照推奨
- **t003 (Dispatcher blocked_by)**: 直接関連は無いが、Dispatcher 全体の pending 判定ロジックを追う際の周辺情報
- **t004 (Worker shutdown ルール化)**: Worker の pull 完了検知と shutdown 判定が絡む

## Director のアクション

追加 Worker 起動要求 3 件 (t003/t004/t005) は **全て却下**。既存 Worker (Omar/Yasmin/Tariq/Jiwon) の調査完了を待つ。

