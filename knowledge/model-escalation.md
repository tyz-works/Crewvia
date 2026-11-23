# モデルの段上げ（品質不足で差し戻した task を 1 段上のモデルで走らせる）— 設計

作成日: 2026-11-24。状態: 設計。**PR-1（記録）は実装済み**（mission `20261124-model-escalation-impl` t001。§7・§12 に実装メモ）。PR-2 / PR-3 は未実装。mission `20261124-model-escalation-design` t001。

- 根拠のコードは **main `e3b2951`**（#433）の行番号。`scripts/plan.sh:N` のように書く。確かめていないことは「未確認」と書く。
- 前提: モデル指定を版なしの別名（`opus` / `sonnet` / `haiku`）だけにする mission `20261124-model-alias-only` が進行中（この文書を書いた時点で t001 実装中・未 merge）。**実装 PR はその merge 後の main に載せる**。codex-review はこのマシンの local（`config/crewvia.local.yaml`）で `haiku`。
- この PR は文書だけ。コードは変えていない。
- 書き方は `knowledge/vnext-02-attempt-reopen-checkpoint.md`・`knowledge/machine-local-config.md` に揃える（決定・不変条件・書く順・壊れた入力・テスト・戻し方）。初版で未決にしていた 8 点は、2026-11-24 にユーザーが決めた（§11 に選んだ案を残し、本文に反映済み）。
- 改訂: 2026-11-24 第 2 版 = §11 の決定の反映 + レビュー（t002）の所見（P2-1 intent の版・P2-2 `model.json` が読めない Worker の退役・P3 ×3）の修正。

## 0. 要約

| 項目 | 決めたこと |
|---|---|
| 引き金 | **実装 task の `plan.sh reopen` は既定で上げる**（review の要修正・QA の FAIL・Codex の指摘の後に Director が打つ唯一の経路だから。§2.4）。起点が review / qa / codex-review の task（`CASCADE_REVIEW_SKILLS`）の reopen は上げない。**検証の差し戻し（`verify-result fail`）も上げる**（§11-1 = a）。`retry` は既定で上げず、`--escalate-model` のときだけ上げる（今の遷移元はどれも品質の判定ではない）。`update --reset` は上げない。どのコマンドでも `--escalate-model` / `--no-escalate` で明示できる |
| 段 | `haiku → sonnet → opus` の 1 段ずつ。opus は上がらない（上限。そのまま）。下げることはしない |
| 範囲 | **上げたモデルはその task の card の欄 `model_override` にだけ載る**。cascade で一緒に戻る review / QA の task には付けない。次の task は skill の既定のまま |
| 前の試行のモデル | 今は記録が無い。**start.sh が Worker ごとの記録 `registry/workers/<Name>/model.json` を書き**、pull がそれを読んで card の `execution_model`・record の `model`・台帳の `model` に写す |
| Worker との束ね | dispatcher は Worker を起動しない（Director に起動コマンドを渡す）。段上げした task には **「その task 専用」の Worker**（`CREWVIA_BIND_TASK=<slug>/<tid>` で起動・記録に `bound_task`）だけが割り当てられ、その Worker は他の task を取らない。判定は新しい lib の 1 関数を dispatcher と pull が共有する（TARGET_DIR の記録 `lib_worker_target` と同じ形） |
| 優先順位 | env `CREWVIA_WORKER_MODEL`（人の明示）> `model_override`（段上げ）> `model_per_skill` > `worker_model` > CLI の既定 |
| 回数 | 段上げで数は増やさない。総試行数の上限（`attempts.max_total`・既定 6）がそのまま効く。`max_review_cycles` は plan review の巡の数で、この機能とは関係ない。**opus での差し戻しが 2 回目になったら Director に 1 回通知**（§11-3 = b・§4.6） |
| 壊れた入力 | card の欄が読めない・知らないモデル名 → **その task は割り当てない（保留）+ Director に 1 回通知**。前の試行のモデルが分からない → skill の既定から 1 段上げる。Worker の `model.json` が読めない → 退役させず待機（§4.3 の 5） |
| PR | 3 本。PR-1 記録だけ（挙動は変わらない）→ PR-2 欄と束ね（手で欄を付けたときだけ効く）→ PR-3 自動の段上げ（reopen / verify-result fail / `retry --escalate-model`・reopen の intent は版 2） |

## 1. 背景とユーザー決定（2026-11-24）

安いモデルで始めて、品質が足りなかったときだけ上げたい。今は Director が起動のたびに `CREWVIA_WORKER_MODEL=opus` を前に付けて手で上げている（この task の Worker もそう起動された）。手でやると忘れる・上げたまま次の task も高いモデルで回す・誰がどのモデルで走ったか残らない。

決定 3 点（task card の本文から写した）:

- **(A) 自動**: review の要修正・QA の FAIL・Codex の指摘で実装 task を差し戻したら、Director がフラグを付けなくても 1 段上げる。
- **(B) 段**: haiku → sonnet → opus の 1 段ずつ。上限は opus（opus の task はそのまま）。
- **(C) 範囲**: 上げたモデルはその task だけに効く（次の task は skill の既定）。

## 2. 現状の確認（e3b2951）

### 2.1 モデルの決まり方

- `scripts/start.sh:28-29` が config を読む**前に**「env `CREWVIA_WORKER_MODEL` が人の手で設定されていたか」を `WORKER_MODEL_EXPLICIT` に控える（`knowledge/model-per-skill.md`「`WORKER_MODEL_EXPLICIT` フラグ」）。
- `start.sh:650-669`: Director は `CREWVIA_DIRECTOR_MODEL`。Worker は 明示 env > `_resolve_worker_model`（`start.sh:107-115` → `scripts/lib_model.py resolve`）。空なら `--model` を付けない。
- `lib_model.py` の `_model_rank`（`opus`=3 / `sonnet`=2 / `haiku`=1。**部分文字列で見る**ので別名でも版つきでも同じに動く。知らない ID は 2 と警告）。複数 skill なら最も高いランク。
- 解決したモデルは `LAUNCH_CMD`（`start.sh:914`）の `--model` に入るだけで、**env にも registry にも残らない**（`ENV_EXPORTS`・`start.sh:832` 付近に無い）。`plan.sh` / `lib_registry.py` / `watchdog.py` / `lib_retirement.py` に `model` の出現は 0（grep で確認）。

### 2.2 Worker は長命で、起動時にはどの task 用か決まっていない

- dispatcher は Worker を**起動しない**。idle の Worker のうち skills ⊇ task の skills で、TARGET_DIR の記録が合う者に `plan pull --task <tid> --mission <slug>` を送る（`scripts/dispatcher.sh:2908-2958` 付近）。1 人の Worker が task を次々に取る。
- 担当できる Worker が居ない task は、Director に起動コマンドを通知する（`dispatcher.sh:3081-3125` 付近の `no_worker_<slug>_<tid>`・コマンドは `worker_start_command`（`dispatcher.sh:1008`）。同じ skills の Worker が生きていれば `--fresh`）。Director がそれを貼って起動する。
- 起動後の Worker は kickoff の指示どおり `plan pull --skills …` で自分で取る（この Worker もそう）。
- 割り当ての判定に「Worker の性質」を足した前例が **TARGET_DIR の記録**（`scripts/lib_worker_target.py`）: start.sh が起動時に `registry/workers/<Name>/target_dir.json` を書き（`start.sh:966`）、dispatcher（`worker_may_take_task`）と plan.sh pull（`--task` の照合と自動の絞り込み）が同じ判定 `worker_may_take()` を使う。記録の無い起動済み Worker を壊さない向きの表もそこにある。**この設計はその形を真似る**。
- 何もしない Worker の退役: 取れる task（`takeable_pending`、`dispatcher.sh:2984`）が空なら退役する。

