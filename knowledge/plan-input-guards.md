# plan.sh の入力の守り（t013 / C4）

`plan.sh` に入る入力の誤りで起きた事故 3 つを、書く前・lint の時点で止める。

| 事故 | 守り |
|---|---|
| 本番確認 QA (t037 / t041, 2026-09-28/29) が観察用の使い捨て mission を `plan.sh init` で作ると、init が `active_missions` に足し `default_mission` も書き換える。dispatcher が probe の task を本物として配ろうとして Director に Worker 起動要求が漏れ、default_mission が probe に変わった | `plan.sh init ... --inactive` |
| `plan.sh update --blocked-by` が循環を作れた (2026-09-28, B5 の Codex 指摘の根) | `add` / `update` は循環になる変更を exit 2 で拒否し、何も書かない |
| Director が途中で足した PR を作る task 3 枚に Codex review と merge の task を積み忘れ、PR が作られても誰も merge しなかった (2026-09-28) | lint が、`deliverable: pr` の下流に skills が `review` の task が無ければ WARN |

## (1) `init --inactive`

`state.yaml` を 1 バイトも変えない（`active_missions` にも `default_mission` にも触れない）。dispatcher は
`active_missions` だけを見るので、その mission の task は誰にも配られない。以降のコマンドは `--mission <slug>` を付ける
（`add` / `update` / `lint` / `done` / `pull --task` はどれも `--mission` 指定で動く）。

* 既存 slug を `--force` で置き換えたとき、「active から外す」だけは `--inactive` でも state に反映される（置き換えた
  mission が active のまま残ると、中身の無い mission を dispatcher が見続けるため）。
* 観察用の mission は**必ずこれで作る**（`skills/crewvia-qa/SKILL.md` Step 5）。state を触らないので、
  片付けで `default_mission` / `active_missions` を戻す作業が要らない。

## (2) 循環の拒否

循環の定義は **`scripts/lib_dep_rules.py` の `find_dependency_cycle(graph)` 1 か所**（root CLAUDE.md 不変条件 #3）。
`plan.sh add` / `update` と `lint_plan.py`（既存 mission の検査）が同じ関数を呼ぶ。

* `add` / `update --blocked-by` は、card を書く**前**に、書き換えと同じロックの中で検査する。循環なら
  `plan.sh <sub>: blocked_by が循環します: a → b → a (何も書いていません)` を stderr に出して exit 2。
* 自己依存（`update t002 --blocked-by t002`、`add` が自分の将来の id を待つ）も循環として拒否される。
* graph に無い id（未定義の依存）は辿らない。それは lint の `blocked_by '<id>' does not exist`（別の検査）の仕事。
* 読めない card は graph から外れる。検査は読めた範囲の依存で行う（読めない card を `[]` に潰して「循環なし」と
  言うのではなく、その card は「依存を持たない」扱いにもならない = 何の辺も足さない）。
* 循環の経路表示は `a → b → a`（以前の lint は `a → b → a → a` と末尾を重複表示していた。同時に直した）。

## (3) `deliverable: pr` の下流に review が無い

`lint_plan.py` の `check_pr_has_review_downstream()`。「下流」は `blocked_by` を逆にたどった推移閉包
（直接・間接に自分を待つ task）。skills に `review` を含む task が 1 つも無ければ **WARN**（FAIL にしない —
既存 mission を壊さないため）。`deliverable` を宣言していない task は対象外（宣言した task だけ）。

## 族ごとの掃除（同じデータ・同じ判定を扱うコード）

| 型 | 列挙（grep） | 処置 |
|---|---|---|
| `blocked_by` を**入力から**書く | `plan.sh` の `cmd_add`（`'blocked_by': blocked_by`）と `cmd_update`（`meta['blocked_by'] = new_blocked`）の 2 箇所だけ。他の `blocked_by` の書き込み（`lib_task_cards.py` の `[]` 補完、`taskvia_sync_add` / taskvia-sync.sh の送信 payload）は既存 card の写しで、新しい辺を作らない | 2 箇所とも `_reject_dependency_cycle()`。dispatcher / watchdog / kai-review / hooks に `blocked_by` の書き手は無い |
| 循環の**検出** | 元は `lint_plan.py` の DFS（in_stack）1 箇所。plan.sh 側には無かった | `lib_dep_rules.find_dependency_cycle()` に移し、lint と plan.sh がそれを呼ぶ。`in_stack` の写しが scripts/ に残らないことをテストが固定 |
| `active_missions` / `default_mission` を**書く** | `cmd_init`（足す・default を奪う）/ `cmd_init --force` の置き換え（外す）/ `cmd_archive`（外す）/ 初期化（`load_state` の空 state） | `--inactive` は init の「足す・奪う」だけを止める。archive・置き換えの「外す」は state の実態に合わせる動作なので不処置 |
| `deliverable: pr` の task を**検査**する | `lint_plan.py` の `check_deliverable`（宣言・権限との突き合わせ）と、`plan.sh done` の `--pr` 要求 / 下流への `pr_number` 伝播 | review の有無は前者と別の問いなので `check_pr_has_review_downstream()` を足した。`done` 側は task が完了した後の処理で、積み忘れは検出できない（lint の役目） |

## 検証

* `tests/test_plan_input_guards.py`（16 件）: (1) state.yaml の不変 + 陽性対照 + 実 dispatcher 1 サイクルで kickoff が飛ばない
  （陽性対照: `--inactive` を外すと飛ぶ）(2) update / add / 自己依存の拒否と無書込、循環にならない付け替えの陽性対照、
  手で書かれた循環を lint が FAIL にする (3) WARN が出る・出ない（直接 / 間接 / review 以外 / 下流でない review）
* 赤の実証: `tests/red_proof_t013_input_guards.sh`（baseline + 修正前の版への差し替え + 個別に欠陥を戻す 7 ケース、
  すべて赤）

## 戻し方

共有規則（呼び出しがすべて同じ plan.sh / lib を通る）なので **env の停止スイッチは付けていない**。戻すときは
**PR revert → `scripts/sync-main-checkout.sh`**（主 checkout の ff とデーモン restart まで完結する）。
一部だけ戻したいとき:

* 循環の拒否が誤検出で正当な更新を止める → `find_dependency_cycle` の入力（graph）を疑う。card を直接編集すれば通る
  （lint が同じ関数で FAIL にするので、直したあとに `plan.sh lint` で確かめる）
* `--inactive` の mission を dispatcher に配らせたい → `state.yaml` の `active_missions` に slug を足す
  （`init` を `--inactive` なしでやり直す必要は無い）
* WARN が邪魔 → 下流に `--skills review` の task を積むのが正しい直し方。WARN 自体は exit code を変えない
