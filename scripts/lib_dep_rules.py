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
"""

from __future__ import annotations

#: 「この依存はもう完了しない」ことが確定している status。crewvia はこれらを
#: 満たされた扱いにして下流を進める — QA が FAIL した直後に、その fix task まで
#: 永久に止まってしまうのを避けるため。
#:
#: 完了した status (`done` / `verified` / `skipped` = plan.sh の
#: TERMINAL_STATUSES) はここに入れない。あちらは done_ids として渡ってくる。
DEAD_DEP_STATUSES = ('failed', 'cancelled')


def unmet_dependencies(blocked_by, done_ids, task_statuses):
    """`blocked_by` のうち、まだ満たされていない依存の一覧を返す。

    存在しない task への依存 (dangling) は `task_statuses` に無いので unmet 側に
    落ちる。crewvia の pull もそう扱う (永久に blocked) ので、ここでも同じ。
    """
    return [dep for dep in (blocked_by or [])
            if dep not in done_ids and task_statuses.get(dep) not in DEAD_DEP_STATUSES]
