# CI 待ちの運用改善（設計）

2026-11-10 / mission `20261110-ci-wait-ops` t001。**設計だけ。実装は §8 の PR 列で別 task にする。**
ユーザー決定（2026-11-10）: ①CI 待ちの持ち主を Worker から dispatcher へ ②Worker の done 条件 ③変更範囲で CI を絞る ④pytest の並列化 ⑤Rule 5 / watchdog の通知、を 1 本の設計にまとめる。

## 0. 要点（先に結論）

| 論点 | 結論 |
|---|---|
| 持ち主 | CI を**見る**のは dispatcher。**待つ**人はいなくする。Worker は push + PR + ローカル確認で done。CI の結果は dispatcher が Director に通知する（`ci-failed` / `ci-green` / 承認 head が揃ったら `merge-ready`）。**ただし今の取得は in_progress 等の task にしか効かず、done の後は誰も見ない**ので、「見る対象」を PR 単位に広げる（§2.0。PR-C に含める）。 |
| merge の門 | `lib_ci_status.gate_verdict()` を新設し `plan.sh ci-gate` で引く。**緑と言えるのは「head が PR の現 head と一致・必須 check が全部 passed・記録が新しい」ときだけ**。「review が承認した head」と比べる規則（§2.5）が入るまで、通知の名前は承認を含意しない `ci-green`。それ以外（running / failed / unknown / none / 古い）は全部「通さない」。merge は `--match-head-commit`。 |
| 「判定に使わない」の改め方 | 禁止を **表示用の読み口（今のまま）と門用の読み口（新設）の 2 つに割る**。門は「通さない」「通知を出す」にだけ使い、**待ちを解く・done にする・Rule 5 を黙らせる**ことには使わない。 |
| Worker の done 条件 | 触ったファイルに対応するテスト + 構造ガードをローカルで通す。**full pytest は流さない**（約 1 時間）。CI は待たない。 |
| 変更範囲で CI を絞る | **効果が小さい**。`knowledge/` `agents/` `config/` `skills/` `scripts/` `hooks/` はテストが読む（§5）。当面やらず、nightly の全件 + `ci-ok` 集約 job を先に入れる。 |
| 並列化 | **手元で 1 回試した: `-n 4` で 24 分 17 秒（CI の直列は約 71 分）**。並列で新たに落ちたテストは 0（§6）。最初に入れる価値が一番大きい。 |
| Rule 5 / watchdog | CI 待ち由来の誤報は、実装・review の Worker が待たなくなれば出なくなる。上限（75 分 / 7200 秒。7200 は review だけでなく実装系 skill にも入っている）は**しばらく残す**。 |

## 1. 現状（実測と、読んで分かったこと）

- `.github/workflows/ci.yml` の `pytest` job は PR と main への push のたびに `tests/` を**全件直列**で流す（198 ファイル相当・10,278 件）。main の直近 run は 4272 秒。変更範囲には関係ない。
- Worker は「CI 全緑で done」で動き、push 後に `gh run watch` / `gh pr checks` で約 1 時間待つ。修正 push を挟むと 2 時間以上。
- 待ち中の Worker に対し Rule 5 の idle-with-task 誤報（約 5 分ごと）と watchdog の見送り通知が出る（1 日で十数通）。
- dispatcher は PR の CI を毎サイクル取得して `registry/ci/` に記録している（`knowledge/ci-status.md`）。ただし「判定に使わない。使うなら別 PR で設計する」と決めてある。**本書がその設計。**
- **取得の対象は狭い**: `lib_ci_status.poll_cycle` は `POLL_STATUSES = lib_task_status.CI_WATCH_STATUSES = {in_progress, ready_for_verification, verifying}`（`scripts/lib_task_status.py:77`）の task だけを見る。Worker が待たずに done する運用では、**done になった瞬間に取得が止まる**（§2.0 で直す）。
- main に branch protection は無い（`gh api …/branches/main/protection` は 404）。merge 前の CI 確認は crewvia の運用（Director / review Worker）だけが担っている。
- **「CI を待ってから done」は文書にほとんど書かれていない。** `agents/worker.md` に CI 待ちの記述は無く、`agents/director.md` も Rule 5 の通知表と §17 くらい。規則は慣習 + Director の指示 + timeout 設定のコメント（`config/crewvia.yaml` の `timeout_max_per_skill` 付近）+ `knowledge/watchdog-idle-judgment.md` §12 + `knowledge/review-ci-wait-timeout.md` に散らばっている。`skills/crewvia-qa/SKILL.md` にも CI 待ちの記述は見つからなかった（QA は隔離 clone で走らせる）。→ §4 の「書き換え」は、実際には**「書く」が多い**。

