#!/usr/bin/env bash
# review-plan.sh <slug>
# Launches Plan Reviewer in a separate tmux window and waits for plan_review.md output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CREWVIA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SLUG="${1:-}"

if [[ -z "$SLUG" ]]; then
    echo "Usage: review-plan.sh <slug>" >&2
    exit 1
fi

MISSION_DIR="$CREWVIA_DIR/queue/missions/$SLUG"
REVIEW_OUTPUT="$MISSION_DIR/plan_review.md"

if [[ ! -d "$MISSION_DIR" ]]; then
    echo "Mission not found: $SLUG" >&2
    exit 1
fi

WINDOW_NAME="plan-reviewer-$$"

# Use lib_mux.sh for mux-backend-agnostic window management.
# shellcheck source=lib_mux.sh
source "${SCRIPT_DIR}/lib_mux.sh"

# --- t002 (mission 20260908-launch-reliability) パターン3対策 ---
# レビュー開始前に既存の plan_review.md を削除する。削除しないと、前 cycle の
# 古いファイルが残っている場合に下の待機ループが即座に「有効な verdict あり」
# と誤認し、古い判定をそのまま新しい判定として採用してしまう (本ミッション
# 自身の cycle 2 で実際に発生。誤った revise が cycle 1 と同一内容のまま返り、
# cycle_count だけを消費した)。
#
# REVIEW_START_EPOCH は、万一 rm が何らかの理由で効かなかった場合の二重の
# 安全策として scripts/wait_for_plan_review.sh に渡す — mtime が
# レビュー開始時刻より新しいファイルだけを「今回の実行の出力」とみなす。
rm -f "$REVIEW_OUTPUT"
REVIEW_START_EPOCH=$(date +%s)

# --- t004 (mission 20260909-dead-config-sweep) ---
# plan-reviewer の最終応答を `claude --json-schema` で
# config/plan-review-verdict.schema.json 形式に強制する。散文プロンプト指示
# (`**Verdict:** approve` を1行目に書け) だけに頼っていた従来方式は
# plan-reviewer (Opus) が規定形式を外すことが繰り返し起きていた
# (agents/plan_reviewer.md の「★ 最重要」参照)。CLI 自身がスキーマ適合を
# 保証する構造化出力を rescue 経路として使うことで、下記の既存 polling/
# normalize 経路 (`scripts/wait_for_plan_review.sh` 呼び出し以降) が失敗した
# 場合でも verdict を機械的に回収できるようにする (先例: kai-review.sh の
# codex exec --output-schema 移行、PR #193)。既存 polling/normalize 経路は
# そのまま残す (プロンプト指示だけで規定形式が書かれた通常ケースでは rescue
# は何もしない no-op)。
# スキーマファイルが無い/壊れている場合でも review-plan.sh 全体を落とさない
# (rescue 機構は既存 polling/normalize 経路の上に乗る追加の保険であり、
# 必須依存にすると新規ファイルの取り違え/削除だけで正規のレビュー実行が
# 止まってしまう。t001 の config/crewvia.yaml 欠如時のフォールバック方針
# (grep が空を返すだけで exit しない) と同じ考え方)。
SCHEMA_FILE="$CREWVIA_DIR/config/plan-review-verdict.schema.json"
VERDICT_SCHEMA=""
if [[ -f "$SCHEMA_FILE" ]]; then
    VERDICT_SCHEMA="$(jq -c . "$SCHEMA_FILE" 2>/dev/null || true)"
fi
if [[ -z "$VERDICT_SCHEMA" ]]; then
    echo "[review-plan.sh] WARNING: verdict schema unavailable ($SCHEMA_FILE not found or invalid JSON) — structured-output rescue disabled, falling back to prose-only verdict parsing" >&2
fi
# mux 経路 (INLINE_CMD は文字列として後で実行されるため single-quote で
# 埋め込む)。inline フォールバック経路は配列展開でそのまま渡す。
SCHEMA_CLI_ARG=""
[[ -n "$VERDICT_SCHEMA" ]] && SCHEMA_CLI_ARG=" --output-format json --json-schema '$VERDICT_SCHEMA'"
SCHEMA_FLAG=()
[[ -n "$VERDICT_SCHEMA" ]] && SCHEMA_FLAG=(--output-format json --json-schema "$VERDICT_SCHEMA")
PLAN_REVIEWER_LOG="/tmp/plan_reviewer_$$.log"

