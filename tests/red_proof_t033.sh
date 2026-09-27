#!/usr/bin/env bash
# worktree の掃除 (t033 / backlog #33) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t033.sh
#
#   baseline — いまの木では tests/test_worktree_gc.py が緑
#   A  dirty (未コミット・untracked) でも remove にする                              → 赤
#   B  origin に無いコミットがあっても remove にする                                  → 赤
#   C  active な mission の worktree も remove にする                                → 赤
#   D  state.yaml が読めないとき「active ゼロ」と読む                                → 赤
#   E  registry の記録が読めなくても remove にする                                   → 赤
#   F  TARGET_DIR 記録が worktree を指していても remove にする                        → 赤
#   G  プロセスの cwd が worktree の中でも remove にする                              → 赤
#   H  プロセス表を取れなくても remove にする                                        → 赤
#   I  locked でも remove にする                                                     → 赤
#   J  主 checkout を管理対象として扱う                                              → 赤
#   K  管理ディレクトリの外の worktree も remove にする                               → 赤
#   L  queue/missions に残っている (archive されていない) mission も remove にする   → 赤
#   M  mission ディレクトリを観測できなくても「無い」と読む                          → 赤
#   N  --apply が消す直前の再判定をしない                                            → 赤
#   O  worktree の隔離 (move) に --force を付ける                                    → 赤
#   P  (退役: t071 で --apply が削除ではなく隔離になり、branch を消さなくなったので
#      「-D で消す」という対象自体が無くなった。branch に一切触れないことは
#      test_quarantine_never_calls_worktree_remove_or_branch_delete が守る)
#   Q  dry-run でも隔離する                                                          → 赤
#   R  cwd を読めないプロセスを、証明なしに無害と読む                                → 赤
#   S  同じ uid のプロセスの cwd を読めなくても、取れたことにする                    → 赤
#   T  git status の失敗を clean と読む                                              → 赤
#   U  rev-list の失敗を「push 済み」と読む                                          → 赤
#   V  untracked を未追跡ディレクトリ 1 行に畳む (--untracked-files=normal)           → 赤
#   W  ignored なファイルがあっても remove にする                                    → 赤 (t057 / PR#239 F1)
#   X  --apply のたびに repository-wide の git worktree prune を復活させる            → 赤 (t057 / PR#239 F2)
#   Y  lsof が非 0 でも部分出力を完全なスキャンとみなす                              → 赤 (t057 / PR#239 F3)
#   Z  GIT_DIR/GIT_WORK_TREE 等を継承し、-C の指定を上書きさせる                     → 赤 (t064 / PR#239 2巡目 P1)
#   AA stat の EACCES 等 ENOENT/ESRCH 以外の OSError も「消えた」に潰す              → 赤 (t064 / PR#239 2巡目 P2)
#   BB assume-unchanged / skip-worktree の index フラグ検出を外す                    → 赤 (t071 / PR#239 3巡目)
#   CC core.ignoreStat=true の検出を外す                                            → 赤 (t071 / PR#239 3巡目)
#   DD --apply が git worktree remove を呼ぶ (隔離ではなく削除に戻す)               → 赤 (t071 / PR#239 3巡目)
#   EE git worktree move の失敗を削除にフォールバックする                          → 赤 (t071 / PR#239 3巡目)
#   FF 隔離先が既に存在してもそのまま move を呼ぶ (上書きの危険)                    → 赤 (t071 / PR#239 3巡目)
#
# t080 (B9 fix 4巡目: Codex review の findings) で追加:
#   GG move は成功したが lock が失敗したエントリを通常の分類 (mission slug 等) に            → 赤 (t080 P2-1)
#      進ませ、.quarantine 自体を mission slug として再判定できてしまう
#   HH --list-quarantine / --restore を lock の成否 (reason の一致) に依存させる          → 赤 (t080 P2-1)
#      (move だけ成功した entry が一覧・復旧から見えなくなる)
#   II 元の絶対パスの復元 (--list-quarantine の original_path) が最初の改行で             → 赤 (t080 P2-2)
#      切り詰められる (埋め込み・末尾の改行を含むパスの往復が壊れる)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# テストは一時ディレクトリの git repo (origin = bare) と隔離した queue / registry だけで動き、
# 本番の主 checkout・queue・registry・mux には届かない (--repo / --queue を必ず明示している)。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t033.XXXXXX")"
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
}

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
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

