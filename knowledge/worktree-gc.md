# 古い worktree を安全に片付ける — `scripts/worktree_gc.py` (t033 / backlog #33)

Worker は task ごとに `.claude/worktrees/<mission_slug>/<task>-<slug>` を作る (`git-helpers.sh` の
`crewvia_create_worktree`)。誰も消さないので、archive 済み mission・merge 済みブランチの worktree が溜まり続け、
主 checkout の `git worktree list` は **398 個** (2026-09-27、この task の着手時は 405 行) になっていた。
手で消すと「今使っている Worker の worktree」「push していないコミットのある worktree」を巻き込みうるので、
**理由付きで keep / remove を出す**道具にした。

```
python3 scripts/worktree_gc.py                # dry-run (既定。何も消さない)
python3 scripts/worktree_gc.py --quiet        # 集計だけ (remove / keep の件数と keep の理由別件数)
python3 scripts/worktree_gc.py --json         # 機械可読 (1 行 1 worktree の action / reason / detail)
python3 scripts/worktree_gc.py --fetch        # 判定の前に git fetch --prune origin
python3 scripts/worktree_gc.py --apply        # remove と判定したものだけを消す
```

`--repo` (既定: カレント。どの worktree からでも主 checkout を辿る) / `--queue` (既定: `$CREWVIA_QUEUE`、無ければ
`<主 checkout>/queue`。registry はその隣) は、テストが本番に触れないように付け替えるための引数。

## remove にしてよいのは、次を **すべて** 満たすものだけ

