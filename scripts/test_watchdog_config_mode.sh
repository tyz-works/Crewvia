#!/usr/bin/env bash
# test_watchdog_config_mode.sh — t016 回帰テスト
#
# 不具合: config/crewvia.yaml:57 の `mode: herdr  # 戻す時は tmux。切替日:
# 2026-09-04` のようなインラインコメント付き mode 行を scripts/lib_mux.py の
# _config_mode() がパースできず None を返していた。backend 選択の config
# フォールバックが丸ごと死に、CREWVIA_MUX env を持たないプロセスは無条件で
# TmuxBackend になる。tmux は動いていないので mux.list() が空になり、
# watchdog の check() が生存中の全 Worker を「window gone」= kill と誤判定し、
# 30秒ごとに KILL ログ/alert を出し続けていた (t004 で Seo が 42分ハングした
# 際も watchdog は何も検知しなかった)。
#
# 修正 (3点 + 設計改善1点):
#   1. scripts/lib_mux.py _config_mode(): 値部分の '#' 以降 (インラインコメント)
#      を比較前に除去する。同じパターンで hooks/lib_approval_channel.sh の
#      _read_approval_yaml() にも同じ欠陥があったため合わせて修正した。
#   2. scripts/start.sh: dispatcher/watchdog を mux window に spawn する際、
#      CREWVIA_MUX をコマンド文字列自体に明示的に埋め込む (mux_spawn は
#      呼び出し側プロセスの env を新しいペインへ自動伝播しない — env= 引数は
#      両 backend とも未実装。herdr は特にサーバー起動時の env スナップショット
#      を全ペインへ継承するため、ambient env 継承に頼ると非対称が起こりうる)。
#   3. scripts/watchdog.py: ログ文言 'tmux window gone' のハードコードを
#      backend 名入りの文言に修正し、起動時にどの backend を使っているかを
#      1行ログするようにした。
#   設計改善: 監視中の Worker が全員 'kill' と判定される場合、個別 KILL を
#      連呼せず「設定エラー」として1行の警告にまとめ、cleanup をスキップする
#      (_is_mass_kill()、watchdog.py)。
#
# t020 (P1 追加修正、Seo 最終レビュー): 上記「設計改善」自体に穴があった。
# _is_mass_kill() は「監視中の全員が kill」で True を返す実装だったため、
# **監視 Worker が1名の時、その1名が本当に window を失うと必ず all-kill に
# なり常に True になる**。crewvia の通常運用は in_progress が1〜2名なので、
# N=1/N=2 はレアケースではなく最頻ケース。この経路に入ると cleanup せず
# continue するため、vanished worker が永久に回収されず、alert も interval
# ごとに永久連投されていた。この Director 指示 (t016) 自体に N=1 の考慮が
# 抜けていたための欠陥であり、実装の責任ではない (Director 確認済み)。
#
# 対応 (Seo 提案、件数からの推論ではなく backend の直接シグナルで裏を取る):
#   - _is_mass_kill() に len(results) >= 2 を必須化 (N=1 を対象外に)
#   - かつ mux.available() が False、または mux.list() が空であることを
#     backend 側の直接シグナルとして必須化 (list() は元々メッセージ生成で
#     呼んでいたので、cheap pre-check の後にのみ呼ぶ形で追加コスト最小化)
#   - alert に _should_alert_mass_kill() による backoff を追加 (連投防止)
#
# 旧テスト (Test 14-16) は N=3 all-kill / N=3 mixed / N=0 の3ケースのみで
# **N=1 を構造的に除外**しており、実装の言い分をそのまま期待値にしていた
# (PR#182 Test 8 と同じ穴のパターン、Seo 指摘)。Test 14-22 として、
# 「件数」と「backend の直接シグナル」を独立した軸で組み合わせた網羅的な
# ケース (特に N=1 実死 / N=2 実死) に置き換えた。
#
# t024 (P2 追加修正、Seo 再レビュー): t020 の対応自体にも穴があった。Seo が
# 自ら前回提案 (len>=2 と backend シグナルの AND) の詰めの甘さを指摘:
# 実死は必ず backend が健全 (list 非空) なので、backend シグナル単独でも
# 実死と config error は区別できる。len>=2 は「区別」には何も寄与しておらず、
# **N=1 + backend 実際に壊れている**というただ1ケースで False を強制する
# だけだった — このケースこそ生きている唯一の Worker が誤 KILL され続ける、
# t016 が防ごうとした症状の再現 (かつ crewvia の最頻ケース)。
# 対応: len(results) >= 2 を撤去し backend シグナルのみで判定。性能配慮
# (cheap pre-check → 重い available() は必要な時だけ) は「全員 kill か」
# だけの判定に変えて維持した。
#
# このテストで検証:
#   1-8. _config_mode() がインラインコメント付き/無し/quote付きの mode 行を
#        正しくパースすること (herdr/tmux/inline/不正値/ファイル無し)
#   9.   CREWVIA_MUX 未設定 + config mode: herdr (インラインコメント付き) の
#        env で _select_backend() が HerdrBackend を選ぶこと
#   10.  同じ条件で mode: tmux (インラインコメント付き) なら TmuxBackend
#   11.  hooks/lib_approval_channel.sh の _read_approval_yaml "mode" が
#        インラインコメント付き approval_channel.mode を正しく返すこと
#   12.  WorkerMonitor.check(): mux window が存在し子プロセスも生きている
#        Worker を正しく "alive" と判定すること (誤 kill しないこと)
#   13.  WorkerMonitor.check(): mux window が本当に存在しない場合は "kill"
#        (回帰確認 — 正しい kill 判定自体は壊していないこと)
#   14-22. _is_mass_kill(): N=3 all-kill+backend down → True / N=3 mixed →
#        False / N=0 → False / **N=1 実死・backend 健全 → False** /
#        **N=1・backend 実際に壊れている → True (t024 で追加)** /
#        N=2 実死・backend 健全 → False / N=2・backend 実際に壊れている →
#        True (2ケース)
#   23-25. _should_alert_mass_kill(): backoff 内は False、backoff 到達/超過
#        で True (alert 連投防止の検証)
#   26.  scripts/start.sh: dispatcher と watchdog 両方の mux_spawn 呼び出しに
#        CREWVIA_MUX 明示伝播 (_MUX_ENV_PREFIX) が入っていること (静的チェック
#        — 非対称の再発防止)
#   27.  scripts/watchdog.py: ログ文言に 'tmux window gone' のハードコードが
#        残っていないこと (静的チェック)
#
# 実行: bash scripts/test_watchdog_config_mode.sh
# 副作用: /tmp 配下に一時ファイルを作成し終了時に削除する。実 config/crewvia.yaml
#         や実 herdr/tmux セッションには一切触れない (_config_mode に一時
#         ファイルパスを直接渡してテストするため)。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_watchdog_config_mode.sh (t016: watchdog config-mode parsing + mass-kill guard) =="