# 呼び出し元の AGENT_NAME / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_py() {  # run_py <pytest の引数...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
}
TESTS=(tests/test_worktree_gc.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local name="$1" frag="$2"
    local out; out="$(run_py "${TESTS[@]}")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

S=scripts/worktree_gc.py

fresh_copy
echo "== baseline"
out="$(run_py "${TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: dirty でも remove にする"
fresh_copy
inject $S "    if dirty_lines:
        return keep(R_DIRTY, f'{len(dirty_lines)} 件の変更 (先頭: {dirty_lines[0]})')" \
           "    if False:
        return keep(R_DIRTY, f'{len(dirty_lines)} 件の変更 (先頭: {dirty_lines[0]})')"
expect_red "case A" "test_a_modified_tracked_file_keeps_it"

echo "== case B: origin に無いコミットがあっても remove にする"
fresh_copy
inject $S "    if out.strip():
        return keep(R_UNPUSHED," "    if False:
        return keep(R_UNPUSHED,"
expect_red "case B" "test_a_commit_that_is_not_on_origin_keeps_it"

echo "== case C: active な mission も remove にする"
fresh_copy
inject $S "    if slug in ctx.active_missions:" "    if False:"
expect_red "case C" "test_an_active_mission_keeps_its_worktrees"

echo "== case D: state.yaml が読めないとき active ゼロと読む"
fresh_copy
inject $S "        return None, ('state.yaml が無い' if is_missing(text) else f'state.yaml が読めない ({text.reason})')" "        return set(), ''"
expect_red "case D" "test_an_unreadable_state_keeps_every_worktree"

echo "== case E: registry の記録が読めなくても remove にする"
fresh_copy
inject $S "    if ctx.registry_problem:" "    if False:"
expect_red "case E" "test_an_unreadable_record_keeps_every_worktree"

echo "== case F: TARGET_DIR 記録が worktree を指していても remove にする"
fresh_copy
inject $S "        if _within(target, wt_real):" "        if False:"
expect_red "case F" "test_a_target_dir_record_pointing_into_the_worktree_keeps_it"

echo "== case G: プロセスの cwd が worktree の中でも remove にする"
fresh_copy
inject $S "        if _within(cwd, wt_real):" "        if False:"
expect_red "case G" "test_a_process_whose_cwd_is_in_the_worktree_keeps_it"

echo "== case H: プロセス表を取れなくても remove にする"
fresh_copy
inject $S "    if ctx.process_problem:" "    if False:"
expect_red "case H" "test_a_scan_that_failed_keeps_every_worktree"

echo "== case I: locked でも remove にする"
fresh_copy
inject $S "    if wt.locked:
        return keep(R_LOCKED)" "    if False:
        return keep(R_LOCKED)"
expect_red "case I" "test_a_locked_worktree_is_kept"

echo "== case J: 主 checkout を管理対象として扱う"
fresh_copy
inject $S "    if is_main or wt_real == ctx.repo or wt.bare:" "    if False:"
expect_red "case J" "test_the_main_checkout_is_never_removed"

echo "== case K: 管理ディレクトリの外の worktree も remove にする"
fresh_copy
inject $S "    if slug is None:
        return keep(R_OUTSIDE" "    if False:
        return keep(R_OUTSIDE"
expect_red "case K" "test_a_worktree_outside_the_managed_dir_is_kept"

echo "== case L: archive されていない mission も remove にする"
fresh_copy
inject $S "        return keep(R_MISSION_NOT_ARCHIVED, f'{mission_dir} が残っている')" "        pass"
expect_red "case L" "test_a_mission_still_in_queue_missions_is_kept_even_if_not_active"

echo "== case M: mission ディレクトリを観測できなくても「無い」と読む"
fresh_copy
inject $S "        return keep(R_MISSION_UNOBSERVABLE, f'{mission_dir}: {e}')" "        pass"
expect_red "case M" "test_an_unobservable_mission_dir_is_kept_not_read_as_absent"

echo "== case N: --apply が消す直前の再判定をしない"
fresh_copy
inject $S "        if again.action != REMOVE:" "        if False:"
expect_red "case N" "test_state_that_changed_since_the_dry_run_is_rejudged_before_quarantining"

echo "== case O: worktree の隔離 (move) に --force を付ける"
fresh_copy
inject $S "run_git(repo, 'worktree', 'move', current.path, dest)" \
           "run_git(repo, 'worktree', 'move', '--force', current.path, dest)"
expect_red "case O" "test_git_is_only_called_with_the_allowed_verbs_and_flags"

# case P は t071 で退役 (上のヘッダコメント参照)。

echo "== case Q: dry-run でも隔離する"
fresh_copy
inject $S "    applied = apply_quarantine(repo, queue, verdicts) if args.apply else None" \
           "    applied = apply_quarantine(repo, queue, verdicts) if True else None"
expect_red "case Q" "test_dry_run_changes_nothing"

echo "== case R: cwd を読めないプロセスを証明なしに無害と読む"
fresh_copy
inject $S "        if state in ('Z', 'X'):" "        if True:"
expect_red "case R" "test_an_unreadable_cwd_is_harmless_only_for_provable_cases"

echo "== case S: cwd を読めない同 uid のプロセスを、取れたことにする"
fresh_copy
inject $S "            return [], f'/proc/{pid}/cwd ({comm}) を読めない ({e})'" "            continue"
expect_red "case S" "test_an_unreadable_unknown_process_of_ours_fails_the_scan"

echo "== case T: git status の失敗を clean と読む"
fresh_copy
inject $S "    if rc != 0:
        return keep(R_STATUS_FAILED," "    if False:
        return keep(R_STATUS_FAILED,"
expect_red "case T" "test_a_failing_git_status_is_a_hold_not_a_pass"

echo "== case U: rev-list の失敗を push 済みと読む"
fresh_copy
inject $S "    if rc != 0:
        return keep(R_HEAD_UNRESOLVED," "    if False:
        return keep(R_HEAD_UNRESOLVED,"
expect_red "case U" "test_a_failing_rev_list_is_a_hold_not_a_pass"

echo "== case V: untracked を畳む (--untracked-files=normal)"
fresh_copy
inject $S "'status', '--porcelain=v1', '--untracked-files=all'" "'status', '--porcelain=v1', '--untracked-files=normal'"
expect_red "case V" "test_an_untracked_file_inside_an_untracked_directory_keeps_it"

echo "== case W: ignored なファイルがあっても remove にする (PR#239 F1)"
fresh_copy
inject $S "    if ignored_lines:
        return keep(R_IGNORED, f'{len(ignored_lines)} 件の ignored ファイル (先頭: {ignored_lines[0][3:]})')" \
           "    if False:
        return keep(R_IGNORED, f'{len(ignored_lines)} 件の ignored ファイル (先頭: {ignored_lines[0][3:]})')"
expect_red "case W" "test_an_ignored_file_keeps_it_even_though_git_status_is_clean"

echo "== case X: --apply のたびに repository-wide prune を復活させる (PR#239 F2)"
fresh_copy
inject $S "    applied = apply_quarantine(repo, queue, verdicts) if args.apply else None" \
           "    applied = apply_quarantine(repo, queue, verdicts) if args.apply else None
    if applied is not None:
        run_git(repo, 'worktree', 'prune')"
expect_red "case X" "test_apply_never_invokes_git_worktree_prune"

echo "== case Y: lsof が非 0 でも部分出力を完全なスキャンとみなす (PR#239 F3)"
fresh_copy
inject $S "    if proc.returncode != 0:
        return [], f'lsof が不完全 (rc={proc.returncode}): {(proc.stderr or \"\").strip()[:200]}'" \
           "    if False:
        return [], f'lsof が不完全 (rc={proc.returncode}): {(proc.stderr or \"\").strip()[:200]}'"
expect_red "case Y" "test_a_nonzero_lsof_exit_with_partial_output_is_a_failure_not_a_partial_success"

echo "== case Z: GIT_DIR/GIT_WORK_TREE 等を継承する (PR#239 2巡目 P1)"
fresh_copy
inject $S "    for key in _GIT_REPO_LOCATION_ENV_VARS:
        env.pop(key, None)
    env['LC_ALL'] = 'C'" \
           "    env['LC_ALL'] = 'C'"
expect_red "case Z" "test_git_env_drops_repo_location_vars"

echo "== case AA: stat の EACCES 等を「消えた」に潰す (PR#239 2巡目 P2)"
fresh_copy
inject $S "            except OSError as stat_e:
                if stat_e.errno in (errno.ENOENT, errno.ESRCH):
                    continue          # プロセスが消えた
                # EACCES / EIO 等は消滅の証拠ではない (族A — 観測の失敗を「不在」に潰さない。
                # PR#239 2巡目 P2、t058 QA が実機で再現: readlink・stat の両方が EACCES を返す
                # read-only probe で \`([], '')\` = スキャン成功として報告されていた)。
                return [], f'/proc/{pid} を stat できない ({stat_e})'" \
           "            except OSError:
                continue                  # stat もできない = 消えた"
expect_red "case AA" "test_a_readonly_probe_where_stat_also_fails_with_eacces_fails_the_scan"

echo "== case BB: assume-unchanged / skip-worktree の index フラグ検出を外す (t071)"
fresh_copy
inject $S "    if flagged:
        return keep(R_INDEX_FLAGS, f'{len(flagged)} 件 (先頭: {flagged[0]})')" \
           "    if False:
        return keep(R_INDEX_FLAGS, f'{len(flagged)} 件 (先頭: {flagged[0]})')"
expect_red "case BB" "test_an_assume_unchanged_file_keeps_it_even_though_status_is_clean"

echo "== case CC: core.ignoreStat=true の検出を外す (t071)"
fresh_copy
inject $S "        if out.strip() == 'true':" "        if False:"
expect_red "case CC" "test_core_ignore_stat_true_keeps_it_even_with_no_flags_yet"

echo "== case DD: --apply が git worktree remove を呼ぶ (隔離ではなく削除に戻す。t071)"
fresh_copy
inject $S "        rc, _out, err = run_git(repo, 'worktree', 'move', current.path, dest)" \
           "        rc, _out, err = run_git(repo, 'worktree', 'remove', current.path)"
expect_red "case DD" "test_quarantine_never_calls_worktree_remove_or_branch_delete"

echo "== case EE: git worktree move の失敗を削除にフォールバックする (t071)"
fresh_copy
inject $S "            results.append({'path': v.path, 'status': 'failed',
                            'detail': f'git worktree move: {(err or \"実行できない\").strip()[:300]}'})
            continue" \
           "            results.append({'path': v.path, 'status': 'failed',
                            'detail': f'git worktree move: {(err or \"実行できない\").strip()[:300]}'})
            run_git(repo, 'worktree', 'remove', current.path)
            continue"
expect_red "case EE" "test_a_worktree_move_failure_is_kept_not_deleted"

echo "== case FF: 隔離先が既に存在してもそのまま move を呼ぶ (t071)"
fresh_copy
inject $S "        if os.path.exists(dest):" "        if False:"
expect_red "case FF" "test_an_existing_quarantine_destination_refuses_the_move_instead_of_calling_it"

echo "== case GG: move は成功したが lock が失敗したエントリを通常の分類へ進ませる (t080 P2-1)"
fresh_copy
inject $S "    if _within(wt_real, quarantine_root_of(ctx.managed_root)):
        # \`.quarantine/\` 配下ではあるが lock が無い・timestamp が一致しない (t080 P2-1:
        # move は成功したが lock が失敗した場合がここに落ちる。手動で置かれた何かの可能性もある)。
        # **正体の分からないエントリは自動で動かさない** — ここで keep せずに下の mission slug
        # 判定へ進ませると、\`.quarantine\` 自体が mission slug として解釈され、通常の条件を
        # 満たせば REMOVE になり、次の --apply が隔離先のパスを「元の場所」として再度動かす
        # (隔離先を隔離する二重隔離)。\`--list-quarantine\` は lock の成否に関わらずこのエントリを
        # 見せる (\`_quarantine_path_parts\` はディレクトリ階層だけで判定する)。
        return keep(R_QUARANTINE_UNVERIFIED,
                    'lock が無いか timestamp が一致しない。\`--list-quarantine\` で確認するか、'
                    '手動で \`git worktree lock\` すること')" \
           "    if False:
        return keep(R_QUARANTINE_UNVERIFIED, '')"
expect_red "case GG" "test_a_move_that_succeeded_but_a_lock_that_failed_is_never_reswept"

echo "== case HH: --list-quarantine / --restore を lock の成否に依存させる (t080 P2-1)"
fresh_copy
inject $S "    for wt in worktrees:
        wt_real = os.path.realpath(wt.path)
        parts = _quarantine_path_parts(wt_real, managed_root)
        if parts is None:
            continue
        timestamp, rel = parts
        out.append((wt, timestamp, _quarantine_original_path(managed_root, rel)))" \
           "    for wt in worktrees:
        wt_real = os.path.realpath(wt.path)
        if not _is_our_quarantine_entry(wt, wt_real, managed_root):
            continue
        parts = _quarantine_path_parts(wt_real, managed_root)
        timestamp, rel = parts
        out.append((wt, timestamp, _quarantine_original_path(managed_root, rel)))"
expect_red "case HH" "test_list_quarantine_sees_an_entry_even_when_the_lock_failed"

echo "== case II: 元のパスの復元が最初の改行で切り詰められる (t080 P2-2)"
fresh_copy
inject $S "        out.append((wt, timestamp, _quarantine_original_path(managed_root, rel)))" \
           "        out.append((wt, timestamp, _quarantine_original_path(managed_root, rel).split(chr(10))[0]))"
expect_red "case II" "test_quarantine_round_trips_a_path_with_embedded_and_trailing_newlines_byte_for_byte"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
