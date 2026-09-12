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
# --- t015 (mission 20260912-verdict-ci-launcher, PR #199 QA(Finn) t003 FAIL-1 /
# Kai-codex P1, Director 設計判断1) ---
# plan_review.md とは別の、review-plan.sh だけが書く専用チャネル。
# scripts/plan.sh は「review-plan.sh が検証した verdict そのもの」だけを
# 消費するために、plan_review.md を独立に読み直すのをやめてこのファイルを
# 読む (下記「Decision 1」参照)。plan-reviewer セッションは
# SKILLS=plan_review の Write 権限が plan_review.md 限定であるため、この
# ファイルには技術的にも書き込めない — mux_kill (プロセス終了) もファイルには
# 触れないため、review-plan.sh がここに書いた後は何者にも書き換えられない
# (=「検証した値」と「消費する値」が同一であることが構造的に保証される)。
VERDICT_FILE="$MISSION_DIR/plan_review.verdict"

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
rm -f "$REVIEW_OUTPUT" "$VERDICT_FILE"
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
    # P2 (t015, mission 20260912-verdict-ci-launcher, Kai-codex t004 review):
    # 以前は .structured_output.verdict の型だけを見ており、result envelope
    # 自体が成功したかどうか (is_error / subtype) を確認していなかった。
    # {"type":"result","subtype":"error_during_execution","is_error":true,
    #  "structured_output":{"verdict":"approve"}} のような「実行が失敗した
    # ターンにたまたま structured_output だけ残っている」envelope でも
    # verdict を信頼してしまっていた。is_error が明示的に false、かつ
    # subtype が明示的に "success" の場合だけを信頼する allowlist にする
    # (どちらかが欠落・想定外の値なら jq の select が失敗し return 1 = 判定不能。
    # 「欠落は安全側」— 成功だと確証できないものは信頼しない)。
    # mux 経路では claude プロセス自体の終了ステータスを review-plan.sh から
    # 直接取得する手段が無い (バックグラウンドで spawn され、パイプの終了
    # ステータスも herdr/tmux 側に閉じている) ため、envelope 自身が自己申告
    # する is_error/subtype を唯一の成功シグナルとして使う。
    verdict="$(printf '%s' "$line" | jq -er '
        select(.is_error == false)
        | select(.subtype == "success")
        | select(.structured_output.verdict | type == "string")
        | .structured_output.verdict
    ' 2>/dev/null)" || return 1

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

# --- Decision 3 (t015, Director 追記 — QA t003 Finn FAIL-1 実測への対応) ---
# 構造化出力が届かなかった場合に reviewer を止め、止まったことを確認する。
# 「止まったことを確認できたか」だけを返す (0=確認できた, 1=できなかった)。
# 確認できない限り plan_review.md を読まない (呼び出し側で PROSE_VERDICT を
# 空のままにする) — 停止を確認できないまま読むと、読んだ直後に reviewer が
# まだ書き込み中だった、という T1/T1r と同型の窓が残ってしまうため。
#
# inline フォールバック経路 (MUX_LAUNCHED=0) では claude が
# `... | tee ...` で既に同期的に完走済み (ここに到達した時点で reviewer
# プロセスはこの review-plan.sh 自身の子プロセスとして既に終了している) の
# で、確認は自明に真。mux 経路 (MUX_LAUNCHED=1) では明示的に mux_kill を呼び、
# mux_pid が「見つからない (プロセス/ペインが消えた)」を返すまでポーリングする
# (herdr の tab close は SIGHUP 後最大 2 秒かかるため、少し余裕を持って待つ)。
# ここで実際に kill するため、EXIT trap による二重 kill を避けるべく
# MUX_LAUNCHED を 0 に落とす (mux_kill 自体は再度呼ばれても no-op に近いが、
# 呼び出し元によっては未定義 pane への kill で警告ログが出るため)。
_STOP_CONFIRM_POLL_INTERVAL="${REVIEW_PLAN_STOP_CONFIRM_POLL_INTERVAL:-1}"
_STOP_CONFIRM_MAX_SECONDS="${REVIEW_PLAN_STOP_CONFIRM_MAX_SECONDS:-5}"

