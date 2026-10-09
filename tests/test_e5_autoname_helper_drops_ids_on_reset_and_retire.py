"""t002 項目 2: tests/e5_autoname.py の AutoName は、reset / retire で試行が閉じた後に保存した ID を捨てる。
捨てないと、次の pull が無い報告 (Director が開き直した card への done 等) に古い ID を名乗らせてしまう。"""

from __future__ import annotations

import json

from e5_autoname import AutoName

XID = "ex-" + "a" * 32


def _pulled():
    a = AutoName()
    a.after(["pull", "--agent", "W"], json.dumps({"id": "t001", "execution_id": XID}), 0)
    assert a.before(["done", "t001"]) == ["done", "t001", "--execution", XID]
    return a


def test_update_reset_drops_the_saved_id():
    a = _pulled()
    a.after(["update", "t001", "--status", "pending", "--reset"], "", 0)
    assert a.before(["done", "t001"]) == ["done", "t001"]


def test_update_close_execution_drops_the_saved_id():
    a = _pulled()
    a.after(["update", "t001", "--close-execution"], "", 0)
    assert a.before(["done", "t001"]) == ["done", "t001"]


def test_retire_drops_the_saved_id():
    a = _pulled()
    a.after(["retire", "t001", "--execution", XID], "", 0)
    assert a.before(["done", "t001"]) == ["done", "t001"]


def test_failed_reset_keeps_the_saved_id_and_other_tasks_are_untouched():
    a = _pulled()
    a.after(["update", "t001", "--reset"], "", 1)
    assert a.before(["done", "t001"])[-1] == XID
    a.after(["update", "t002", "--reset"], "", 0)
    assert a.before(["done", "t001"])[-1] == XID


def test_plain_update_does_not_drop_the_saved_id():
    a = _pulled()
    a.after(["update", "t001", "--priority", "high"], "", 0)
    assert a.before(["done", "t001"])[-1] == XID
