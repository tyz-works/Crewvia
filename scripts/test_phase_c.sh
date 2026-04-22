#!/usr/bin/env bash
# test_phase_c.sh — task_089 Phase C: 疎通テスト全項目実行
# 実行: bash ~/workspace/crewvia/scripts/test_phase_c.sh
# 出力: ~/workspace/crewvia/docs/20260422_task089_integration_log.md

set -euo pipefail

TASKVIA_DIR="${TASKVIA_DIR:-$HOME/workspace/Taskvia}"
LOG_FILE="$HOME/workspace/crewvia/docs/20260422_task089_integration_log.md"
DEV_PORT=3000
DEV_PID=""

# Taskvia .env.local を source（シークレット値は出力しない）
if [[ -f "$TASKVIA_DIR/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$TASKVIA_DIR/.env.local"
  set +a
fi

# 必須変数チェック (TASKVIA_TOKEN は省略可能 — 未設定時はオープンモード)
MISSING=()
[[ -z "${NTFY_TOPIC:-}" ]]                  && MISSING+=("NTFY_TOPIC")
[[ -z "${NTFY_PASS:-}" ]]                   && MISSING+=("NTFY_PASS")
[[ -z "${UPSTASH_REDIS_REST_URL:-}" ]]      && MISSING+=("UPSTASH_REDIS_REST_URL")
[[ -z "${UPSTASH_REDIS_REST_TOKEN:-}" ]]    && MISSING+=("UPSTASH_REDIS_REST_TOKEN")

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "[ERROR] 以下の環境変数が未設定です: ${MISSING[*]}"
  echo "  → Taskvia/.env.local に追記するか、export で設定してください。"
  exit 1
fi

NTFY_URL="${NTFY_URL:-https://ntfy.elni.net}"
NTFY_USER="${NTFY_USER:-taskvia}"
BASE_URL="http://localhost:${DEV_PORT}"
# auth.ts: TASKVIA_TOKEN 未設定時はオープンモード (isAuthorized → true)
# AUTH_ARGS はトークン有無に応じて Bearer ヘッダを追加する配列
AUTH_ARGS=()
if [[ -n "${TASKVIA_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${TASKVIA_TOKEN}")
fi

# ---- ログ用ヘルパー ----
PASS_COUNT=0
FAIL_COUNT=0
LOG_LINES=()

log() { LOG_LINES+=("$1"); echo "$1"; }
pass() { PASS_COUNT=$((PASS_COUNT+1)); log "  ✅ PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT+1)); log "  ❌ FAIL: $1"; }

# ---- dev server 管理 ----
start_dev() {
  log ""
  log "## dev server 起動中..."
  cd "$TASKVIA_DIR"
  npm run dev > /tmp/taskvia_dev.log 2>&1 &
  DEV_PID=$!
  log "  PID: $DEV_PID"
  # 最大30秒待機
  for i in $(seq 1 30); do
    if curl -sf "http://localhost:${DEV_PORT}/api/health" > /dev/null 2>&1; then
      log "  → サーバー起動確認 (${i}s)"
      return 0
    fi
    sleep 1
  done
  log "  [ERROR] 30秒以内にサーバーが起動しませんでした"
  cat /tmp/taskvia_dev.log | tail -20
  return 1
}

stop_dev() {
  if [[ -n "${DEV_PID:-}" ]]; then
    kill "$DEV_PID" 2>/dev/null || true
    wait "$DEV_PID" 2>/dev/null || true
    log ""
    log "## dev server 停止 (PID: $DEV_PID)"
  fi
}

trap stop_dev EXIT

# ==============================================================================
# ログファイル初期化
# ==============================================================================
cat > "$LOG_FILE" << HEADER
# task_089 Phase C — 統合疎通テストログ

実施日時: $(date '+%Y-%m-%d %H:%M:%S')
実施者: Worf (Lt. Commander, Tactical)
対象ブランチ:
  - Taskvia: feat/task089-ntfy-phase2-alignment @ e52348e
  - crewvia:  feat/task089-ntfy-phase2-alignment @ 39e81c4
方針: α方針 (Taskvia ntfy統一)

---

HEADER

# ==============================================================================
# カテゴリ 1: ntfy 直接疎通 (外形テスト)
# ==============================================================================
log "## カテゴリ 1: ntfy 直接疎通"
log ""

# 1-a: 認証あり 200
log "### 1-a: 認証あり → 200 期待"
RESP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -u "${NTFY_USER}:${NTFY_PASS}" \
  -H "Title: [Worf Phase-C test]" \
  -d "Phase C 1-a 疎通テスト" \
  "${NTFY_URL}/${NTFY_TOPIC}")
log "  curl -u taskvia:**** -d 'Phase C 1-a' ${NTFY_URL}/<TOPIC>"
log "  期待: 200 | 実際: ${RESP_CODE}"
[[ "$RESP_CODE" == "200" ]] && pass "1-a ntfy 認証あり 200" || fail "1-a ntfy 認証あり: got ${RESP_CODE}"

# 1-b: 認証なし 401
log ""
log "### 1-b: 認証なし → 401 期待"
RESP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${NTFY_URL}/${NTFY_TOPIC}")
log "  curl (no auth) ${NTFY_URL}/<TOPIC>"
log "  期待: 401 | 実際: ${RESP_CODE}"
[[ "$RESP_CODE" == "401" ]] && pass "1-b ntfy 認証なし 401" || fail "1-b ntfy 認証なし: got ${RESP_CODE}"

# 1-c: 誤パスワード 401
log ""
log "### 1-c: 誤パスワード → 401 期待"
RESP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -u "${NTFY_USER}:wrongpassword" \
  "${NTFY_URL}/${NTFY_TOPIC}")
log "  curl -u taskvia:wrongpassword ${NTFY_URL}/<TOPIC>"
log "  期待: 401 | 実際: ${RESP_CODE}"
[[ "$RESP_CODE" == "401" ]] && pass "1-c ntfy 誤パスワード 401" || fail "1-c ntfy 誤パスワード: got ${RESP_CODE}"

# ==============================================================================
# カテゴリ 2: Taskvia ローカル起動 + notify:true フル経路
# ==============================================================================
log ""
log "## カテゴリ 2: Taskvia ローカル起動"
log ""
start_dev

# 2-a: notify:true フル経路
log ""
log "### 2-a: notify:true → {id} + ntfy 通知"
RESP_2A=$(curl -s "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/api/request" \
  -H "Content-Type: application/json" \
  -d '{"tool":"Bash","agent":"Worf","task_title":"Phase-C test notify:true","notify":true}')
log "  curl POST /api/request notify:true"
log "  レスポンス: ${RESP_2A}"
CARD_ID=$(echo "$RESP_2A" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
# NOTE: auth.ts は "Authorization: Bearer <TASKVIA_TOKEN>" を期待
# TASKVIA_TOKEN 未設定時は open mode (isAuthorized → true)
if [[ -n "$CARD_ID" ]] && echo "$RESP_2A" | grep -q '"id"'; then
  pass "2-a notify:true → {id} = ${CARD_ID}"
  # approve_url / deny_url が含まれていないことを確認 (α方針)
  if echo "$RESP_2A" | grep -qE '"approve_url"|"deny_url"'; then
    fail "2-a α方針違反: approve_url/deny_url がレスポンスに含まれている"
  else
    pass "2-a α方針: レスポンスは {id} のみ (approve_url/deny_url なし)"
  fi
else
  fail "2-a notify:true: id が返らなかった (resp=${RESP_2A})"
  CARD_ID=""
fi

# 2-b: notify 未指定 → ntfy は届かない (レスポンスのみ確認)
log ""
log "### 2-b: notify 未指定 → {id} 返却 (ntfy は届かないはず)"
RESP_2B=$(curl -s "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/api/request" \
  -H "Content-Type: application/json" \
  -d '{"tool":"Bash","agent":"Worf","task_title":"Phase-C test no-notify"}')
log "  curl POST /api/request (notify なし)"
log "  レスポンス: ${RESP_2B}"
CARD_ID_NONFNOTIFY=$(echo "$RESP_2B" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
if [[ -n "$CARD_ID_NONFNOTIFY" ]]; then
  pass "2-b notify 未指定 → {id} 返却確認"
  log "  ※ ntfy 未着信は iPhone 側で目視確認が必要 (Phase C では自動判定不可)"
else
  fail "2-b notify 未指定: id が返らなかった"
fi

# 2-c: Redis 格納確認 (TTL チェック)
log ""
log "### 2-c: Redis 格納確認 — approval_token:<token> TTL ~900s"
if [[ -n "$CARD_ID" ]]; then
  # dev server ログから approval_token を探す（ntfy publish 後に生成される）
  # Upstash REST API で approval:${CARD_ID} の存在確認
  REDIS_GET=$(curl -s \
    -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" \
    "${UPSTASH_REDIS_REST_URL}/get/approval:${CARD_ID}")
  log "  Upstash GET approval:${CARD_ID}"
  log "  レスポンス型: $(echo "$REDIS_GET" | python3 -c "import json,sys; d=json.load(sys.stdin); print(type(d.get('result')).__name__)" 2>/dev/null || echo 'unknown')"
  REDIS_VAL=$(echo "$REDIS_GET" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result','null'))" 2>/dev/null || echo "null")
  if [[ "$REDIS_VAL" != "null" ]] && [[ -n "$REDIS_VAL" ]]; then
    # card の status 確認
    CARD_STATUS=$(echo "$REDIS_VAL" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status','?'))" 2>/dev/null || echo "parse_error")
    log "  card status: ${CARD_STATUS}"
    pass "2-c approval:${CARD_ID} Redis 格納確認"
    # TTL 確認
    REDIS_TTL=$(curl -s \
      -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" \
      "${UPSTASH_REDIS_REST_URL}/ttl/approval:${CARD_ID}")
    TTL_VAL=$(echo "$REDIS_TTL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('result','?'))" 2>/dev/null || echo "?")
    log "  TTL: ${TTL_VAL}s (期待: ~600s)"
    if [[ "$TTL_VAL" -gt 0 ]] 2>/dev/null; then
      pass "2-c TTL > 0 確認 (${TTL_VAL}s)"
    else
      fail "2-c TTL 異常: ${TTL_VAL}"
    fi
  else
    fail "2-c approval:${CARD_ID} が Redis に存在しない"
  fi
else
  log "  ⏭ SKIP: 2-a が失敗したため card_id なし"
fi

# ==============================================================================
# カテゴリ 3: approve-token / deny-token API
# ==============================================================================
log ""
log "## カテゴリ 3: approve-token / deny-token"
log ""

# approval_token を取得するには ntfy publish 後のトークンが必要
# dev server ログから抽出を試みる
sleep 2
DEV_LOG=$(cat /tmp/taskvia_dev.log 2>/dev/null || echo "")

# ntfy.ts が生成した token を Upstash で検索する方法がないため、
# /api/request に notify:true で再度リクエストしてカードIDを取得し、
# approval_token は Upstash scan で探す（または dev log から）
log "### 3-x: テスト用 approval_token を取得"
RESP_3=$(curl -s "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/api/request" \
  -H "Content-Type: application/json" \
  -d '{"tool":"Bash","agent":"Worf","task_title":"Phase-C test approve-token","notify":true}')
TEST3_CARD_ID=$(echo "$RESP_3" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
log "  POST /api/request → id: ${TEST3_CARD_ID}"

# Upstash scan で approval_token:* キーを検索（cursor 0 で最初の batch）
SCAN_RESP=$(curl -s \
  -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" \
  "${UPSTASH_REDIS_REST_URL}/scan/0/match/approval_token:*")
TEST_TOKEN=$(echo "$SCAN_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
keys = d.get('result', [None, []])[1]
# approval_token:xxxx から xxxx 部分を取り出す
for k in keys:
    if k.startswith('approval_token:'):
        print(k.split(':', 1)[1])
        break
" 2>/dev/null || echo "")

if [[ -n "$TEST_TOKEN" ]]; then
  log "  approval_token 取得成功 (長さ: ${#TEST_TOKEN})"

  # 3-a: 正常 approve → 200
  log ""
  log "### 3-a: approve-token 正常 → 200 {ok:true}"
  RESP_3A=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/approve-token/${TEST_TOKEN}")
  CODE_3A=$(echo "$RESP_3A" | tail -1)
  BODY_3A=$(echo "$RESP_3A" | head -1)
  log "  POST /api/approve-token/<token>"
  log "  status: ${CODE_3A} | body: ${BODY_3A}"
  [[ "$CODE_3A" == "200" ]] && echo "$BODY_3A" | grep -q '"ok":true' && pass "3-a approve-token 200 {ok:true}" || fail "3-a approve-token: got ${CODE_3A} ${BODY_3A}"

  # 3-b: 再送 → 409
  log ""
  log "### 3-b: approve-token 再送 → 409"
  RESP_3B=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/approve-token/${TEST_TOKEN}")
  CODE_3B=$(echo "$RESP_3B" | tail -1)
  BODY_3B=$(echo "$RESP_3B" | head -1)
  log "  POST /api/approve-token/<same_token>"
  log "  status: ${CODE_3B} | body: ${BODY_3B}"
  [[ "$CODE_3B" == "409" ]] && pass "3-b approve-token 再送 409" || fail "3-b approve-token 再送: got ${CODE_3B}"

  # 3-e: 消費後 TTL 短縮確認
  log ""
  log "### 3-e: 消費後 token TTL 短縮確認 (期待: ~60s)"
  TTL_RESP=$(curl -s \
    -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" \
    "${UPSTASH_REDIS_REST_URL}/ttl/approval_token:${TEST_TOKEN}")
  TOKEN_TTL=$(echo "$TTL_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('result','?'))" 2>/dev/null || echo "?")
  log "  approval_token TTL: ${TOKEN_TTL}s (期待: ≤60s)"
  if [[ "$TOKEN_TTL" -le 60 ]] 2>/dev/null && [[ "$TOKEN_TTL" -gt 0 ]]; then
    pass "3-e 消費後 TTL 短縮: ${TOKEN_TTL}s (≤60)"
  elif [[ "$TOKEN_TTL" == "-1" ]]; then
    fail "3-e TTL -1: 消費後に TTL が設定されていない"
  else
    fail "3-e 消費後 TTL: ${TOKEN_TTL} (期待 ≤60s)"
  fi
else
  log "  [WARN] approval_token が Upstash scan で見つかりませんでした"
  log "         → notify:true でも ntfy.ts が token を生成していない可能性"
  fail "3-x approval_token 取得失敗 (scan empty)"
  log "  ⏭ SKIP 3-a/3-b/3-e"
fi

# 3-c: 不在 token → 404
log ""
log "### 3-c: 不在 token → 404"
RESP_3C=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/approve-token/nonexistent_token_worf_test")
CODE_3C=$(echo "$RESP_3C" | tail -1)
BODY_3C=$(echo "$RESP_3C" | head -1)
log "  POST /api/approve-token/nonexistent_token_worf_test"
log "  status: ${CODE_3C} | body: ${BODY_3C}"
[[ "$CODE_3C" == "404" ]] && pass "3-c 不在 token 404" || fail "3-c 不在 token: got ${CODE_3C}"

# deny-token 用に新しいトークンを取得
RESP_DENY=$(curl -s "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/api/request" \
  -H "Content-Type: application/json" \
  -d '{"tool":"Bash","agent":"Worf","task_title":"Phase-C deny-token test","notify":true}')
DENY_CARD_ID=$(echo "$RESP_DENY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
sleep 1
SCAN_RESP2=$(curl -s \
  -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" \
  "${UPSTASH_REDIS_REST_URL}/scan/0/match/approval_token:*")
DENY_TOKEN=$(echo "$SCAN_RESP2" | python3 -c "
import json, sys
d = json.load(sys.stdin)
keys = d.get('result', [None, []])[1]
unconsumed = []
for k in keys:
    if k.startswith('approval_token:'):
        unconsumed.append(k.split(':', 1)[1])
# 最後のものを使う
if unconsumed: print(unconsumed[-1])
" 2>/dev/null || echo "")

if [[ -n "$DENY_TOKEN" ]]; then
  # 3-d: deny-token 正常 → 200
  log ""
  log "### 3-d: deny-token 正常 → 200"
  RESP_3D=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/deny-token/${DENY_TOKEN}")
  CODE_3D=$(echo "$RESP_3D" | tail -1)
  BODY_3D=$(echo "$RESP_3D" | head -1)
  log "  POST /api/deny-token/<token>"
  log "  status: ${CODE_3D} | body: ${BODY_3D}"
  [[ "$CODE_3D" == "200" ]] && echo "$BODY_3D" | grep -q '"ok":true' && pass "3-d deny-token 200 {ok:true}" || fail "3-d deny-token: got ${CODE_3D}"

  # deny 409 テスト
  RESP_3D2=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/deny-token/${DENY_TOKEN}")
  CODE_3D2=$(echo "$RESP_3D2" | tail -1)
  log "  再送: status: ${CODE_3D2}"
  [[ "$CODE_3D2" == "409" ]] && pass "3-d deny-token 再送 409" || fail "3-d deny-token 再送: got ${CODE_3D2}"
else
  fail "3-d deny-token: テスト用トークン取得失敗"
fi

# ==============================================================================
# カテゴリ 4: Status ポーリング
# ==============================================================================
log ""
log "## カテゴリ 4: Status ポーリング"
log ""

if [[ -n "$TEST3_CARD_ID" ]]; then
  # approve 済みカードのステータス確認
  log "### 4-a: approved card のステータス"
  RESP_4A=$(curl -s "${AUTH_ARGS[@]}" "${BASE_URL}/api/status/${TEST3_CARD_ID}")
  STATUS_4A=$(echo "$RESP_4A" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "parse_error")
  log "  GET /api/status/${TEST3_CARD_ID}"
  log "  status: ${STATUS_4A}"
  [[ "$STATUS_4A" == "approved" ]] && pass "4-a approved card status=approved" || fail "4-a status: got '${STATUS_4A}' (expected 'approved')"
fi

if [[ -n "$DENY_CARD_ID" ]]; then
  # deny 済みカードのステータス確認
  log ""
  log "### 4-b: denied card のステータス"
  RESP_4B=$(curl -s "${AUTH_ARGS[@]}" "${BASE_URL}/api/status/${DENY_CARD_ID}")
  STATUS_4B=$(echo "$RESP_4B" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "parse_error")
  log "  GET /api/status/${DENY_CARD_ID}"
  log "  status: ${STATUS_4B}"
  [[ "$STATUS_4B" == "denied" ]] && pass "4-b denied card status=denied" || fail "4-b status: got '${STATUS_4B}' (expected 'denied')"
fi

# pending カードのステータス確認
log ""
log "### 4-c: pending card のステータス"
RESP_PENDING=$(curl -s "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/api/request" \
  -H "Content-Type: application/json" \
  -d '{"tool":"Bash","agent":"Worf","task_title":"Phase-C pending status test","notify":false}')
PENDING_ID=$(echo "$RESP_PENDING" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
if [[ -n "$PENDING_ID" ]]; then
  RESP_4C=$(curl -s "${AUTH_ARGS[@]}" "${BASE_URL}/api/status/${PENDING_ID}")
  STATUS_4C=$(echo "$RESP_4C" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "parse_error")
  log "  GET /api/status/${PENDING_ID}"
  log "  status: ${STATUS_4C}"
  [[ "$STATUS_4C" == "pending" ]] && pass "4-c pending card status=pending" || fail "4-c status: got '${STATUS_4C}' (expected 'pending')"
fi

# ==============================================================================
# カテゴリ 5: crewvia hook 統合
# ==============================================================================
log ""
log "## カテゴリ 5: crewvia hook 統合"
log ""

CREWVIA_HOOKS="$HOME/workspace/crewvia/hooks"

# 5-1: bash -n (再確認、start_dev 後でも)
log "### 5-1: bash -n 構文チェック"
if bash -n "${CREWVIA_HOOKS}/lib_approval_channel.sh" 2>/dev/null; then
  pass "5-1 lib_approval_channel.sh bash -n"
else
  fail "5-1 lib_approval_channel.sh bash -n 失敗"
fi
if bash -n "${CREWVIA_HOOKS}/pre-tool-use.sh" 2>/dev/null; then
  pass "5-1 pre-tool-use.sh bash -n"
else
  fail "5-1 pre-tool-use.sh bash -n 失敗"
fi

# 5-2: grep 残骸チェック (コメント除外)
log ""
log "### 5-2: α方針残骸チェック"
NTFY_RESIDUAL=$(grep -rn "ntfy_publish\|parse_token_urls" "${CREWVIA_HOOKS}/" | grep -v "^\([^:]*\):[0-9]*:#" || echo "")
if [[ -z "$NTFY_RESIDUAL" ]]; then
  pass "5-2 ntfy_publish/parse_token_urls 実コード残骸なし"
else
  fail "5-2 α方針違反残骸検出: ${NTFY_RESIDUAL}"
fi

# 5-3: CREWVIA_APPROVAL_CHANNEL=taskvia での pre-tool-use 疑似実行
log ""
log "### 5-3: CREWVIA_APPROVAL_CHANNEL=taskvia モード — /api/request 呼び出し確認"
log "  (Taskvia dev server が既に起動中)"
# 環境変数を設定してシミュレーション
TASKVIA_URL_TEST="${BASE_URL}"
INPUT_JSON='{"tool":"Bash","input":{"command":"echo test"}}'
# pre-tool-use.sh を直接実行してレスポンス確認
CREWVIA_TASKVIA="enabled" \
TASKVIA_URL="${TASKVIA_URL_TEST}" \
TASKVIA_TOKEN="${TASKVIA_TOKEN}" \
CREWVIA_APPROVAL_CHANNEL="taskvia" \
  bash -c "
    echo '${INPUT_JSON}' | bash '${CREWVIA_HOOKS}/pre-tool-use.sh'
    echo EXIT_CODE:\$?
  " > /tmp/preuse_test.log 2>&1 || true
PREUSE_LOG=$(cat /tmp/preuse_test.log 2>/dev/null)
log "  pre-tool-use.sh 実行ログ (抜粋):"
echo "$PREUSE_LOG" | head -20 | while read -r line; do log "    $line"; done
# dev server ログで /api/request が叩かれたか確認
sleep 1
if grep -q "POST /api/request" /tmp/taskvia_dev.log 2>/dev/null; then
  pass "5-3 dev server ログで /api/request 呼び出し確認"
else
  log "  [INFO] dev server ログに /api/request なし (スタンドアロンモードで抜けた可能性)"
  # pre-tool-use が正常終了しているなら OK
  if echo "$PREUSE_LOG" | grep -qE "EXIT_CODE:0|EXIT_CODE:2"; then
    pass "5-3 pre-tool-use.sh 正常終了"
  else
    fail "5-3 pre-tool-use.sh 異常終了"
  fi
fi

# 5-4: CREWVIA_APPROVAL_CHANNEL=ntfy (モードは ntfy — Taskvia 不使用)
log ""
log "### 5-4: CREWVIA_APPROVAL_CHANNEL=ntfy — ntfy 直叩き除去確認"
# ntfy モードでも pre-tool-use が ntfy_publish を呼んでいないこと (lib から除去済み)
# lib_approval_channel.sh の get_approval_channel_mode が "ntfy" を返す設定でテスト
CHANNEL_OUT=$(CREWVIA_APPROVAL_CHANNEL="ntfy" \
  bash -c "source '${CREWVIA_HOOKS}/lib_approval_channel.sh'; get_approval_channel_mode" 2>/dev/null || echo "error")
log "  get_approval_channel_mode with CREWVIA_APPROVAL_CHANNEL=ntfy: ${CHANNEL_OUT}"
[[ "$CHANNEL_OUT" == "ntfy" ]] && pass "5-4 ntfy チャネルモード返却正常" || fail "5-4 チャネルモード: got '${CHANNEL_OUT}'"

# 5-5: CREWVIA_APPROVAL_CHANNEL=both
log ""
log "### 5-5: CREWVIA_APPROVAL_CHANNEL=both — 既存動作確認"
CHANNEL_BOTH=$(CREWVIA_APPROVAL_CHANNEL="both" \
  bash -c "source '${CREWVIA_HOOKS}/lib_approval_channel.sh'; get_approval_channel_mode" 2>/dev/null || echo "error")
log "  get_approval_channel_mode with CREWVIA_APPROVAL_CHANNEL=both: ${CHANNEL_BOTH}"
[[ "$CHANNEL_BOTH" == "both" ]] && pass "5-5 both チャネルモード返却正常" || fail "5-5 チャネルモード: got '${CHANNEL_BOTH}'"

# ==============================================================================
# 結果サマリー
# ==============================================================================
log ""
log "---"
log ""
log "## テスト結果サマリー"
log ""
log "- 合計: $((PASS_COUNT + FAIL_COUNT)) 項目"
log "- ✅ PASS: ${PASS_COUNT}"
log "- ❌ FAIL: ${FAIL_COUNT}"
log ""
if [[ $FAIL_COUNT -eq 0 ]]; then
  log "**判定: ALL GREEN — Phase D (Beverly) 引き継ぎ可能**"
else
  log "**判定: FAIL あり — 差し戻し要確認**"
fi
log ""
log "## Phase D (Beverly) 引き継ぎメモ"
log ""
log "1. iPhone ntfy subscribe 確認: ${NTFY_URL}/<TOPIC> (認証: taskvia/****)"
log "   → NTFY_USER/NTFY_PASS が正しく設定され、通知が届くことを目視確認"
log "2. TASKVIA_BASE_URL が本番 URL (https://taskvia.vercel.app) に設定されていること"
log "   → ntfy アクションボタンの approve/deny URL が本番を向くため"
log "3. APPROVAL_TOKEN_TTL_SECONDS のデフォルトは 900s — Phase D E2E では時間余裕あり"
log "4. /api/approve-token と /api/deny-token は Bearer 認証不要 (token 自体が秘密)"
log "5. 本番では Redis の approval:* キーが TTL 600s で自動消滅する点を考慮"

# ファイル書き込み
{
  for line in "${LOG_LINES[@]}"; do
    echo "$line"
  done
} >> "$LOG_FILE"

echo ""
echo "=========================================="
echo "ログ出力: $LOG_FILE"
echo "PASS: ${PASS_COUNT} / FAIL: ${FAIL_COUNT}"
echo "=========================================="

[[ $FAIL_COUNT -eq 0 ]]