## 2. 設計 1: CI 待ちの持ち主を dispatcher へ

### 2.0 CI を「見る対象」（取得を止めない）

現状の取得対象（`CI_WATCH_STATUSES`）のままでは、実装 Worker が push 後すぐ done し、review も CI と並行して done すると、その PR の task はどれも done になり**誰も CI を取得しなくなる**。`ci-failed` も `ci-green` / `merge-ready` も `ci-unknown` も出ず、門の条件 4（`observed_at` が 10 分以内）は done の後に引くと必ず `stale` になる（fail-closed の向きには倒れるが、「Worker は待たず、知らせは dispatcher が出す」という設計の要が黙って欠ける）。したがって**見る対象を設計に入れる**。

- **対象の定義**: `pr_number` を持ち、PR が **open で未 merge** の task（status は問わない。done を含む）。`lib_task_status` に `CI_WATCH_STATUSES` とは**別の集合**（例: `CI_FOLLOW_PR_STATES`）として 1 か所に定義する。`CI_WATCH_STATUSES` は他の読み手（`codex_reviews_awaiting_pr` など）と共有の定義なので変えない。PR 番号ごとの重複は `by_pr` で既に畳まれる（同じ PR を指す task が何本あっても 1 回）。
- **やめる条件**: 取得のたびに PR の `state` / `mergedAt` を同じ `gh` 呼び出しで取り、`MERGED` なら記録に終端を書いて対象から外す（`CLOSED` は reopen されうるので 900 秒ごとに取り直す。t003 / 11-14）。task が終端（mission の後始末で queue から消える）になった場合も外れる。`pr_number` を持たない task は対象外。
- **負荷**: 既存の上限（`MAX_TASKS_PER_CYCLE=4`・サイクル予算・60 秒間隔）は**PR 単位**で数え直す。open な PR は同時に数本なので、done の task が増え続けても対象は増えない（merged / closed で外れるため）。上限を超える場合は `observed_at` の古い順に回す。結論が出た PR（passed / failed で head が変わらない）は取得の間隔を延ばす（既定 5 分）。head が変わったら 60 秒に戻す。
- **置き場所**: PR-C に含める（通知より先に対象を直す。対象だけ直す PR を B と C の間に置いてもよいが、挙動が変わるのは通知が入るときなので 1 本にまとめる）。表示用の読み口（status / tasks.json）には、done の task の CI 行が出るようになるだけで、判定には使わない（§2.3）。

### 2.1 流れ

```
実装 Worker:  ローカル確認 → push → gh pr create → plan done      （CI は待たない）
review Worker: diff を読む（CI と並行）→ 所見を Result に残して done （merge しない）
dispatcher:   毎サイクル「open な PR」の CI を取得（task が done でも続ける。§2.0）
              → CI が failed になったら Director に 1 回通知（ci-failed）
              → gate_verdict が通ったら Director に 1 回通知（ci-green。§2.5 の承認 head が揃っていれば merge-ready）
Director:     通知を受けたら review の所見と承認 head を読み、plan.sh ci-gate <task> を引き直し、
              gh pr merge --squash --match-head-commit <gate が見た head>
```

- merge を **Director に寄せる**理由: 今も review Worker の merge は Claude Code の classifier に拒否されうる（`knowledge/review.md`「承認・merge の経路」。#338 拒否・#339/#345/#351 通過）。拒否時は `needs-director` に行くのが正常系になっている。「CI が緑になるまで Worker が生きて待つ」形をやめると、merge できる瞬間に Worker がいない。ならば最初から Director が merge する方が経路が 1 本になる。
- review task の位置づけ: 「読んで所見を出す」まで。CI 待ちを含めない → `timeout.max=7200` の根拠が消える（§7）。
- CI が `failed`: dispatcher は**通知だけ**（第 1 段階）。修正 task の自動起票は第 2 段階の候補（§9 Q2）。通知には PR 番号・head・落ちた check 名（既に 80 字に切って無害化済みの値）だけを入れる。
- 通知の重複防止: 既存の通知台帳（`registry/daemons/notified-state.json`）に、キー `(pr, head, 種類)` で 1 回だけ。head が変われば（修正 push）別の通知になる。台帳を消すと同じ通知が 1 回ずつ再送されるだけ（既存の不変条件 7 と同じ性質）。

