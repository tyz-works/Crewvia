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

# --- モデル解決 (t001, mission 20260909-dead-config-sweep) ---
# 以前は claude 起動時の --model に claude-opus-4-5 を直書きしており、
# config/crewvia.yaml の model_per_skill.plan_review 設定が完全に無視される
# dead config になっていた (しかも claude-opus-4-5 は存在しない世代の ID)。
# scripts/start.sh の _resolve_worker_model / MODEL_CLI_ARG と同じ規約に倣う:
#   1. scripts/lib_model.py resolve --skills plan_review で解決
#   2. 空なら config の worker_model にフォールバック (lib_model.py 自体が
#      config 不在・PyYAML 不在・YAML 破損時に空文字を返す設計のため)
#   3. それでも空なら --model を付けず claude CLI のデフォルトに委ねる
CONFIG_FILE="${CREWVIA_DIR}/config/crewvia.yaml"
WORKER_MODEL_FROM_CONFIG=""
if [[ -f "$CONFIG_FILE" ]]; then
    WORKER_MODEL_FROM_CONFIG=$(grep -E '^worker_model:[[:space:]]*\S' "$CONFIG_FILE" | awk '{print $2}' | tr -d '"' | head -1)
fi
SELECTED_MODEL="$(python3 "${SCRIPT_DIR}/lib_model.py" resolve --config "$CONFIG_FILE" --skills plan_review)"
if [[ -z "$SELECTED_MODEL" ]]; then
    SELECTED_MODEL="$WORKER_MODEL_FROM_CONFIG"
fi
if [[ -n "$SELECTED_MODEL" ]]; then
    echo "[review-plan.sh] Model: $SELECTED_MODEL"
fi
# mux 経路 (INLINE_CMD は文字列として eval されるため single-quote で囲む。
# start.sh の MODEL_CLI_ARG と同じ規約)。
MODEL_CLI_ARG=""
[[ -n "$SELECTED_MODEL" ]] && MODEL_CLI_ARG=" --model '$SELECTED_MODEL'"
# inline フォールバック経路 (実配列展開のため単純な --model フラグでよい)。
MODEL_FLAG=()
[[ -n "$SELECTED_MODEL" ]] && MODEL_FLAG=(--model "$SELECTED_MODEL")

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
INLINE_CMD="unset CLAUDE_CODE_CHILD_SESSION; export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; export AGENT_NAME='Plan-Reviewer'; export SKILLS=plan_review; cd '$CREWVIA_DIR' && CLAUDE_SKILL=plan_review claude${MODEL_CLI_ARG} \
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
    # start.sh の exec claude "${MODEL_FLAG[@]+...}" と同じ規約: 空配列を
    # set -u 下で展開してもエラーにならないよう ${arr[@]+"${arr[@]}"} で守る。
    CLAUDE_SKILL=plan_review claude "${MODEL_FLAG[@]+"${MODEL_FLAG[@]}"}" \
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
# verdict が既知の3値以外、のいずれでも何もしない (呼び出し元が判定不能として扱う)。
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

