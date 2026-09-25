# `plan.sh fail` の証拠要求 (backlog #8 / t004)

## 事故

`qa_checkpoints` / `required_evidence` は `plan.sh done` だけを守っていて、FAIL の報告は検証ゲートの
外にあった。QA Worker が **前回の handoff (別 head 時点のもの) を 36 秒で再提出** できた。差し戻すと
古い handoff が `registry/handoffs/<agent>/<task>_HANDOFF.md`（agent 名 + task id で決まる固定パス）
に残り、同名の Worker がやり直すとそのまま出せる。

## 規則

```
plan.sh fail <task_id> [<handoff_path>] --head <sha> [--mission <slug>]
plan.sh fail <task_id> [<handoff_path>] --no-head "<理由 1 行>" [--mission <slug>]
```

- `--head` は必須。実在する commit に解決できなければ拒否（形の誤り / 打ち間違い / 別 repo の SHA / 曖昧な略称）。
  card には**完全な SHA** を `fail_head` 欄と Result に残す。
- `handoff_path` を付けるなら、その handoff が報告する head に触れていること（7 桁以上の 16 進の語が
  head の先頭と一致）。別 head 専用の古い handoff はここで止まる。ファイルが無いときは警告して通す
  （「古い再提出」にはなり得ない）。ファイルを**読めない**ときは通さない（観測できなかったことを根拠にしない）。
- `--no-head "<理由>"` は明示的な免除。検証対象が git 管理外で head が出せないときだけ。card の
  `fail_head_waiver` 欄・Result・stderr 警告に残るので外から見える（Director が
  `required_evidence: []` を置くのと同じ扱い）。「渡せないから検証しない」に静かには倒れない。

## 設計判断: done と同じ検証を流用しない

done の検証（QA Gate / required_evidence）は「PASS の証拠が揃っていること」を要求する。
FAIL の理由が「PASS の証拠が出ない」ことであるのは普通なので、流用すると required な checkpoint が
`failed` / `not_run` の FAIL — 最も正当な FAIL — が報告できなくなる（別の outage）。
`qa_checkpoints` / `required_evidence` を宣言した task でも、FAIL に課す証拠は宣言の有無で変えない
（head と、handoff の head 結び付き）。ただし**入口は 1 つ**: done も fail も
`_gate_terminal_report()` を通る。`tests/test_fail_evidence.py` が AST で
「status を done / failed に書く関数は必ず入口を通る」「FAIL の規則が PASS の検証を呼ばない」を確かめる。

## 呼び出し元の洗い出し（2026-09-25）

- `plan.sh fail` を呼ぶのは **エージェント** だけ: `agents/worker.md`（Rule 4 / §7 graceful handoff）。
  どちらも head を渡せる（手順を更新済み）。
- 自動経路は 0 件: dispatcher.sh / watchdog.py / lib_retirement.py は `plan.sh retire`、
  kai-review.sh は `done` / `needs-director`、skills（crewvia-qa）は `done` / `needs-director`。
  watchdog の terminate メッセージ (`TERMINATE_MESSAGE`) も fail を指示していない。
  増えたら `test_no_automated_caller_invokes_fail_without_a_decision` が赤くなる。
- `update --status failed` / `update --status done` は人間の手作業（cmd_update の docstring）で、ここでは対象外。

## 誘因の側

`plan.sh update <id> --reset` は `handoff_path` / `fail_head` / `fail_head_waiver` を消し、古い handoff
ファイルを `<path>.stale-<UTC>` に**退避**する（削除しない。`registry/handoffs/` の外のパスは動かさない）。
`retire` は in_progress からしか動かず、handoff_path を持ち得ないので対象外。

## ロールバック

停止スイッチは**設けていない**（証拠要求を env で切れると、それが「静かに検証しない」経路になる）。
`plan.sh` は呼び出しごとに読み直されるスクリプトで、デーモンの再起動は要らない: この PR を revert
すれば即座に元に戻る。一時的に FAIL を通す必要があるときは、呼び出しごとに
`--no-head "<理由>"`（記録に残る）を使う。