### 2.2 門（gate）: 誰がどこで見るか

`scripts/lib_ci_status.py` に **`gate_verdict(record, *, pr_head, now)`** を 1 つだけ作る（`lib_dep_rules` と同じ「定義は 1 か所」）。

通す条件（**すべて**）:

1. 記録の `state == "passed"`（全体は既存の集約: failed > unknown > running > passed）。
2. 記録の `head` が **PR の現 head と一致**。現 head は門を引く瞬間に `gh pr view --json headRefOid` で取り直す（記録の head と突き合わせる。取り直せなければ通さない）。
3. check が 1 件以上あり、**必須 check が全部 `passed`**。必須 check は crewvia 本体の repo なら §5 の `ci-ok`、他の repo は config `ci.required_checks`（`config/crewvia.local.yaml`。`knowledge/ci-status.md`「repo ごとの必須 check」）の集合。`none`（CI 未登録）は通さない。
4. 記録の `observed_at` が新しい（既定 10 分以内）。古ければ「通さない」と理由 `stale`。
5. `neutral`（skipped 等）は passed とは数えないが、`ci-ok` が passed なら妨げない。ただし **`ci-ok` 自体は「needs が全部 success」のときだけ緑**にする（§5.2-1。skipped を許すのは paths の絞りを入れる PR になってから）。
6. （`merge-ready` にだけ要る条件。`ci-green` には要らない）承認した head が現 head と一致する（§2.5）。`gate_verdict` は CI の緑だけを見る関数のまま保ち、承認 head との比較は通知・`plan.sh ci-gate` の外側の層で行う。

通さない理由は固定語で返す（`running` / `failed` / `unknown:<reason>` / `none` / `head_mismatch` / `stale` / `no_required_check` / `no_required_check_config`）。`no_required_check` = 必須 check が記録に無い / 緑でない、`no_required_check_config` = その repo の必須 check が config に無い（未設定は通さない。出口は `crewvia.local.yaml` の `ci.required_checks` に足して dispatcher restart）。**疑わしきは通さない**＝ fail-closed。`gh` の失敗・timeout・形の違いは既存どおり `unknown` で、緑にも「CI なし」にも倒さない。

使う場所は 2 つだけ:

- `plan.sh ci-gate <task>`（CLI。exit 0 = 通す / 非 0 = 通さない + 理由 1 行）。Director が merge の直前に引く。
- dispatcher の `ci-green` / `merge-ready` 通知を出す条件（`merge-ready` は条件 6 も要る）。

**使わない場所**（不変）: 待ちを解く・card を done にする・Rule 5 / watchdog の通知を黙らせる・タスクの割り当て。門は「止める」と「知らせる」にしか効かない。これで「CI の記録が壊れたら全部動かなくなる」ことはなく、壊れたときは**知らせが出ないだけ**（Director が `plan.sh ci-gate` を手で引けば分かる）。

### 2.3 `lib_ci_status` の「判定に使わない」をどう改めるか

`CLAUDE.md` 不変条件・`scripts/CLAUDE.md`・`knowledge/ci-status.md` の文言を次のように改める（PR-D）:

- 表示用（`read_record` / `format_line` / `summarize`）: **今のまま判定に使わない**。
- 門用（`gate_verdict`）: 上の 2.2 の目的でだけ使う。**共有規則**なので env の停止スイッチは付けない（不変条件 5。dispatcher と `plan.sh ci-gate` で答えが割れるため）。止めたいときは PR revert。
- 不変条件 1 に従い、記録を開くコードは `lib_task_cards` / `lib_daemon_state` の入口を通す。読めない記録は `Unreadable` で、門は「通さない（`unknown:unreadable`）」。

### 2.4 gh が使えないとき

`unknown` が **3 サイクル（既定 180 秒）以上続いたら** Director に `ci-unknown` を 1 回通知（理由つき）。それまでは何も出さない（一過性の timeout で騒がない）。門は常に「通さない」側。

### 2.5 `merge-ready` の承認 head（review の done は承認ではない）

