#!/usr/bin/env python3
"""旧形式の `update --reset` を再現する (01c E4a。rollback 中の旧コード・E4a より前の plan.sh が書いた形)。

旧形式の reset は status / worker / started_at だけを動かして assignment の枠を外し、execution の欄 (current_execution_id /
execution_status ...) には触れない。E4a の `update --reset` は試行も閉じるので、E2 の CAS の「旧形式の書き手との共存」の
テストはこの script で旧形式の書き込みを作る (本物の plan.sh を呼ばない)。

    python3 old_format_reset.py <queue> <mission> <task>
"""

import pathlib
import re
import sys


def main(argv):
    queue, mission, task = pathlib.Path(argv[1]), argv[2], argv[3]
    card = queue / "missions" / mission / "tasks" / f"{task}.md"
    text = card.read_text()
    for key, value in (("status", "pending"), ("worker", "null"), ("started_at", "null")):
        text, n = re.subn(rf"^{key}:.*$", f"{key}: {value}", text, count=1, flags=re.M)
        if n != 1:
            print(f"old_format_reset: {key} の行が見つからない", file=sys.stderr)
            return 1
    card.write_text(text)
    assignments = queue / "assignments"
    for slot in assignments.glob("*") if assignments.is_dir() else ():
        if slot.is_file() and slot.read_text().strip() == f"{mission}:{task}":
            slot.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
