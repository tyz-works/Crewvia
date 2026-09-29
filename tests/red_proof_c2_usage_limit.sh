#!/usr/bin/env bash
# 利用枠切れの扱い (C2 / t005) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_c2_usage_limit.sh          (BASE=<修正前の ref> で基点を変えられる)
#
#   baseline — いまの木では tests/test_usage_limit.py が全部緑
#   base     — dispatcher.sh / watchdog.py を修正前 (BASE、既定 origin/main の merge-base) に戻す
#              → (1) Rule 5 が出る / (2) 再開の促しが無い / (3) idle で終了・max に数える  → 赤
#   A  同定: 通知行の位置 (行頭) を外し、画面のどこにあっても拾う                      → 赤 (陰性)
#   B  同定: 入力欄の枠が直下にあるという条件を外す                                    → 赤 (陰性)
#   C  dispatcher: 免除の上限を外す (永久に黙る)                                       → 赤
#   D  dispatcher: 画面が読めないとき「利用枠切れ」に倒す                              → 赤
#   E  同定: リセット時刻を毎回計算し直す (過ぎた時刻が翌日に化ける)                   → 赤
#   F  watchdog: 免除が終わった後の idle の床を外す (止まっていた沈黙で即 hard idle)   → 赤
#   G  watchdog: 観測できなかった空白も max から除く (1 回の上限を外す)                → 赤
#   H  watchdog: 免除の上限を外す (永久に殺さない)                                     → 赤
#   I  dispatcher: 空の capture (失敗) を「利用枠切れではない」に潰して記録を消す (t018 P1) → 赤
#   J  watchdog: 空の capture で entry を None に上書きする (t018 P1)                  → 赤
#   K  dispatcher: 記録の保存失敗を飲んで True を返す (t018 P2)                        → 赤
#   L  dispatcher: 回復時に台帳キーを消さず記録だけ消す (t018 P2)                      → 赤
#   M  dispatcher: 台帳の fp から見え始めを外す (前回のキーが残ると最初の通知が出ない)  → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する (本番の worktree のファイルには触らない)。pytest は
# FakeMux / 隔離した queue・registry で動き、本物の herdr / tmux / 本番 queue には届かない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-c2.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0; N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

BASE="${BASE:-$(git -C "$REPO_ROOT" merge-base HEAD origin/main 2>/dev/null || echo HEAD)}"

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
}

inject() {  # inject <file> <old> <new> — old はちょうど 1 か所。無ければ FATAL
    FILE="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"count={s.count(old)}", file=sys.stderr)
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_py() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
}
T=(tests/test_usage_limit.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片> [pytest の引数...]
    local name="$1" frag="$2"; shift 2
    local out; out="$(run_py "${T[@]}" "$@")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${T[@]}")"
if echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== base: dispatcher.sh / watchdog.py を修正前 ($BASE) に戻す"
fresh_copy
for f in scripts/dispatcher.sh scripts/watchdog.py; do
    git -C "$REPO_ROOT" show "$BASE:$f" > "$TREE/$f" || { echo "FATAL: $BASE:$f"; exit 2; }
done
out="$(run_py "${T[@]}")"
for frag in test_a_usage_limited_worker_gets_one_director_notice_and_no_rule5 \
            test_after_the_reset_time_the_worker_is_nudged_exactly_once \
            test_idle_does_not_terminate_a_usage_limited_worker \
            test_time_spent_usage_limited_is_not_counted_toward_max; do
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "base → 赤 ($frag)"
    else ng "base → 赤にならなかった ($frag)"; fi
done

echo "== case A: 通知行の位置 (行頭) を外す"
fresh_copy
inject scripts/lib_usage_limit.py "        m = _NOTICE_RE.match(raw)" \
    "        m = re.search(r'⚠\\uFE0F?\\s*usage limit reached\\b(?P<rest>.*)\$', raw, re.IGNORECASE)"
expect_red "case A" "test_a_screen_without_the_real_notice_position_is_not_a_usage_limit"

echo "== case B: 入力欄の枠が直下にある、という条件を外す"
fresh_copy
inject scripts/lib_usage_limit.py "        if not any(_FRAME_RE.match(ln) for ln in below):
            continue" "        pass"
expect_red "case B" "test_a_screen_without_the_real_notice_position_is_not_a_usage_limit"

