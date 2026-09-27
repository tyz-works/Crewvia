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
  Director の判断待ち。進めるなら plan.sh release-dep tNNN --mission <slug>` を出す。詳細を
  開かないと見えない場所には置かない。DAG (`task-graph`) は `[保留: tXXX が failed]` の印で
  blocked にする (印は理由だけで、コマンドは持たない)。dispatcher のログは `[held]` で出口の
  コマンドまで書く。自動選択の pull が空振りしたときの診断と、`pull --task` の拒否にも同じ
  文面が付く。
- **案内のコマンドは `--mission` 付き**: task ID は mission ごとの自動採番なので、別 mission
  に同じ `tNNN` が普通にある。`--mission` の無い `release-dep` / `update` は default_mission の
  task に当たり、**案内どおり打つと別の task が解除される / skip され、意図した task は保留の
  まま**になる (PR #217 Kai P1)。`held_dependency_hint(task_id, held, slug)` の `slug` は既定値の
  無い必須引数で、案内を出す 4 経路 (status 要約 / status 詳細 / pull 診断 / pull --task) は
  全部これを通る。**保留を見つけた人が打つ手順は必ずこの形**:
  ```
  plan.sh release-dep <task_id> --mission <slug>                  # 進めてよい
  plan.sh update <task_id> --mission <slug> --status skipped      # 中止
  ```
  (`plan.sh done` が `--mission` を要るのと同じ理由。)
- **解除できる**: `plan.sh release-dep <task_id> [--dep <csv>] [--mission <slug>]`。card に
  `released_deps` を記録し、`blocked_by` は消さない (DAG に依存の履歴が残る)。
- **`released_deps` は task ID (`tNNN`) の list だけ受理する** (PR #217 Kai P2)。欄が無い /
  `null` / `[]` は「解除なし」。`true` / `123` / `t001` (list でない) / mapping /
  要素が ID の形でない list は、card ごと `[破損]` に隔離する (`lib_task_cards.py` の
  `released_deps_problem()` が判定の本体)。黙って解釈すると 2 方向に壊れる: `set(True)` の
  `TypeError` で **1 枚の card が dispatch と status を落とす** / mapping `t001: false` が
  `set()` でキーだけになり **failed の依存を誤って解除する**。
  - **隔離は出口を塞がない**: `plan.sh status` に `💥 tNNN [破損] — released_deps ...` と直し方が
    出る。`plan.sh release-dep <id> --mission <slug>` は不正な値を捨てて `[tXXX]` の list に
    書き直す (解除の**記録**を捨てるだけで、保留を外す方向には働かない)。`update --blocked-by`
    も同様に不正な値を捨てる。手で `released_deps: [t001]` に直してもよい。
  - 2 枚目の網: `lib_dep_rules.card_dependencies()` は list-of-str 以外を **解除なし (= 保留のまま)**
    として扱う。読み取りの隔離を通らない生の card (release-dep が読むもの) が来ても、落ちず、
    誤って解除もしない。判定の本体は 1 つ (`released_deps_problem`)、2 枚目は型だけを見る
    (`lib_dep_rules` は他を import しない)。
- **`blocked_by` は「空でない文字列の list」だけ受理し、falsy な要素を絶対に落とさない**
  (PR #217 Kai 2 巡目 P2 — t025 の修正が作った fail-open 回帰)。受理するのは欄が無い /
  `null` / `[]` (= 依存なし) と `[t001, t002]`。`[null]` / `[false]` / `[0]` / `[""]` / `["   "]` /
  `[123]` / `[true]` / mapping / 素の文字列 / bool / int は、card ごと `[破損]` に隔離する
  (`lib_task_cards.py` の `blocked_deps_problem()` が判定の本体)。
  **なぜ落としてはいけないか**: 依存の宣言は「これが済むまで進めるな」という**制約**で、読み違えて
  落とすと制約が消える。t025 が `released_deps` を検証したときに入れた `if d` (truthiness) フィルタが
  `blocked_by: [null]` を「依存なし」にし、pull も dispatch も開始した。#9 が潰そうとした事故
  (依存が満たされていないのに下流が進む) を逆向きから作り直し、修正前 (`null` が unmet) より悪かった。
  `blocked_by: false` / `0` の `or []` も同じ穴 (「無い」に潰す)。
  - **隔離を選んだ理由** (「falsy を unmet で保持」だけにしない): 保持だけだと、`[null]` の card は
    `plan.sh status` に依存名 `None` の待ちとして出て、原因が読めない。隔離なら `[破損]` と理由・
    直し方が出る。**2 枚目の網も持つ** (下)。
  - **隔離は出口を塞がない**: `plan.sh update <id> --mission <slug> --blocked-by t001,t002`
    (`--blocked-by ""` で依存なし) は raw の card を書き直すので、隔離された card にも効く。
    `release-dep` は、何の依存を解除するのか読めない card では**断る** (`released_deps` と違い、
    不正な値を捨てて書き直さない — `blocked_by` は宣言そのもので、捨てると制約が消える)。
  - **2 枚目の網**: `lib_dep_rules.declared_dependencies()` は、要素を**1 つも落とさず**、空でない
    文字列でないものを `<不正な依存: 値>` という依存名にして unmet に残す。list でない値は丸ごと
    `<不正な blocked_by: 値>` 1 件にする。反復で `TypeError` も、文字列の 1 文字ずつ分割も起きない。
    `card_dependencies()` / `unmet_dependencies()` / task-graph / release-dep がこれを通る。
  - **形だけを見る**: 存在しない task ID (dangling) は受理する (unmet で永久に待つ = 既に fail closed)。
    `tNNN` の形まで縛ると、別の id 体系の card を黙って待たせず隔離してしまう。
  - 検証: `tests/test_malformed_blocked_by.py` が 17 種の不正値 × 5 者 (自動 pull / `pull --task` /
    task-graph / status / 実 dispatcher 1 サイクル) で「開始されない」ことと、健全な形の対照
    (進める / 待つ) を固定する。消費側に `if d` 型のフィルタが戻らないことは構造テスト
    (`test_no_consumer_drops_falsy_dependencies`) が固定する。欠陥注入は `tests/red_proof_t025.sh` の P3。
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
- **dispatcher は呼び出し側まで実挙動で固定する** (QA t008 観点 5 / t025):
  `dependency_gate()` を直接問うだけでは、`dispatch()` の呼び出し箇所で旧規則を直書きされても
  全スイートが緑のままだった (D1)。`tests/test_dispatcher_cycle_honours_hold.py` が本物の
  `dispatch()` を、mux だけフェイク・idle Worker 1 人で 1 サイクル回し、同じ依存パターンで
  「kickoff が飛んだか」を assert する (挙動 = 本筋)。併せて AST で、`dispatch()` が
  `dependency_gate` を呼ぶこと・規則の名前を直接触らないこと・`verdict` を上書きしないことを
  固定する (形。これだけでは verdict を触らない迂回 — D3 — を見逃すので挙動が要る)。
  欠陥注入 (D1-D3) は `tests/red_proof_t025.sh`。
- 見送り (P3): `scripts/taskvia-sync.sh` の `taskvia_status()` は独自の blocked 判定
  (done のみ満たされた扱い) を持ち、保留 / 解除を判別できない。表示だけの差で dispatch・pull
  には効かず、より保守的な側 (blocked と出す) に倒れる。TASKVIA_TOKEN が無い環境では同期自体が
  skip される。直すなら `lib_dep_rules.card_dependencies()` を読む別 task で。

`taskvia-sync.sh` の blocked 判定は元から done しか満たされた扱いにしない (より保守的)
ので、保留とは矛盾しない。

## merge 後に必要な作業 (restart)

**`lib_dep_rules.py` / `lib_task_cards.py` は dispatcher が起動時に import するので、merge 後に
dispatcher の restart が必要** (restart は Director が行う。`lib_daemon_watch.py restart dispatcher`)。

watchdog は **`lib_dep_rules.py` は読まない** (`grep lib_dep_rules scripts/watchdog.py` は 0 件) が、
**`lib_task_cards.py` は読む** (`from lib_task_cards import ...` で `list_task_cards()` を使う)。
`released_deps` / `blocked_by` の検証 (P2) は `lib_task_cards.py` にあるので、restart するまで
watchdog の目には不正な `released_deps` / `blocked_by` の card も `pending` のまま見える。watchdog は依存を判定せず
task の status しか使わないので、**誤動作はしない (= restart は必須ではない)** が、
`plan.sh status` と watchdog の見え方を揃えるなら両方を一度に restart する
(既存の運用どおり、害は無い)。`verifier-dispatcher.sh` / `taskvia-sync.sh` も
`lib_task_cards` を読むので、常駐させているなら同じ扱い。

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