### 2.3 試行の記録にモデルが無い

- card の試行の欄 `EXECUTION_FIELDS`（`scripts/lib_execution.py:85`）・record の形（`record_shape_problem`、`lib_execution.py:399`）・台帳の欄（`lib_history.build_event`、`scripts/lib_history.py:261`）のどれにもモデルが無い。
- record の `git` は start（`lib_task_controller.start_execution`、`scripts/lib_task_controller.py:695`）が渡したときだけ入る。モデルも同じ点（pull の 2 つ目のロック）で入れられる。

### 2.4 「差し戻し」は実際には何で起きているか

| 所見 | 下流の task に起きること | 実装 task に起きること |
|---|---|---|
| review の要修正 | review task は `approved_head:` 行なしで **done**（`agents/worker.md` の review の手順） | done のまま |
| QA の FAIL | QA task は `needs-director`（必須の checkpoint を満たせないと done が断られる。`skills/crewvia-qa/SKILL.md` §PASS/FAIL）か、承認なしで done | done のまま |
| Codex の指摘 | kai-review.sh が codex-review task を `needs-director`（`scripts/kai-review.sh:67`・`:216`） | done のまま |

どの場合も実装 task を戻すのは **Director の `plan.sh reopen <impl> --reason … --branch reuse --cascade reviews`**（`agents/director.md:326`）だけ。下流の所見を機械が読み取って実装 task を戻す経路は無い。だから (A) の「自動」は **reopen そのものに段上げを載せる**ことで満たす（所見の文面を読んで判定する経路は作らない。§3.1）。

### 2.5 retry の遷移元と終わり方

- `retry` の遷移元は `lib_task_status.ACCEPTS_FROM['retry']` = failed / needs_director / needs_human_review / verification_failed（needs_human_review は試行が running のまま残るので実際は `ATTEMPT_ACTIVE`。`agents/director.md:324`）。
- `verify-result fail`（< `max_rework`）は **retry を通らない**: 試行を `VERIFICATION_REJECTED` で閉じて task を直接 pending に戻す（`scripts/CLAUDE.md`「報告の 6 コマンド」）。だから retry の遷移元に `VERIFICATION_REJECTED` の試行は来ない。
- `verification_failed` の status を書くコードは今は無い（`grep "verification_failed'"` は表示用の表だけ。語彙と `ACCEPTS_FROM['retry']` に残っている）。
- 閉じた試行の終わり方は card の `execution_end_code`（固定コード。`lib_execution.py:52-60`）: `WORKER_FAILED`（Worker が `fail`）/ `NEEDS_DIRECTOR`（Worker・kai-review の `needs-director`）/ `VERIFICATION_REJECTED`（`verify-result fail`）/ `RETIRED`（watchdog の退役 = 停止・tab 消失・timeout）/ `WORKSPACE_CREATE_FAILED` / `RESET_BY_DIRECTOR` / `ABANDONED_OUTSIDE_CONTROLLER`。

### 2.6 上限

- 総試行数: card の `max_attempts` → `config/crewvia.yaml` の `attempts.max_total` → 6（`lib_history.max_attempts_of`）。retry / reopen の入口だけで検査（`ATTEMPT_CAP`）。
- `max_review_cycles`（`plan.sh:2977`、mission.yaml）は **Director の plan review の巡の数**。実装 task の差し戻しとは別物。
- `max_rework`（既定 3）は verify-result の差し戻しの数。

## 3. 用語

| 語 | 意味 |
|---|---|
| 段 | `haiku` < `sonnet` < `opus`。ランクは `lib_model._model_rank` の 1 か所で決める（部分文字列。新しい定義を作らない） |
| 既定モデル | task の skills から `model_per_skill` → `worker_model` で決まるモデル（今の `lib_model.resolve`） |
| `model_override` | card の新しい欄。段上げで決めたモデルの別名。**その task の以後の全試行に効く**（§4.4） |
| 実効モデル | その task を走らせるべきモデル = `model_override` があればそれ、無ければ既定モデル（env の明示は Worker 側の事情なので含めない。§5） |
| `execution_model` | card の新しい欄。**今の試行を実際に走らせた Worker のモデル**（pull が Worker の記録から写す） |
| 束ねた Worker | `CREWVIA_BIND_TASK=<slug>/<tid>` で起動され、記録に `bound_task` を持つ Worker。その task しか取らない |

## 4. 決定

### 4.1 引き金（(1)）

| コマンド | 既定 | 理由 |
|---|---|---|
| `plan.sh reopen <I>`（I が実装 task） | **上げる**（起点 I だけ） | §2.4 のとおり、review / QA / Codex の所見で実装 task を戻す経路はこれだけ。reopen の遷移元は done / verified / skipped で、「終わったと思った結果が足りなかった」以外に打つ理由が無い |
| `plan.sh reopen <I>`（I の skills が `lib_dep_rules.CASCADE_REVIEW_SKILLS` = review / qa / codex-review のどれかを含む） | **上げない**（`--escalate-model` でも上げない。警告 `MODEL_ESCALATION_NOT_APPLICABLE`） | **段上げは実装 task だけ**。review / QA を戻すのは「見直してほしい」であって、その task の出来が品質不足と判定されたのではない（判定したのは自分自身）。集合は `CASCADE_REVIEW_SKILLS` を import する（新しい集合を作らない） |
| reopen の cascade で戻る下流（review / qa / codex-review・経路の中間） | **上げない** | 古い実装の上の作業だから戻すだけで、その task の品質が足りなかったわけではない（§11-8 = a） |
| `plan.sh verify-result fail`（< `max_rework`。task は直接 pending に戻る） | **上げる**（§11-1 = a） | 検証者が出来を断った = 品質由来で、固定コード `VERIFICATION_REJECTED` で言い切れる唯一の終わり方。Controller の `fail_execution(VERIFICATION_REJECTED)` の **同じ書き込み**で `model_override` を付ける（card の pending と別の書き込みにしない）。`--no-escalate` で止められる |
| `plan.sh retry`（`WORKER_FAILED` / `NEEDS_DIRECTOR` / `RETIRED` / `WORKSPACE_CREATE_FAILED` / `RESET_BY_DIRECTOR` / `ABANDONED_OUTSIDE_CONTROLLER` / end_code なし。status `verification_failed` は今は書き手が無い） | **上げない**。`--escalate-model` のときだけ上げる（§11-1 = a） | Worker の停止・tab 消失・timeout・環境の失敗・Director の操作。品質の判定ではない。`NEEDS_DIRECTOR` は「Worker が詰まって判断を仰いだ」と「kai-review の NEEDS FIX」の両方を含み、終わり方だけでは分けられないので上げない側に倒す（Codex の指摘の正しい出口は実装 task の reopen = 上がる。codex-review task 自体の retry は上げない = §11-2 = a） |
| `plan.sh update --status pending --reset` | **上げない**（既存の `model_override` は消さない） | 理由の要らない差し戻し・壊れた card の出口。意味を持たせない |
| どのコマンドでも `--escalate-model` | 上げる | 終わり方で判別できない品質由来（Worker が自分で `fail` した等）を Director が明示する |
| どのコマンドでも `--no-escalate` | 上げない | 指摘が軽い・モデルのせいでないと Director が判断したとき |