_stop_reviewer_and_confirm() {
    if [[ "$MUX_LAUNCHED" -ne 1 ]]; then
        # inline 経路: ここに到達した時点で claude は既に完走している。
        return 0
    fi

    mux_kill "$WINDOW_NAME" 2>/dev/null || true
    MUX_LAUNCHED=0

    local elapsed=0
    while [[ "$elapsed" -lt "$_STOP_CONFIRM_MAX_SECONDS" ]]; do
        if ! mux_pid "$WINDOW_NAME" >/dev/null 2>&1; then
            # pane/プロセスが見つからない = 停止確認。
            return 0
        fi
        sleep "$_STOP_CONFIRM_POLL_INTERVAL"
        elapsed=$((elapsed + _STOP_CONFIRM_POLL_INTERVAL))
    done
    echo "[review-plan.sh] WARNING: could not confirm plan-reviewer pane '$WINDOW_NAME' stopped within ${_STOP_CONFIRM_MAX_SECONDS}s after kill" >&2
    return 1
}

# --- Decision 2/3 (t015, Director 追記) ---
# QA t003 (Finn) が実測した FAIL-1 (TOCTOU) は、review-plan.sh がプローズを
# 「reviewer がまだ書いている可能性がある時点」で読み、prose=revise/reject の
# ときは構造化出力を待たずに確定していたことに起因する一部だった
# (もう一部は plan.sh 側の独立読み直し — 下記 plan.sh 側の修正で対応)。
#
# ここでの修正:
#   1. プローズの値 (approve/revise/reject/判定不能) に関わらず、**必ず**同じ
#      MAX_WAIT だけ構造化出力の到着を待つ (以前は revise/reject だと待たな
#      かった — F2g: prose=revise / structured=approve 遅延、を検出できない
#      原因だった)。
#   2. プローズは「書き手のセッションが終わった後」に**1回だけ**読む。
#      構造化出力 ("type":"result") の到着そのものをセッション終了の合図と
#      する。到着しない場合は reviewer を明示的に止め、停止を確認できてから
#      読む。停止を確認できなければ読まない (=判定不能に倒れる)。
STRUCTURED_VERDICT=""
# --- t018 (mission 20260912-verdict-ci-launcher, PR #199 QA t016 Finn FAIL-A /
# Kai-codex t004 2 回目 P1, Director 設計判断1-4) ---
# plan_review.md の読み取り結果を「判定不能」1 種類にまとめず、状態として持つ。
# t015 までは lib_verdict.py の出力が空なら一律「判定不能 → 構造化出力で救済」
# としていたため、1 行目 revise + 本文に approve の自己矛盾ファイルや
# `**Verdict:** REVISE` が structured=approve で ready になっていた。
#   valid     — 1 行目だけに正規の verdict がある (PROSE_VERDICT に値が入る)
#   no_sign   — verdict 行の兆候がファイルのどこにも無い (救済してよい唯一の状態)
#   violation — 兆候はあるが規定形式でない / 自己矛盾 / lib_verdict.py が
#               想定外の終了コードや出力を返した (落ちた場合を含む)
#   unread    — reviewer の停止を確認できず、読んでいない (decision 3)
PROSE_STATE="unread"
PROSE_VERDICT=""

# lib_verdict.py の終了コードは allowlist で解釈する: 0 + 正規の 1 語 = valid、
# 10 + 出力なし = no_sign、それ以外はすべて violation。Python の未捕捉例外
# (終了コード 1) やスクリプト不在 (2) を no_sign に丸めると、lib_verdict.py が
# 落ちるだけで救済経路に入れてしまう。stderr は理由のログとしてそのまま流す。
_read_prose_verdict() {
    local out="" rc=0
    out="$(python3 "${SCRIPT_DIR}/lib_verdict.py" "$REVIEW_OUTPUT")" || rc=$?
    PROSE_VERDICT=""
    if [[ "$rc" -eq 0 ]]; then
        case "$out" in
            approve|revise|reject)
                PROSE_STATE="valid"
                PROSE_VERDICT="$out"
                ;;
            *)
                PROSE_STATE="violation"
                ;;
        esac
    elif [[ "$rc" -eq 10 && -z "$out" ]]; then
        PROSE_STATE="no_sign"
    else
        PROSE_STATE="violation"
    fi
}

if [[ -n "$VERDICT_SCHEMA" ]]; then
    STOPPED_CONFIRMED=0
    if STRUCTURED_VERDICT="$(_wait_for_structured_verdict "$PLAN_REVIEWER_LOG")"; then
        # 構造化出力の到着 = セッション終了の合図。
        STOPPED_CONFIRMED=1
    else
        STRUCTURED_VERDICT=""
        if _stop_reviewer_and_confirm; then
            STOPPED_CONFIRMED=1
        fi
    fi

    if [[ "$STOPPED_CONFIRMED" -eq 1 ]]; then
        _read_prose_verdict
    else
        echo "[review-plan.sh] WARNING: plan-reviewer session end could not be confirmed — refusing to read ${REVIEW_OUTPUT} (fail-closed, decision 3)" >&2
    fi