# ---------------------------------------------------------------------------
# Test 1-8: _config_mode() インラインコメント対応
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 1-8: lib_mux._config_mode() parses inline comments correctly ---"
PYOUT=$(python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys, tempfile, os
sys.path.insert(0, sys.argv[1])
import lib_mux
from pathlib import Path

cases = [
    ("mode: herdr  # 戻す時は tmux。切替日: 2026-09-04\n", "herdr"),
    ("mode: herdr\n", "herdr"),
    ('mode: "herdr"\n', "herdr"),
    ("mode: tmux\n", "tmux"),
    ("mode: tmux  # trailing comment\n", "tmux"),
    ("mode: inline  #comment-no-space\n", "inline"),
    ("mode: bogus-value\n", None),
    ("no mode key here\n", None),
]
for content, expected in cases:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        got = lib_mux._config_mode(Path(path))
    finally:
        os.unlink(path)
    result = "PASS" if got == expected else "FAIL"
    print(f"{result}\t{content!r}\tgot={got!r}\texpected={expected!r}")
PYEOF
)
PYEXIT=$?
echo "$PYOUT" | while IFS=$'\t' read -r result rest; do
  echo "  [$result] $rest"
done
# 出力に "FAIL" が無いことだけでなく python 自体が正常終了したことも確認する
# (未処理の例外は途中の case で止まり、それ以降 FAIL 行を一切出力しないまま
# 非ゼロ終了する — 出力の grep だけでは黙って PASS 扱いになってしまう)
if [[ "$PYEXIT" -ne 0 ]] || echo "$PYOUT" | grep -q "^FAIL"; then
  fail "_config_mode() inline comment parsing (exit=$PYEXIT) — see cases above"
