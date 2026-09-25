# failed の依存は「保留」にする (t007 / backlog #9)

## 起きたこと

QA が FAIL した直後、その QA に `blocked_by` している review / merge task が **自動で
unblock され**、レビュアーが `pull --task` で取り直して merge 寸前まで進んだ。
Director の制止が間に合わなかっただけで、止める仕組みは無かった。

## 根本原因

`scripts/lib_dep_rules.py` の `DEAD_DEP_STATUSES = ('failed', 'cancelled')` が、`failed`
の依存を「もう完了しない」として **満たされた扱い** にしていた。意図は「QA FAIL 直後に
fix task まで永久に止まるのを避ける」で、これ自体は正しい (PR #108)。

穴は **「failed の依存を満たされた扱いにする規則が、fix task (進めてよい) と review /
merge task (進めてはいけない) を区別できない」** こと。`plan.sh pull` /
`plan.sh task-graph` / `dispatcher.sh` は同じ規則を読むので、3 者そろって同じ穴を通した。
`pull --task` の blocked_by ガード (PR #108 の defense-in-depth) も、同じ規則を使うので
効いていなかった。

worker.md / 過去の task 記述の「着手したらまず QA の status を確認し fail なら差し戻せ」は
**手続き的な防御** —— 行動点で発火しないガードの一種で、規則そのものを直さない限り
また抜ける。

## 選択肢の比較

| | (a) hard / soft の区別 | (b) failed dep は明示的な保留 | (c) 現状維持 + 手続き |
|---|---|---|---|
| 仕組み | 依存の辺ごとに hard (failed も待つ) / soft (failed は進む) を持たせる。review 系は hard | failed dep を持つ task は「Director の再計画待ち」。dispatch も pull も拒否。Director が解除したときだけ進む | worker.md の注意書きだけ |
| 誰が決めるか | **plan を書く Director が、まだ何も起きていない時点で** 辺ごとに選ぶ | **failed が起きた後に、実際の状況を見て** Director が選ぶ | Worker が着手後に自分で気付く |
| 選び忘れたとき | 既定を hard にすれば安全側 (= 結局 (b) と同じ保留になり、解除手段が要る) / 既定を soft にすれば今回の事故がそのまま残る | 何も選ばなくても止まる (安全側) | 事故が再発する |
| 永久保留のリスク | hard の辺が failed になると、解除手段が別に要る | 解除手段 (`release-dep`) と可視化を最初から持つ | — |
| 必要な変更 | frontmatter に辺ごとの属性、`plan.sh add` の記法拡張、既存 card の移行、review skill の記述規則 | 判定関数 1 つ + 解除コマンド 1 つ + 表示 | なし |

**(b) を採用した。** 理由:

1. failed の依存を持つ task を進めてよいかは、**failed になった理由 (QA の指摘の中身) を
   見てから**でないと決められない。(a) は plan 時点で決めさせるので、決める材料が無い。
2. 選び忘れが安全側に倒れる。(a) の既定を soft にすると事故が残り、hard にすると
   結局 (b) の解除手段が要る。
3. (a) の良い部分は **事前解除** として (b) に取り込める: `release-dep --dep <id>` は、
   まだ failed でない依存を名指しできる (「もし failed になっても待たない」。今の依存は
   待ったまま)。fix task を最初から「QA が落ちても進める」と決めておきたいなら、それで足りる。

### 「永久保留」という別の outage を作らないために

保留にすると、Director が気付かなければ task は誰にも拾われない。だから出口を 3 つ置いた:

- **見える**: `plan.sh status` (要約にも詳細にも) が `🛑 tNNN ... HELD: 依存 tXXX が failed —
  Director の判断待ち。進めるなら plan.sh release-dep tNNN` を出す。詳細を開かないと
  見えない場所には置かない。DAG (`task-graph`) は `[保留: tXXX が failed]` の印で blocked
  にする。dispatcher のログは `[held]` で出口のコマンドまで書く。
  自動選択の pull が空振りしたときの診断にも同じ文面が付く。
- **解除できる**: `plan.sh release-dep <task_id> [--dep <csv>]`。card に `released_deps`
  を記録し、`blocked_by` は消さない (DAG に依存の履歴が残る)。
- **打ち間違いが解除に見えない**: `blocked_by` に無い依存の名指し、pending でない task、
  保留が無い task への引数なし実行は、どれも 1 バイトも書かずに拒否する。

`cancelled` の依存は従来どおり満たされた扱い。`cancelled` は Director 自身が下した判断
(task を中止した) なので、保留にすると自分の判断で下流が止まる。

## 実装 (規則は 1 箇所)

- `scripts/lib_dep_rules.py`: `HELD_DEP_STATUSES = ('failed',)`。
  `card_dependencies(meta, done_ids, statuses)` が `DependencyVerdict(unmet, held)` を返す。
  **3 者はこれだけを呼ぶ** — `blocked_by` と `released_deps` を呼び出し側が別々に
  取り出す形だと、片方を渡し忘れる経路ができるので、card (meta) を丸ごと渡す。
- `plan.sh pull` (自動選択 / `--task`)・`plan.sh task-graph`・`plan.sh status`・
  `dispatcher.sh` (`dependency_gate()`) が同じ答えを出す。
- 検証: `tests/test_failed_dependency_hold.py` が、18 通りの依存パターン
  (done / verified / skipped / cancelled / pending / in_progress / verification_failed /
  needs_director / blocked / dangling / failed / 解除済み / 事前解除 / 複数依存の組み合わせ) ×
  5 者 (自動 pull / `pull --task` / task-graph / status / dispatcher) の突き合わせを直接
  assert する。どれか 1 者が別の答えを出すと赤になる。

`taskvia-sync.sh` の blocked 判定は元から done しか満たされた扱いにしない (より保守的)
ので、保留とは矛盾しない。

## merge 後に必要な作業 (restart)

**`lib_dep_rules.py` は dispatcher が起動時に import するので、merge 後に dispatcher の
restart が必要** (restart は Director が行う。`lib_daemon_watch.py restart dispatcher`)。
watchdog はこのモジュールを読まない (`grep lib_dep_rules scripts/watchdog.py` は 0 件) ので
この変更のための restart は要らないが、既存の運用どおり両方を一度に restart しても害は無い。

restart するまでの間は、走っている dispatcher が **古い規則** (failed を満たされた扱い) で
task を Worker に投げる。その kickoff は新しい `plan.sh pull --task` (defense-in-depth の
ガード) が `HELD` で拒否するので、review task が merge まで進むことは無い —— ただし
Worker の起動 1 回分 (kickoff の prompt) が無駄になる。これは
`knowledge/dispatcher-restart-after-merge.md` の一般則の一例。

## rollback (本番で問題が出たとき)

**停止スイッチは設けていない。** env var で規則を切り替える形にすると、長寿命の
dispatcher (起動時の env で固定) と、呼ばれるたびに env を読み直す `plan.sh` で **別の答えが
出る** —— この変更が消そうとしている「3 者の食い違い」を、スイッチ自身が作ってしまう。

- **個々の task を今すぐ進めたい**: `plan.sh release-dep <task_id>` (旧規則と同じ結果になる)。
  merge の直後に大量の保留が出て捌けないなら、`plan.sh status` の 🛑 を 1 件ずつ解除する。
- **規則ごと戻したい**: この PR を revert する (1 PR = 1 revert)。順序:
  1. main で revert PR を merge
  2. dispatcher を restart (Director。これをしないと dispatcher だけが新しい規則のまま)
  3. `plan.sh status` で保留が消えていることを確認
  card に残った `released_deps` 行は revert 後の plan.sh には読まれない余分なキーで、
  害は無い (revert 前の plan.sh で `update` を打っても行は保たれることを確認済み。
  消したければ手で行を削除)。