| # | 条件 | keep の理由コード |
|---|---|---|
| 1 | 主 checkout 自身ではない | `main-checkout` |
| 2 | `<主 checkout>/.claude/worktrees/<mission_slug>/<name>` 配下 | `outside-managed-dir` |
| 3 | `git worktree lock` されていない・ディレクトリがある (無ければ `git worktree prune` の仕事) | `locked` / `prunable` |
| 4 | mission が active でない (`queue/state.yaml` の `active_missions` に無い) | `mission-active` |
| 5 | `queue/missions/<slug>` も残っていない (= archive 済み)。**task の指示より厳しい** 追加条件 | `mission-not-archived` / `mission-dir-unobservable` |
| 6 | Worker が使っていない: TARGET_DIR 記録 (`registry/workers/*/target_dir.json`) が worktree を指さない | `in-use-target-dir` |
| 7 | どのプロセスの cwd も worktree の中に無い (Worker の claude・pane のシェル・この実行自身) | `in-use-process` |
| 8 | 未コミット・untracked の変更が無い (`git status --porcelain --untracked-files=all` が空) | `dirty` |
| 9 | ignore されている内容も無い (`git status --ignored=matching` が空)。「ignore されている = 捨ててよい」は成り立たない — `.env` 等の secrets・ローカル状態を ignore しているこの repo では `git worktree remove` は `--force` 無しでも ignored ファイルの削除を許すため (PR#239 F1、2026-09-27) | `ignored-files-present` |
| 10 | HEAD から辿れるコミットがすべて `origin/*` にある (merge 済み、または remote branch に push 済み) | `unpushed-commits` |

判定の順序は安いものから (構造 → mission → 使用中 → git の中身)。最初に当たった keep の理由が出る。
`--json` の `summary.keep_by_reason` が理由別件数。

## 判定できないものは keep (破棄ではなく保留)

memory `fail-closed-discard-vs-hold` / `evidence-for-destructive-decisions` の向き。**「読めなかった」を
「無い」「空」と読まない** (ENOENT だけが本当に無い)。

| 観測できなかったもの | 結果 |
|---|---|
| `queue/state.yaml` が無い・読めない・解釈できない・`active_missions` が文字列のリストでない | **全部 keep** (`state-unreadable`)。`active_missions: []` は正常な状態で、全 mission が非 active |
| `registry/workers` を列挙できない・TARGET_DIR 記録が 1 つでも読めない | 全部 keep (`registry-unreadable`)。`registry/workers` が無い (ENOENT) のは「記録ゼロ」 |
| プロセス表を取れない (`/proc` も `lsof` も) | 全部 keep (`process-scan-failed`) |
| `queue/missions/<slug>` を lstat できない (ENOENT 以外) | その worktree を keep (`mission-dir-unobservable`) |
| `git status` / `git rev-list` が失敗・時間切れ | その worktree を keep (`git-status-failed` / `head-unverifiable`) |
| `git worktree list` が取れない | 何もせず exit 1 |

### プロセスの cwd (Linux)

`/proc/<pid>/cwd` を全プロセス分読む。他の uid のプロセスは Worker になりえないので飛ばす。**自分と同じ uid で
読めなかった**プロセスは、その cwd が worktree の中かもしれないので原則「取れなかった」(= 全 keep) だが、
次だけは「掴んでいない」と言えるので飛ばす (2026-09-27 の実機で、これを飛ばさないと 405 件すべてが
`process-scan-failed` になった):

- 消えた (`/proc/<pid>` が無い)・zombie / 終了処理中 (`state` が Z / X、または `cmdline` が空)
- **allowlist** (`UNOBSERVABLE_BUT_NOT_A_WORKER`): `ssh-agent` / `sshd` / `sshd-session` / `systemd` / `(sd-pam)` /
  `gpg-agent`。カーネルは dumpable でないプロセスの cwd を同じ uid にも読ませない。Worker (claude と pane の
  シェル) は dumpable なので読める。**未知の名前で読めないものは「取れなかった」に倒す** (denylist にしない —
  memory `approve-judgment-needs-allowlist-and-scope`)。足したいときはコードの allowlist に理由付きで足す。

`/proc` が無い環境 (macOS) は `lsof -a -d cwd -Fpn`。出力が空なら「見えなかった」であって「無い」ではない。
**lsof の終了コードが 0 以外なら、stdout に何か出ていても不完全なスキャンとして拒否する** (PR#239 F3、
2026-09-27)。lsof は一部のプロセスの検査に失敗しても集められた分の部分出力を出しつつ非 0 を返すことが
あり、その部分出力を「見つからなかった (= 使われていない)」の証拠にしてはいけない。

## `--apply` の安全策

- `git worktree remove` — **`--force` を使わない** (git が dirty / locked を自分でも断る)。
- ローカルブランチは `git branch -d` — **`-D` を使わない**。git が merge 済みと認めないなら残す
  (`branch: kept <name> (...)` と出る。失敗ではない)。detached の worktree はブランチに触れない。
  worktree を消せなかったときもブランチに触れない。
- `rm -rf` / `shutil.rmtree` / `unlink` は使わない。remote branch は触らない。
- **repository-wide の `git worktree prune` は呼ばない** (PR#239 F2、2026-09-27 で削除)。元の実装は
  `--apply` のたびに全 verdict が keep でも `state.yaml` が読めなくても無条件に走らせており、管理対象
  ディレクトリの外にある worktree (一度も verdict を出していない対象) のメタデータまで消しうった。
  移動中・一時的に見えないだけの detached worktree では、未 push のコミットを守る HEAD・reflog が消え、
  git の紐付けが壊れる。個別に判定した対象は `git worktree remove` (自分が消した登録だけを消す) で消し、
  それ以外 (`prunable` = ディレクトリが無い判定を含む) は触らない。
- **消す直前に、その worktree の判定をもう一度やり直す** (dry-run と `--apply` の間に Worker が起動した・変更が
  入った・mission が active になった、を拾う)。remove でなくなっていれば `skipped`。
- 1 件の失敗で止めない。失敗は報告して次へ進み、終了コード 1。
- これらは `tests/test_worktree_gc.py::TestNoForcefulOperations` が **AST で固定**している
  (`run_git(...)` の literal 引数から verb / flag を洗い出し、`--force` / `-f` / `-D` / `--hard` 等が
  1 つでも増えたら赤。ファイルを直接消す呼び出しも赤。`("worktree", "prune")` の呼び出しも赤にする)。

## 限界 (知っておくこと)

- 「origin にある」は**手元の remote-tracking ref** で見る (fetch しない = dry-run は読み取り専用)。
  `--fetch` を付けると `git fetch --prune origin` してから判定する。merge 後に remote branch が消えていれば、
  squash merge したブランチは「push 済みと確かめられない」= keep になる。それが保留の向き。
  **本番で `--apply` する前は `--fetch` 付きの dry-run を 1 回見ること。**
- squash merge されたブランチのコミットは origin/main の祖先にならない。remote branch が残っていれば
  「push 済み」で remove になり、消えていれば keep。PR の merge 状態 (`gh`) は見ない (確かめられなければ keep)。
- 2026-09-27 の本番 dry-run (読み取りのみ): 405 行 → remove 218 / keep 187。keep の大半 (157) は `dirty` で、
  そのうち約 140 は `.crewvia-env` (`.gitignore` 登録前に作られた worktree では追跡または untracked) だけの差。
  これを「変更なし」と読むかは Director の判断 (この道具は読まない。ファイル名で例外を作ると、本物の変更を
  消す穴になりうる)。
- 戻せない操作は `git worktree remove` と `git branch -d` だけ。コミットが origin にあれば
  `git worktree add <path> origin/<branch>` / `git checkout -b <branch> origin/<branch>` で作り直せる。

## 戻し方

この道具は **`--apply` を付けない限り何も変えない**。共有規則ではなく (dispatcher / plan.sh は読まない)、
env の停止スイッチも付けていない。PR を revert すれば道具が無くなるだけで、稼働中のものへの影響は無い
(デーモンの再起動も要らない)。`--apply` で消した worktree を戻したいときは上の「限界」の最後の項。

## 検証

- `python3 -m pytest tests/test_worktree_gc.py -q` — 一時 git repo (origin = bare) と隔離した queue / registry で、
  各条件の keep / remove・観測できない場合の保留・`--apply` しても keep が残る・再判定・`-D` / `--force` を使わない。
- 赤の実証: `bash tests/red_proof_t033.sh` (25 ケース。W/X/Y は PR#239 の Codex findings 3 件、t057)。