else
    # スキーマ不在時のフォールバック (t004 以前と同じ挙動): 構造化出力による
    # 確認は原理的に不可能なので、この経路では待ちも kill-confirm もしない。
    # プローズをそのまま読む (t004 以前の挙動と同じ。approve を含め、schema
    # 不在時の安全性は wait_for_plan_review.sh 側の判定・下の WAIT_STATUS
    # フォールバック分岐に委ねられている)。
    PROSE_VERDICT="$(python3 "${SCRIPT_DIR}/lib_verdict.py" "$REVIEW_OUTPUT" 2>/dev/null || true)"
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
#
# t018 判定マトリクス (Director 設計判断1-4。上の F2 マトリクスを置き換える):
#   prose=violation (兆候はあるが規定形式でない / 自己矛盾 / lib_verdict 異常)
#                                         → 判定不能 (structured の値に関係なく。救済しない)
#   prose=unread (停止を確認できず読んでいない) → 判定不能 (救済しない)
#   prose=valid & structured が食い違う      → 判定不能 (conflict)
#   prose=valid & structured が同じ値        → その値
#   prose=valid(revise/reject) & structured 無し → prose をそのまま採用
#   prose=valid(approve) & structured 無し   → 判定不能 (F2)
#   prose=no_sign & structured あり          → structured (救済。本旨なので残す)
#   prose=no_sign & structured 無し          → 判定不能
# t015 までは「prose が空 = 判定不能」の 1 状態しか無く、自己矛盾・書式違反も
# 救済の行に流れていた (QA t016 FAIL-A: 1 行目 revise + 本文 approve +
# structured=approve → ready)。救済してよいのは no_sign だけに絞る。
FINAL_VERDICT=""
REFUSAL_REASON=""
if [[ "$PROSE_STATE" == "violation" ]]; then
    REFUSAL_REASON="${REVIEW_OUTPUT} contains a verdict-line sign but is not exactly one canonical '**Verdict:** approve|revise|reject' on its first line (format violation or self-contradiction; structured output verdict: '${STRUCTURED_VERDICT:-none}')"
    echo "[review-plan.sh] WARNING: ${REFUSAL_REASON} — the structured output is NOT used to rescue it (fail-closed, t018)" >&2
elif [[ "$PROSE_STATE" == "valid" && -n "$STRUCTURED_VERDICT" && "$STRUCTURED_VERDICT" != "$PROSE_VERDICT" ]]; then
    REFUSAL_REASON="structured output verdict ('$STRUCTURED_VERDICT') disagrees with the verdict written in ${REVIEW_OUTPUT} ('$PROSE_VERDICT')"
    echo "[review-plan.sh] WARNING: ${REFUSAL_REASON} — refusing both and falling back to manual inspection (fail-closed)" >&2
elif [[ "$PROSE_STATE" == "valid" && -n "$STRUCTURED_VERDICT" ]]; then
    FINAL_VERDICT="$STRUCTURED_VERDICT"
elif [[ "$PROSE_STATE" == "valid" && "$PROSE_VERDICT" != "approve" ]]; then
    # revise/reject は安全な結論なので、構造化出力による確認が無くても
    # プローズをそのまま採用してよい。
    FINAL_VERDICT="$PROSE_VERDICT"
elif [[ "$PROSE_STATE" == "valid" ]]; then
    # F2 (本 PR の直接の動機): プローズは approve だったが、待ち切っても
    # 構造化出力による確認が得られなかった。危険な結論 (approve) は
    # 構造化出力による確認を必須にする。
    REFUSAL_REASON="prose verdict was 'approve' but no confirming structured output verdict could be obtained after waiting"
    echo "[review-plan.sh] WARNING: ${REFUSAL_REASON} — refusing to approve without confirmation (fail-closed, F2)" >&2
elif [[ "$PROSE_STATE" == "no_sign" && -n "$STRUCTURED_VERDICT" ]]; then
    FINAL_VERDICT="$STRUCTURED_VERDICT"
fi

VERDICT_BOUND=0
if [[ -n "$REFUSAL_REASON" ]]; then
    WAIT_STATUS="TIMEOUT_FRESH"
    WAIT_RC=1