# SKILLS=plan_review: config/skill-permissions.yaml の plan_review セクション
# (Bash 全面禁止 / Edit・MultiEdit 禁止 / Write は plan_review.md 限定) が
# 実際に適用されるようにするための必須設定。
#
# 発見した副次バグ (t002): hooks/pre-tool-use.sh の per-skill チェックは
# `SKILLS` 環境変数が設定されている場合にしか動かない。旧版はここで
# CLAUDE_SKILL=plan_review しか設定しておらず、hook はその変数を一切読まない
# ため、plan-reviewer セッションは skill-permissions.yaml の plan_review
# セクションを完全にバイパスして動いていた (Bash も Edit も無制限)。
# 実際に mission.yaml が書き換えられた事故は、この export 漏れが真因の一つ。
# CLAUDE_SKILL は claude CLI 自体には影響しないが、ログ上の識別用に残す。
#
# 注意: SKILLS=plan_review を有効にすると Bash が完全に deny されるため、
# agents/plan_reviewer.md 側も「Step 1 で `ls` (Bash) を使う」という旧来の
# 手順を Glob ツールに置き換えてある (Bash 前提の手順のままだと reviewer が
# Step 1 から動けなくなる)。
#
# AGENT_NAME='Plan-Reviewer' (t008, PR#188 レビュー指摘 P1・Codex 指摘):
# review-plan.sh は Director セッションの子プロセスとして起動されるため、
# ここで AGENT_NAME を上書きしないと Director の AGENT_NAME (registry で
# role: director) がそのまま継承される。hooks/pre-tool-use.sh の Director
# bypass (role: director を見て即 allow) は SKILLS=plan_review の per-skill
# チェックより前に評価されるため、上書きしないと reviewer セッションは
# 「登録済み Director」として全ツールが即通過し、上の SKILLS=plan_review が
# 実質 dead code になっていた (mission.yaml を書き換えられる事故の未解決分)。
# registry/workers.yaml に登録の無い名前を使うことで role: director 判定に
# 一致させず、SKILLS ベースの per-skill チェックを必ず通過させる
# (scripts/kai-review.sh / scripts/start.sh と同じ「非 Director identity で
# 起動する」パターン)。
#
# unset CLAUDE_CODE_CHILD_SESSION: herdr server 由来の汚染変数が Plan Reviewer に伝播しないよう除去。
# CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1: 二重防御として transcript 保存を公式 env var で保証 (→ t004)。
INLINE_CMD="unset CLAUDE_CODE_CHILD_SESSION; export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; export AGENT_NAME='Plan-Reviewer'; export SKILLS=plan_review; cd '$CREWVIA_DIR' && CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
     ${SCHEMA_CLI_ARG} \
     -p 'Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。' \
     2>&1 | tee '$PLAN_REVIEWER_LOG'"

MUX_LAUNCHED=0
# F4 (PR#188 t012 Seo 指摘): 以前は成功時とタイムアウト時の2箇所に明示的な
# mux_kill を置いていたが、それでは Director の Ctrl-C (SIGINT) / SIGTERM /
# spawn 後〜末尾の間で set -e により中断した場合の経路をカバーできず、
# plan-reviewer の pane が残り続けていた。EXIT/INT/TERM の trap 1箇所に
# 集約することで、スクリプトがどう終わっても (正常終了・タイムアウト・
# 中断) 必ず1回だけ kill されるようにする (成功時・タイムアウト時の個別
# kill は不要になったため削除)。
trap '[[ $MUX_LAUNCHED -eq 1 ]] && mux_kill "$WINDOW_NAME" 2>/dev/null || true' EXIT INT TERM

if mux_available && mux_spawn "$WINDOW_NAME" "$INLINE_CMD" "$CREWVIA_DIR"; then
    MUX_LAUNCHED=1
else
    echo "[review-plan.sh] WARNING: mux unavailable or spawn failed — running Plan Reviewer inline" >&2
    cd "$CREWVIA_DIR"
    # herdr server 汚染の伝播を防ぐため claude 実行直前に除去する。
    unset CLAUDE_CODE_CHILD_SESSION
    # 二重防御: transcript 保存を公式 env var で保証する。
    export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1
    # 上の INLINE_CMD と同じ理由 (Director identity 継承による
    # skill-permissions.yaml バイパスを防ぐため)。
    export AGENT_NAME='Plan-Reviewer'
    # 上の INLINE_CMD と同じ理由 (skill-permissions.yaml の plan_review 制限を
    # 実際に適用するため)。
    export SKILLS=plan_review
    CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
        "${SCHEMA_FLAG[@]+"${SCHEMA_FLAG[@]}"}" \
        -p "Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。" \
        2>&1 | tee "$PLAN_REVIEWER_LOG"
    if [[ $? -ne 0 ]]; then
        echo "[review-plan.sh] ERROR: Plan Reviewer exited with non-zero status" >&2
        exit 1
    fi
fi