- `--escalate-model` と `--no-escalate` は同時に渡せない（UsageExit・何も書かない）。
- **判別は固定コード（`execution_end_code`）だけで行う**。Result・理由・needs_director_reason の文面は読まない（文面の判定は `lib_verdict` の経緯のとおり、否定形と場所の無限集合に負ける）。
- codex-review の task（skills に `codex-review`）は kai-review.sh が走らせ、Claude Worker のモデルを使わない。**この task には `model_override` を書かない**（フラグで明示されても `MODEL_ESCALATION_NOT_APPLICABLE` で警告して書かない。reopen / retry 自体は止めない）。上の `CASCADE_REVIEW_SKILLS` の行に含まれる。
- 「実装 task か」の判定は 1 関数 `lib_model.escalation_applicable(skills)`（= skills が `CASCADE_REVIEW_SKILLS` と交わらない）。reopen・verify-result fail・retry の 3 つの入口が全部これを通す。

### 4.2 前の試行のモデルの知り方（(2)）

**書く場所と順番:**

1. **start.sh** が Worker の起動時、`SELECTED_MODEL` が決まった後（`start.sh:669` の後・spawn の前）に `registry/workers/<Name>/model.json` を書く:
   ```json
   {"agent": "Minjun", "model": "opus", "source": "env", "bound_task": null, "written_at": 1795000000}
   ```
   - `model`: `SELECTED_MODEL`（空 = `--model` なし = CLI の既定は `null`）。
   - `source`: `env`（`WORKER_MODEL_EXPLICIT=1`）/ `override`（§4.3 の束ねた起動で card の `model_override` を使った）/ `skill`（`model_per_skill` か `worker_model`）/ `none`（解決が空）。
   - `bound_task`: `"<slug>/<tid>"` か `null`（§4.3）。
   - 書き手は start.sh だけ（CLI は新しい lib。§4.3）。原子的に書く（`lib_state_store.atomic_write_text`）。置き場は TARGET_DIR の記録と同じ dir で、**`registry/mux/*.json`（kill の認可の証拠）には相乗りしない**（`lib_worker_target.py` 冒頭と同じ理由）。
2. **pull** がロック 1 の前に自分（`--agent`）の `model.json` を読み（`lib_daemon_state.load_json_store`。`Unreadable` を `None` に潰さない）、ロック 2 の `start_execution(…, model=…)` に渡す。Controller は card の `execution_model` と record の `model` に書く（`git` と同じ「start が渡したときだけ」）。記録が無い・読めない Worker（PR-1 の前に起動した Worker を含む）は**欄を書かない**（`unknown` のような値を作らない）。
3. **台帳**: 試行を閉じる遷移（end / reset / retire）と retry / reopen の台帳に `model`（閉じた試行の `execution_model`。無ければ欄なし）を足す（`lib_history.build_event` の引数を 1 つ増やす）。`plan.sh history` と pull の `previous_attempt` に出る。

**env ではなく記録ファイルにする理由**: dispatcher も同じ値を読む必要がある（§4.3 の割り当て）。読み口を 1 つにする。Worker のセッションの中で人が `/model` で切り替えた分は観測できない（記録は起動時の値。限界として §12 に残す）。

**段上げの基準（「1 段上」の起点）** — 1 関数 `lib_model.escalate(base, override)` に置く:

1. 閉じた試行の `execution_model` のランクが分かれば、それを起点にする。
2. 無ければ card の今の `model_override`。
3. それも無ければ既定モデル（`lib_model.resolve(config, task の skills)`）。
4. 新しい値 = 起点 + 1 段（opus で止まる）。**既にある `model_override` より下げない**（`max(既存, 起点 + 1)`）。

起点に env の明示（`source: env`）の試行が来ることがある（人が opus で走らせた試行が差し戻された等）。そのときもその値を起点にする（opus なら上がらない）。

### 4.3 Worker の起動とモデルの束ね方（(3)）

**流れ（段上げした task が走るまで）:**

1. Director が `plan.sh reopen <I> …` → I の card に `model_override: opus`（§4.1・§4.2）。I は pending（依存の無い実装 task ならすぐ取れる状態）。
2. dispatcher: I は「実効モデルが既定と違う task」。**束ねた Worker（`bound_task == <slug>/I`）以外には割り当てない**。生きていなければ既存の `no_worker` 通知で Director に起動コマンドを渡す:
   ```
   cd <repo> && AGENT_NAME=$(bash scripts/assign-name.sh <skills> --fresh --exclude <同じ skills の生きている名前>) \
     CREWVIA_BIND_TASK=<slug>/<tid> CREWVIA_MUX_ENABLED=1 CREWVIA_MUX=<backend> bash scripts/start.sh worker <skills>
   ```
   - `CREWVIA_WORKER_MODEL` は**付けない**（付けると「人の明示」と区別できなくなる。§5）。モデルは start.sh が card から取る。
   - 同じ skills の 2 人目は `--fresh` だけだとプール枯渇で空になりうる（memory `parallel-same-skill-worker-needs-exclude`）ので `--exclude` を並べる（`worker_start_command` を直す）。
   - 通知の文面に「モデル: sonnet → opus（段上げ）」を足す。
3. start.sh: `CREWVIA_BIND_TASK` があれば、値を `check_mission_slug` / `check_task_id`（`lib_git_policy`）で検査し、`plan.sh model-for <tid> --mission <slug>`（**新しい読み取り専用のサブコマンド**。ロックなし。実効モデルを 1 行で出す。決められなければ exit 1・stdout 空）でモデルを得る。明示 env があればそちらが勝つ（§5。下がる向きなら警告を 1 行）。`model.json` に `bound_task` を書く。kickoff の指示を `plan pull --task <tid> --mission <slug>` にする。
4. pull・dispatcher の判定（**新しい lib `scripts/lib_worker_model.py` の `worker_may_take(record, task_meta, slug)` の 1 関数**。`lib_worker_target.worker_may_take` と並べて両方を AND で使う。dispatcher の `worker_may_take_task`・`takeable_pending`・`can_handle` と、plan.sh pull の `--task` の照合・自動の絞り込みが同じ関数を呼ぶ）:

| task の `model_override` | Worker の記録 | 結果 |
|---|---|---|
| 無い | 無い（`is_missing`） | **取れる**（従来どおり。PR-1 より前に起動した Worker を止めない） |
| 無い | 読めない（missing でない `Unreadable`） | **取らない**（保留。束ねられているか分からない。§4.8。退役もさせない = 5） |
| 無い | `bound_task: null` | 取れる |
| 無い | `bound_task` が別の task | **取らない**（束ねた Worker は他の task を取らない） |
| ある | `bound_task` == この task | 取れる |
| ある | `bound_task: null` かつ `source: env` かつ ランク ≥ override | 取れる（人が明示して高いモデルで起動した Worker。§5） |
| ある | 上記以外（無い・読めない・`bound_task: null` で skill の既定・ランク不足・別の task に束ねられている） | **取らない**（保留に倒す） |
| 読めない・知らないモデル名 | 何でも | **取らない**（§4.8） |

5. **退役（dispatcher の idle Worker の判定）**。`takeable_pending` は `lib_worker_target.worker_may_take` と `lib_worker_model.worker_may_take` の AND で絞るので、新しい退役の経路は足さないが、**「取れない」が観測できなかったせいのときは退役させない**（今の TARGET_DIR の `_rec_known` と同じ向き。`scripts/dispatcher.sh:2990`）:

   | Worker の `model.json` | 残っている task | 結果 |
   |---|---|---|
   | 読めた | `takeable_pending` が空（束ねた Worker が自分の task を手放した・override の task しか残っていない等） | **退役**（今の規則のまま。束ねた Worker は done / fail / needs-director の後にここで窓が閉じる） |
   | **無い**（`is_missing`。PR-1 より前に起動した Worker） | override の無い task は取れる。override の task しか残っていない | **退役でよい**。表（上）のとおり「無い」Worker は override の task を**永久に**取れない（記録は起動時にしか書かれない）。待つ理由が無い = TARGET_DIR の C3 / t009 と同じ |
   | **読めない**（missing でない `Unreadable`。権限・壊れた JSON・形の違い） | `takeable_pending` が空 | **退役させず待機**（ログに 1 回。キー `model_rec_unknown_<Name>`）。束ねられているかも分からない = 取れないと確定していない。観測できなかったものを根拠に破壊しない |

   - 実装は今の `_rec_known` を「TARGET_DIR の記録が分かる **かつ** `model.json` が missing か読めた」に広げる（`_rec_known = not is_unreadable(target_rec) and (is_missing(model_rec) or not is_unreadable(model_rec))`。`is_missing` を先に見る。memory `load-json-store-missing-is-also-unreadable`）。2 つの記録の判定を 1 か所の式に置き、分岐を増やさない。
   - 同じ task が reopen で戻ってきたら、まだ生きていれば同じ Worker がそのまま取る（`bound_task` が一致する）。
   - **束ねた起動コマンドを 2 回貼ったとき**（同じ `CREWVIA_BIND_TASK` の Worker が 2 人）: 先に pull した方が取り、もう 1 人は `takeable_pending` が空なので上の 1 行目で退役するだけ（task を二重に取ることは pull の CAS が既に防ぐ）。特別な検査は足さない。
6. pull の `--task` の拒否は TARGET_DIR と同じく exit 3（`PRECONDITION_UNMET`）・何も書かない・出口をメッセージに出す（「この task は opus に段上げ済みです。`CREWVIA_BIND_TASK=… bash scripts/start.sh …` で起動した Worker が取ります」）。

起動は今の運用どおり **Director が通知のコマンドを貼る**（§11-7 = a。dispatcher は起動しない）。

**WIP との関係**: 束ねた Worker も 1 人に数える。`wip_limit`（`config/crewvia.yaml`・既定 8）は Director の判断の目安で、機械では数えていない（`start.sh:45-67` が env に出すだけ）。段上げの Worker の起動が WIP を超えるときの扱いは Director の今の運用（`agents/director.md` §7 の例外）に従う。機械の制限は足さない。

**採らなかった案:**

- **起動コマンドに `CREWVIA_WORKER_MODEL=opus` を付けるだけ**: 束ねが無いので、(i) opus の Worker が次の docs task も取り続ける（(C) に反する）、(ii) 既定モデルの idle の Worker が先に段上げした task を取る。どちらも起きる。
- **dispatcher が Worker を起動する**: 今は Director の仕事（承認・WIP の判断）。範囲が大きく別の設計。
- **生きている Worker のモデルを task ごとに切り替える（`/model` を送る）**: 送った結果を観測できず（画面の文言に依存）、失敗したら古いモデルのまま走る。
- **Worker ではなく task 側だけで照合する（Worker の記録の `model` == 実効モデル）**: 束ねより簡単だが、人が env で opus にした汎用の Worker が既定 sonnet の task を取れなくなり、今の運用（この task のように env で上げて起動する）を壊す。

### 4.4 card の欄（(4)）

| 欄 | 形 | 書き手 | 読み手 |
|---|---|---|---|
| `model_override` | `haiku` / `sonnet` / `opus` の**別名だけ**（小文字・完全一致。版つき ID は書かない） | Controller の `_write_reopen`（retry / reopen。引数 `escalate_to`）・`fail_execution(VERIFICATION_REJECTED)`（verify-result fail。同じ引数）と、plan.sh の `update --model-override <別名>|none`（Director の手。既存の `save_task` の経路） | dispatcher・plan.sh（pull / status / model-for / store-check / lint）・task-graph |
| `execution_model` | 文字列（Worker の記録の `model` をそのまま。版つきも有り得る） | Controller の `start_execution`（pull のロック 2） | status・`previous_attempt`・段上げの起点（§4.2） |

- 並び（`lib_state_store.TASK_META_KEY_ORDER`、`scripts/lib_state_store.py:520`）: `execution_model` は `*_ex.EXECUTION_FIELDS` の直後、`model_override` は `max_attempts` の後。**`EXECUTION_FIELDS` には入れない**（`fields_problem` / `attempt_view` / store-check の試行の検査に波及させない。欄が無い card が今と同じに読めることを保つ）。
- `model_override` は **task が終わっても消さない**（(C) の「その task だけ」は「他の task に効かない」の意味。同じ task が後で reopen されたら opus から始まる）。消すのは Director の `update --model-override none` だけ。
- Worker は書かない（hooks の task ファイル書き込みガードは今のまま）。
- `plan.sh add` は `model_override` を受け付けない（最初から上げたいなら skills か env で。段上げの意味を保つ。§11-5 = a）。`update --model-override` は Director の手で使える。
- `model_at_cap_count`（整数・既定 0 = 欄なし）・`model_at_cap_execution`（`ex-<32hex>`。数が 2 になった差し戻しで閉じた試行）: §4.6 の通知のため。書き手は `model_override` と同じ Controller の書き込みだけ。`update --model-override none` で両方消す。

**不変条件への影響と、cutover としてユーザー承認が要る箇所:**

