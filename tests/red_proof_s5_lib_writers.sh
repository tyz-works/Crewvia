#!/usr/bin/env bash
# S5 (vNext 01a / t020): ロック外・非原子的だった書き手を lib に寄せた修正が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_s5_lib_writers.sh        (約 3〜4 分)
#
#   baseline — いまの木では S5 のテストと構造ガードが緑
#   case A   — plan.sh verifying の status 検査を外す (動いた card を巻き戻す旧 dispatcher の挙動)   → 赤
#   case B   — hooks/pre-compact.sh を旧版 (b6becfb: card を in-place・ロックなし) に戻す              → 赤
#   case C   — lib の atomic_write_text を in-place の書き込みに戻す (途中で落ちると空)                 → 赤
#   case D   — risk flags の適用を verdict のトランザクションの外に出す                              → 赤
#   case E   — .crewvia-env を open('w') に戻す                                                       → 赤
#   case F   — locked_update_json の flock を外す (taskvia map の lost update)                       → 赤
#   case G   — lib_registry.write を open('w') に戻す                                                 → 赤
#   case H   — assign-name.sh に `printf 'workers: []' >` の初期化を戻す                              → 赤
#   case I   — durable_rename が元の親 dir を fsync しない (shutil.move と同じ)                       → 赤
#   case J   — parse_opts が本文の引数の検査前に queue の骨組みを作る                                → 赤
#   case K   — taskvia-sync.sh が map を素の open('w') で書く (lib を通らない書き込みを 1 つ足す)      → 赤 (構造ガード)
#   case L   — pre-compact.sh に queue への `>` を 1 行足す (bash の書き込み)                       → 赤 (構造ガード)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree・queue・registry には触れない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_SHA="b6becfb"          # S3 (t012) の merge。S5 より前の hooks/pre-compact.sh がここにある
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t020.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
    find "$TREE" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true
}

