"""E5 PR-2 の後始末: 報告 (done / fail / needs-director …) の前に pull していたテストの道具。

名乗りなしの報告は、実行中の試行がある task に対して拒否される (`EXECUTION_REQUIRED`)。これらのテストの主題は名乗りではないので、
Worker が worker.md どおりにするのと同じく、**自分の pull の JSON の `execution_id` を覚えて**、同じ task への報告に `--execution` で付ける。
拒否を緩めるのではなく、テスト側が名乗る (card の `current_execution_id` を読んで補うことはしない — pull の JSON だけが出どころ)。
"""

from __future__ import annotations

import json

ENDING = frozenset({"done", "fail", "needs-director", "verify-result"})
REPORTS = frozenset({"done", "fail", "needs-director", "ready-for-verification", "verifying", "verify-result"})


class AutoName:
    def __init__(self):
        self.ids = {}                                  # task id -> 直近の pull の execution_id

    def before(self, args):
        args = list(args)
        if len(args) < 2 or args[0] not in REPORTS or "--execution" in args:
            return args
        xid = self.ids.get(args[1])
        return args + ["--execution", xid] if xid else args

    def after(self, args, stdout, returncode=None):
        if returncode == 0 and args and args[0] in ENDING:
            self.ids.pop(args[1] if len(args) > 1 else None, None)       # 試行は終わった。以後の報告は名乗る相手がいない
        if returncode == 0 and args and len(args) > 1 and (
                args[0] == "retire" or (args[0] == "update" and ("--reset" in args or "--close-execution" in args))):
            self.ids.pop(args[1], None)                                  # reset / retire で試行は閉じた。保存した ID は捨てる (次の pull が新しい ID を入れる)
        if args and args[0] == "pull" and stdout:
            try:
                data = json.loads(stdout)
            except ValueError:
                return
            if isinstance(data, dict) and data.get("id") and data.get("execution_id"):
                self.ids[data["id"]] = data["execution_id"]