| 不変条件・構造ガード | 影響 | 承認 |
|---|---|---|
| Controller の呼び出し元（`tests/test_task_controller_has_no_callers_yet.py` の `ALLOWED_MENTIONS` / `PLAN_SH_ALLOWED_OPERATIONS`） | **変わらない**。呼び出し元は plan.sh だけのまま。操作も増えない（`reopen_task` と `fail_execution` に `escalate` 系の引数、`start_execution` に `model` の引数が増えるだけ） | 不要 |
| `lib_task_status` の語彙・集合（`tests/test_task_status_single_definition.py`） | **変わらない**（status を足さない） | 不要 |
| queue / registry への書き込み（`tests/test_queue_writes_go_through_the_store.py`） | start.sh（CLI は `lib_worker_model.py record`）の `registry/workers/<Name>/model.json` を表に理由つきで足す | **要る**（registry の書き手が増える） |
| 割り当ての規則（dispatcher と pull の共有・不変条件 5） | 新しい共有規則 `lib_worker_model.worker_may_take`。**env の停止スイッチを付けない** | **要る**（割り当ての判定が変わる） |
| 試行の記録の形（`lib_execution.record_shape_problem`） | 任意の欄 `model`（`None` か文字列）を足す。`RECORD_SCHEMA_VERSION` は上げない（旧コードは知らない欄を読まない） | 要る（record の形） |
| 台帳の欄（`lib_history`）・再送の比較（`HISTORY_MISMATCH`） | `model`・`model_from`・`model_to`・`model_escalation` を足す。再送の比較に**含める**（同じ再送なら同じ値になる。§6） | 要る |
| reopen の intent（`reopen-intents/`・`parse_reopen_intent`） | `parse_reopen_intent`（`scripts/lib_task_controller.py:1130`）は欄の集合を**厳密に照合**する（`set(data) != _INTENT_KEYS or data['version'] != INTENT_VERSION` → `shape`）。だから欄を足すだけでは旧コードも新コードも割れる。**`INTENT_VERSION` を 2 に上げ、v2 の intent に欄 `model`（`{"escalate_to": <別名>|null, "model_escalation": <コード>, "at_cap_count": <int>, "at_cap_execution": <ex-…>|null}`。中の欄も集合を厳密に照合する）を足す**。新しいコードは **v1（欄なし = 段上げしない）と v2 の両方を受ける**（v1 は `_INTENT_KEYS` と完全一致、v2 は `_INTENT_KEYS ∪ {'model'}` と完全一致。版ごとに集合を持つ表 `_INTENT_KEYS_BY_VERSION`）。旧コードは v2 を `shape` で拒否する（= `REOPEN_INTENT_INCOMPLETE`。その mission の pull だけ止まる。§9） | **要る**（intent の形・版が変わる） |
| card の欄 `model_at_cap_count` / `model_at_cap_execution` | §4.6 の通知の数と fingerprint。`TASK_META_KEY_ORDER` に `model_override` の直後 | 要る（card の形） |
| `_INTENT_FREEZE` | 新しいサブコマンド `model-for` は読み取り専用 = `allow` に載せる（載せないと構造テストが赤） | 不要（表の更新だけ） |
| start.sh の起動（`LAUNCH_CMD`・`_shq`） | `--model` の値の出どころが増えるだけ。埋め込みは今の `_shq` を通す | 不要 |
| Git Policy の呼び出し元（`ALLOWED_CALLERS`） | start.sh が `check_mission_slug` / `check_task_id` を使うなら増える。plan.sh `model-for` の側で検査し start.sh は値を渡すだけにすれば増えない（**後者を採る**） | 不要 |

### 4.5 優先順位（(5)）

```
CREWVIA_WORKER_MODEL（起動前に人が設定。WORKER_MODEL_EXPLICIT=1）
  > model_override（束ねた起動で card から。source: override）
  > model_per_skill（task / Worker の skills）
  > worker_model
  > --model なし（CLI の既定）
```

- 解決は start.sh の 1 か所（`start.sh:650-669` の分岐に `CREWVIA_BIND_TASK` の段を 1 つ足す）。dry-run（`CREWVIA_PRINT_MODEL=1`）も同じ helper を通す（`knowledge/model-per-skill.md` の「dry-run と実際の起動で挙動が一致」を保つ）。
- 束ねた起動で明示 env が override より**低い**ときは env が勝つが、stderr に 1 行警告し、`model.json` は `source: env`。その Worker は表（§4.3）の「`bound_task` == この task」の行で取れる（人の明示を尊重する）。
- dispatcher の判定は「実効モデル」に env を含めない（env は Worker 側の事情で、task の性質ではない）。

### 4.6 上限・回数（(6)）

- 段上げは**試行の数を増やさない・別の数も持たない**。retry / reopen の `ATTEMPT_CAP`（総試行数）がそのまま効く。haiku から opus まで 2 回の reopen で届くので、既定 6 で足りる。
- `max_review_cycles` は plan review の巡（§2.6）。触らない。`max_rework` も触らない。
- **opus で再び差し戻されたとき**: `model_override` は opus のまま（上がらない）。台帳の reopen / retry / verify-result に `model_escalation: at_cap` を残し、出力に「モデルは上限（opus）のまま」と 1 行出す。止めない・理由も求めない（§11-3 = b。(c) は採らない）。
- `--over-cap` で上限を超えた試行も段上げの規則は同じ。

**opus での差し戻しが 2 回目になったら Director に 1 回通知（§11-3 = b）:**

- **数える**: 段上げが適用される差し戻し（§4.1 で「上げる」になった reopen・verify-result fail・`retry --escalate-model`）のうち、起点がもう opus で `model_escalation: at_cap` になったものだけを、card の `model_at_cap_count` に +1 する（`model_override` / pending と**同じ書き込み**。reopen は intent の `model.at_cap_count` に決めた値を書き、回復はその値を写す = 数え直さない）。`--no-escalate`・適用外（review / qa / codex-review・`update --reset`・フラグなしの retry）は数えない。sonnet → opus に上がった回は 0 回目（at_cap ではない）。
- **出す人**: dispatcher（Director の画面に届く唯一の常駐者。reopen の出力は打った本人の画面に出るだけで、verify-result fail を打つのは verifier）。サイクルごとに読む card の `model_at_cap_count >= 2` を見て `notify_state_once` で送る。plan.sh は通知を送らない（今の作法どおり。出力に「次に dispatcher が通知します」と 1 行）。
- **台帳のキー**: `model-at-cap-<slug>-<tid>`（task ごとに 1 つ・`notified-state.json`）。fingerprint は card の `model_at_cap_execution` = **数が 2 になった差し戻しで閉じた試行の `execution_id`**（Controller が +1 の書き込みで数が ちょうど 2 になったときだけ書く。3 回目以降は書き換えない）。`reopen_count` を fp にしない（verify-result fail・retry では動かないので、数え直した後の 2 回目を見分けられない）。
- **1 回だけの規則**: 1 つの task につき、`model_at_cap_count` が 2 に達した時点の 1 回だけ送る。3 回目以降の差し戻しでは**送らない**（fp が変わらない。「設計を見直すきっかけ」は 1 回で足りる・毎回送ると差し戻しの度にうるさい）。台帳を消したら次のサイクルでもう一度 1 回届く（消してよい台帳の作法どおり・不変条件 7）。`update --model-override none` は `model_at_cap_count` と `model_at_cap_execution` も消す = 数え直し（次に 2 に達したら別の試行の ID が fp になり、もう一度 1 回届く）。
- **文面**: `[model-at-cap] <slug>/<tid> は opus のまま 2 回差し戻されました。task の分割・設計の見直しを検討してください（plan.sh history <tid> --mission <slug>）`。card の本文・理由・Result は出さない（識別子と数だけ）。
- 欄が読めない（整数でない・負）は通知しない + `store-check` / `lint` が報告（通知の側は「出さない」に倒す。保留ではない = 割り当てには使わない値なので task は止めない）。