# inject <tree 内の相対パス> <old> <new> — old が 1 か所だけ在ること (無ければ FATAL)
inject() {
    F="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock" PYTHONDONTWRITEBYTECODE=1 \
        python3 -m pytest tests/test_s5_writers_lock_and_atomic.py tests/test_queue_writes_go_through_the_store.py \
            -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -12; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑 ($(echo "$out" | grep -o '[0-9]* passed'))"
else ng "baseline が緑でない"; echo "$out" | tail -12; fi

echo "== case A: verifying の status 検査を外す"
fresh_copy
inject scripts/plan.sh "        if not _TASK_STATUS.accepts('verifying', cur_status):
            refuse_transition('verifying', task_id, cur_status)" "        pass"
expect_red "case A" "test_verifying_refuses_a_card_that_moved_on_and_writes_nothing"

echo "== case B: hooks/pre-compact.sh を旧版に戻す"
fresh_copy
git -C "$REPO_ROOT" show "$BASE_SHA:hooks/pre-compact.sh" > "$TREE/hooks/pre-compact.sh" \
    || { echo "FATAL: $BASE_SHA の pre-compact.sh を取り出せない"; exit 2; }
expect_red "case B (hook)" "test_the_hook_calls_plan_sh_snapshot_and_falls_back_to_a_log_when_refused"
expect_red "case B (guard)" "test_moved_writers_no_longer_appear"

echo "== case C: atomic_write_text を in-place の書き込みに戻す"
fresh_copy
inject scripts/lib_state_store.py "    _fault('atomic:begin', path)
    _ensure_dir(parent, path)
" "    _fault('atomic:begin', path)
    _ensure_dir(parent, path)
    with open(path, 'w') as _f:            # 欠陥: in-place (開いた時点で truncate される)
        _fault('atomic:tmp_created', path)
        _f.write(text)
    return
"
expect_red "case C" "test_killing_snapshot_at_every_write_point_leaves_the_old_or_the_whole_new_card"
expect_red "case C (env)" "test_a_write_killed_midway_never_leaves_a_partial_env_file"

echo "== case D: risk flags の適用を verdict のトランザクションの外へ"
fresh_copy
inject scripts/plan.sh "        # --- Step 6: apply risk_flags → verification.mode upgrade (verdict と同じトランザクション) ---
        _apply_risk_flag_upgrades(slug, risk_upgrades)

    risk_upgrades = _parse_risk_flags(review_output)
    with_lock(_do_verdict)
" "
    risk_upgrades = _parse_risk_flags(review_output)
    with_lock(_do_verdict)
    with_lock(lambda: _apply_risk_flag_upgrades(slug, risk_upgrades))
"
expect_red "case D" "test_risk_flag_parsing_never_touches_a_card"

echo "== case E: .crewvia-env を open('w') に戻す"
fresh_copy
inject scripts/plan.sh "                _STORE.atomic_write_text(env_file, env_text)" \
       "                open(env_file, 'w').write(env_text)"
expect_red "case E" "test_crewvia_env_is_written_with_atomic_write_text_not_open_w"
expect_red "case E (guard)" "test_no_unlisted_write_remains"

echo "== case F: locked_update_json の flock を外す"
fresh_copy
inject scripts/lib_state_store.py "            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            raise LockFailed(f\"cannot lock {lock_path}: {e}\") from e
        text = _cards.read_regular_text_or_unreadable(path)
        problem = None" "            pass
        except OSError as e:
            raise LockFailed(f\"cannot lock {lock_path}: {e}\") from e
        text = _cards.read_regular_text_or_unreadable(path)
        problem = None"
expect_red "case F" "test_two_writers_of_the_taskvia_map_lose_no_entries"

echo "== case G: lib_registry.write を open('w') に戻す"
fresh_copy
inject scripts/lib_registry.py "    atomic_write_text(path, ''.join(out))" \
       "    with open(path, 'w') as f:
        f.writelines(out)"
expect_red "case G" "test_registry_write_killed_at_every_point_leaves_the_old_or_the_whole_new_file"

echo "== case H: assign-name.sh に初期化の printf を戻す"
fresh_copy
inject scripts/assign-name.sh "# registry の dir と workers.yaml は、ここでは作らない (S5 / t020)。" \
       "mkdir -p \"\$REGISTRY_DIR\"
if [[ ! -f \"\$REGISTRY_YAML\" ]]; then
  printf 'workers: []\\n' > \"\$REGISTRY_YAML\"
fi
# registry の dir と workers.yaml は、ここでは作らない (S5 / t020)。"
expect_red "case H" "test_assign_name_no_longer_initialises_the_registry_outside_the_lock"

echo "== case I: durable_rename が元の親 dir を fsync しない"
fresh_copy
inject scripts/lib_state_store.py "        _fsync_dir(dst_parent, dst)
        if src_parent != dst_parent:
            _fsync_dir(src_parent, src)" "        _fsync_dir(dst_parent, dst)"
expect_red "case I" "test_archive_and_init_force_fsync_both_parent_directories_after_the_rename"

echo "== case J: parse_opts が queue の骨組みを先に作る"
fresh_copy
inject scripts/plan.sh "    return opts, positional


# ---------------------------------------------------------------------------
# Subcommands" "    _ensure_queue_dirs()
    return opts, positional


# ---------------------------------------------------------------------------
# Subcommands"
expect_red "case J" "test_a_rejected_body_argument_leaves_no_queue_skeleton"

echo "== case K: taskvia-sync.sh が map を素の open('w') で書く"
fresh_copy
inject scripts/taskvia-sync.sh "        locked_update_json(path, path + '.lock', _merge, on_unreadable='reset')" \
       "        open(path, 'w').write('{}')"
expect_red "case K" "test_no_unlisted_write_remains"

echo "== case L: pre-compact.sh に queue への > を 1 行足す"
fresh_copy
inject hooks/pre-compact.sh "exit 0" "echo x > \"\$QUEUE_DIR/missions/m/tasks/t001.md\"
exit 0"
expect_red "case L" "test_no_unlisted_write_remains"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
