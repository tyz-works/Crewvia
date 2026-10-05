#!/usr/bin/env python3
"""Telegram の経路 (PR-A) の赤の実証: 欠陥を 1 つずつ戻した複製に同じテストを走らせ、**狙ったテストが assert で落ちる**ことを見る。

期待値をテスト内に再実装したテストは欠陥の留め金にならない (regression-test-must-prove-red)。ここでは本物の
`scripts/lib_telegram.py` / `scripts/dispatcher.sh` を**複製**し、1 か所だけ壊して、狙ったテスト (名前の部分一致) が
`FAILED` になることを確かめる。本物の worktree・registry・queue には触れない (複製は `scripts/` `tests/` だけ)。

* 置換元がちょうど 1 回でなければ BROKEN (注入点が消えた = この証明が失効した)。
* 赤と数えるのは、狙ったテストが FAILED のとき**だけ** (collection / ImportError・他のテストの失敗は数えない)。
* `PYTHONDONTWRITEBYTECODE` + `__pycache__` を複製に持ち込まない (古い pyc が緑を見せる)。

使い方: python3 tests/red_proof_telegram.py [名前の部分一致]   (全体で数分)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

LIB = "scripts/lib_telegram.py"
SH = "scripts/dispatcher.sh"
PURE = "tests/test_telegram_pure.py"
FAKE = "tests/test_telegram_fake_bot_api.py"
GLUE = "tests/test_telegram_dispatcher_glue.py"

# (名前, 壊すファイル, 置換元 (ちょうど 1 回), 置換先, 実行するテストファイル, 赤になるべきテスト名の部分一致)
MUTANTS = [
    ("callback_data を fullmatch でなく match に", LIB, "_CALLBACK_RE.fullmatch(data)", "_CALLBACK_RE.match(data)",
     PURE, "test_callback_data_must_match_the_whole_string"),
    ("札 (nonce) を照合しない", LIB, "if entry is None or entry['nonce'] != nonce:", "if entry is None:",
     PURE, "test_classify_update_full_cross_product_against_a_handwritten_oracle"),
    ("message_id を照合しない", LIB, "        if message.get('message_id') != entry.get('message_id'):\n            return Decision('reject', qid, callback_id=cb_id, reply='不明な質問です')\n",
     "", PURE, "test_classify_update_full_cross_product_against_a_handwritten_oracle"),
    ("押した人 (from) を照合しない", LIB, "        if not _same_id(sender, chat) and str(sender) != chat:\n            return Decision('ignore')\n", "",
     PURE, "test_classify_update_full_cross_product_against_a_handwritten_oracle"),
    ("二重押しで上書きする (CAS を外す)", LIB, "    if entry is None or entry['status'] != 'open':\n        return ledger\n    new = dict(ledger)\n    e = dict(entry)\n    e['status'], e['answer']",
     "    if entry is None:\n        return ledger\n    new = dict(ledger)\n    e = dict(entry)\n    e['status'], e['answer']",
     PURE, "test_first_answer_wins_and_is_never_overwritten"),
    ("stale のしきい値を心拍の間隔と同じに", LIB, "RECEIVER_STALE_SECONDS = 3 * RECEIVER_HEARTBEAT_SECONDS", "RECEIVER_STALE_SECONDS = RECEIVER_HEARTBEAT_SECONDS",
     PURE, "test_stale_threshold_is_derived_from_the_heartbeat_not_from_poll_interval"),
    ("別の bot を断らない (mismatch を見ない)", LIB, "    if mine is not None and (receiver.get('bot_id'), receiver.get('chat_hash')) != tuple(mine):\n        return False, 'receiver_mismatch'\n", "",
     PURE, "test_receiver_verdict_table"),
    ("env の CREWVIA_TG_* を読む", LIB, "    if source == 'op':\n        token_ref = creds_cfg.get('token_ref', '')",
     "    _e = os.environ if environ is None else environ\n    if _e.get('CREWVIA_TG_BOT_TOKEN') and _e.get('CREWVIA_TG_CHAT_ID'):\n        return _validated(_e['CREWVIA_TG_BOT_TOKEN'], _e['CREWVIA_TG_CHAT_ID'])\n    if source == 'op':\n        token_ref = creds_cfg.get('token_ref', '')",
     PURE, "test_op_source_ignores_env_and_the_other_source"),
    ("ask も運搬用の変数を読む", LIB, "    if carried:\n        environ = os.environ if environ is None else environ", "    if True:\n        environ = os.environ if environ is None else environ",
     PURE, "test_ask_does_not_read_the_carrier_but_dispatcher_verbs_do"),
    ("file の権限: group の読みを許す", LIB, "(st.st_mode & 0o077)", "(st.st_mode & 0o007)",
     PURE, "test_file_source_permission_rule"),
    ("file の権限が合わなくても中身を開く", LIB, "    if not stat.S_ISREG(st.st_mode) or st.st_uid != euid or (st.st_mode & 0o077):\n        return None, 'credential_file_permissions'\n", "",
     PURE, "test_file_source_never_opens_a_file_with_bad_permissions"),
    ("未設定でも受信側のファイルを作る", LIB, "        if not exists and reason == 'no_credentials':\n            return {'enabled': False, 'reason': reason, 'wrote': False, 'file_exists': False}\n", "",
     FAKE, "test_unconfigured_cycle_touches_nothing"),
    ("答えを書けなくても offset を進める (process 段のロック失敗)", LIB, "            return out                              # offset を進めない → 次のサイクルで同じ update を受ける\n",
     "            pass\n", FAKE, "test_offset_does_not_advance_when_only_the_answering_step_cannot_lock"),
    ("hold (message_id null) でも offset を進める", LIB, "new_offset = min(hold_ids) if hold_ids else updates[-1]['update_id'] + 1",
     "new_offset = updates[-1]['update_id'] + 1", FAKE, "test_press_on_a_question_without_message_id_holds_the_offset"),
    ("台帳が壊れていても転送・応答する (冗長な 2 つの防御を両方外す)", LIB, [
        ("        except LedgerUnreadable:\n            out['error'] = 'ledger_unreadable'      # offset を進めず、answerCallbackQuery も呼ばない (§2-3)\n            return out\n",
         "        except LedgerUnreadable:\n            pass\n"),
        ("        ledger = read_questions(registry_dir)\n        if is_unreadable(ledger):\n            out['error'] = 'ledger_unreadable'\n            return out\n        if not open_questions(ledger, now):",
         "        ledger = read_questions(registry_dir)\n        if is_unreadable(ledger):\n            ledger = {}\n        if not open_questions(ledger, now):"),
    ], "",
     FAKE, "test_unreadable_ledger_forwards_nothing_does_not_advance_and_does_not_answer"),
    ("poll に token を argv で渡す", LIB, "        cmd = ['timeout', '8', python, str(Path(__file__).resolve()), 'poll', '--registry-dir', str(registry_dir)]",
     "        cmd = ['timeout', '8', python, str(Path(__file__).resolve()), 'poll', '--registry-dir', str(registry_dir), '--token', creds.token]",
     FAKE, "test_the_poll_subprocess_receives_credentials_by_env_never_by_argv"),
    ("send 状態を読み直さず上書きする (2 者の書き込みを数え損ねる)", LIB, "        state['last_sent_at'] = start + wait\n", "        state['last_sent_at'] = start\n",
     FAKE, "test_concurrent_senders_never_lose_a_slot"),
    ("バックオフを入れない", LIB, "        state['backoff_until'] = finished + delay\n", "        state['backoff_until'] = None\n",
     FAKE, "test_backoff_doubles_up_to_the_cap_and_success_resets"),
    ("例外の型でなく token 入りの文を出す", LIB, "        sys.stderr.write(f'internal_error:{type(e).__name__}\\n')",
     "        sys.stderr.write(f'internal_error:{type(e).__name__}:{e}\\n')", FAKE, "test_an_unexpected_exception_prints_only_its_type_never_its_message"),
    ("大域の urlopen を使う", LIB, "urllib.request.build_opener().open(req, timeout=timeout)", "urllib.request.urlopen(req, timeout=timeout)",
     FAKE, "test_api_call_does_not_depend_on_the_global_urlopen"),
    ("dispatcher: env コマンドで token を渡す", SH, '  _CREWVIA_TG_RESOLVED_TOKEN="$tg_token" _CREWVIA_TG_RESOLVED_CHAT_ID="$tg_chat" \\\n  _CREWVIA_TG_RESOLVE_REASON="$tg_reason" \\\n  python3 -',
     '  env _CREWVIA_TG_RESOLVED_TOKEN="$tg_token" _CREWVIA_TG_RESOLVED_CHAT_ID="$tg_chat" \\\n  _CREWVIA_TG_RESOLVE_REASON="$tg_reason" \\\n  python3 -',
     GLUE, "test_the_dispatch_call_passes_credentials_by_prefix_assignment_not_env"),
    ("dispatcher: 運搬用の変数を os.environ に残す", SH, "{k: os.environ.pop(k, '') for k in", "{k: os.environ.get(k, '') for k in",
     GLUE, "test_carried_credentials_are_removed_from_the_environment_at_import"),
    ("dispatcher: 取り直しをサイクルごとにする", SH, '     && (( $(date +%s) - tg_resolved_at >= TG_RESOLVE_RETRY_SECONDS )); then', '     && (( $(date +%s) - tg_resolved_at >= 0 )); then',
     GLUE, "test_a_failed_resolution_is_retried_only_after_the_long_interval"),
    ("dispatcher: 「無効になった」通知を毎サイクル送る", SH, "TELEGRAM_DISABLED_KEY, fingerprint(rsn), 'telegram-receiver', '_daemon', 'telegram',",
     "TELEGRAM_DISABLED_KEY, fingerprint(rsn, time.time()), 'telegram-receiver', '_daemon', 'telegram',",
     GLUE, "test_a_failure_reason_from_startup_becomes_a_disabled_receiver_and_one_director_notice"),
]


def run_one(name, target, old, new, test_file, selector, base):
    work = Path(tempfile.mkdtemp(prefix="red-proof-telegram-"))
    try:
        for d in ("scripts", "tests", "config"):
            shutil.copytree(base / d, work / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        path = work / target
        text = path.read_text(encoding="utf-8")
        pairs = old if isinstance(old, list) else [(old, new)]
        for o, n in pairs:                                  # 複数の注入点 (冗長な防御を全部外す) も 1 つの欠陥として扱う
            if text.count(o) != 1:
                return "BROKEN", f"置換元が {text.count(o)} 回 (ちょうど 1 回でなければ注入点が失効): {o[:50]!r}"
            text = text.replace(o, n)
        path.write_text(text, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CREWVIA_TG", "_CREWVIA_TG"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q", "-p", "no:cacheprovider", "-k", selector,
                               "-rf", "--tb=no"], cwd=work, capture_output=True, text=True, env=env, timeout=600)
        out = proc.stdout + proc.stderr
        failed = [m for m in re.findall(r"^FAILED (\S+)", out, re.M) if selector in m]
        if failed and "error" not in out.lower().split("passed")[-1][:0]:
            reasons = re.findall(r"^FAILED \S+ - (.*)$", out, re.M)
            if any(("ImportError" in r or "SyntaxError" in r or "ModuleNotFoundError" in r) for r in reasons):
                return "BROKEN", "複製が import できない: " + "; ".join(reasons)[:120]
            return "RED", f"{len(failed)} 件が FAILED"
        return "GREEN", (out.strip().splitlines() or [""])[-1]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    pat = sys.argv[1] if len(sys.argv) > 1 else ""
    results = []
    for name, target, old, new, test_file, selector in MUTANTS:
        if pat and pat not in name:
            continue
        verdict, detail = run_one(name, target, old, new, test_file, selector, REPO)
        results.append(verdict)
        print(f"{verdict:6} {name}  [{selector}] {detail}", flush=True)
    bad = [v for v in results if v != "RED"]
    print(f"\n{results.count('RED')}/{len(results)} 件が赤 (欠陥を戻すとテストが落ちる)。"
          + ("" if not bad else f" 赤にならなかった: GREEN={results.count('GREEN')} BROKEN={results.count('BROKEN')}"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