# Wait up to 600s for plan_review.md with valid verdict.
# ポーリング判定本体は scripts/wait_for_plan_review.sh に切り出してある —
# claude CLI を spawn しない独立スクリプトにすることで、t002 の3パターン
# (規定形式 / 別表記 / 前 cycle の残骸) を claude を起動せずに回帰テストできる
# (scripts/test_wait_for_plan_review.sh 参照)。
echo "[review-plan.sh] Waiting for plan_review.md with valid verdict (max 600s)..."
set +e
WAIT_OUTPUT="$(bash "${SCRIPT_DIR}/wait_for_plan_review.sh" "$REVIEW_OUTPUT" "$REVIEW_START_EPOCH" 600 5)"
WAIT_RC=$?
set -e
WAIT_STATUS="$(printf '%s\n' "$WAIT_OUTPUT" | head -1)"
# 2行目以降 (人間向けログ) を stderr に転記する。実際のログは
# wait_for_plan_review.sh 自身も stderr に出しているため、ここでは1行目だけ
# 拾えれば十分だが、標準出力に紛れ込んだ場合に備えて残りも表示しておく。
printf '%s\n' "$WAIT_OUTPUT" | tail -n +2 >&2 || true

# --- t004 / t012: 構造化出力を verdict の主経路にする ---
# claude --json-schema が返す structured_output.verdict は
# config/plan-review-verdict.schema.json の enum ("approve"|"revise"|"reject")
# に CLI 自身が適合を保証した値であり、「フェンスの内側か」「否定文か」
# といった曖昧性が原理的に存在しない単一の判定 unit である
# (QA t008 が自作入力 25/25 で fail-closed を実測済み)。
#
# t004 版はこれを「プローズ解析が失敗したときだけ動く後段の rescue」として
# 置いていた。t012 でその位置づけを変え、**プローズの成否に関わらず必ず
# 読み、判定の権威とする** — plan_review.md 側の規定形式解析
# (scripts/lib_verdict.py) は t012 で「ファイルの最初の非空行だけを完全一致で
# 見る」形に絞り込まれており (QA t011 NEW-1/NEW-2 対応)、書式を外した
# plan_review.md は意図的にすべて判定不能になる。その回収をこの経路が担う。
#
# 倒れる方向: ログが無い/JSON としてパースできない/ドキュメントが複数ある/
# verdict が既知の3値以外、のいずれでも何もしない (WAIT_STATUS を変更しない)
# — 既存の判定 (失敗ならタイムアウト) をそのまま採用する。
_rescue_verdict_from_structured_output() {
    local log="$1"
    [[ -f "$log" ]] || return 1

    # F-B (kai-review.sh 同型対策, PR #193): "type":"result" 行がちょうど1件
    # でなければ判定不能として諦める (2>&1 でマージされた stderr の混入や、
    # 複数ドキュメントの誤採用を防ぐ)。
    local candidates
    candidates="$(grep -c '"type":"result"' "$log" 2>/dev/null || true)"
    [[ "$candidates" -eq 1 ]] || return 1

    local line verdict
    line="$(grep '"type":"result"' "$log" 2>/dev/null)"
    verdict="$(printf '%s' "$line" | jq -er 'select(.structured_output.verdict | type == "string") | .structured_output.verdict' 2>/dev/null)" || return 1

    case "$verdict" in
        approve|revise|reject) ;;
        *) return 1 ;;
    esac

    printf '%s\n' "$verdict"
}

# plan_review.md 側 (規定形式の1行目) から読める verdict。
# 読めなければ空文字 (判定不能)。
PROSE_VERDICT="$(python3 "${SCRIPT_DIR}/lib_verdict.py" "$REVIEW_OUTPUT" 2>/dev/null || true)"

STRUCTURED_VERDICT=""
if [[ -n "$VERDICT_SCHEMA" ]]; then
    if [[ "$WAIT_RC" -ne 0 || "$WAIT_STATUS" != "OK" ]]; then
        # mux 経路では review-plan.sh は plan-reviewer プロセスの終了を直接
        # 待たない (plan_review.md の mtime 安定化だけで判定している) ため、
        # 上の polling が抜けた時点で最終ターン (構造化出力) がまだ書き終わって
        # いない可能性がある。短い bounded retry で追いつくのを待つ (最大
        # 8 * 3 = 24秒。プローズが判定不能だった経路でしか発火しないため
        # 通常ケースの所要時間には影響しない)。
        _rescue_i=0
        while [[ "$_rescue_i" -lt 8 ]]; do
            if STRUCTURED_VERDICT="$(_rescue_verdict_from_structured_output "$PLAN_REVIEWER_LOG")"; then
                break
            fi
            STRUCTURED_VERDICT=""
            _rescue_i=$((_rescue_i + 1))
            sleep 3
        done
    else
        # プローズ側が既に判定できている場合は retry せず1回だけ読む
        # (正常系の所要時間を 24 秒伸ばさないため)。読めなければ
        # プローズの判定をそのまま使う。
        STRUCTURED_VERDICT="$(_rescue_verdict_from_structured_output "$PLAN_REVIEWER_LOG")" || STRUCTURED_VERDICT=""
    fi