review task の done は「所見を書き終えた」であって承認ではない。今の運用では「要修正」の所見でも done する（例: 要修正の review t020 が done → 修正 t021 → 確認 t022）。また門は「CI の head == PR の現 head」しか見ず、「review が読んだ head == 現 head」を見ない。review の後に修正が push されると、新しい head の CI が緑になった時点で、未レビューの head に `merge-ready` が出てしまう（memory「Director の直しは merge 前にレビュー」と同種の事故）。

- **規則（PR-D で worker.md / review の手順に入れる）**: PR に紐づく review 系 task（review / codex-review / qa。mission ごとに Director が PR に紐づけた task が対象）が**全部終端**で、**最後のもの**の Result が承認を明示する。承認するときだけ Result の末尾に 1 行 `approved_head: <40 桁の sha>` を残す（要修正・条件付きのときは書かない）。
- **比較**: `merge-ready` の通知条件と `plan.sh ci-gate` の出力で、最後の review の `approved_head` と PR の現 head（門が取り直したもの）を比べる。一致しなければ `ci-green` のまま（`merge-ready` を出さない）。理由は固定語 `approved_head_mismatch` / `no_approval`。
- **移行中の名前**: この規則と比較が入る（PR-D）まで、dispatcher が出す通知の名前は **`ci-green`**（CI が緑になった事実だけ。承認を含意しない）。`merge-ready` は PR-D の後。Director は `ci-green` を受けたら review の所見を自分で読んで merge 判断する（今と同じ）。
  **→ 移行は完了した（2026-11-13）**: 比較は PR-D1（#379。`approved_head` / `approval_verdict`）、規則の文書化は PR-D2（#381）。`merge-ready` と理由つきの `ci-green` が両方出る。`qa` への反映と「読む前に head を控える」の抜けは #383 で埋めた。
- 下流の review が複数ある mission（codex-review → qa → 確認）では「最後に終端したもの」を見る。順序は card の依存で決まるので、PR 番号に紐づく review 系 task の `done` 時刻の最大を取る。

## 3. 設計 2: Worker の done 条件

「CI が見る」を前提にすると、Worker は**CI より速く確かめられるもの**だけ確かめて done する。

1. 触ったファイルに対応する pytest（`tests/test_<module>*.py` と、触った module を import するテスト）を流す。
2. **構造ガード**を流す: 全ファイルを走査するテスト群（単一定義ガード・import 追加の検査・queue 書き込み走査・git 判断走査など。memory の「新しい lib・import の追加は構造ガードを全部回してから push」が実際に CI 1 周を失った事例）。**一覧を人が覚える形をやめ、`pytest -m structural` 1 コマンドにする**（印を付ける作業は PR-D の前に PR-B' として入れる。現状は一覧の正本が無い）。
3. shell を触ったら `bash -n`、yaml を触ったら構文確認（CI の `shell-validate` / `yaml-validate` と同じ軽い検査はローカルでも秒で終わる）。
4. `git push` 後 `gh pr view --json headRefOid` が**ローカルの HEAD と一致**することを確認（push 漏れで古い head を見せない）。
5. **full pytest は流さない**（約 1 時間。`config/crewvia.yaml` の `timeout_max_per_skill` のコメントにある「ローカル full pytest」はやらない運用に改める）。

done の報告に「CI は未完了。結果は dispatcher が通知する」と 1 行入れる。

### 書き換え範囲の棚卸し

| ファイル | 内容 | 種類 |
|---|---|---|
| `agents/worker.md` §5 Pre-Done（約 623 行〜）と「作業完了時の手順」（約 983 行〜） | 上の 1〜5。**CI を待たない**を明記 | 追記 + 一部書き換え |
| `agents/worker.md` review 関連（約 662・1009 行の「merge は review Worker」） | merge は Director に変更。review は所見まで。承認するときだけ Result に `approved_head: <sha>` を残す（§2.5） | 書き換え |
| `agents/director.md` | ci-green / merge-ready / ci-failed / ci-unknown の通知表への追加、merge 手順（`ci-gate` → `--match-head-commit`）、§17 Rule 5 の CI 待ち言及 | 追記 + 書き換え |
| `knowledge/review.md` プロトコル 3〜5・「承認・merge の経路」 | merge を試みる手順を外し、所見 → done に | 書き換え |
| `knowledge/review-ci-wait-timeout.md` | 「選んだ対応」に**後継**の注記（§7） | 注記 |
| `knowledge/watchdog-idle-judgment.md` §12 | CI 待ち由来の上限の位置づけを更新 | 注記 |
| `knowledge/ci-status.md` | 「見える化だけ」→ 表示と門の 2 本立て（§2.3） | 書き換え |
| `CLAUDE.md` / `scripts/CLAUDE.md` | 「判定に使わない」の文言（§2.3）。`CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS` の記述は据え置き | 書き換え |
| `skills/crewvia-qa/SKILL.md` | CI 待ちの記述なし。QA の対象 head を `ci-gate` が見た head と揃える 1 行だけ検討 | 任意 |
| `config/crewvia.yaml` コメント | ローカル full pytest の記述。`timeout_max_per_skill` の 7200 は review だけでなく実装系 skill（bash / code / typescript / python / database / cloud / ops）にも入っている（§7） | 書き換え |

