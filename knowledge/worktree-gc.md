# 古い worktree を安全に片付ける — `scripts/worktree_gc.py` (t033 / backlog #33)

Worker は task ごとに `.claude/worktrees/<mission_slug>/<task>-<slug>` を作る (`git-helpers.sh` の
`crewvia_create_worktree`)。誰も消さないので、archive 済み mission・merge 済みブランチの worktree が溜まり続け、
主 checkout の `git worktree list` は **398 個** (2026-09-27、この task の着手時は 405 行) になっていた。
手で消すと「今使っている Worker の worktree」「push していないコミットのある worktree」を巻き込みうるので、
**理由付きで keep / remove を出す**道具にした。

**`--apply` は削除ではなく隔離する (t071 / PR#239 3巡目)。** Codex review で 3 巡連続、「削除してよいかの
判定が何かを見落とす → 唯一のコピーごと消える」P1 が出た (ignored ファイル・`GIT_DIR` 継承・
`assume-unchanged`/`skip-worktree` の index フラグ)。見落とし方を列挙して塞ぐやり方は 4 巡目がありうる
ので、**判定が見落としても復旧できる**ように設計を変えた: `remove` と判定したものは `git worktree remove`
で消す代わりに `git worktree move` + `git worktree lock` で `.quarantine/` へ移す。**実際に消す操作は
このツールには無い** — 最終的な削除は人間が手で行う (このツールの外)。

```
python3 scripts/worktree_gc.py                       # dry-run (既定。何も変えない)
python3 scripts/worktree_gc.py --quiet               # 集計だけ (remove / keep の件数と keep の理由別件数)
python3 scripts/worktree_gc.py --json                # 機械可読 (1 行 1 worktree の action / reason / detail)
python3 scripts/worktree_gc.py --fetch                # 判定の前に git fetch --prune origin
python3 scripts/worktree_gc.py --apply                # remove と判定したものを隔離する (削除しない)
python3 scripts/worktree_gc.py --list-quarantine      # 隔離済みの一覧 (隔離日時・元の場所)
python3 scripts/worktree_gc.py --restore <path>       # 隔離を元に戻す (隔離先・元のパスのどちらでもよい)
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
| 10 | `assume-unchanged` / `skip-worktree` の index フラグが付いた tracked ファイルが無い (`git ls-files -v` が空)。どちらのフラグも付いたファイルへの編集は `git status` に**一切出ない** — 空の status は「変わっていない」を示さない (PR#239 3巡目、t071) | `index-flags-present` |
| 11 | `core.ignoreStat` が有効でない。有効だと、以後の checkout 等で触れたファイルへ自動で assume-unchanged が付き、上の 10 の観測が今後の編集を拾えなくなる (PR#239 3巡目、t071) | `core-ignorestat-enabled` |
| 12 | HEAD から辿れるコミットがすべて `origin/*` にある (merge 済み、または remote branch に push 済み) | `unpushed-commits` |

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

`readlink(cwd)` が ENOENT/ESRCH 以外 (典型は EACCES) を返したときのフォールバックの `stat(/proc/<pid>)` も、
**その stat 自体が失敗した場合は ENOENT/ESRCH だけを「消えた」として続行し、それ以外はスキャン失敗として
返す** (PR#239 2巡目 P2、t064、2026-09-27)。両方が EACCES を返す read-only な probe を「消えた」に潰すと、
`([], '')` = スキャン成功として `classify()` に渡り、実際には worktree の中で作業中の Worker プロセスの
在圏を見落として remove へ進みうる。族A (観測の失敗を「不在」に潰す) の欠陥が lsof の rc チェック
(finding #3) と同じ型で `/proc` 経路のこの 1 箇所にだけ残っていた。

## git 呼び出しの環境分離 (`_git_env()`)

`run_git()` は必ず `git -C <path> ...` の形で候補 worktree を指定するが、**環境変数がその指定を上書き
しうる** (族B — 検査した対象と実際に作用する対象が違う。PR#239 2巡目 P1、t064、2026-09-27)。
`_git_env()` は呼び出し元のシェルから継承した `os.environ` のうち、リポジトリ/worktree/index の場所を
決める変数 (`GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_OBJECT_DIRECTORY` /
`GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_COMMON_DIR` / `GIT_NAMESPACE`) を明示的に unset する。
これらが残っていると、`git -C candidate status` が実際には **candidate ではなく env の指す別リポジトリ**
を検査してしまい (git hook の中・別ツールのラッパー経由・`git -C` を多用するセッションなど混入経路は
複数考えられる)、未コミットの変更があっても「クリーン」に見え、他の条件さえ揃えば remove されうる
(`--fetch` の `git fetch --prune origin` や `apply_quarantine` の再判定ステップも同じ `_git_env()` を
通るため影響範囲は同じ)。

## `--apply` の安全策 (隔離。削除しない — PR#239 3巡目、t071)

- **`git worktree remove` は呼ばない。** remove と判定したものは `git worktree move <candidate> <隔離先>`
  で `.claude/worktrees/.quarantine/<timestamp>/<元の相対パス>` へ移し、直後に
  `git worktree lock --reason "quarantined by worktree_gc <timestamp>"` を付ける。**元の絶対パスは
  reason に埋め込まない**（t080 P2-2 — 埋め込みの改行を含むパスは DOTALL 無しの正規表現でマッチ自体に
  失敗し、末尾の改行は `git worktree lock` 自身が読み出し時に黙って落とすことを実機で確認した。reason
  に何を書いても手遅れ）。元のパスは、隔離先のディレクトリ階層（`.quarantine/<timestamp>/` を除いた
  残り = 元の相対パス）から `_quarantine_path_parts()` が機械的に復元する — ディレクトリ名はどんな
  バイト列（埋め込み・末尾の改行を含む）も失わずに保持できる。`move` は登録・ブランチ・未コミットの
  変更をすべて保ったまま移動し、`lock` された worktree は `git worktree prune` でも消えない。
  `--list-quarantine` / `--restore` は lock の成否を見ない — move さえ済んでいれば機能する
  （t080 P2-1: move は成功したが lock が失敗した entry も、一覧・復旧の対象になる）。
- `timestamp` は秒精度ではなく `%Y%m%d-%H%M%S-%f` (マイクロ秒まで)。同じ元パスを 2 回に分けて隔離する
  2 回の `--apply` が同じ秒に収まると、秒精度では隔離先が文字列として一致し 2 回目が「隔離先が既に存在
  する」で失敗する (`second-precision-timestamp-is-not-a-generation` と同型。世代として突き合わせる値は
  衝突してはいけない)。
- **`git branch -d` / `-D` も呼ばない** — 隔離では branch に触れる理由が無い (branch は移動した worktree
  にそのまま残る)。`rm -rf` / `shutil.rmtree` / `unlink` は使わない。remote branch は触らない。
- 隔離先が既に存在する場合は **`git worktree move` を呼ばずに失敗として報告する**。空ディレクトリで
  あっても `git worktree move` は「その中へ移す」(`mv` と同じ挙動) なので、既存の何かを上書きすることは
  ないが、意図しない場所への配置になりうる (`git worktree move` 自身は「成功」を返すので、これを見落とすと
  誤配置に気付けない)。
- `git worktree move` が失敗する場合 (submodule を含む worktree 等) は、**削除にフォールバックせず keep**
  にする (族A — 安全な操作の失敗を、より危険な操作へのフォールバックの合図にしない)。
- **repository-wide の `git worktree prune` は呼ばない** (PR#239 F2、2026-09-27 で削除。隔離設計でも
  この制約は変わらず有効)。元の実装は `--apply` のたびに全 verdict が keep でも `state.yaml` が読めなくても
  無条件に走らせており、管理対象ディレクトリの外にある worktree (一度も verdict を出していない対象) の
  メタデータまで消しうった。
- **隔離する直前に、その worktree の判定をもう一度やり直す** (dry-run と `--apply` の間に Worker が起動した・
  変更が入った・mission が active になった、を拾う)。remove でなくなっていれば `skipped`。
- 1 件の失敗で止めない。失敗は報告して次へ進み、終了コード 1。
- **実際の削除はこのツールの外。** 隔離された worktree を最終的に消すかどうかは人間が判断する。
- これらは `tests/test_worktree_gc.py::TestNoForcefulOperations` が **AST で固定**している
  (`run_git(...)` の literal 引数から verb / flag を洗い出し、`--force` / `-f` / `-D` / `--hard` 等が
  1 つでも増えたら赤。ファイルを直接消す呼び出しも赤。`worktree remove` / `worktree prune` / `branch` の
  呼び出しが 1 つでもあれば赤 — 隔離設計では削除する verb 自体が存在しない）。

## 隔離領域そのものの扱い

- **`classify()` での「安全に keep」の判定**（`.quarantine/` 配下は次の `--apply` に巻き込まない）は、
  パスが `.quarantine/` 配下であること**だけ**で足りる（`R_QUARANTINE_UNVERIFIED`。t080 P2-1）。
  ここで lock の確認までは要求しない — move が成功してさえいれば（lock が失敗していても）安全側の
  keep に倒す。「正体の分からないエントリを自動で動かさない」ためのガードなので、`.quarantine/` 配下に
  何があろうと（手動で置かれた何かでも）そのまま通常の分類（mission slug 等）へ進ませない。
- **「lock まで含めて完全に確認できた」("--restore で戻すか --list-quarantine で" の案内を出す
  `R_QUARANTINED`)** は、構造上の場所 (`.quarantine/<timestamp>/<rel>`) **と** lock の reason に
  埋め込まれた timestamp が**その場所の timestamp と一致する**ことの 2 つの一致で確かめる
  (`_is_our_quarantine_entry()`。族B: 対象の同定)。`R_QUARANTINED` と `R_QUARANTINE_UNVERIFIED` は
  どちらも KEEP なので、`--apply` の安全性そのものはどちらでも変わらない — 違いは人間への案内
  （lock されていて `--restore` にすぐ進めるか、確認が要るか）だけ。
- **`--list-quarantine` / `--restore` は lock を見ない**（`_quarantine_path_parts()` がディレクトリ
  階層だけで判定・復元する。t080 P2-1）。move だけ成功した (lock 失敗) entry も一覧・復旧できる。
- `.claude/worktrees/` は `.gitignore` 済みなので、その下の `.quarantine/` も追跡対象にならない。

## 隔離の運用 (一覧・復旧・最終削除)

- `--list-quarantine` (`--json` 可): 隔離済みの worktree を隔離日時・元の場所つきで一覧する。
- `--restore <path>`: 隔離を元に戻す (unlock + move back)。`<path>` は隔離先・隔離される前の元のパスの
  どちらでもよい。**元の場所に既に何かあれば上書きせず拒否する** (`git worktree move` は既存ディレクトリを
  「その中へ移す」ので、素通しすると誤配置になる)。同じ元パスが複数回隔離されて一意に決まらないときは
  隔離先のパスを直接指定するよう求める (`--list-quarantine` で一覧してから指定する)。
- **最終的な削除はこのツールの外、人間が手で行う** (`git worktree remove` / `rm -rf` 等。隔離した worktree
  を眺めて安全だと確認したあとで)。このツール自身は削除する経路を持たない。

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
- **戻せない操作はこのツールには無い** (PR#239 3巡目、t071 以降)。`--apply` がやるのは `git worktree move`
  + `git worktree lock` だけで、`move` さえ成功していれば `lock` が失敗していても `--restore` で完全に
  戻せる (t080 P2-1: unlock はロック済みのときだけ呼ぶ。move back はファイル・登録・branch のすべてを
  戻す)。実際に消す操作 (`git worktree remove` や `rm -rf`) はこのツールの外、人間の判断で行う。
- **`git worktree move` 自体の原子性はこのツールの外側の前提。** move がファイルシステム上の移動と
  git 内部の登録更新の両方を行う操作である以上、その 2 つが中途半端に食い違う状態はこのツールの検出・
  復旧の対象外 (git 自身の実装に委ねている)。
- **既知の限界 (直さないと決めた backlog、5 巡目、t035 / PR#239 QA)**:
  - `_scan_with_lsof()` は lsof の `n` 欄が出すエスケープ済みファイル名 (`\r` `\n` `\xHH`) をデコードせずに
    そのまま realpath と比べる。`/proc` が使えない環境 (macOS 等) だけの代替経路で、見落としても隔離は
    復旧できるため本題外と判断した (backlog #4)
  - `.quarantine/` そのものが symlink だと `apply` は移動・lock できるが、`--list-quarantine` /
    `--restore` は resolve した worktree と resolve しない root を比べていて見つけられない。worktree は
    消えず lock されたまま残るので手で戻せる (backlog #4)

## 戻し方

この道具は **`--apply` を付けない限り何も変えない**。共有規則ではなく (dispatcher / plan.sh は読まない)、
env の停止スイッチも付けていない。PR を revert すれば道具が無くなるだけで、稼働中のものへの影響は無い
(デーモンの再起動も要らない)。`--apply` で隔離した worktree を戻したいときは `--restore` (上の「隔離の運用」)。

## 検証

- `python3 -m pytest tests/test_worktree_gc.py -q` — 一時 git repo (origin = bare) と隔離した queue / registry で、
  各条件の keep / remove・観測できない場合の保留・隔離しても keep が残る・再判定・`--restore` の往復・
  `-D` / `--force` / `worktree remove` / `branch` を一切呼ばない、を確かめる。
- 赤の実証: `bash tests/red_proof_t033.sh` (baseline + 34 ケース。W/X/Y は PR#239 1巡目の Codex findings 3 件
  (t057)、Z/AA は PR#239 2巡目の findings 2 件 (t064: GIT_DIR 系の継承・`/proc` stat の EACCES 扱い)、
  BB〜FF は PR#239 3巡目の findings (t071: index フラグ / `core.ignoreStat` の検出漏れ・隔離設計への回帰
  ３パターン)、GG〜II は PR#239 4巡目の findings (t080: move 成功・lock 失敗が再隔離される / 一覧・復旧が
  lock に依存する / 元のパス復元が改行で切り詰められる)。P (branch を `-D` で消す) は t071 で退役 —
  隔離設計になり branch を消す経路自体が無くなったため)。
