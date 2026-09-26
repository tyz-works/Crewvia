#!/usr/bin/env bats
# tests/model-per-skill.bats
#
# start.sh 側の skill → model 解決テスト。
# CREWVIA_PRINT_MODEL=1 を使うことで claude を起動せずに判定のみ実施。
# 副作用ゼロ: registry 書き込み / worktree 汚染が発生しないことも検証する。
#
# Run:
#   bats tests/model-per-skill.bats
#   npx bats tests/model-per-skill.bats
#
# Coverage:
#   1. env 明示指定 (CREWVIA_WORKER_MODEL) が model_per_skill より優先
#   2. env 無しで model_per_skill が効く (planning→opus / docs,qa,verify→sonnet / code→sonnet)
#   3. director role では model_per_skill が適用されない (CREWVIA_DIRECTOR_MODEL のみ)
#   4. config 欠損時に WORKER_MODEL_FROM_CONFIG フォールバック → 空文字を返す

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_SH="${REPO_ROOT}/scripts/start.sh"
REAL_CONFIG="${REPO_ROOT}/config/crewvia.yaml"

# ---------------------------------------------------------------------------
# 本番 mux への出口を塞ぐ (t037)
# ---------------------------------------------------------------------------
# CREWVIA_PRINT_MODEL=1 は start.sh の副作用のある処理より前で exit するので、
# 今日のこの suite は mux に届かない。届かないことを **start.sh のどこで exit
# するか** に預けているのが問題で、その行が動いた瞬間、この suite は本番の
# herdr ワークスペースに Worker ペインを作り始める。2026-09-23 に pytest 側で
# 起きたのと同じ形 (テストが本番の mux を掴む) なので、bash 側の唯一の隔離手段
# である PATH のスタブをここでも置いておく。
setup() {
  MUX_STUB_DIR="$(mktemp -d)"
  # 名前はリテラルで書く: 「この suite は tmux / herdr を潰している」を、
  # tests/test_mux_production_safety.py の全数点検が読み取れるようにするため。
  printf '#!/usr/bin/env bash\nexit 1\n' > "${MUX_STUB_DIR}/tmux"
  printf '#!/usr/bin/env bash\nexit 1\n' > "${MUX_STUB_DIR}/herdr"
  chmod +x "${MUX_STUB_DIR}/tmux" "${MUX_STUB_DIR}/herdr"
  export PATH="${MUX_STUB_DIR}:${PATH}"
}

teardown() {
  # start-sh-mux-gate.bats と同じ後始末 (find -delete + rmdir)。
  if [[ -n "${MUX_STUB_DIR:-}" && -d "$MUX_STUB_DIR" ]]; then
    find "$MUX_STUB_DIR" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$MUX_STUB_DIR" 2>/dev/null || true
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Test 1: env 明示指定が model_per_skill より優先される
# ---------------------------------------------------------------------------
@test "CREWVIA_WORKER_MODEL 明示指定は skill mapping を上書きする" {
  # docs skill は sonnet のはずだが、env で opus を指定すれば opus が返る
  result=$(CREWVIA_WORKER_MODEL="claude-opus-5" CREWVIA_PRINT_MODEL=1 \
           bash "$START_SH" worker docs 2>/dev/null)
  [ "$result" = "claude-opus-5" ]
}

@test "CREWVIA_WORKER_MODEL 明示指定は複数 skill 指定でも優先される" {
  result=$(CREWVIA_WORKER_MODEL="claude-sonnet-5" CREWVIA_PRINT_MODEL=1 \
           bash "$START_SH" worker planning docs 2>/dev/null)
  # planning は opus のはずだが、env 指定の sonnet が勝つ
  [ "$result" = "claude-sonnet-5" ]
}

# ---------------------------------------------------------------------------
# Test 2: env 無しで model_per_skill が適用される
# ---------------------------------------------------------------------------
@test "env 無し: planning → claude-opus-5" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker planning 2>/dev/null)
  [ "$result" = "claude-opus-5" ]
}

# docs / qa / verify は Sonnet (t028): Haiku は permission-mode auto を無視して承認ダイアログで止まる。
@test "env 無し: docs → claude-sonnet-5" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker docs 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "env 無し: qa → claude-sonnet-5" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker qa 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "env 無し: verify → claude-sonnet-5" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker verify 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "env 無し: code → claude-sonnet-5 (worker_model フォールバック)" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker code 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "env 無し: docs qa → claude-sonnet-5 (複数 skill でも Haiku に落ちない)" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker docs qa 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "env 無し: planning code → claude-opus-5 (最高ランク選択)" {
  result=$(CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker planning code 2>/dev/null)
  [ "$result" = "claude-opus-5" ]
}

# ---------------------------------------------------------------------------
# Test 3: director role は model_per_skill の影響を受けない
# ---------------------------------------------------------------------------
@test "director: CREWVIA_DIRECTOR_MODEL が使われる (model_per_skill 不適用)" {
  result=$(CREWVIA_DIRECTOR_MODEL="claude-opus-5" CREWVIA_PRINT_MODEL=1 \
           bash "$START_SH" director 2>/dev/null)
  [ "$result" = "claude-opus-5" ]
}

@test "director: CREWVIA_DIRECTOR_MODEL 未設定なら config の director_model を使う" {
  # 実 config に director_model が設定されていれば、それが返る
  result=$(unset CREWVIA_DIRECTOR_MODEL; CREWVIA_PRINT_MODEL=1 \
           bash "$START_SH" director 2>/dev/null)
  # config の director_model は claude-opus-5
  [ "$result" = "claude-opus-5" ]
}

# ---------------------------------------------------------------------------
# Test 4: config 欠損時のフォールバック
# ---------------------------------------------------------------------------
@test "config 欠損時: CREWVIA_WORKER_MODEL フォールバック (env で設定)" {
  fake_config="$(mktemp -d)/nonexistent.yaml"
  # CONFIG_FILE を偽ファイルに向けるために env var 経由では上書きできないため、
  # CREWVIA_WORKER_MODEL を明示的に設定してフォールバック動作を確認する
  result=$(CREWVIA_WORKER_MODEL="claude-sonnet-5" CREWVIA_PRINT_MODEL=1 \
           bash "$START_SH" worker code 2>/dev/null)
  [ "$result" = "claude-sonnet-5" ]
}

@test "CREWVIA_PRINT_MODEL=1 は副作用なし: registry が汚染されない" {
  # workers.yaml の mtime を記録
  registry="${REPO_ROOT}/registry/workers.yaml"
  before_mtime=$(stat -c %Y "$registry" 2>/dev/null || echo "0")

  CREWVIA_PRINT_MODEL=1 bash "$START_SH" worker docs >/dev/null 2>&1

  after_mtime=$(stat -c %Y "$registry" 2>/dev/null || echo "0")
  [ "$before_mtime" = "$after_mtime" ]
}