### 4.7 表示（(7)）

| 場所 | 出すもの |
|---|---|
| `plan.sh reopen` / `retry` の出力 | `モデル: sonnet → opus（段上げ）` / `モデル: opus のまま（上限）` / `段上げしない（理由: RETIRED）` |
| `plan.sh status` | `model_override` がある task の行に `model=opus↑`。走っている task は `execution_model` も |
| pull の JSON | `model_override`・`execution_model`（あれば）。`previous_attempt.model` |
| dispatcher の通知 | `no_worker` の文面にモデルと束ねた起動コマンド。割り当てのメッセージは変えない。§4.6 の `model-at-cap` |
| task-graph（`registry/task-graph/tasks.json`） | 出さない（plugin の schema に欄が無い。§11-6 = a） |

- **`no_worker` の理由の並び**: skill の合う Worker が居るのに担当できないとき、今は `worker_may_take_task(...)[1]`（TARGET_DIR の理由）を 1 つ付けている（`scripts/dispatcher.sh:3117`）。判定が 2 つの AND になるので、理由は **TARGET_DIR → model の順**で、最初に断った方の 1 つだけを出す（`lib_worker_target` が先・`lib_worker_model` が後。AND の評価順と同じにして、TARGET_DIR が合わない Worker に「モデルが違う」と出さない）。

### 4.8 壊れた入力（(8)）

| 入力 | 扱い | 理由 |
|---|---|---|
| card の `model_override` が別名でない・型が違う（手編集） | dispatcher はその task を**割り当てない** + Director に 1 回（`notify_state_once`。キー `model-override-invalid-<slug>-<tid>`）。pull は exit 3・何も書かない。`plan.sh lint` / `store-check` が報告。出口: `update --model-override <別名>|none` | 既定モデルに黙って倒すと、上げたはずの task が安いモデルで走る（観測できなかったことを「無い」に倒さない） |
| Worker の `model.json` が無い | `model_override` の無い task は取れる・ある task は取らない（§4.3 の表）。override の task しか残らなければ**退役でよい**（§4.3 の 5。永久に取れないので待つ理由が無い） | PR-1 の前に起動した Worker を止めない（`lib_worker_target` と同じ向き） |
| `model.json` が読めない・壊れている（missing でない） | 「無い」と同じ扱い**ではない**: 束ねられているか分からないので、`model_override` の無い task も**取らない**（保留）+ dispatcher のログに 1 回。**退役もさせない（待機）**（§4.3 の 5。`_rec_known` と同じ） | 束ねた Worker が別の task を取るのを防ぐ側。かつ観測できなかったことを根拠に破壊（退役）しない。`is_missing` と壊れたを取り違えない（memory `load-json-store-missing-is-also-unreadable`） |
| 前の試行の `execution_model` が無い・知らない名前（`fable` 等の段に無いモデル） | 起点を `model_override` → 既定モデルの順に探す（§4.2） | 起点が分からないことを理由に段上げを止めない |
| 既定モデルも決められない（config が `Unreadable`） | **reopen / retry / verify-result fail は進める・段上げはしない**・stderr に固定コード `MODEL_ESCALATION_SKIPPED:config_unreadable`・台帳に `model_escalation: skipped:config_unreadable` | 差し戻しは Director の主要な出口で、config の壊れで止めると出口が消える（§11-4 = a） |
| reopen の intent が v1（PR-3 の前に書かれ、完了していない） | 新しいコードが**段上げなしで完了させる**（`model` 欄が無い = 段上げしない。v1 の時点の決定には段上げが含まれていない）。`model_at_cap_count` も触らない | 決定のコミット点は intent。回復で決定を足さない（「一覧を作り直さない」と同じ原則） |
| `CREWVIA_BIND_TASK` の値が壊れている・card が無い・`model-for` が exit 1 | start.sh は**起動しない**（exit 1・`logs/start-sh/refusals.log`） | 束ねの無い Worker が起動して普通に task を取るより安全 |
| 明示 env の値が段に無い名前 | 今どおり（`lib_model` の警告だけ・そのまま `--model` に渡す）。記録には書く | 人の明示は通す（今の挙動） |

## 5. 解決の 1 か所（実効モデル）

- `lib_model.effective_model(config, skills, override)` = override があればそれ、無ければ `resolve(config, skills)`。dispatcher（`no_worker` の文面）・plan.sh（`model-for` / status）・start.sh（`model-for` 経由）が全部これを通す。`model_per_skill` を読む箇所を増やさない（config は `lib_config` 経由。不変条件 1）。
- 別名の集合（`haiku` / `sonnet` / `opus`）とその順は `lib_model` に 1 回だけ書く（`ESCALATION_LADDER`）。`model_override` の検査（plan.sh の `update`・lint・dispatcher・pull）は全部それを import する。

## 6. 書く順（トランザクション）

**reopen（PR-3）:**

1. 検査を全部先に（今の reopen と同じ）。**段上げの値（`escalate_to`）はこの段階で決める**（起点の card・config を読む）。
2. intent を書く（コミット点）。**intent は版 2（`INTENT_VERSION = 2`）で、欄 `model` に I の `escalate_to`・判定の結果 `model_escalation`・`at_cap_count`・`at_cap_execution` を書く**（§4.4 の表）。回復（`_complete_reopen_intents`）は intent の値を使い、config を読み直して計算し直さない（決定の後に config が変わっても同じ結果になる。「一覧を作り直さない」と同じ原則）。
   - **読み口は v1 と v2 の両方を受ける**: `parse_reopen_intent` は `data['version']` で欄の集合（`_INTENT_KEYS_BY_VERSION[1]` = 今の `_INTENT_KEYS`・`[2]` = それ ∪ `{'model'}`）を選んで厳密に照合する。知らない版（3 以上・0・bool）は今どおり `shape`。v1 は `model` 欄なし = **段上げしない**で完了させる（§4.8）。
   - 書き手は常に v2（v1 を書く経路を残さない）。
   - 旧コード（PR-3 より前）は v2 を `shape` で拒否する（`REOPEN_INTENT_INCOMPLETE`・その mission の pull が止まる）。だから PR-3 の revert の前に未完の v2 の intent を完了させる（§9）。
3. 台帳（I の reopen に `model_from` / `model_to` / `model_escalation`）→ I の card（`model_override`・`model_at_cap_count`・`model_at_cap_execution` は `status: pending` と**同じ書き込み**。別の書き込みにしない = 途中で止まっても「pending だが override が無い」が生まれない）→ intent を消す。
4. dry_run（fix 6 / t072）にも同じ引数を通す（`_write_reopen(dry_run=True)` の台帳の比較に `model_*` が入る）。

**verify-result fail（PR-3）:** 今の `fail_execution(VERIFICATION_REJECTED)` の順（台帳 → card → 枠）のまま、台帳に `model_*`、card の pending の書き込みに `model_override`（と §4.6 の欄）を載せる。段上げの値は検査の段階（ロックの中・書く前）で決める。再送は台帳の比較に `model_*` が入る。

