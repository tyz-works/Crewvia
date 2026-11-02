# review skill の card が CI 完走待ちで watchdog の max に殺される事故 (t002 / 20261102-review-ci-timeout)

## 事故

review Worker が PR の CI 完走を待っている最中に、watchdog の絶対上限 (`max_threshold`) で
terminate される。レビュー作業自体は完遂していても merge 直前で殺され、task は pending に戻り、
別 Worker がやり直す（無駄な再試行）。

- 2026-09-13 (Ren / Erik): 1 時間の上限で終了。当時は通知面だけ直した
  (watchdog の timeout 終了を Director に 1 回だけ伝える。`knowledge/daemon-authority.md`
  「watchdog の timeout 終了が Director に届かない」節)。根治はされなかった。
- 2026-11-02 (Hugo, mission 20261030-leftover-cleanup t010): PR #334 の CI 完走待ち中、
  経過 3603s で terminate。pytest の実測所要は 1h6m15s (3975s) — `scripts/watchdog.py` の
  `PROFILES['feature_impl']['max']` (既定 3600s) を 375s 超えていた。**再発**。

## 判断材料 (t001 / 20261102-review-ci-timeout で確認した事実)

- `scripts/watchdog.py:850-857` の絶対上限 (`max_threshold`) は idle/executing 判定から独立した
  「最後の安全弁」として設計されている (PR #204 / t016, 2026-09-21)。`knowledge/watchdog-idle-judgment.md`
  §6 も「max_exceeded はプロセス層で抑制しない。最後の網として残す」と明記。**この判断は今も有効**
  — グローバル既定 (`PROFILES['feature_impl']['max']`) を上げる案は、review 以外の全 task でも
  この安全弁を弱めるため却下した。
- `worker_profile:` (watchdog.py:257) は実装はあるが実運用で使われていない拡張点 (0 件。
  `knowledge/watchdog-idle-judgment.md:218`)。`config/timeout-profiles.yaml` が意図していた
  9-profile + skills 自動マッピングは watchdog.py が読んでいない。この統合は別の設計判断
  (quick_edit の idle 短縮が既定より厳しくなる等) が要るため本件のスコープ外。
- `CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS` (既定 4500s = 75分。CI 待ちが収まる長さ、t004) と
  watchdog の `max` (既定 3600s) の非対称は意図的かつ既知 (`knowledge/watchdog-idle-judgment.md` §12
  「残る穴 (承知の上)」)。だが watchdog 側の値をどうするかの判断は保留のままだった。本件はその
  「残る穴」が実際に踏み抜かれた例。
- `plan.sh update` には `--max-timeout` / `--idle-timeout` が無く、既存 card の上限変更は
  frontmatter 直編集に依存していた。flow 形式 `timeout: {max: 7200}` は `lib_task_cards.parse_yaml`
  が丸ごと 1 本の文字列として読むため、watchdog.py の `timeout.get("max")` が `AttributeError` に
  なる罠がある (block 形式・2 スペースのみ有効)。

## 選んだ対応 (案B。Director 承認 2026-11-02)

**review skill の card に、既定で `timeout.max` を自動付与する。** `config/crewvia.yaml` の
`model_per_skill:` と同じパターンで、`plan.sh add` が `--max-timeout` を明示しない card に
skill 一致で既定値を書き込む。明示値は上書きしない。

却下した案:
- **A (feature_impl.max をグローバルに 4500s に)**: 上の「最後の安全弁」を review 以外の
  全 task にも広げて弱める。却下。
- **C (worker_profile を活かす)**: PROFILES はモジュール定数なので新しい profile を足すなら
  A と同じ daemon restart が要る。review が特定の profile (例: research) を流用する結びつきも
  文書にも card にも残らず保守リスクが大きい。B の下位互換で利点が無い。却下。
- **D (CI 待ちを Worker の中でやめ、needs-director → CI 緑後に再 pull)**: `knowledge/ci-status.md`
  の「CI の記録は判定に使わない」原則と衝突し、dispatcher に新しい再割当て経路が要る。
  Rule 5 §12/§13 の「review Worker は CI 待ちの間 in_progress のまま」という前提全体の作り直しに
  なり、本 mission のスコープを超える。今回は却下（必要なら別 mission）。

**B を選んだ決め手**: card の `timeout:` は watchdog が監視を始める時 (新しく pull された task を
`WorkerMonitor` が作られるとき) に読むので、この既定値の変更は **watchdog プロセスの daemon
restart が不要**（A/C は `PROFILES` というモジュール定数に触れるため restart が要る）。
ただし「既に監視中の task に即反映される」という意味ではない — 下の「`plan.sh update
--idle-timeout <s> --max-timeout <s>`」節を参照。

## 実装 (t002)

### timeout の正本は 3 か所 (混同しないこと)

| # | ファイル | 役割 | 変更に daemon restart が要るか |
|---|---|---|---|
| 1 | `scripts/watchdog.py` の `PROFILES` | 実際に terminate を判定する値 | **要る** (モジュール定数、import 時束縛) |
| 2 | `config/timeout-profiles.yaml` | `lint_plan.py` / `run_verification_checks.py` が読む境界チェック専用。watchdog は読まない | 要らない (lint は毎回読み直す) |
| 3 | `config/crewvia.yaml` の `timeout_max_per_skill` | `plan.sh add` が card の `timeout.max` を自動で埋める既定値。card に書き込まれた後は普通の card.timeout として #1 が解決する | 要らない |

今回の変更は #3 だけを足した。#1 (`PROFILES`) には一切触れていない — 触ると「restart 不要」という
B の利点が消える。#2 は review の既存コメントの意味(単体作業時間の実測)が誤解されないよう追記しただけ。

**#3 の既定値 7200 が #2 の lint 集計境界に収まることを確認済み**: `lint_plan.py` の
`check_timeout_validity()` は `config/timeout-profiles.yaml` の全 profile の idle/max から
`[min, max]` の集計境界を作り (:292-299)、card の `timeout.max` がその外なら WARN を出す
(FAIL にはしない。`--strict` のときだけ FAIL に昇格)。現行の profile 群は `max` が
600〜14400 の範囲 (`ops_long` が最大)。7200 はこの範囲内なので、新しい既定値付きで作られた
card を lint が落とすことはない。

### 解決ロジック (`scripts/plan.sh` の `_default_timeout_max_for_skills()`)

`config/crewvia.yaml` の `timeout_max_per_skill:` を `lib_task_cards.parse_yaml`（narrow subset パーサー、
`lib_history.load_default_max_attempts` と同じ読み方）で読む。**意図して 1 段の flat map
(`skill: 整数秒`) に留めている** — このパーサーは 2 段ネストした block map
(`skill:\n    max: N`) の内側を黙って読み飛ばすため、2 段ネストにすると値が静かに失われる
(`timeout_per_skill: {review: {max: 7200}}` のような形は**使わないこと**)。idle の既定が要るときは
`timeout_idle_per_skill:` のような別の flat map を同じパターンで足すこと。

複数 skill が異なる値にマッチしたら最も緩い (大きい) 方を採る
(`config/timeout-profiles.yaml` 末尾の既存の解決規則を踏襲)。config が読めない・mapping が無い・
一致する skill が無ければ None (何もしない。Worker 起動は絶対に止めない)。

`plan.sh add` は `--max-timeout` が明示されたときだけそれを使い、明示が無ければこの既定値で埋める
(どちらも無ければ何もせず、watchdog.py の `DEFAULT_PROFILE = feature_impl` (3600s) のまま)。

### `plan.sh update --idle-timeout <s> --max-timeout <s>` (新設)

既存 card の `timeout.idle` / `timeout.max` を書き換える、frontmatter 直編集に依らない唯一の経路。
既存の timeout dict とマージ (片方だけ渡せばもう片方は保たれる)。壊れた値 (flow 形式が文字列として
読まれた等) を持つ card でも、指定した方は正しい block 形式で書き直される
(`lib_state_store.dump_yaml` は dict を常に block 形式でしか出力しない)。

**監視中 (in_progress) の task には即効かない。** `scripts/watchdog.py` の `WorkerMonitor.__init__`
は `max_threshold` / `idle_threshold` を監視を始めた時点で card から 1 回だけ読んで束縛する
(watchdog.py:264-265)。既存の monitor がある task は、以後の cycle で card を読み直すのは
`refresh_card_pr_number()` (PR 番号の照合) だけで、timeout は読み直さない (watchdog.py:1812)。
「card の timeout は毎 cycle 読まれる」が成り立つのは **新しく監視が始まる瞬間** (`pull` で
再度 in_progress になり新しい `WorkerMonitor` が作られたとき) だけで、今まさに動いている Worker
の上限をこの update でその場で引き上げることはできない。daemon restart が要らないのは
別の話 (`PROFILES` というモジュール定数を触っていないので watchdog プロセス自体の再起動は
不要という意味) で、「今動いている監視に即反映される」という意味ではない。混同しないこと。

## 本 mission 自身の t003 (この変更の review task) について

t003 は t002 の PR の CI 完走待ちという、まさに同じ事故に当たる構成。Director が応急処置として
frontmatter に `timeout.max: 7200` を手で入れ済み。**この変更後も t003 には触れていない**
(`plan.sh add` の既定値の自動付与は add 時点の 1 回だけで、既存 card を遡及的に書き換えない。
t003 は既に明示値を持っているので、新しい既定値の有無に関わらず動作は変わらない)。

## 暫定運用 (merge まで。すでに不要)

t002 の merge 前は、他 mission の review / 長時間 task が同じ事故に遭いうるため、
`plan.sh add --skills review ... --max-timeout 7200` を明示する運用で凌いだ。
**この PR の merge + `scripts/sync-main-checkout.sh` 反映後は不要** — 新規
`plan.sh add --skills review ...` が自動で `timeout.max=7200` を持つ。

## 残る注意点

- 7200s は現状の実測 (pytest 3975s) に対する安全率 約1.8。CI がさらに長くなったら再検討が要る。
- `worker_profile` と `config/timeout-profiles.yaml` の 9-profile 統合は本件のスコープ外のまま
  (quick_edit の idle 短縮が既定より厳しくなる等、別の設計判断が要る)。
- `started_at` が監視開始時刻であり watchdog 再起動で max タイマーがリセットされる件
  (`knowledge/daemon-authority.md` §4-3) は別件として backlog のまま (本件では扱っていない)。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。`config/crewvia.yaml` の `timeout_max_per_skill:` と
`plan.sh update` の `--idle-timeout` / `--max-timeout` は revert で消える。revert 後に作られた card は
再び既定 (feature_impl 3600s) に戻る。revert 前に自動付与された `timeout.max: 7200` を持つ既存 card は
そのまま残ってよい (旧コードもこの欄を普通に読む。card が壊れるわけではない)。
