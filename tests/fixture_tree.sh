#!/usr/bin/env bash
# tests/fixture_tree.sh — scripts/ の隔離コピーを作る**唯一の**入口 (bats / sh 用)。
#
# pytest 用は tests/fixture_tree.py (同じ規則)。理由と経緯はそちらの docstring:
# plan.sh / lib_mux.py などは `scripts/lib_*.py` を自分の位置から読むので、
# 「そのとき要ると思った lib」だけを写す fixture は、lib を足すたびに CI でだけ赤くなる
# (3 回再発)。lib は名前でなく glob (`lib_*`) で **まとめて** 写す。
#
# 使い方 (source して呼ぶ):
#   source "<checkout>/tests/fixture_tree.sh"
#   copy_scripts_libs "$SRC_ROOT" "$DEST_ROOT"          # DEST_ROOT/scripts/ に lib_* だけ
#   copy_plan_tree    "$SRC_ROOT" "$DEST_ROOT"          # plan.sh + lint_plan.py + lib_*
#
# どちらも `DEST_ROOT/scripts/` だけに書く (queue / registry は呼び出し側が作る)。
# 写さないもの: git-helpers.sh (plan.sh は「その有無」で pull 時の worktree 自動作成を決める。
# 置くとテストの外側に worktree を作る) / review-plan.sh (claude を起動する)。必要なテストが
# 自分で置く。
#
# 「tests/ と scripts/test_*.sh が plan.sh / lib_* を個別にコピーしていたら赤」を
# tests/test_registry_isolation.py が構造で固定している。

# copy_scripts_libs SRC_ROOT DEST_ROOT
copy_scripts_libs() {
    local src_root="$1" dest_root="$2" f
    [ -d "${src_root}/scripts" ] || { echo "copy_scripts_libs: no ${src_root}/scripts" >&2; return 1; }
    mkdir -p "${dest_root}/scripts" || return 1
    for f in "${src_root}"/scripts/lib_*; do
        [ -f "$f" ] || continue
        cp "$f" "${dest_root}/scripts/$(basename "$f")" || return 1
    done
}

# copy_plan_tree SRC_ROOT DEST_ROOT
copy_plan_tree() {
    local src_root="$1" dest_root="$2" f
    copy_scripts_libs "$src_root" "$dest_root" || return 1
    for f in plan.sh lint_plan.py; do
        [ -f "${src_root}/scripts/${f}" ] || continue
        cp "${src_root}/scripts/${f}" "${dest_root}/scripts/${f}" || return 1
    done
}