**retry（PR-3。`--escalate-model` のときだけ値が付く）:** 台帳（`model_*`）→ card（`model_override` と pending を同じ書き込み）→ 枠の撤去。再送の同定キーは今のまま（`execution_id`, `retry`, `reopen_count`）。再送で config が変わって `model_to` が変わったら `HISTORY_MISMATCH`（出口は今と同じ「同じ報告を送り直す」。起点は台帳に書いた値を使うので、普通の再送では変わらない）。

**pull（PR-1）:** `model.json` はロックの外・ロック 1 の前に読む（registry の記録で queue ではない）。`start_execution(model=)` の card → record の順は今の `git` と同じ。

**update --model-override（PR-2）:** `with_lock` の中の `save_task`。未完の reopen の intent がある mission では `_INTENT_FREEZE` の `update` の行（`refuse`）に従う（今の `update` と同じく凍結される）。

## 7. PR の分割

| PR | 中身 | 挙動の変化 | 依存 |
|---|---|---|---|
| **PR-1 記録** | `lib_worker_model.py`（記録の形・`record` / `show` の CLI・`load_record`）／start.sh が `model.json` を書く／pull が `execution_model` を card・record に写す／台帳の `model`／`previous_attempt.model`／status の表示 | **無い**（書いて見せるだけ。割り当ては変わらない） | alias mission の merge |
| **PR-2 欄と束ね** | card の `model_override`・`ESCALATION_LADDER`・`effective_model`・`escalate()`／`plan.sh update --model-override`・`model-for`／start.sh の `CREWVIA_BIND_TASK`／`worker_may_take` を dispatcher と pull に配線／`worker_start_command` の `--exclude` と束ねた起動コマンド／lint・store-check の検査／§4.8 の通知 | `model_override` を手で付けた task だけ | PR-1 |
| **PR-3 自動** | reopen・`verify-result fail`・`retry --escalate-model` の段上げ（§4.1 の表・`escalation_applicable`・`--escalate-model` / `--no-escalate`・intent の版 2 と v1 互換・台帳・dry_run）／`model_at_cap_count` と dispatcher の `model-at-cap` 通知（§4.6）／出力／文書（`agents/director.md` の「差し戻しと指摘の修正」・`agents/worker.md`・root `CLAUDE.md` の環境変数の表に `CREWVIA_BIND_TASK`・`knowledge/model-per-skill.md`・`knowledge/file-map.md`・`scripts/CLAUDE.md`） | 差し戻しが自動で上がる | PR-2（§11 は 11-24 に決定済み） |

PR-1 と PR-2 は独立に revert できる。PR-3 は PR-2 の上でだけ意味がある。

## 8. テスト計画

**PR-1**
- `tests/test_worker_model_record.py`: 記録の形（全欄の欠け・型違いを 1 つずつ。memory `store-check-required-fields-match-reader-subscripts`）・原子的な書き込み・`is_missing` と壊れたの区別・symlink / FIFO を開かない（`read_regular_text_or_unreadable`）。
- start.sh: `CREWVIA_PRINT_MODEL=1` と実際の起動で `model.json` の `model` / `source` が一致（env 明示 / skill / 空 の 3 通り。スタブの claude で LAUNCH_CMD を評価する既存の bats に足す）。
- pull: 記録あり → card の `execution_model`・record の `model`・台帳の `model`。記録なし・読めない → 欄が無い（`unknown` を作らない）・pull は止まらない。
- 構造ガード: `test_queue_writes_go_through_the_store.py` の表に start.sh の行。

**PR-2**
- `worker_may_take` の総当たり（§4.3 の表 × task の override の有無 / 壊れ × 記録の 4 状態 × source × ランク）。dispatcher と pull が**同じ関数**を呼ぶことを AST で固定（コピーさせない）。
- dispatcher: 束ねた Worker は他の task を取らない／既定の Worker は override の task を取らない／`no_worker` の文面に `CREWVIA_BIND_TASK` と `--exclude`／理由の並びが TARGET_DIR → model（両方断るときは TARGET_DIR の理由だけ）。
- **退役の総当たり**（§4.3 の 5）: `model.json` の 3 状態（読めた / 無い / 読めない（壊れた JSON・権限・形の違い・symlink を 1 つずつ））× TARGET_DIR の記録の 3 状態 × 残っている task（無い / override の無い task / override の task だけ / 自分に束ねた task / 別の task に束ねた task）× in_progress の有無。期待: 読めない記録が 1 つでもあれば退役しない（ログ 1 回）・無い記録 × override の task だけ → 退役・束ねた Worker が手放した後 → 退役・束ねた起動を 2 回貼った 2 人目 → 退役。赤の実証: `_rec_known` から `model.json` の項を外すと「読めない → 待機」の行が赤になること。
- start.sh: `CREWVIA_BIND_TASK` の壊れ・card なし → 起動しない（exit 1・refusals.log）。明示 env が低いときの警告。
- `update --model-override` の検査（別名以外は exit 2 で何も書かない）・`none` で消える・intent 凍結中は拒否。
- 赤の実証: 判定の関数を「常に取れる」に差し替えると総当たりが赤になること（`tests/red_proof_*` の作法）。

**PR-3**
- §4.1 の表をそのまま総当たり（コマンド（reopen / verify-result fail / retry）× end_code × フラグ × 今の override × skills（review / qa / codex-review / 実装）。起点が `CASCADE_REVIEW_SKILLS` の reopen は `--escalate-model` でも上がらない）。
- cascade: I だけが上がり、下流は上がらない（`tests/test_reopen_targets_matrix.py` に軸を 1 つ足す）。
- intent: 書き込みの全点で kill → 回復後の card が kill なしと同じ（`model_override` を含む）。intent の後に config を変えても回復の結果が変わらない。
- **intent の版**: (i) **v1 の intent（今の形・`model` 欄なし）を置いて、新しいコードの次の queue を書くコマンドが完了させる**（card は pending・`model_override` は付かない・台帳に `model_*` が無い）。(ii) v2 の欄の欠け・余分・`model` の中の欄の欠け・型違い・`version` が 3 / 0 / `true` → `REOPEN_INTENT_INCOMPLETE`（完了させない・消さない）。(iii) v1 に `model` 欄がある・v2 に無い → `shape`。(iv) 旧コードの `parse_reopen_intent`（main の版を写した fixture）が v2 を `shape` で拒否することを固定（§9 の根拠が崩れたら赤）。
- 台帳の再送: 同じ再送は通る・`model_to` が違えば `HISTORY_MISMATCH`。
- opus の再 reopen: override は opus のまま・`model_escalation: at_cap`・`model_at_cap_count` が +1。
- **at_cap の通知**: 1 回目は送らない・2 回目で 1 回・3 回目は送らない・台帳を消すともう一度 1 回・`update --model-override none` の後に 2 回目へ再到達したらもう一度 1 回（fp = `model_at_cap_execution`）。verify-result fail と `retry --escalate-model` の混ぜ合わせでも数が同じ。`--no-escalate`・フラグなしの retry は数えない。文面に本文・理由が出ない（secret を仕込んだ card で確認）。
- **旧仕様のテストを洗う**: reopen / retry の出力・台帳の欄の数・pull の JSON の欄を固定している既存テストを `grep` で先に列挙して card に書く（memory `spec-change-pr-grep-old-behavior-tests`。#408 で 1 周損した）。
- 新しい lib・import を足す PR は、push の前に単一定義・再起動対象・JSON 読みの構造ガードを全部回す（memory `new-lib-trips-single-definition-guards`）。