# --- F2 (t002, mission 20260912-verdict-ci-launcher) ---
# 構造化出力の到着を待つ。mux 経路では review-plan.sh は plan-reviewer
# プロセスの終了を直接待たない (plan_review.md の mtime 安定化だけで判定
# している) ため、プローズが読めた時点でもまだ最終ターン (構造化出力) が
# 書き終わっていない可能性がある。
#
# 旧実装は「プローズが判定不能だったときだけ」固定 8*3=24秒の retry を
# 挟んでいた。これは2つの意味で誤りだった:
#   1. プローズ=approve (=WAIT_STATUS==OK) の経路では retry 自体が
#      スキップされ、構造化出力による食い違い検出が一度も armed されない
#      まま「プローズの approve」だけで確定してしまっていた (F2 本体の
#      バグ — 本番では構造化出力は必ずプローズより後に届くため、
#      この経路は実質的に無検証だった)。
#   2. 24秒という上限は「救済経路 (プローズ失敗時) でしか発火しない」
#      という前提で決めた値であり、approve 経路の正常系の所要時間を
#      測って決めた値ではない。
#
# 対応: 待つかどうかを WAIT_STATUS ではなく実際のプローズ判定 (下の
# PROSE_VERDICT) で決める。プローズが revise/reject の場合は安全な結論
# なので従来どおり retry せず1回だけ読む。プローズが approve、または
# 判定不能の場合は必ず待つ。
#
# 待ち時間: Director 指摘 — 固定秒数は本番の実遅延を実測して決めるべきだが、
# 過去の plan-reviewer ログから実測する手段が無かった。「reviewer プロセス/
# pane の終了を待つ」の代替として、当初はログサイズの変化が止まったこと
# (quiescence) を「セッション終了」の代理シグナルにする案を検討したが、
# 却下した: claude セッションは長い思考や道具の実行で出力が数十秒単位で
# 止まることがあり、その「一時的な静止」と「本当にセッションが終わった」を
# 外から区別する手段が無い。static な安定判定 (例: 2 回連続無変化) を使うと、
# 正常系でもまだ書いている途中のセッションを「終わった」と誤認して諦める
# 経路が生まれ、これは F2 本体と同じ形の失敗 (見積もりが正常系の実際の
# 遅延を下回って安全側に誤爆する) になる。
#
# 採用した方針: 「正常系の所要時間を言い当てる」ことを諦め、代わりに
# **失敗方向にしか効かない**単純な固定間隔ポーリングにする。max_wait は
# 正常系の所要時間の見積もりではなく、壊れた/応答不能なセッションを
# 無限に待たないための安全弁 (暴走防止) として十分長い値
# (デフォルト180秒 = wait_for_plan_review.sh の 600秒 timeoutより十分短い)
# を置くだけで、量を上げても安全側が壊れることはない (見つかれば即座に
# 抜けるため正常系の待ち時間には影響しない。見つからない場合だけ
# max_wait 分待ってから諦める — 待たされる分には approve が誤って
# 通ることはなく、F2 が守りたい性質を壊さない)。
_STRUCTURED_WAIT_POLL_INTERVAL="${REVIEW_PLAN_STRUCTURED_POLL_INTERVAL:-3}"
_STRUCTURED_WAIT_MAX_SECONDS="${REVIEW_PLAN_STRUCTURED_MAX_WAIT:-180}"

_wait_for_structured_verdict() {
    local log="$1"
    local poll_interval="$_STRUCTURED_WAIT_POLL_INTERVAL"
    local max_wait="$_STRUCTURED_WAIT_MAX_SECONDS"
    local verdict
    local elapsed=0

    while :; do
        if verdict="$(_rescue_verdict_from_structured_output "$log")"; then
            printf '%s\n' "$verdict"
            return 0
        fi
        [[ "$elapsed" -ge "$max_wait" ]] && break
        sleep "$poll_interval"
        elapsed=$((elapsed + poll_interval))
    done
    echo "[review-plan.sh] $log: no confirming structured output verdict found within ${max_wait}s" >&2
    return 1
}

# plan_review.md 側 (規定形式の1行目) から読める verdict。
# 読めなければ空文字 (判定不能)。
PROSE_VERDICT="$(python3 "${SCRIPT_DIR}/lib_verdict.py" "$REVIEW_OUTPUT" 2>/dev/null || true)"