## 4. 移行の注意（文書を先に直さない）

文書だけ先に直すと「待たなくてよい」と書いたのに通知が出ない期間ができる。**PR-C（通知）が merge・dispatcher restart されるまで PR-D（Worker への「待つな」）を入れない**（§8）。稼働中の Worker は起動時のプロンプトで動くので、PR-D の後に起動した Worker から新運用になる。

## 5. 設計 3: 変更範囲で CI を絞る

### 5.1 飛ばしてよい範囲は狭い

テストが読むファイルを調べた。**数え方**: `grep -l -F "<dir>/" tests/*.py | wc -l`（`tests/` 直下の `*.py` だけ。下位ディレクトリは含めず、裸の語は数えない。実際に読むかではなく**パスの文字列を含むファイル数**。2026-11-10 の main で、レビュー担当が同じ式で再現した）:

| ディレクトリ | 参照するテストファイル数 |
|---|---|
| `scripts/` | 84 |
| `knowledge/` | 59 |
| `hooks/` | 23 |
| `config/` | 15 |
| `agents/` | 13 |
| `skills/` | 7 |
| `docs/` | 3 |

`scripts/`（84）と `hooks/`（23）まで入れると、絞れる余地はさらに小さい。

文書をテストが抽出する例（`tests/test_irc_relay_pure.py` は文書の付録を読む、`test_agent_docs_take_pr_base_from_plan_pr_base.py` は `agents/*.md` を検査する）が示すとおり、**「md だから飛ばせる」は成り立たない**。本書自身（`knowledge/*.md`）を足す PR も、`knowledge/` を読む 60 ファイルの対象になりうる。

過去 40 日の変更ファイルの内訳（`git log`。`origin/main`）は `tests/` 348・`scripts/` 219・`knowledge/` 91・`agents/` 34・`config/` 22・`bench/` 13・`hooks/` 6。**`docs/` だけの変更はほぼ無い**。飛ばせる範囲を作っても当たる PR が少ない。

### 5.2 推奨: 絞る前に「穴を作らない」土台を入れる

1. **`ci-ok` 集約 job**: 全 job を `needs` にし `if: always()` で、**needs が全部 `success` のときだけ緑**にする（`failure` / `cancelled` / `skipped` のどれかがあれば落ちる）。`if:` の書き間違いなどで job が skipped になっても緑にならない＝ fail-closed。以後、門（§2.2）と将来の branch protection は `ci-ok` だけを見ればよい。**skipped を許す（paths で飛んだ job を無視する）のは、絞りを入れる PR（Q4 の b）で、その PR が許可する job を明示して緩める**。それまでは緩めない。
2. **main への push は常に全件**（今のまま）。
3. **nightly の全件**（`schedule:` + `workflow_dispatch:`）を追加。絞った PR で見逃したものを翌朝までに拾う。落ちたときの通知の出し方は**未決**（今は dispatcher が main の CI を見ていない。§9 Q8）。
4. 絞るのは**テストの入力でないと確かめられた path だけ**。確かめ方: 手元で一度、テスト実行中に開かれたリポジトリ内ファイルを記録して（`builtins.open` / `pathlib.Path.read_*` / `subprocess` の引数を差し込む使い捨ての conftest。リポジトリには入れない）、**一度も開かれなかった path 群**を許可リストの候補にする。許可リストはファイルにして理由を 1 行ずつ書く（`scripts/ci-script-tests-excluded.txt` と同じ作法）。
5. 穴が開いたときの保証: 絞った PR が main に入ると全件が走る。そこで赤になれば Director への通知（§2.1）で拾う。**PR 時点で絞ったことによる見逃しは、main の全件 + nightly で必ず検出される**（遅れるだけで消えない）。