echo "== case C: dispatcher の免除の上限を外す"
fresh_copy
inject scripts/dispatcher.sh "    if now > lib_usage_limit.excuse_deadline(entry):" "    if False:"
expect_red "case C" "test_the_exemption_has_an_upper_bound_and_then_rule5_returns"

echo "== case D: 画面が読めないとき利用枠切れに倒す"
fresh_copy
inject scripts/dispatcher.sh "        return False   # 観測できない → 利用枠切れではない (従来どおり)" "        return True"
expect_red "case D" "test_an_unreadable_screen_falls_to_the_normal_rule5"

echo "== case E: リセット時刻を毎回計算し直す"
fresh_copy
inject scripts/lib_usage_limit.py "    if (isinstance(previous, dict) and identity(previous.get('notice')) == identity(found.notice)" \
    "    if (False and isinstance(previous, dict) and identity(previous.get('notice')) == identity(found.notice)"
expect_red "case E" "test_the_reset_time_is_kept_while_the_same_notice_stays"

echo "== case F: watchdog の idle の床を外す"
fresh_copy
inject scripts/watchdog.py "            activity_mtime = max(activity_mtime, self._limit_floor)" "            pass"
expect_red "case F" "test_idle_restarts_from_the_end_of_the_limit_not_from_the_stale_silence"

echo "== case G: watchdog の 1 回あたりの除外上限を外す"
fresh_copy
inject scripts/watchdog.py "            self._limit_excluded += min(max(now - self._limit_excused_at, 0.0),
                                        self.LIMIT_EXCLUDE_MAX_STEP)" \
    "            self._limit_excluded += max(now - self._limit_excused_at, 0.0)"
expect_red "case G" "test_one_long_unobserved_gap_is_not_credited_to_the_limit"

echo "== case H: watchdog の免除の上限を外す"
fresh_copy
inject scripts/watchdog.py "        if now > lib_usage_limit.excuse_deadline(entry):
            self._limit_excused_at = None" "        if False:
            self._limit_excused_at = None"
expect_red "case H" "test_the_exemption_has_an_upper_bound_and_the_director_is_told"

echo "== case I: dispatcher が空の capture で記録を消す"
fresh_copy
inject scripts/dispatcher.sh "    if not lib_usage_limit.observable(screen):" "    if False:"
expect_red "case I" "test_a_blank_capture_does_not_renew_the_deadline_in_dispatcher"

echo "== case J: watchdog が空の capture で entry を上書きする"
fresh_copy
inject scripts/watchdog.py "        if not lib_usage_limit.observable(screen):" "        if False:"
expect_red "case J" "test_a_blank_capture_does_not_renew_the_deadline_in_watchdog"

echo "== case K: dispatcher が保存失敗を飲む"
fresh_copy
inject scripts/dispatcher.sh "        log(f'WARNING: cannot write usage-limit entry for {name!r}: {e}')
        return False" "        log(f'WARNING: cannot write usage-limit entry for {name!r}: {e}')
        return True"
expect_red "case K" "test_when_the_record_cannot_be_kept_rule5_returns"

echo "== case L: 回復時に記録だけ消して台帳キーを残す"
fresh_copy
inject scripts/dispatcher.sh "観測していないので保つ。
        _retire_usage_limit(name)" "観測していないので保つ。
        _save_usage_limit(name, None)"
expect_red "case L" "test_recovery_then_the_same_notice_notifies_first_time_again"

echo "== case M: 台帳の fp から見え始めを外す"
fresh_copy
inject scripts/dispatcher.sh "    ident = f'{lib_usage_limit.identity(entry[\"notice\"])}@{int(entry[\"first_seen\"])}'" "    ident = lib_usage_limit.identity(entry['notice'])"
expect_red "case M" "test_a_leftover_ledger_key_does_not_silence_the_next_episode"

echo "== case N: mux state が unknown / 割り当てなし idle でも記録を畳む (t020 P1: 回復の観測に束縛しない)"
fresh_copy
inject scripts/dispatcher.sh "    elif st == 'working' and _usage_limit_path(name).exists():" \
    "    elif _usage_limit_path(name).exists():"
expect_red "case N" "test_an_unknown_mux_state_is_not_a_recovery_and_keeps_the_record"

echo
echo "Results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