else
  pass "_config_mode() correctly parses all 8 mode-line variants"
fi

# ---------------------------------------------------------------------------
# Test 9-10: _select_backend() config フォールバックが機能すること
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 9: CREWVIA_MUX unset + config 'mode: herdr  # comment' -> HerdrBackend ---"
BACKEND=$(env -i PATH="$PATH" HOME="$HOME" python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys, tempfile, os
sys.path.insert(0, sys.argv[1])
import lib_mux
from pathlib import Path

fd, path = tempfile.mkstemp(suffix=".yaml")
with os.fdopen(fd, "w") as f:
    f.write("mode: herdr  # 戻す時は tmux。切替日: 2026-09-04\n")
try:
    orig = lib_mux._config_mode
    lib_mux._config_mode = lambda config_path=None: orig(Path(path))
    print(lib_mux._select_backend().__name__)
finally:
    os.unlink(path)
PYEOF
)
if [[ "$BACKEND" == "HerdrBackend" ]]; then
  pass "_select_backend() picks HerdrBackend from config (no CREWVIA_MUX env)"
else
  fail "_select_backend() should pick HerdrBackend — got: $BACKEND"
fi

echo ""
echo "--- Test 10: CREWVIA_MUX unset + config 'mode: tmux  # comment' -> TmuxBackend ---"
BACKEND=$(env -i PATH="$PATH" HOME="$HOME" python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys, tempfile, os
sys.path.insert(0, sys.argv[1])
import lib_mux
from pathlib import Path

fd, path = tempfile.mkstemp(suffix=".yaml")
with os.fdopen(fd, "w") as f:
    f.write("mode: tmux  # some comment\n")
try:
    orig = lib_mux._config_mode
    lib_mux._config_mode = lambda config_path=None: orig(Path(path))
    print(lib_mux._select_backend().__name__)
finally:
    os.unlink(path)
PYEOF
)
if [[ "$BACKEND" == "TmuxBackend" ]]; then
  pass "_select_backend() picks TmuxBackend from config (no CREWVIA_MUX env)"
else
  fail "_select_backend() should pick TmuxBackend — got: $BACKEND"
fi

# ---------------------------------------------------------------------------
# Test 11: hooks/lib_approval_channel.sh の同じパターンの欠陥
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 11: lib_approval_channel.sh _read_approval_yaml handles inline comment on mode ---"
TMP_APPROVAL_CFG="$(mktemp)"
cat > "$TMP_APPROVAL_CFG" << 'EOF'
approval_channel:
  mode: ntfy  # switched for testing, revert later
  ntfy:
    url: https://ntfy.example.com
    topic: crewvia-test
EOF
MODE_OUT=$(
  CREWVIA_REPO="$OWN_CHECKOUT_ROOT" \
  bash -c '
    source "'"$OWN_CHECKOUT_ROOT"'/hooks/lib_approval_channel.sh"
    _APPROVAL_CONFIG_FILE="'"$TMP_APPROVAL_CFG"'"
    _read_approval_yaml "mode"
  '
)
rm -f "$TMP_APPROVAL_CFG"
if [[ "$MODE_OUT" == "ntfy" ]]; then
  pass "_read_approval_yaml mode with inline comment -> clean 'ntfy' (got: '$MODE_OUT')"
else
  fail "_read_approval_yaml mode should be clean 'ntfy' — got: '$MODE_OUT'"
fi