STRUCTURED_VERDICT=""
if [[ -n "$VERDICT_SCHEMA" ]]; then
    if [[ -z "$PROSE_VERDICT" || "$PROSE_VERDICT" == "approve" ]]; then
        # プローズが判定不能、または approve (=危険な結論。構造化出力による
        # 確認を必須にする、F2) の場合は待つ。
        STRUCTURED_VERDICT="$(_wait_for_structured_verdict "$PLAN_REVIEWER_LOG")" || STRUCTURED_VERDICT=""
    else
        # revise/reject は安全な結論なのでプローズを直接信頼してよい。待ちは
        # せず、機会があれば1回だけ読んで矛盾検出にだけ使う (正常系の所要
        # 時間を伸ばさないため)。読めなければプローズの判定をそのまま使う。
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
#
# F2 判定マトリクス (Director 追記):
#   prose=approve   & structured=approve        → approve
#   prose と structured が食い違う               → 判定不能 (conflict)
#   prose=approve   & 待ち切っても structured 無し → 判定不能 (安全側)
#   prose 判定不能   & structured=approve        → approve (救済。本旨なので残す)
#   prose 判定不能   & structured 無し           → 判定不能 (上と同じ経路)
#   prose=revise/reject                          → 従来通りそのまま採用
#                                                   (構造化出力との食い違いだけは検出する)
FINAL_VERDICT=""
VERDICT_CONFLICT=0
if [[ -n "$STRUCTURED_VERDICT" && -n "$PROSE_VERDICT" && "$STRUCTURED_VERDICT" != "$PROSE_VERDICT" ]]; then
    VERDICT_CONFLICT=1
    echo "[review-plan.sh] WARNING: structured output verdict ('$STRUCTURED_VERDICT') disagrees with the verdict written in ${REVIEW_OUTPUT} ('$PROSE_VERDICT') — refusing both and falling back to manual inspection (fail-closed)" >&2
elif [[ -n "$STRUCTURED_VERDICT" ]]; then
    FINAL_VERDICT="$STRUCTURED_VERDICT"
elif [[ -n "$PROSE_VERDICT" && "$PROSE_VERDICT" != "approve" ]]; then
    # revise/reject は安全な結論なので、構造化出力による確認が無くても
    # プローズをそのまま採用してよい (F2 マトリクス最終行)。
    FINAL_VERDICT="$PROSE_VERDICT"
fi
# NOTE: PROSE_VERDICT=="approve" だが STRUCTURED_VERDICT が最後まで空のまま
# だった場合、FINAL_VERDICT もここまで空のまま残る — 意図的 (F2 マトリクス
# 3行目「prose=approve & structured 無し → 判定不能」)。下の分岐で拾う。

if [[ "$VERDICT_CONFLICT" -eq 1 ]]; then
    WAIT_STATUS="TIMEOUT_FRESH"
    WAIT_RC=1
elif [[ -n "$FINAL_VERDICT" ]]; then
    if [[ "$FINAL_VERDICT" != "$PROSE_VERDICT" ]]; then
        echo "[review-plan.sh] recovered verdict '$FINAL_VERDICT' from structured output (${PLAN_REVIEWER_LOG}); plan_review.md had no verdict readable in the canonical form (wait status was: $WAIT_STATUS)" >&2
        # plan_review.md の**1行目**に規定形式の行を機械的に書き込む。
        # scripts/lib_verdict.py は最初の非空行だけを見るため、この prepend が
        # そのまま plan.sh cmd_review の読む唯一の判定になる。
        # normalize_plan_review_verdict.py と同じ「原文は残す」prepend パターン
        # (F1 で normalize_plan_review_verdict.py 自体は削除したが、prepend
        # パターンはここに移植済みなので影響しない)。
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
elif [[ "$PROSE_VERDICT" == "approve" ]]; then
    # F2 (本 PR の直接の動機): プローズは approve だったが、待ち切っても
    # 構造化出力による確認が得られなかった。WAIT_STATUS がここまで "OK"
    # (プローズ単体は読めていた) であっても、確認なしに approve を通さない
    # — 危険な結論 (approve) は構造化出力による確認を必須にする、という
    # F2 マトリクスの安全側の行をここで実際に enforcement する。
    echo "[review-plan.sh] WARNING: prose verdict was 'approve' but no confirming structured output verdict could be obtained after waiting — refusing to approve without confirmation (fail-closed, F2)" >&2
    WAIT_STATUS="TIMEOUT_FRESH"
    WAIT_RC=1
fi
# else: FINAL_VERDICT も PROSE_VERDICT も空 (かつ conflict でもない) —
# wait_for_plan_review.sh が返した元の WAIT_STATUS/WAIT_RC (通常は
# TIMEOUT_FRESH/TIMEOUT_NONE) をそのまま使う。

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