### 5.3 推奨判断

paths による絞りは**当面やらない**。得られる短縮が小さく（当たる PR が少ない）、穴の管理コストが勝つ。並列化（§6）で全件が約 25〜40 分になる方が全 PR に効く。絞りたくなったら 5.2 の 1〜5 を先に入れる。

## 6. 設計 4: pytest の並列化（pytest-xdist）

### 6.1 手元での 1 回目の実測（本番には触っていない・CI 設定は変えていない）

- 環境: WSL2・6 コア・メモリ約 6GB。隔離 venv（`pytest 9.2.0` + `pytest-xdist 3.8.0` + `pyyaml`）。worktree の中で実行。`CREWVIA_QUEUE` / `CREWVIA_REPO_ROOT` / `CREWVIA_MUX` / `CREWVIA_MISSION_SLUG` / `CREWVIA_TASK_ID` は外した（QA clone が本番 queue を継ぐ事故の再発防止）。
- コマンド: `pytest tests/ -n 4 --dist loadfile -p no:cacheprovider`
- 結果: **10,256 passed / 33 skipped / 18 failed、所要 1457 秒（24 分 17 秒）**。CPU 時間は user 57m27s + sys 17m7s（合計約 74 分 ≒ CI の直列 71 分と同程度 = 仕事量は変わらず 4 並列にほぼ素直に分かれた）。
- **18 件の失敗は並列のせいではない**: 同じ 2 ファイル（`tests/test_worktree_gc.py` 17 件・`tests/test_leaked_descendants_guard.py` 1 件）だけを**直列で流しても同じ 18 件が落ちた**（83 秒）。原因は手元（Claude Code のサンドボックス内）の環境: worktree_gc は `process-scan-failed`（/proc の走査が通らず全 worktree が keep 扱いになる）、`leaked_descendants` は「このカーネルは `pidfd_open` が使えない」警告。CI では通っている想定の組（main の CI は緑）。**つまり並列で新たに落ちたテストは今回は 0 件。**
- 限界: 1 回だけ・`-n 4` だけ・手元の直列の基準値は取っていない（1 時間以上かかるため）。flaky を取りこぼしている可能性は残る。**CI で 3 回以上流して初めて「落ちない」と言える**（PR-A の受け入れ条件）。
- 終盤の 3% に数分かかった: `--dist loadfile` だと**重いファイルが 1 本の worker に載る**ので、最後の 1 本の長さが下限になる。`--durations=30` で上位を出し、重い上位は `--dist loadgroup` + 分割で均す余地がある（PR-A の後の調整）。

### 6.2 CI での見積もり

GitHub の `ubuntu-latest` は 2〜4 vCPU（public repo は 4、private は 2）。CPU 時間約 74 分を割ると:

| vCPU | 目安 |
|---|---|
| 2（`-n 2`） | 約 40 分前後（71 分 → 約 4 割減） |
| 4（`-n 4`） | 約 22〜25 分（手元の実測に近い） |

`-n auto` なら runner の vCPU に追従する。メモリは worker ごとに増える（手元の `-n 4` で空きが約 4GB まで減った程度）ので、2 vCPU の runner（メモリ約 7GB）では問題ない見込み。

### 6.3 既知の並列で落ちうる候補と、見立て

| 候補 | 見立て |
|---|---|
| `/proc` の cmdline 走査（`tests/kill_budget.py` `leaked_descendants.py` `proc_group.py` ほか 10 ファイル以上が `/proc` を参照） | 走査が「自分の pytest プロセスの子孫」に限られていれば worker 間で干渉しない。`leaked_descendants` は pid で名前空間を切っている。**全プロセスを走査して他 worker の子を数える作りのテストが無いか**、PR-A の CI 3 回で確かめる。 |
| 本物の tmux（45 ファイルが tmux に触れる。`test_daemon_husk_respawn.py` は実窓） | `conftest.py` の `_NONCE` が `os.getpid()` + uuid なので worker（別プロセス）ごとに別のセッション名・ペイン接頭辞になる。`conftest.py` に「xdist でも衝突しない」旨のコメントが既にある。今回は落ちなかった。 |
| 共有の一時パス | 今回の失敗には出なかった。固定名の `/tmp/…` を作るテストが残っていないかを `grep -n '"/tmp/' tests/` で洗う（PR-A）。 |
| `CREWVIA_MUX_TEST_ISOLATION` | `pytest_configure` が worker ごとに走り、env は worker プロセスに閉じる。問題なし。 |
| 実 `gh` / ネットワーク | `CREWVIA_CI_ALLOW_GH_IN_TESTS` が無ければ呼ばない。問題なし。 |