# ---------------------------------------------------------------------------
# Test 12-13: watchdog.py WorkerMonitor.check() が生存中の Worker を誤 kill しない
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 12-13: WorkerMonitor.check() correctly distinguishes alive vs. genuinely-gone ---"
PYOUT2=$(python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys, tempfile
sys.path.insert(0, sys.argv[1])
import watchdog
from pathlib import Path

repo_root = Path(tempfile.mkdtemp())

class FakeMuxAlive:
    def list(self, suffix=None):
        return ["Sofia-worker"]
    def pid(self, name):
        return 12345

class FakeMuxGone:
    def list(self, suffix=None):
        return []  # simulates the t016 bug: wrong backend, empty list()
    def pid(self, name):
        return None

task_card = {"worker": "Sofia"}

# Case 1: window present + live child process -> "alive" (not a false kill)
watchdog._mux = FakeMuxAlive()
mon = watchdog.WorkerMonitor(task_id="t999", task_card=task_card, profiles=watchdog.PROFILES, repo_root=repo_root)
mon._has_child_processes = lambda: True  # simulate a live pgrep hit
status_alive = mon.check()
print(f"{'PASS' if status_alive == 'alive' else 'FAIL'}\talive-worker\tgot={status_alive!r}")

# Case 2: window genuinely absent -> "kill" (real kill detection still works)
watchdog._mux = FakeMuxGone()
mon2 = watchdog.WorkerMonitor(task_id="t998", task_card=task_card, profiles=watchdog.PROFILES, repo_root=repo_root)
status_gone = mon2.check()
print(f"{'PASS' if status_gone == 'kill' else 'FAIL'}\tgenuinely-gone\tgot={status_gone!r}")
PYEOF
)
echo "$PYOUT2" | while IFS=$'\t' read -r result rest; do
  echo "  [$result] $rest"
done
if echo "$PYOUT2" | grep -Pq "^PASS\talive-worker"; then
  pass "check() reports 'alive' for a Worker whose window+process are live"
else
  fail "check() should report 'alive' for a live Worker — output: $PYOUT2"
fi
if echo "$PYOUT2" | grep -Pq "^PASS\tgenuinely-gone"; then
  pass "check() still reports 'kill' when the window is genuinely absent"
else
  fail "check() should still report 'kill' for a genuinely gone window — output: $PYOUT2"
fi

# ---------------------------------------------------------------------------
# Test 14-22: _is_mass_kill() — t020 (P1) + t024 (P2) fixes
#
# PR#184 の元テストは N=3 all-kill / N=3 mixed / N=0 の3ケースのみで、
# 「監視 Worker が1名の時、その1名が本当に死ぬと必ず all-kill になる」
# という N=1 のケースを構造的に除外していた (Seo 指摘、PR#182 Test 8 と
# 同じ穴のパターン)。t020 でこれを直したが、その修正 (len(results)>=2 を
# 必須化) 自体にも穴があった: N=1 で backend が実際に壊れている場合、
# len>=2 ゲートが常に False を強制するため、生きている唯一の Worker が
# 誤って KILL 判定され monitors から削除される — t016 が防ごうとした症状
# そのものが、crewvia の最頻ケース (in_progress 1名) で再現していた
# (Seo 自身が前回提案の詰めの甘さを指摘、t024)。
#
# t024 で len(results) >= 2 ゲートを撤去し、backend の直接シグナルのみで
# 判定するよう修正。ここでは「件数」と「backend の直接シグナル」を
# 独立した軸として組み合わせた網羅的なケースで検証する
# (n1-backend-down が t024 で新規追加した核心ケース)。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 14-22: _is_mass_kill() uses backend signal alone, no count gate (t020 P1 + t024 P2 fix) ---"
PYOUT3=$(python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import watchdog

def r(*statuses):
    return {("m", f"t{i}"): s for i, s in enumerate(statuses)}

cases = [
    # (name, results, mux_available, mux_list_empty, expected)
    ("n3-all-kill-backend-down", r("kill", "kill", "kill"), False, True, True),
    ("n3-mixed", r("alive", "kill", "warn"), True, False, False),
    ("n0-empty", {}, True, False, False),
    # 実死は常に backend が健全 (list 非空) なので、件数によらず backend
    # シグナルだけで False と判定できる — 件数ゲートは元々不要だった
    ("n1-real-death-backend-fine", r("kill"), True, False, False),
    # t024 の核心修正: N=1 + backend 実際に壊れている -> True (t020 の
    # len>=2 ゲートでは False になり誤って cleanup されていたケース)。
    # 1件だけでは「本当に死んだ」か「backend 誤設定」か区別できないため、
    # config error 側に倒して monitors から誤って消さないようにする
    ("n1-backend-down", r("kill"), False, True, True),
    # N=2 の実死: backend は健全 (available かつ list 非空) なので corroboration が
    # 無い → 2人が同時にたまたま死んだだけの本物の kill として扱われるべき
    ("n2-real-death-backend-fine", r("kill", "kill"), True, False, False),
    # N=2 かつ backend が実際に壊れている場合は従来通り config error 扱い
    ("n2-backend-down", r("kill", "kill"), False, True, True),
    ("n2-backend-list-empty-but-available", r("kill", "kill"), True, True, True),
]
for name, results, avail, list_empty, expected in cases:
    got = watchdog._is_mass_kill(results, mux_available=avail, mux_list_empty=list_empty)
    status = "PASS" if got is expected else "FAIL"
    print(f"{status}\t{name}\tgot={got!r}\texpected={expected!r}")
PYEOF
)
PYEXIT3=$?
echo "$PYOUT3" | while IFS=$'\t' read -r result rest; do
  echo "  [$result] $rest"
