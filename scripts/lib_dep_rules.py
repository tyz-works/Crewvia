"""「依存が満たされた」の定義 —— crewvia の中で唯一の置き場。

この規則を読む主体は 3 つある。

* `plan.sh pull`     — この task を割り当ててよいか
* `plan.sh task-graph` — DAG に READY と書くか WAIT と書くか
* `dispatcher.sh`    — idle Worker にこの task を投げてよいか

3 つが別々に規則を持つと、ズレは静かに起きる。しかも痛むのは常に同じ瞬間だ:
QA が FAIL した直後 —— 「次に何が動けるのか」を最も知りたい瞬間 —— にだけ、
実際は dispatch される task を DAG が WAIT と表示する、あるいは逆に、
dispatcher が投げない task を READY と表示する。

PR #212 は plan.sh の中を `unmet_dependencies()` に一本化したが、
dispatcher.sh には同じ規則の独自コピーが残っていた (QA t002 の指摘 F-2b)。
「今は一致している」は、片方だけ直せる形が残っているかぎり保証ではない。
そこでコピーを消し、両者がこのファイルを読む形にした。

このモジュールは **フォールバックを持たない**。読み込めなければ呼び出し側は
そのまま死ぬ。ここで「読めなかったら自前の規則で続行」に倒すと、規則が 1 つで
あるという性質そのものが、最も気付きにくい形 (壊れた環境でだけコピーが動く)
で失われる。

## failed の依存は「満たされた」ではなく「保留」(t007 / backlog #9)

以前の規則は `failed` / `cancelled` の依存を *満たされた* 扱いにしていた
(QA が FAIL した直後に、その fix task まで永久に止まらないように)。だがその規則は
fix task (進めてよい) と review / merge task (進めてはいけない) を区別できず、
QA FAIL の直後に review task が自動で unblock され、merge 寸前まで進んだ。

いまは:

* `failed` の依存 = **保留 (held)**。unmet に数え、誰も自動では進めない。
  Director が `plan.sh release-dep` で解除したとき (card の `released_deps`) だけ
  満たされる。保留は `plan.sh status` に理由と解除コマンド付きで出る。
* `cancelled` の依存 = 従来どおり満たされた扱い。Director 自身が下した判断なので、
  保留にすると自分の判断で下流が止まる。

選択肢の比較 (hard/soft 区別 vs 明示保留) は knowledge/failed-dependency-hold.md。
"""

from __future__ import annotations

from collections import namedtuple

#: 「この依存はもう完了しない」ことが確定しており、**Director 自身の判断で**そうなった
#: status。満たされた扱いにして下流を進める。
#:
#: 完了した status (`done` / `verified` / `skipped` = plan.sh の
#: TERMINAL_STATUSES) はここに入れない。あちらは done_ids として渡ってくる。
DEAD_DEP_STATUSES = ('cancelled',)

#: 「もう完了しない」が確定しているが、**誰の判断も経ていない** status。保留にして
#: Director の解除 (`released_deps`) を待つ。ここに `failed` 以外を足すときは、
#: それが本当に Director の判断待ちなのかを先に決めること。
HELD_DEP_STATUSES = ('failed',)

#: `unmet` = 満たされていない依存すべて (保留を含む)。`held` = そのうち、Director の
#: 解除待ちのもの。`held` は必ず `unmet` の部分集合。
DependencyVerdict = namedtuple('DependencyVerdict', ['unmet', 'held'])


def unmet_dependencies(blocked_by, done_ids, task_statuses, released=()):
    """`blocked_by` のうち、まだ満たされていない依存の一覧を返す。

    `released` は Director が明示的に解除した依存 (card の `released_deps`)。効くのは
    その依存が **いま `failed` のときだけ** —— まだ走っている依存を解除しても待つ。
    「事前に解除しておく」(failed になったら待たない) はできるが、今の依存を飛ばす
    ことはできない。

    存在しない task への依存 (dangling) は `task_statuses` に無いので unmet 側に
    落ちる。crewvia の pull もそう扱う (永久に blocked) ので、ここでも同じ。
    解除しても dangling は満たされない (status が無いので `failed` ではない)。
    """
    released = set(released or ())
    unmet = []
    for dep in (blocked_by or []):
        if dep in done_ids:
            continue
        status = task_statuses.get(dep)
        if status in DEAD_DEP_STATUSES:
            continue
        if status in HELD_DEP_STATUSES and dep in released:
            continue
        unmet.append(dep)
    return unmet


def held_dependencies(blocked_by, done_ids, task_statuses, released=()):
    """`unmet_dependencies()` のうち、Director の解除待ち (保留) のもの。"""
    return [dep for dep in unmet_dependencies(blocked_by, done_ids, task_statuses, released)
            if task_statuses.get(dep) in HELD_DEP_STATUSES]


def card_dependencies(meta, done_ids, task_statuses):
    """card (frontmatter の dict) から `DependencyVerdict` を作る。

    pull / task-graph / dispatcher は **これだけ**を呼ぶ。`blocked_by` と
    `released_deps` を呼び出し側が別々に取り出す形だと、片方 (released_deps) を
    渡し忘れる経路ができる —— 渡し忘れは「常に保留」に倒れるので事故にはならないが、
    Director が解除したのに誰も進めない、という見えにくい壊れ方になる。
    """
    blocked_by = [d for d in (meta.get('blocked_by') or []) if d]
    released = meta.get('released_deps') or ()
    return DependencyVerdict(
        unmet_dependencies(blocked_by, done_ids, task_statuses, released),
        held_dependencies(blocked_by, done_ids, task_statuses, released),
    )