## 9. 戻し方

| PR | 手順 | 残るもの |
|---|---|---|
| PR-1 | revert → `scripts/sync-main-checkout.sh` | `registry/workers/*/model.json`・card の `execution_model`・record の `model`・台帳の `model`。どれも旧コードは読まない（残ってよい） |
| PR-2 | revert → sync。**束ねた Worker が生きていれば先に Director が退役させる**（旧 dispatcher は `bound_task` を知らず、その Worker に普通の task を回す = 高いモデルで回る。害はコストだけ） | card の `model_override`（旧コードは無視 = 既定モデルで走る） |
| PR-3 | **revert の前に未完の reopen の intent を全部完了させる**（`plan.sh store-check` の `reopen-intent-incomplete` が 0 になるまで、該当 mission で queue を書くコマンド（同じ reopen の再実行か pull）を打つ）→ revert → sync | 既に付いた `model_override` は効き続ける（PR-2 が残っていれば）。止めたければ `update --model-override none`。`model_at_cap_count` / `model_at_cap_execution` は旧コードが読まない（通知が止まるだけ） |

- **PR-3 の revert の前に intent を完了させる根拠**: PR-3 の後に書かれた intent は版 2（§6）。旧コードの `parse_reopen_intent` は欄の集合と版を厳密に照合するので v2 を `shape` で拒否し、`REOPEN_INTENT_INCOMPLETE` でその mission の pull を止める（壊れた intent は消さない）。つまり revert すると、止まった reopen が完了できず mission が動かなくなる。逆向き（PR-3 を入れる時）は新しいコードが v1 も受けるので何もしなくてよい（§8 の intent の版 (i)）。どうしても revert 後に v2 が残ったら、Director が中身を見て消し、card を手で直す（今の「intent を読まない旧コード」の手順と同じ）。

- 本番では merge のたびに dispatcher の restart が要る（`scripts/sync-main-checkout.sh`）。start.sh の変更は次に起動する Worker から効く（起動済みの Worker は記録を持たない = §4.3 の表の「無い」の行）。
- env の停止スイッチは付けない（割り当ての規則は dispatcher と plan.sh の共有規則。不変条件 5）。一時的に段上げを止めたいときは `--no-escalate` を打つ（PR-3 の運用）。

## 10. 実装時の落とし穴（過去の教訓の写し）

- `load_json_store` は「無い」も `Unreadable`。`is_missing` を先に見る（memory `load-json-store-missing-is-also-unreadable`）。§4.8 の「無い」と「壊れている」で扱いが違う。
- `None → 例外` 化や新しい import はモジュール先頭の呼び側を洗う（dispatcher / watchdog の `_mux = Mux()` の例。memory `raise-instead-of-none-check-module-level-callers`）。
- start.sh の値の埋め込みは全部 `_shq`（t070 P1）。`CREWVIA_BIND_TASK` を `LAUNCH_CMD` に入れるなら同じ。
- 同じ skills の 2 人目は `--exclude`（`--fresh` は枯渇で空）。
- reopen の intent の「一覧を作り直さない」原則を、段上げの値にも当てる（§6）。

## 11. 決定（2026-11-24 ユーザー回答）

初版で「ユーザーに決めてもらうこと」として並べた 8 点。選んだ案と、採らなかった案を残す（本文はこの決定で書き直し済み）。

| # | 問い | 決定 | 採らなかった案 | 反映先 |
|---|---|---|---|---|
| 1 | 検証の差し戻しと retry | **(a)** `verify-result fail`（task が直接 pending に戻る）でも上げる。retry は既定で上げず、`--escalate-model` のときだけ上げる | (b) verify-result fail も上げない（決定 (A) の文面どおり review / QA / Codex だけ）／(c) (a) に加え retry の `WORKER_FAILED` も上げる | §0・§4.1・§6・§8 |
| 2 | Codex の NEEDS FIX の後 | **(a)** 正しい出口は実装 task の reopen（= 上がる）。codex-review task 自体の retry（Codex を走らせ直す）は上げない | (b) needs-director の codex-review task の retry で上流の実装 task にも override を付ける | §4.1 |
| 3 | opus で再び差し戻されたとき | **(b)** opus での差し戻しが 2 回目になったら Director に 1 回通知（止めない・理由も求めない） | (a) opus のまま黙って続ける（`at_cap` だけ）／(c) opus での差し戻しは `--over-cap` と同じく理由を必須にする | §0・§4.4・§4.6・§8 |
| 4 | 既定モデルが決められない（config が壊れている）ときの差し戻し | **(a)** 差し戻しは進め、段上げだけ飛ばす | (b) reopen を断る（`--no-escalate` を付ければ通る） | §4.8 |
| 5 | 最初から上げた状態で task を作れるようにするか | **(a)** `add` は受け付けない。`update --model-override` は Director の手で使える | (b) `add --model-override` も受け付ける | §4.4 |
| 6 | task-graph に出すか | **(a)** 出さない | (b) 表示名の末尾に `[opus↑]` | §4.7 |
| 7 | 束ねた Worker の起動 | **(a)** 今の運用どおり Director が通知のコマンドを貼って起動する | (b) dispatcher が自分で起動する（別の設計・範囲外） | §4.3 |
| 8 | cascade で戻る下流の review の扱い | **(a)** 上げない（review / plan_review / research は既に opus。qa / verify は sonnet） | (b) 実装 task の `model_override` 以上に下流の review も揃える | §4.1 |

## 12. 未解決の課題（この文書のレビューで積む場所）

- **PR-1 実装メモ（2026-11-24）**: `plan.sh` の `lib_task_cards` は `_load_scripts_module` で別に読み込まれ、`lib_worker_model` が import する `lib_task_cards` とは**別のモジュール**。呼び出し側が `is_missing()` / `is_unreadable()` を取り違える（isinstance が偽になり `rec['model']` が TypeError）ので、「無い / 読めない / 値」の判定は `lib_worker_model.recorded_model()` に閉じた。PR-2 の `worker_may_take` も plan.sh から呼ぶなら同じ形（判定を lib の中で済ませる）にする。
- PR-1 の限界: Worker が消えても `model.json` は掃除しない（`lib_worker_target.sweep_stale_records` は `target_dir.json` だけ見る）。後任の `start.sh` が置き換えるので害は無いが、記録の書き込みに失敗した後任には先任の記録が残りうる。PR-2 で `worker_may_take` が読むようになる前に、掃除に `model.json` を含めるか決める。

- Worker のセッションの中で人が `/model` を切り替えた分は記録に入らない（§4.2）。transcript から読む案はあるが、v1 ではやらない。
- 別名（`opus`）が指す版は CLI の既定で決まるので、台帳の `model: opus` は「その時点の opus」。版まで残したいなら Worker の会話記録の `message.model` を読む必要がある（未確認・v1 ではやらない）。
- WIP を機械で数えていないので、段上げの Worker が増えても dispatcher は止めない（§4.3）。運用で困ったら別途。