done
if [[ "$PYEXIT3" -ne 0 ]] || echo "$PYOUT3" | grep -q "^FAIL"; then
  fail "_is_mass_kill() case matrix (exit=$PYEXIT3) — see cases above"
else
  pass "_is_mass_kill() correctly uses backend signal alone, no count gate (8 cases incl. N=1 real death and N=1 backend-down)"
fi

# ---------------------------------------------------------------------------
# Test 23-25: _should_alert_mass_kill() — alert backoff (t020)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 23-25: _should_alert_mass_kill() throttles repeated Taskvia alerts (t020) ---"
PYOUT4=$(python3 - "$OWN_CHECKOUT_ROOT/scripts" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import watchdog

cases = [
    ("just-alerted", 0.0, 100.0, 300, False),   # 100s < 300s backoff -> suppress
    ("exactly-at-backoff", 0.0, 300.0, 300, True),
    ("well-past-backoff", 0.0, 900.0, 300, True),
    # last_alert_at=0.0 (never alerted) with a realistic "now" far past epoch
    # is trivially >= backoff — this is what run() actually passes on its
    # first-ever mass-kill cycle (last_mass_kill_alert_at starts at 0.0).
    ("never-alerted-yet", 0.0, 1_700_000_000.0, 300, True),
]
for name, last_at, now, backoff, expected in cases:
    got = watchdog._should_alert_mass_kill(last_at, now, backoff_seconds=backoff)
    status = "PASS" if got is expected else "FAIL"
    print(f"{status}\t{name}\tgot={got!r}\texpected={expected!r}")
PYEOF
)
PYEXIT4=$?
echo "$PYOUT4" | while IFS=$'\t' read -r result rest; do
  echo "  [$result] $rest"
done
if [[ "$PYEXIT4" -ne 0 ]] || echo "$PYOUT4" | grep -q "^FAIL"; then
  fail "_should_alert_mass_kill() backoff cases (exit=$PYEXIT4) — see cases above"
else
  pass "_should_alert_mass_kill() correctly throttles repeated alerts"
fi

# ---------------------------------------------------------------------------
# Test 17-18: 静的チェック (start.sh の非対称解消 / ログ文言のハードコード除去)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 26: start.sh passes CREWVIA_MUX explicitly to both dispatcher and watchdog spawns ---"
START_SH="$OWN_CHECKOUT_ROOT/scripts/start.sh"
DISPATCHER_LINE=$(grep -n 'mux_spawn "dispatcher"' "$START_SH")
WATCHDOG_LINE=$(grep -n 'mux_spawn "watchdog"' "$START_SH")
if echo "$DISPATCHER_LINE" | grep -q '_MUX_ENV_PREFIX' && echo "$WATCHDOG_LINE" | grep -q '_MUX_ENV_PREFIX'; then
  pass "both mux_spawn calls (dispatcher, watchdog) embed _MUX_ENV_PREFIX"
else
  fail "dispatcher/watchdog mux_spawn calls should both embed _MUX_ENV_PREFIX — dispatcher: $DISPATCHER_LINE / watchdog: $WATCHDOG_LINE"
fi

echo ""
echo "--- Test 27: watchdog.py no longer hardcodes 'tmux window gone' in log/alert text ---"
if grep -q "tmux window gone\|tmux window が消失" "$OWN_CHECKOUT_ROOT/scripts/watchdog.py"; then
  fail "watchdog.py still contains a hardcoded 'tmux window' log/alert string"
else
  pass "watchdog.py no longer hardcodes 'tmux window gone/消失' (backend-agnostic now)"
fi

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
