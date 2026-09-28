#!/usr/bin/env bash
# tests/trust_fixture.sh — start.sh の trust 事前検査 (t021 / backlog #28) を通すための偽の設定。
#
# start.sh は、並列 (mux) モードで claude を起動する前に `~/.claude.json` を読み、cwd に
# `hasTrustDialogAccepted: true` が無ければ止まる (scripts/lib_trust.py)。fake tmux で start.sh を
# 走らせるテストは、この検査を通さないと起動まで進めない。開発機では `~/.claude.json` が crewvia を
# 信頼しているので通ってしまい、**CI (`~/.claude.json` が無い) でだけ落ちる**。だから start.sh を
# mux モードで走らせるテストは、必ずここで信頼を宣言する。
#
# 使い方 (bats):
#     source "$(dirname "$BATS_TEST_FILENAME")/trust_fixture.sh"
#     setup()    { trust_fixture_setup; ... }          # 引数なし = "/" を信頼 (全 dir が祖先の継承で通る)
#     teardown() { trust_fixture_teardown; ... }
#
# 本物の `~/.claude.json` は読まない・書かない: `CLAUDE_CONFIG_DIR` (claude 本体が読む規則と同じ) を
# 使い捨ての dir に向ける。HOME は変えない (HOME を変えると ~/.local の python パッケージを見失う)。

trust_fixture_setup() {
    TRUST_FIXTURE_DIR="$(mktemp -d)"
    python3 - "$TRUST_FIXTURE_DIR/.claude.json" "${@:-/}" <<'PYEOF'
import json, sys
path, *trusted = sys.argv[1:]
with open(path, 'w') as f:
    json.dump({'projects': {d: {'hasTrustDialogAccepted': True} for d in trusted}}, f)
PYEOF
    export CLAUDE_CONFIG_DIR="$TRUST_FIXTURE_DIR"
}

trust_fixture_teardown() {
    if [[ -n "${TRUST_FIXTURE_DIR:-}" && -d "$TRUST_FIXTURE_DIR" ]]; then
        find "$TRUST_FIXTURE_DIR" -depth -delete 2>/dev/null || true
    fi
    unset CLAUDE_CONFIG_DIR TRUST_FIXTURE_DIR
}
