#!/usr/bin/env python3
"""dispatcher.sh の Telegram の差し込み (PR-A) — 本物のコードを名前空間に読み込んで確かめる。

* python 側: `run_telegram_cycle()` (心拍・転送・Director への「無効になった」通知 1 回・未設定なら何も書かない)。
  `tests/test_dispatcher_notify_once.py` の Harness と同じく、dispatcher.sh の埋め込み python を CYCLE ENTRY POINT の手前まで exec する。
  **ネットワークは使わない** (未回答の質問が無い状態・答え済みの台帳だけ。poll は別のテストで偽サーバー相手に見ている)。
* bash 側: `_tg_resolve` / `_tg_maybe_retry` (起動時に 1 回・失敗の間は長い間隔でだけ取り直す・サイクル単位で 1Password を呼ばない) を
  dispatcher.sh から**そのまま切り出して**走らせる。認証情報の運搬は前置代入で、`env` コマンドを使わない (静的 + 実行時)。

実行: python3 -m pytest tests/test_telegram_dispatcher_glue.py -q
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# tree() が比べない registry の名前 (読み取りだけ)。登録簿の静的検査 (test_registry_lock.sh) が近くの書き込みと取り違えないよう、上に置く
IGNORED_REGISTRY_NAMES = ("workers.yaml",)

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(SCRIPTS))

from test_dispatcher_notify_once import FakeMux, Harness, DISPATCHER_SH, SLUG  # noqa: E402
import lib_telegram as t  # noqa: E402

TOKEN = "123456:FAKE-TOKEN-abcdefghij"
CHAT = "5550001"


class TgHarness(Harness):
    def __init__(self, root, monkeypatch, carried=True, reason=""):
        super().__init__(root, monkeypatch)
        # 全 pytest の途中で `lib_mux` が別のモジュールオブジェクトに差し替わっていても、dispatcher の埋め込み python が
        # `from lib_mux import Mux` で引くのは sys.modules の今のもの。**そちらに** FakeMux を差す
        # (継承元は import 時の `lib_mux` に差すので、単独では緑・全体では「Director 不在」になる)。
        import importlib
        live_mux = importlib.import_module("lib_mux")
        monkeypatch.setattr(live_mux, "Mux", FakeMux)
        monkeypatch.setattr(live_mux, "repo_identity_ok", lambda *a, **kw: True)
        (root / "config").mkdir()
        self.cred = root / "telegram.env"
        self.cred.write_text(f"bot_token={TOKEN}\nchat_id={CHAT}\n")
        os.chmod(self.cred, 0o600)
        monkeypatch.setenv("CREWVIA_REPO_ROOT", str(root))
        for k in ("_CREWVIA_TG_RESOLVED_TOKEN", "_CREWVIA_TG_RESOLVED_CHAT_ID", "_CREWVIA_TG_RESOLVE_REASON"):
            monkeypatch.delenv(k, raising=False)
        if carried:
            monkeypatch.setenv("_CREWVIA_TG_RESOLVED_TOKEN", TOKEN)
            monkeypatch.setenv("_CREWVIA_TG_RESOLVED_CHAT_ID", CHAT)
        if reason:
            monkeypatch.setenv("_CREWVIA_TG_RESOLVE_REASON", reason)
        self.daemons = self.registry / "daemons"

    def configure(self, source="file"):
        text = "telegram:\n"
        if source:
            text += f"  credentials:\n    source: {source}\n    file: {self.cred}\n"
        (self.root / "config" / "crewvia.yaml").write_text(text)

    def load(self):
        """CYCLE ENTRY POINT の手前まで exec した名前空間 (run_telegram_cycle を直接呼ぶ)。"""
        self.cycle()
        return self.ns

    def tree(self):
        return {str(p.relative_to(self.registry)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.registry.rglob("*"))
                if p.name not in IGNORED_REGISTRY_NAMES}


@pytest.fixture
def h(tmp_path, monkeypatch):
    return TgHarness(tmp_path / "repo", monkeypatch)


def telegram_messages():
    return [m["message"] for m in FakeMux.sent if m["message"].startswith("[telegram")]


def test_unconfigured_dispatcher_writes_nothing_and_sends_nothing(tmp_path, monkeypatch):
    h = TgHarness(tmp_path / "repo", monkeypatch, carried=False)
    h.configure(source="")
    ns = h.load()
    before = h.tree()
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    assert h.tree() == before and telegram_messages() == []
    assert not (h.daemons / "telegram-receiver.json").exists()


def test_carried_credentials_are_removed_from_the_environment_at_import(h):
    h.configure()
    ns = h.load()
    assert "_CREWVIA_TG_RESOLVED_TOKEN" not in os.environ and "_CREWVIA_TG_RESOLVED_CHAT_ID" not in os.environ, \
        "以降の subprocess (mux send・git) が token を継承しない"
    assert ns["_TG_CARRIED"]["_CREWVIA_TG_RESOLVED_TOKEN"] == TOKEN


def test_heartbeat_is_written_with_non_secret_identifiers_only(h):
    h.configure()
    ns = h.load()
    ns["run_telegram_cycle"]()
    path = h.daemons / "telegram-receiver.json"
    data = json.loads(path.read_text())
    assert data["enabled"] is True and data["reason"] == "ok" and data["bot_id"] == 123456
    assert TOKEN not in path.read_text() and CHAT not in path.read_text()
    assert sorted(p.name for p in h.daemons.iterdir() if p.name.startswith("telegram")) == ["telegram-receiver.json"], \
        "未回答の質問が無い・台帳が無いので、心拍のほかは何も作らない (通信ゼロ)"


def test_a_failure_reason_from_startup_becomes_a_disabled_receiver_and_one_director_notice(tmp_path, monkeypatch):
    h = TgHarness(tmp_path / "repo", monkeypatch, carried=False, reason="credential_command_failed")
    h.configure()
    ns = h.load()
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    data = json.loads((h.daemons / "telegram-receiver.json").read_text())
    assert data == {"enabled": False, "checked_at": data["checked_at"], "reason": "credential_command_failed"}
    notices = telegram_messages()
    assert len(notices) == 1 and "credential_command_failed" in notices[0]
    for _ in range(3):
        ns["run_telegram_cycle"]()
    assert len(telegram_messages()) == 1, "同じ理由の間は繰り返さない (状態ベースで 1 回)"
    # 解けたら (認証情報が取れた) 台帳を捨てる → 次に同じ理由で止まったら再び 1 通
    ns["_TG_CARRIED"]["_CREWVIA_TG_RESOLVED_TOKEN"] = TOKEN
    ns["_TG_CARRIED"]["_CREWVIA_TG_RESOLVED_CHAT_ID"] = CHAT
    ns["run_telegram_cycle"]()
    assert json.loads((h.daemons / "telegram-receiver.json").read_text())["enabled"] is True
    told = json.loads((h.daemons / "notified-state.json").read_text())
    assert "telegram_receiver_disabled" not in told
    ns["_TG_CARRIED"]["_CREWVIA_TG_RESOLVED_TOKEN"] = ""
    ns["_TG_CARRIED"]["_CREWVIA_TG_RESOLVED_CHAT_ID"] = ""
    ns["_TG_RESOLVE_REASON"] = "credential_command_failed"
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    assert len(telegram_messages()) == 1


def answered_ledger(h, qid="q-1a2b3c4d", forwarded=False):
    h.daemons.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (h.daemons / "telegram-questions.json").write_text(json.dumps({qid: {
        "nonce": "9f3a1c", "question": "x", "options": ["A: 続ける", "B: 止める"], "message_id": 4711,
        "status": "answered", "created_at": now - 60, "expires_at": now + 3600, "closed_at": now - 5,
        "forwarded": forwarded, "slug": SLUG, "task": "t001",
        "answer": {"kind": "choice", "index": 1, "at": now - 5, "update_id": 9001}}}))
    return qid


def test_an_answer_is_forwarded_to_the_director_screen_once(h):
    h.configure()
    h.card("t001", "needs_director", needs_director_reason="x")
    ns = h.load()
    qid = answered_ledger(h)
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    lines = [m for m in telegram_messages() if m.startswith("[telegram-answer]")]
    assert lines == [f'[telegram-answer] q={qid} task={SLUG}/t001 choice="B: 止める" index=1 task_state=needs_director']
    assert json.loads((h.daemons / "telegram-questions.json").read_text())[qid]["forwarded"] is True
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    assert [m for m in telegram_messages() if m.startswith("[telegram-answer]")] == []


def test_forwarding_is_skipped_without_recording_when_the_director_is_absent(h):
    h.configure()
    ns = h.load()
    qid = answered_ledger(h)
    FakeMux.directors = []
    FakeMux.sent = []
    ns["_director_live_memo"].clear()
    ns["run_telegram_cycle"]()
    assert telegram_messages() == []
    assert json.loads((h.daemons / "telegram-questions.json").read_text())[qid]["forwarded"] is False
    FakeMux.directors = ["Sora-director"]
    ns["_director_live_memo"].clear()
    ns["run_telegram_cycle"]()
    assert len([m for m in telegram_messages() if m.startswith("[telegram-answer]")]) == 1, "戻ったらすぐ送る"


def test_forwarding_failure_is_retried(h):
    h.configure()
    ns = h.load()
    qid = answered_ledger(h)
    FakeMux.send_ok = False
    ns["run_telegram_cycle"]()
    assert json.loads((h.daemons / "telegram-questions.json").read_text())[qid]["forwarded"] is False
    FakeMux.send_ok = True
    ns["run_telegram_cycle"]()
    assert json.loads((h.daemons / "telegram-questions.json").read_text())[qid]["forwarded"] is True


def test_a_corrupt_ledger_never_raises_out_of_the_cycle(h):
    h.configure()
    ns = h.load()
    h.daemons.mkdir(parents=True, exist_ok=True)
    (h.daemons / "telegram-questions.json").write_text('{"q-1a2b3c4d": 7}')
    ns["run_telegram_cycle"]()
    assert "telegram-questions" in h.log_text()
    assert TOKEN not in h.log_text()


def test_a_broken_lib_never_raises_out_of_the_cycle(h, monkeypatch):
    h.configure()
    ns = h.load()
    monkeypatch.setattr(t, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}")))
    ns["run_telegram_cycle"]()                     # 例外を出さない
    assert "[telegram] cycle failed: RuntimeError" in h.log_text()
    assert TOKEN not in h.log_text(), "例外の型名だけをログに出す (メッセージに token が混ざっても漏らさない)"


def test_an_unreadable_offset_is_reported_to_the_director_once(h, monkeypatch):
    """poll が offset_unreadable を返したら Director に 1 回だけ知らせる (同じ理由を毎サイクル繰り返さない)。"""
    h.configure()
    ns = h.load()
    monkeypatch.setattr(t, "run_cycle", lambda *a, **k: {
        "receiver": None, "forwarded": 0, "polled": True, "identity_changed": [], "poll": {"offset_unreadable": "EACCES"}})
    FakeMux.sent = []
    ns["run_telegram_cycle"]()
    ns["run_telegram_cycle"]()
    lines = [m for m in telegram_messages() if "telegram-offset.json" in m]
    assert len(lines) == 1 and "EACCES" in lines[0]


def _poll_cycle(monkeypatch, polls):
    """run_cycle を差し替え、サイクルごとに polls の次の要素を poll 結果として返す。"""
    it = iter(polls)
    monkeypatch.setattr(t, "run_cycle", lambda *a, **k: {
        "receiver": None, "forwarded": 0, "polled": True, "identity_changed": [], "poll": next(it)})


def test_offset_unreadable_notice_is_cleared_on_recovery_so_the_second_failure_is_told(h, monkeypatch):
    """回復 (読めた) で台帳を畳む。畳まないと `_daemon` slug は prune されず、同じ障害の 2 回目が永久に黙る。"""
    h.configure()
    ns = h.load()
    _poll_cycle(monkeypatch, [{"offset_unreadable": "EACCES"}, {"offset_unreadable": "EACCES"}, {"offset_ok": True},
                              {"offset_unreadable": "EACCES"}])
    FakeMux.sent = []
    for _ in range(4):
        ns["run_telegram_cycle"]()
    assert len([m for m in telegram_messages() if "telegram-offset.json" in m]) == 2, "回復後の 2 回目の障害が通知されない"


def test_a_poll_that_did_not_read_the_offset_does_not_clear_the_notice(h, monkeypatch):
    h.configure()
    ns = h.load()
    _poll_cycle(monkeypatch, [{"offset_unreadable": "EACCES"}, {"error": "ledger_busy"}, {"offset_unreadable": "EACCES"}])
    FakeMux.sent = []
    for _ in range(3):
        ns["run_telegram_cycle"]()
    assert len([m for m in telegram_messages() if "telegram-offset.json" in m]) == 1


def test_an_unwritable_offset_is_told_once_and_cleared_on_recovery(h, monkeypatch):
    h.configure()
    ns = h.load()
    _poll_cycle(monkeypatch, [{"offset_unwritable": True, "offset_unreadable": "EISDIR"}, {"offset_unwritable": True},
                              {"offset_write_ok": True, "offset_ok": True}, {"offset_unwritable": True}])
    FakeMux.sent = []
    for _ in range(4):
        ns["run_telegram_cycle"]()
    lines = [m for m in telegram_messages() if "書けません" in m]
    assert len(lines) == 2, "1 回目と回復後の 2 回目だけ"
    assert not [m for m in telegram_messages() if "読めません" in m], "書けない通知があるときは読めない通知で二重に鳴らさない"


# ---------------------------------------------------------------------------
# bash 側: 起動時に 1 回・前置代入・取り直しは長い間隔だけ
# ---------------------------------------------------------------------------

SH = DISPATCHER_SH.read_text()


def test_the_dispatch_call_passes_credentials_by_prefix_assignment_not_env():
    m = re.search(r"run_dispatch\(\) \{\n(.*?)\n\}\n", SH, re.S)
    body = m.group(1)
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r'^\s*_CREWVIA_TG_RESOLVED_TOKEN="\$tg_token" _CREWVIA_TG_RESOLVED_CHAT_ID="\$tg_chat" \\$', code, re.M)
    assert not re.search(r"\benv\b[^\n]*(_CREWVIA_TG|tg_token|tg_chat)", code), "`env VAR=…` は env の argv に token が載る"
    assert not re.search(r"export [^\n]*(tg_token|tg_chat|_CREWVIA_TG)", SH), "export しない (bash の env にも載せない)"
    # python のサイクルの中で subprocess に env を渡すのは lib_telegram.run_cycle の poll だけ (dispatcher.sh は渡さない)
    assert "_CREWVIA_TG_RESOLVED_TOKEN=" not in re.sub(r"#.*", "", SH.split("run_dispatch() {")[0].split("TG_RESOLVE_RETRY_SECONDS")[0])


def bash_section():
    start = SH.index("TG_RESOLVE_RETRY_SECONDS=600")
    end = SH.index("# ---------------------------------------------------------------------------\n# One dispatch cycle")
    return SH[start:end]


def run_bash(tmp_path, script, env_extra=None):
    cfg_root = tmp_path / "repo"
    (cfg_root / "config").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CREWVIA_TG", "_CREWVIA_TG"))}
    env.update({"CREWVIA_REPO_ROOT": str(cfg_root)})
    env.update(env_extra or {})
    full = f'''set -euo pipefail
SCRIPT_DIR="{SCRIPTS}"
log() {{ echo "LOG: $*" >&2; }}
{bash_section()}
{script}
'''
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, env=env, timeout=120)


def write_op(tmp_path, token=TOKEN, chat=CHAT, fail_file=None, sleep=0):
    counter = tmp_path / "op-calls.txt"
    op = tmp_path / "fake-op"
    cond = f'[[ -e "{fail_file}" ]] && exit 1' if fail_file else "true"
    op.write_text(f'#!/usr/bin/env bash\necho "$2" >> "{counter}"\nsleep {sleep}\n{cond}\n'
                  f'case "$2" in *token*) echo "{token}" ;; *) echo "{chat}" ;; esac\n')
    os.chmod(op, 0o755)
    cfg = tmp_path / "repo" / "config" / "crewvia.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("telegram:\n  credentials:\n    source: op\n    token_ref: op://v/token\n"
                   f"    chat_id_ref: op://v/chat\n    op_command: {op}\n")
    return counter


def test_credentials_are_resolved_once_at_startup_and_never_per_cycle(tmp_path):
    counter = write_op(tmp_path)
    r = run_bash(tmp_path, 'for i in 1 2 3 4 5; do _tg_maybe_retry; done; echo "reason=$tg_reason"; '
                           '[[ "$tg_token" == "' + TOKEN + '" ]] && echo token-held; [[ -z "${_CREWVIA_TG_RESOLVED_TOKEN:-}" ]] && echo not-exported')
    assert r.returncode == 0, r.stderr
    assert "reason=ok" in r.stdout and "token-held" in r.stdout and "not-exported" in r.stdout
    assert counter.read_text().splitlines() == ["op://v/token", "op://v/chat"], "起動時に token と chat_id を 1 回ずつ。サイクルでは呼ばない"
    assert TOKEN not in r.stderr and "telegram credentials: ok" in r.stderr


def test_a_failed_resolution_is_retried_only_after_the_long_interval(tmp_path):
    fail = tmp_path / "fail"
    fail.write_text("x")
    counter = write_op(tmp_path, fail_file=fail)
    script = '''
for i in 1 2 3 4 5 6; do _tg_maybe_retry; done
echo "after-cycles reason=$tg_reason calls=$(wc -l < "%(counter)s")"
rm "%(fail)s"
tg_resolved_at=$(( $(date +%%s) - 601 ))
_tg_maybe_retry
echo "after-interval reason=$tg_reason calls=$(wc -l < "%(counter)s") token_held=$([[ "$tg_token" == "%(token)s" ]] && echo yes || echo no)"
for i in 1 2 3; do _tg_maybe_retry; done
echo "settled calls=$(wc -l < "%(counter)s")"
''' % {"counter": counter, "fail": fail, "token": TOKEN}
    r = run_bash(tmp_path, script)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "after-cycles reason=credential_command_failed calls=2"        # 起動時の 1 回 (token の 1 回で失敗→chat は呼ばない or 2)
    assert lines[1].startswith("after-interval reason=ok") and lines[1].endswith("token_held=yes")
    assert lines[2] == "settled calls=" + lines[1].split("calls=")[1].split()[0], "取れたら以後は呼ばない"
    assert "telegram credentials: credential_command_failed" in r.stderr and "retry: ok" in r.stderr


def test_unconfigured_is_never_retried_and_never_logged(tmp_path):
    (tmp_path / "repo" / "config").mkdir(parents=True)
    (tmp_path / "repo" / "config" / "crewvia.yaml").write_text("wip_limit: 8\n")
    r = run_bash(tmp_path, 'tg_resolved_at=0; for i in 1 2 3; do _tg_maybe_retry; done; echo "reason=$tg_reason"')
    assert r.stdout.strip() == "reason=no_credentials" and "telegram credentials" not in r.stderr


def test_the_token_is_in_no_process_cmdline_while_resolving(tmp_path):
    write_op(tmp_path, sleep=1)
    found, stop = [], threading.Event()

    def scan():
        while not stop.is_set():
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        text = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    except OSError:
                        continue
                    if TOKEN in text and int(pid) != os.getpid():
                        found.append(text[:100])
            time.sleep(0.02)
    th = threading.Thread(target=scan, daemon=True)
    th.start()
    try:
        r = run_bash(tmp_path, 'echo "reason=$tg_reason"')
    finally:
        stop.set()
        th.join()
    assert "reason=ok" in r.stdout and found == [], found