elif [[ -n "$FINAL_VERDICT" ]]; then
    if [[ "$PROSE_STATE" == "no_sign" ]]; then
        echo "[review-plan.sh] recovered verdict '$FINAL_VERDICT' from structured output (${PLAN_REVIEWER_LOG}); plan_review.md had no verdict-line sign anywhere (wait status was: $WAIT_STATUS)" >&2
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
    # --- Decision 1 (t015, Director 設計判断1) ---
    # plan.sh はもう plan_review.md を独立に読み直さない。ここで確定した
    # FINAL_VERDICT だけを、review-plan.sh 以外の誰にも書き換えられない
    # 専用ファイルにアトミックに書き (tmp → mv)、plan.sh はこれだけを消費する。
    # plan_review.md 自体は人間向けの記録として残るが、判定には使われない。
    #
    # t018 (鮮度, QA t016 Finn E_A3): 2 行目に plan.sh から渡された実行ごとの
    # 識別子 (CREWVIA_PLAN_REVIEW_RUN_ID) を書く。plan.sh は識別子が一致しない
    # ファイルを消費しない。書き込みに失敗した場合は bound 扱いにしない。
    if printf '%s\nrun_id=%s\n' "$FINAL_VERDICT" "${CREWVIA_PLAN_REVIEW_RUN_ID:-}" > "${VERDICT_FILE}.tmp" \
        && mv "${VERDICT_FILE}.tmp" "$VERDICT_FILE"; then
        VERDICT_BOUND=1
        WAIT_STATUS="OK"
        WAIT_RC=0
    else
        REFUSAL_REASON="could not write ${VERDICT_FILE}"
        echo "[review-plan.sh] ERROR: ${REFUSAL_REASON}" >&2
        WAIT_STATUS="TIMEOUT_FRESH"
        WAIT_RC=1
    fi
fi
# else: 確定も拒否もしていない (prose=unread、または prose=no_sign で
# 構造化出力も無い) — wait_for_plan_review.sh が返した元の WAIT_STATUS/WAIT_RC
# をそのまま使う。

if [[ "$WAIT_RC" -eq 0 && "$WAIT_STATUS" == "OK" ]]; then
    if [[ "$VERDICT_BOUND" -eq 1 ]]; then
        echo "[review-plan.sh] plan_review.md output complete — verdict '${FINAL_VERDICT}' bound to ${VERDICT_FILE}"
    else
        # t018 (QA t016 Finn E_A1/E_A2): wait_for_plan_review.sh が途中で一度
        # 規定形式の verdict を見た (OK) が、停止確認後の読み取りでは確定
        # できなかった経路。終了コードは従来どおり 0 のまま (plan.sh は
        # plan_review.verdict が無いことで rollback + cycle refund する) だが、
        # 「output complete」とは言わない。
        echo "[review-plan.sh] WARNING: no verdict was confirmed in this run — ${VERDICT_FILE} was NOT written (plan_review.md state: ${PROSE_STATE}, structured output verdict: ${STRUCTURED_VERDICT:-none}). scripts/plan.sh will roll the mission back to drafting without consuming a review cycle; inspect ${REVIEW_OUTPUT} by hand." >&2
    fi
    # kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
    exit 0
fi

# --- t002: タイムアウト時の挙動改善 ---
# plan_review.md 自体は書かれていた場合 (TIMEOUT_FRESH) は「判定が読めなかった
# だけ」であり、内容自体は活かせる可能性が高い。scripts/plan.sh 側 (cmd_review)
# がこのメッセージを拾って Director に「再レビューではなく手動確認」を促す。
if [[ -n "$REFUSAL_REASON" ]]; then
    # t018: 書式違反・食い違い・F2 で拒否した場合はタイムアウトではないので、
    # 拒否の理由をそのまま出す。
    echo "[review-plan.sh] No verdict bound: ${REFUSAL_REASON}. Inspect ${REVIEW_OUTPUT} by hand — scripts/plan.sh rolls the mission back to drafting without consuming a review cycle." >&2
elif [[ "$WAIT_STATUS" == "TIMEOUT_FRESH" ]]; then
    echo "[review-plan.sh] Timeout: plan_review.md was written during this run but no verdict (standard or recognized alternate wording) could be found. Inspect ${REVIEW_OUTPUT} by hand — the judgement content may still be usable without consuming another review cycle." >&2
else
    echo "[review-plan.sh] Timeout: plan_review.md was not produced within 600s" >&2
fi

# kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
exit 1
