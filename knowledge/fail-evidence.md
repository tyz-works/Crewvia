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
- **`handoff_path` は絶対パスだけ受け付ける**（`--no-head` のときも。相対パスは拒否）。dispatcher.sh は
  相対パスを registry の親 (main repo) 基準で読むが、`plan.sh fail` は Worker の cwd (worktree) 基準で
  検証していた。相対パスが渡ると、検証したファイルと dispatcher が読むファイルが別物になり、
  stale-handoff の確認を迂回できる (Kai P2 / PR #216)。基準を揃えるより受け付けない方を選んだ:
  Worker は元から絶対パスを渡す (`crewvia_handoff_path`、`agents/worker.md` §7 Step 2 / Rule 4)。
  card には検証したのと同一の (正規化した) 絶対パスを書く。
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
- 自動経路は 0 件（呼び出しの形を問わない。下の「ガードが検出できない形」を除く）: dispatcher.sh / watchdog.py / lib_retirement.py は `plan.sh retire`、
  kai-review.sh は `done` / `needs-director`、skills（crewvia-qa）は `done` / `needs-director`。
  watchdog の terminate メッセージ (`TERMINATE_MESSAGE`) も fail を指示していない。
  増えたら `test_no_automated_caller_invokes_fail_without_a_decision` が赤くなる。
- **対象外**（head を要求しない。意図的）:
  - `update --status failed` / `update --status done` — 人間の手作業（cmd_update の docstring）。
  - `plan.sh verify-result <id> fail` — Verifier（`agents/verifier.md` / `verifier-dispatcher.sh`）の
    別の FAIL 報告経路。機械 check（`run_verification_checks.py`）と acceptance_criteria の判定を書くもので、Worker の
    handoff 再提出という事故の形を持たないため gate の対象にしていない。
    この経路に「古い結果の再提出」が起きるなら別 task で扱う。

### ガードが検出できない形

`test_no_automated_caller_invokes_fail_without_a_decision` は **変数名に依存しない**: plan.sh を参照する
ファイル（scripts/ と hooks/）で、python はリスト / タプルの要素・呼び出しの位置引数に単独の `"fail"` が、
shell はコメントでない行に単独の語 `fail` があれば落とす（`["bash", str(self.plan_sh), "fail", ...]` も
`"$PLAN_SH" fail ...` も拾う。検出力は `test_the_guard_detects_the_forms_the_daemons_actually_use` と
`tests/red_proof_t004.sh` の case I で確かめてある）。旧版はリテラル `plan.sh fail` しか拾わず、
変数経由の実際の呼び出し形を素通ししていた（t005 QA F1）。

それでも**検出できない形**が残る（表明は「増えたら必ず赤くなる」ではなく「下の形以外は赤くなる」）:

- サブコマンド名を組み立てる形: `"fa" + "il"` / `verb=fail; "$PLAN_SH" "$verb"` / 設定ファイル・引数から読む
- plan.sh を参照しない別ファイル経由の呼び出し（`plan` という語を含まない wrapper が `fail` を叩く）
- scripts/ と hooks/ の外（skills/、config/ 等）にある呼び出し — `skills/` は洗い出しを 1 回手で行った（`done` /
  `needs-director` のみ）が、機械では見ていない

これらは静的に塞げない（塞ぐには実行時に plan.sh 側で「呼び出し元が自動経路か」を知る必要があり、
それは env で偽装できる）。塞げない分は、`plan.sh fail` が head を要求すること自体が最後の砦になる
（自動経路が増えても `--head` なしなら拒否される。増えて困るのは「--no-head で静かに通す」形だけで、
それは card の `fail_head_waiver` に残る）。

## 誘因の側

`plan.sh update <id> --reset` は `handoff_path` / `fail_head` / `fail_head_waiver` を消し、古い handoff
ファイルを `<path>.stale-<UTC>` に**退避**する（削除しない。`registry/handoffs/` の外のパスは動かさない）。

- 相対の `handoff_path`（古い card / 手書きの値）は、dispatcher と同じ基準（repo root）で解く。cwd 基準だと
  dispatcher が読むのと別のファイルを動かして、古い handoff が残る。
- **退避先は衝突しない**: `os.rename` / `os.replace` は既存の宛先を黙って置換するので、秒精度の名前だけでは
  同じ秒の 2 回目が 1 回目の証拠を消す。`_reserve_unique_path()` が退避先の名前を `O_EXCL` で確保し
  （衝突したら `-1`, `-2`, ...）、その確保済みの名前へ置換する。「存在しなければ改名」の check-then-act にしない。
`retire` は in_progress からしか動かず、handoff_path を持ち得ないので対象外。

## ロールバック

停止スイッチは**設けていない**（証拠要求を env で切れると、それが「静かに検証しない」経路になる）。
`plan.sh` は呼び出しごとに読み直されるスクリプトで、デーモンの再起動は要らない: この PR を revert
すれば即座に元に戻る。一時的に FAIL を通す必要があるときは、呼び出しごとに
`--no-head "<理由>"`（記録に残る）を使う。
