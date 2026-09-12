#!/usr/bin/env bats
# tests/crewvia-launcher-mux-mode.bats
#
# t009 (mission 20260912-verdict-ci-launcher): ./crewvia ランチャの起動モード
# 判定の回帰テスト。
#
# 症状: crewvia:92-122 (修正前) は mode: tmux / 不明値のとき CREWVIA_TMUX=1
# だけを export し、CREWVIA_MUX を export しなかった (mode: herdr だけが
# CREWVIA_MUX=herdr を export していた)。CREWVIA_TMUX は mission
# 20260907-tmux-name-cleanup で CREWVIA_MUX_ENABLED に改名され、現在
# CREWVIA_TMUX を読むコードは crewvia 自身以外に存在しない (死んだ変数)。
# 一方 scripts/start.sh の実効ゲート (ROLE=worker 経路) は CREWVIA_MUX の
# 有無で並列モードを決める。そのため config を mode: tmux に戻すと
# `./crewvia worker ...` は CREWVIA_MUX が一切 export されず、start.sh 側で
# インラインモードに転落していた。
#
# さらに crewvia:95 `if [[ -z "${CREWVIA_TMUX:-}" ]]` は、利用者の env に
# 旧 CREWVIA_TMUX が残っていると config の mode: 読み込みそのものをスキップ
# する副作用があった。
#
# 修正: mode: tmux / 不明値 → CREWVIA_MUX=tmux を export。mode: herdr は
# 従来通り CREWVIA_MUX=herdr。mode: inline は CREWVIA_MUX を export しない
# (start.sh 側がインラインにフォールバックする)。CREWVIA_MUX が env で
# 明示指定済みならそれを常に優先し (config より優先)、旧 CREWVIA_TMUX の
# 値は一切参照しない (config 読み込みをブロックしない)。
#
# 隔離方針 (Director 追記 / プランレビュー指摘 1 を反映):
#   - crewvia 本体は cp で複製し無改変のまま検証する (ロジックの書き写しは不可)
#   - scripts/start.sh と scripts/lib_mux.py は複製先でスタブに差し替える。
#     実際の Worker/Director 起動・mux spawn・herdr server 自動起動は
#     絶対に発生させない
#   - herdr server から継承される env (CREWVIA_MUX, HERDR_ENV, CREWVIA_REPO_ROOT,
#     CREWVIA_QUEUE, TMUX, CREWVIA_MUX_ENABLED, 旧 CREWVIA_TMUX) は毎回明示的に
#     unset してから実行する。PATH からは実 herdr を外し、代わりにスタブ
#     herdr バイナリ (存在チェックのみ通す) を前に置く
#   - CREWVIA_HERDR_SOCK / HOME は変更しない (本物の herdr server が読まない
#     ため、変更すると本番ソケットに 2 台目の server が立ってしまう)
#
# Run: bats tests/crewvia-launcher-mux-mode.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
CREWVIA_LAUNCHER="${REPO_ROOT}/crewvia"

# env -u で確実に落とす変数一覧 (herdr server pane からの継承汚染を模した実行環境を再現)
ISOLATE_ENV=(
  -u CREWVIA_MUX
  -u CREWVIA_MUX_ENABLED
  -u CREWVIA_TMUX
  -u HERDR_ENV
  -u TMUX
  -u CREWVIA_REPO_ROOT
  -u CREWVIA_QUEUE
)

# config/crewvia.yaml の mode: を指定してスタブ環境一式を作る。
# $1 = mode の値 (tmux / herdr / inline / bogus-value 等)
setup_fake_env() {
  local mode="$1"
  FAKE_DIR="$(mktemp -d)"
  FAKE_LOG="${FAKE_DIR}/start_sh_calls.log"
  touch "$FAKE_LOG"

  mkdir -p "${FAKE_DIR}/scripts" "${FAKE_DIR}/config"

  # crewvia 本体: cp で複製、無改変 (テスト対象そのもの)
  cp "$CREWVIA_LAUNCHER" "${FAKE_DIR}/crewvia"
  chmod +x "${FAKE_DIR}/crewvia"

  # scripts/start.sh をスタブに置換: 受け取った env と引数をログに書くだけ。
  # 実際の Worker/Director 起動は一切行わない。
  cat > "${FAKE_DIR}/scripts/start.sh" <<'FAKESCRIPT'
#!/usr/bin/env bash
{
  echo "ARGS: $*"
  echo "CREWVIA_MUX=${CREWVIA_MUX:-<unset>}"
  echo "CREWVIA_TMUX=${CREWVIA_TMUX:-<unset>}"
  echo "CREWVIA_MUX_ENABLED=${CREWVIA_MUX_ENABLED:-<unset>}"
} >> "$FAKE_LOG"
exit 0
FAKESCRIPT
  chmod +x "${FAKE_DIR}/scripts/start.sh"

  # scripts/lib_mux.py をスタブに置換: server-running / available は常に成功。
  # 実 herdr への ping・自動起動は絶対に発生しない。
  cat > "${FAKE_DIR}/scripts/lib_mux.py" <<'FAKESCRIPT'
#!/usr/bin/env python3
import sys
print("lib_mux.py stub called with: %r" % (sys.argv[1:],), file=sys.stderr)
sys.exit(0)
FAKESCRIPT
  chmod +x "${FAKE_DIR}/scripts/lib_mux.py"

  # config/crewvia.yaml: mode のみ指定。taskvia は disabled にして対話プロンプトを回避。
  cat > "${FAKE_DIR}/config/crewvia.yaml" <<EOF
mode: ${mode}
taskvia: disabled
EOF

  # スタブ herdr バイナリ: 「存在するか」チェックのみ通す。呼び出されても何もしない
  # (crewvia 本体は herdr 自体を実行せず command -v でしか見ない。実処理は
  # 上のスタブ lib_mux.py に委譲される)。
  cat > "${FAKE_DIR}/herdr" <<'FAKESCRIPT'
#!/usr/bin/env bash
exit 0
FAKESCRIPT
  chmod +x "${FAKE_DIR}/herdr"

  export FAKE_DIR FAKE_LOG
}