洗い出しの方法（PR-A の手順）: ①`-n 2` と `-n 4` を CI で各 3 回 ②落ちたテストは**直列で単独実行して**環境由来か並列由来かを分ける（今回の 18 件がその例）③並列由来は `@pytest.mark.xdist_group("name")` + `--dist loadgroup` で同じ worker に寄せるか、一時パスを `tmp_path` に直す。

## 7. 設計 5: Rule 5 / watchdog の CI 待ち通知

| 通知 | 本設計の後 |
|---|---|
| Rule 5 idle-with-task（CI 待ち由来） | 実装・review の Worker が CI を待たなくなるので**出なくなる**。 |
| Rule 5 idle-with-task（ローカルテスト待ち由来） | 残る。ただし full pytest をやめる（§3）ので短くなる。 |
| watchdog の見送り通知（hard_idle） | CI 待ち由来は消える。`ci-status` の 1 行を通知に添える表示は**そのまま**（判定には使わない）。 |
| `CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS`（既定 4500 秒） | **触らない**。CI 以外の背景 job にも効く。CI 待ちが無くなれば上限に届かなくなるだけ。 |
| `timeout.max=7200`（`config/crewvia.yaml` の `timeout_max_per_skill` を `plan.sh add` が card に写す。`knowledge/review-ci-wait-timeout.md`） | **review だけでなく実装系 skill（bash / code / typescript / python / database / cloud / ops）にも入っている**。根拠は「push 後の CI 完走待ちとローカル full pytest」で、review も実装系も、待たなくなれば（§3）消える。**PR-D の後、2 週間は残して**CI 待ちの timeout が出ないことを確かめてから、**両方の既定を外す**（PR-F）。`watchdog.py` の `PROFILES` には触らない（restart が要るため）。 |

## 8. 実装 PR の分け方と順序

| PR | 内容 | 依存 | 備考 |
|---|---|---|---|
| **A** | ci.yml の pytest を xdist 化（`pip install pytest-xdist`・`-n auto --dist loadfile`・`--durations=30`）。並列で落ちるテストの洗い出し | なし | **先に入れる**。CI で 3 回以上緑を確認。最も短縮が大きい。 |
| **B** | `lib_ci_status.gate_verdict()` + `plan.sh ci-gate` + テスト（赤の実証つき）。`pytest -m structural` の印付け（B'） | なし（A と並行可） | 挙動は変わらない（誰も呼ばない）。 |
| **C** | CI を見る対象の拡張（§2.0。done の task の PR も、open な間は取得する）+ dispatcher の `ci-failed` / `ci-green` / `ci-unknown` 通知、台帳、テスト | B、**E** | 本番では dispatcher restart が要る（`scripts/sync-main-checkout.sh`）。E が先でないと門が `no_required_check` のままで `ci-green` が出ない。 |
| **D** | 文書: worker.md / director.md / review.md / ci-status.md / CLAUDE.md の書き換え（§3 の表）+ 承認 head の規則（§2.5）と `merge-ready` の比較 | **C が本番に入ってから**（C ← E ← A） | 「待つ」をやめると書くのはここ。`ci-ok` が無いまま「待つな」を入れない。**3 本に分かれて完了した**: **D1**（#379）= 比較の実装、**D2**（#381）= §3 の表の文書、**D3**（#383）= D2 の抜け（`skills/crewvia-qa/SKILL.md` に承認 head が無かった・`gh pr diff` の後に head を取ると未レビューの head を承認しうる・file-map / plan.sh の docstring）。 |
| **E** | ci.yml に `ci-ok` 集約 job（needs が全部 success のときだけ緑）と nightly の全件 | A | paths 絞りは含めない。**C・D より先**。 |
| **F** | `timeout.max=7200` の既定（review と実装系の両方）を外す。Rule 5 文言の整理 | D から 2 週間後 | |

