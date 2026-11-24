#!/usr/bin/env bash
# tests/bats_sandbox.sh — bats が実 checkout の registry/ を書かないための隔離 (t006)。
#
# 背景: start.sh / lib_mux.py は spawn 記録 (registry/mux/<名前>.json)・Worker の記録
# (registry/workers/)・workers.yaml を **自分のいる checkout** の registry/ に書く (`_own_repo_root()` は
# `__file__` 基準。変えられない = 偽の env で別の場所に向けられないための設計)。つまり bats を主 checkout で
# 回すと、本番の registry/mux に Test-worker.json などが残る (11-24: fake tmux の endpoint を指す古い記録)。
# tests/CLAUDE.md の「実 registry を書くテストは作らない」の bats 版。
#
# 使い方 (bats ファイルの先頭で source):
#   source "$(dirname "$BATS_TEST_FILENAME")/bats_sandbox.sh"
#   setup()    { bats_sandbox_create; }            # start.sh を走らせるファイル (木ごとの複製)
#   setup()    { bats_sandbox_create light; }      # lib_mux.py だけ走らせるファイル (scripts/lib_* だけ)
#   teardown() { ...; bats_sandbox_destroy; }       # 最後に呼ぶ (複製を消し、実 checkout が変わっていないことを assert)
# create の後、REPO_ROOT / START_SH / LIB_MUX_PY (定義されていれば) は複製の中を指す。
# 構造ガード: tests/test_bats_do_not_write_the_real_registry.py

BATS_REAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 開発者の checkout が変わっていないことを見る印 (中身は読まない)。
_bats_real_footprint() {
    local f
    if [[ -e "${BATS_REAL_ROOT}/registry/workers.yaml" ]]; then
        echo "workers.yaml $(stat -c '%s %Y' "${BATS_REAL_ROOT}/registry/workers.yaml")"
    else
        echo "workers.yaml absent"
    fi
    for f in registry/mux registry/workers; do
        if [[ -d "${BATS_REAL_ROOT}/${f}" ]]; then
            echo "${f}:"
            ls -A "${BATS_REAL_ROOT}/${f}" | sort
        fi
    done
}

# bats_sandbox_create [light]
bats_sandbox_create() {
    BATS_REAL_FOOTPRINT_BEFORE="$(_bats_real_footprint)"
    SANDBOX="$(mktemp -d)"
    if [[ "${1:-}" == "light" ]]; then
        source "${BATS_REAL_ROOT}/tests/fixture_tree.sh"
        copy_scripts_libs "$BATS_REAL_ROOT" "$SANDBOX" || return 1
        # lib_mux.py は scripts/bin (python3 shim) と git-helpers 等を PATH / 位置から参照する。
        # ./crewvia (ランチャー) を複製して走らせるテストがある (setup_fake_crewvia_tree)。
        [[ -f "${BATS_REAL_ROOT}/crewvia" ]] && cp "${BATS_REAL_ROOT}/crewvia" "${SANDBOX}/crewvia"
        if [[ -d "${BATS_REAL_ROOT}/scripts/bin" ]]; then
            cp -R "${BATS_REAL_ROOT}/scripts/bin" "${SANDBOX}/scripts/bin"
        fi
    elif git -C "$BATS_REAL_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        # 追跡ファイル + 未追跡 (ignore 除く)。ignore された物 (registry/ 等) は持ち込まない。
        ( cd "$BATS_REAL_ROOT" && git ls-files -z --cached --others --exclude-standard \
            | tar --null --ignore-failed-read -T - -cf - 2>/dev/null ) \
            | tar -xf - -C "$SANDBOX"
    else
        ( cd "$BATS_REAL_ROOT" && tar --exclude=./.git --exclude=./.claude/worktrees -cf - . ) \
            | tar -xf - -C "$SANDBOX"
    fi
    REPO_ROOT="$SANDBOX"
    START_SH="${REPO_ROOT}/scripts/start.sh"
    LIB_MUX_PY="${REPO_ROOT}/scripts/lib_mux.py"
    [[ -f "$LIB_MUX_PY" ]]
}

bats_sandbox_destroy() {
    # 消すのは create が作った複製だけ。実 checkout のファイルは一切消さない。
    if [[ -n "${SANDBOX:-}" && "$SANDBOX" != "$BATS_REAL_ROOT" && -d "$SANDBOX" ]]; then
        find "$SANDBOX" -depth -delete 2>/dev/null || true
    fi
    # どのテストも、開発者の checkout を 1 バイトも変えていない。
    [ "$(_bats_real_footprint)" = "${BATS_REAL_FOOTPRINT_BEFORE:-}" ]
}