teardown() {
  if [[ -n "${FAKE_DIR:-}" && -d "$FAKE_DIR" ]]; then
    rm -rf "$FAKE_DIR"
  fi
}

run_crewvia_worker() {
  run env "${ISOLATE_ENV[@]}" PATH="${FAKE_DIR}:${PATH}" \
    bash "${FAKE_DIR}/crewvia" worker code
}

# --- 事前確認: スタブが本当に呼ばれているか ---

@test "sanity: stubbed start.sh is invoked and logs env (stub wiring works)" {
  setup_fake_env "inline"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  [ -s "$FAKE_LOG" ]
  grep -q "^ARGS: worker code$" "$FAKE_LOG"
}

# --- mode 4通り (env 明示指定なし) ---

@test "mode: tmux + no env override -> CREWVIA_MUX=tmux propagated to start.sh (main fix)" {
  setup_fake_env "tmux"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=tmux$" "$FAKE_LOG"
}

@test "mode: herdr + no env override -> CREWVIA_MUX=herdr propagated (already worked pre-fix)" {
  setup_fake_env "herdr"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=herdr$" "$FAKE_LOG"
}

@test "mode: inline + no env override -> CREWVIA_MUX stays unset (no parallel mode)" {
  setup_fake_env "inline"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=<unset>$" "$FAKE_LOG"
}

@test "mode: unknown value + no env override -> falls back to CREWVIA_MUX=tmux with warning" {
  setup_fake_env "totally-bogus-mode"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=tmux$" "$FAKE_LOG"
  [[ "$output" == *"WARNING"* ]]
}

# --- env の CREWVIA_MUX 明示指定が config mode より優先されること (既存仕様維持) ---

@test "env CREWVIA_MUX=herdr overrides config mode: tmux (env priority preserved)" {
  setup_fake_env "tmux"
  run env "${ISOLATE_ENV[@]}" CREWVIA_MUX=herdr PATH="${FAKE_DIR}:${PATH}" \
    bash "${FAKE_DIR}/crewvia" worker code
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=herdr$" "$FAKE_LOG"
}

# --- herdr server pane からの継承を模したケース (プランレビュー指摘の必須ケース) ---
# 本番 Worker pane は herdr server から CREWVIA_MUX=herdr を継承している。
# その状態で config が mode: inline に変わっていても、env 明示指定が優先される
# ("設定したのに効かない" を作らない)。

@test "herdr-pane-inherited env (CREWVIA_MUX=herdr pre-set) overrides config mode: inline" {
  setup_fake_env "inline"
  run env "${ISOLATE_ENV[@]}" CREWVIA_MUX=herdr PATH="${FAKE_DIR}:${PATH}" \
    bash "${FAKE_DIR}/crewvia" worker code
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=herdr$" "$FAKE_LOG"
}

# --- 旧 CREWVIA_TMUX env が config 読み込みをブロックしないこと ---

@test "legacy CREWVIA_TMUX=1 in env does not block config read -> CREWVIA_MUX=tmux still resolved" {
  setup_fake_env "tmux"
  run env "${ISOLATE_ENV[@]}" CREWVIA_TMUX=1 PATH="${FAKE_DIR}:${PATH}" \
    bash "${FAKE_DIR}/crewvia" worker code
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=tmux$" "$FAKE_LOG"
}

@test "legacy CREWVIA_TMUX=0 in env does not block config read -> CREWVIA_MUX=tmux still resolved" {
  setup_fake_env "tmux"
  run env "${ISOLATE_ENV[@]}" CREWVIA_TMUX=0 PATH="${FAKE_DIR}:${PATH}" \
    bash "${FAKE_DIR}/crewvia" worker code
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_MUX=tmux$" "$FAKE_LOG"
}

# --- 旧 CREWVIA_TMUX 変数そのものが死んでいる (もう export されない) こと ---

@test "CREWVIA_TMUX is never exported by crewvia anymore (dead variable fully removed)" {
  setup_fake_env "tmux"
  run_crewvia_worker
  [ "$status" -eq 0 ]
  grep -q "^CREWVIA_TMUX=<unset>$" "$FAKE_LOG"
}