順序の依存は **A → E → C → D → F**、B は A と並行して C の前に。並列に進められるのは A と B だけ（E は A の後、C は B と E の後）。各 PR の前に base の CI が緑かを確認する（`git.md` のルール）。壊れた CI の上に積まない。

## 9. Director に決めてほしいこと

**Q1. merge の持ち主**
- (a) **Director が `merge-ready`（PR-D の前は `ci-green` + 所見の確認）を受けて `ci-gate` → `--match-head-commit` で merge（推奨）**: 経路が 1 本。classifier の拒否が来ても Director が自分で対応できる。
- (b) review Worker が merge を続け、CI が緑になるまで再 pull で待つ: 待ち主が dispatcher に変わるだけで、Worker が再割当てされる仕組みが要る（`review-ci-wait-timeout.md` の案 D を却下した理由と同じ）。
- (c) dispatcher が自動 merge: 外向きの変更を無人で行うため非推奨。

**Q2. CI が赤になったとき**
- (a) **通知だけ（推奨・第 1 段階）**: Director が修正 task を切る。誤検出（flaky の 1 回落ち）で task が増えない。
- (b) dispatcher が修正 task を自動起票: 速いが、flaky と本物の区別（再実行の仕組み）が先に要る。第 2 段階で再検討。

**Q3. main の branch protection（必須 check = `ci-ok`）を有効にするか**
- (a) **PR-E の後に有効化（推奨）**: 門が crewvia の運用だけに依存している現状を、GitHub 側でも固定できる。操作はユーザーの GitHub 権限が要る（認証アカウントが PR の author と同一なので、bypass の扱いを併せて決める）。
- (b) 当面は入れない。

**Q4. paths による絞り（§5）**
- (a) **やらない。`ci-ok` と nightly だけ入れる（推奨）**
- (b) PR-E で許可リスト付きで入れる（§5.2 の手順 4 を先にやる）

**Q5. Worker のローカル確認の範囲（§3）**
- (a) **対応テスト + 構造ガード（推奨）**
- (b) 今まで通り full pytest（約 1 時間のまま。本設計の効果の大半が消える）

**Q6. xdist の worker 数**
- (a) **`-n auto`（推奨）** (b) 固定の `-n 2`（メモリが逼迫したとき用）

**Q7. 稼働中の Worker の扱い**
- PR-D の merge 後に起動する Worker から新運用。稼働中の Worker は古い手順のまま完走させる（**推奨**。途中で手順を変えると done 条件が食い違う）。

**Q8. nightly の全件が落ちたときの通知（§5.2-3）**
- (a) **dispatcher が main の CI も見て `ci-failed` と同じ経路で通知（推奨）**: 通知の経路が 1 本。取得の対象（§2.0）に「main の最新 run」を足す小さな拡張になる。
- (b) GitHub の標準通知（メール）に任せる: 実装は無いが Director の画面には出ない。

**Q9. 承認 head の規則（§2.5）**
- (a) **review の Result に `approved_head: <sha>` を残し、`merge-ready` はそれと現 head を比べる（推奨）**。PR-D までは通知名を `ci-green` にする。
- (b) 規則を入れず `ci-green` のまま、Director が毎回所見を読んで判断する（今と同じ。機械的な保証は無い）。

## 10. 戻し方

- A: ci.yml を revert（直列に戻る）。B: 呼び手がいないので放置でよい。C: PR revert + dispatcher restart（通知が止まるだけ）。D: 文書の revert。E/F: ci.yml / 既定値の revert（F は review と実装系の両方）。C を revert すると取得の対象が元に戻る（done の task の PR は見なくなる）が、通知も同時に止まるので整合する。
- 台帳（`notified-state.json`）は消してよい（同じ通知が 1 回ずつ再送される）。`registry/ci/` も消してよい（次のサイクルで取り直す）。

## 付録: 今回確かめていないこと

- 手元の**直列**の実行時間（1 時間以上かかるため取っていない）。短縮率は CI の直列 4272 秒との比較で見積もった。
- xdist の 2 回目以降・`-n 2`・`loadgroup`。CI 上の挙動。
- `ubuntu-latest` の vCPU 数（repo の公開状態に依る）。
- 「絞ってよい path」の実測（§5.2 手順 4）。