fi

# 採用する verdict を1つに決める。
# 構造化出力とプローズが食い違った場合は**どちらも採らない** — 同じ
# plan-reviewer セッションが plan_review.md と最終応答で違うことを言った
# なら判定は曖昧であり、本ミッションで繰り返し確認している
# 「曖昧なら安全側 (判定不能) に倒す」原則をそのまま適用する。
# 「厳しい側を選んで書き戻す」案も検討したが却下した: 書き戻すと
# plan_review.md 内に異なる値の verdict 行が併存し、lib_verdict の
# 自己矛盾チェックで結局判定不能になる (二度手間かつ挙動が分かりにくい)。
# ここで判定不能に倒せば scripts/plan.sh が cycle を refund した上で
# Director に手動確認を促すため、review cycle も失われない。
FINAL_VERDICT=""
VERDICT_CONFLICT=0
if [[ -n "$STRUCTURED_VERDICT" && -n "$PROSE_VERDICT" && "$STRUCTURED_VERDICT" != "$PROSE_VERDICT" ]]; then
    VERDICT_CONFLICT=1
    echo "[review-plan.sh] WARNING: structured output verdict ('$STRUCTURED_VERDICT') disagrees with the verdict written in ${REVIEW_OUTPUT} ('$PROSE_VERDICT') — refusing both and falling back to manual inspection (fail-closed)" >&2
elif [[ -n "$STRUCTURED_VERDICT" ]]; then
    FINAL_VERDICT="$STRUCTURED_VERDICT"
fi

if [[ "$VERDICT_CONFLICT" -eq 1 ]]; then
    WAIT_STATUS="TIMEOUT_FRESH"
    WAIT_RC=1
elif [[ -n "$FINAL_VERDICT" ]]; then
    if [[ "$FINAL_VERDICT" != "$PROSE_VERDICT" ]]; then
        echo "[review-plan.sh] recovered verdict '$FINAL_VERDICT' from structured output (${PLAN_REVIEWER_LOG}); plan_review.md had no verdict readable in the canonical form (wait status was: $WAIT_STATUS)" >&2
        # plan_review.md の**1行目**に規定形式の行を機械的に書き込む。
        # scripts/lib_verdict.py は最初の非空行だけを見るため、この prepend が
        # そのまま plan.sh cmd_review の読む唯一の判定になる。
        # normalize_plan_review_verdict.py と同じ「原文は残す」prepend パターン。
        TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        # NOTE: 最後の文を `[[ -f ]] && cat` の短絡形にすると、ファイルが
        # 無い場合に `{ ... }` グループ自体が非0を返し、後続の `&& mv` が
        # 発火しない (mv されず .tmp のまま残る) 実害があったため、if 文で
        # グループの終了ステータスを常に0に固定する。
        {
            printf '**Verdict:** %s\n' "$FINAL_VERDICT"
            printf '<!-- authoritative verdict written by scripts/review-plan.sh from the claude --json-schema structured output at %s; original content (if any) preserved below (t004/t012) -->\n\n' "$TS"
            if [[ -f "$REVIEW_OUTPUT" ]]; then
                cat "$REVIEW_OUTPUT"
            fi
        } > "${REVIEW_OUTPUT}.tmp" && mv "${REVIEW_OUTPUT}.tmp" "$REVIEW_OUTPUT"
    fi
    WAIT_STATUS="OK"
    WAIT_RC=0
fi

if [[ "$WAIT_RC" -eq 0 && "$WAIT_STATUS" == "OK" ]]; then
    echo "[review-plan.sh] plan_review.md output complete"
    # kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
    exit 0
fi

# --- t002: タイムアウト時の挙動改善 ---
# plan_review.md 自体は書かれていた場合 (TIMEOUT_FRESH) は「判定が読めなかった
# だけ」であり、内容自体は活かせる可能性が高い。scripts/plan.sh 側 (cmd_review)
# がこのメッセージを拾って Director に「再レビューではなく手動確認」を促す。
if [[ "$WAIT_STATUS" == "TIMEOUT_FRESH" ]]; then
    echo "[review-plan.sh] Timeout: plan_review.md was written during this run but no verdict (standard or recognized alternate wording) could be found. Inspect ${REVIEW_OUTPUT} by hand — the judgement content may still be usable without consuming another review cycle." >&2
else
    echo "[review-plan.sh] Timeout: plan_review.md was not produced within 600s" >&2
fi

# kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
exit 1
