#!/usr/bin/env bash
# hooks/post-tool-use.sh
# Claude Code PostToolUse hook — Taskvia 作業ログ投稿
#
# ~/.claude/settings.json に登録:
#   "hooks": {
#     "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "/path/to/hooks/post-tool-use.sh" }] }]
#   }
#
# 環境変数:
#   TASKVIA_URL    — Taskvia のベースURL (default: https://taskvia.vercel.app)
#   TASKVIA_TOKEN  — Bearer トークン（未設定時はスキップ）
#   AGENT_NAME     — エージェント識別子 (default: hostname)
#   TASK_TITLE     — 現在のタスク名 (任意)
#   TASK_ID        — 現在のタスクID (任意)

set -euo pipefail

# クラッシュガード: set -euo pipefail で予期せず exit した場合に exit 0 で収束させる。
# PostToolUse はログ投稿のみで、失敗してもエージェント動作に影響しないため exit 0 が正しい。
# trap の登録を set -euo pipefail の直後に置くことで、以降のどの行でクラッシュしても捕捉できる。
#
# _INTENTIONAL_EXIT_CODE: 既定は 0 (クラッシュガードの本来の収束先)。
# t008 の同時死 backstop だけが意図的に 2 にセットする — PostToolUse hook が
# exit 2 で終わると stderr が Claude (Director) に渡る、という Claude Code の
# hook 仕様を使い、ポーリングさせずに文脈へ流し込む。exit code をそのまま
# 使わずこの変数を経由するのは、「予期しないクラッシュ」と「意図的な signal」を
# 区別するため — どちらも trap には非ゼロ exit として届き、素の $? だけでは
# 見分けられない。
_INTENTIONAL_EXIT_CODE=0
_CURRENT_STEP="init"
_crash_guard() {
  local _EXIT_CODE=$?
  if [ "$_EXIT_CODE" -ne 0 ] && [ "$_EXIT_CODE" != "$_INTENTIONAL_EXIT_CODE" ]; then
    echo "[post-tool-use] ⚠️ crash guard: hook exited unexpectedly (exit=${_EXIT_CODE}, step=${_CURRENT_STEP})" >&2
  fi
  exit "$_INTENTIONAL_EXIT_CODE"
}
trap '_crash_guard' EXIT

_CURRENT_STEP="env-setup"
TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"
AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"
TASK_TITLE="${TASK_TITLE:-}"
TASK_ID="${TASK_ID:-}"

# env に TASK_ID がなければ assignments ファイルから補完する
_CURRENT_STEP="task-id-lookup"
if [ -z "$TASK_ID" ] && [ -n "$AGENT_NAME" ]; then
  _CREWVIA_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  _ASSIGNMENT_FILE="${_CREWVIA_REPO}/queue/assignments/${AGENT_NAME}"
  if [ -f "$_ASSIGNMENT_FILE" ]; then
    _ASSIGNMENT="$(cat "$_ASSIGNMENT_FILE" | tr -d '\n')"
    _MISSION_SLUG="${_ASSIGNMENT%%:*}"
    TASK_ID="${_ASSIGNMENT##*:}"
    _TASK_FILE="${_CREWVIA_REPO}/queue/missions/${_MISSION_SLUG}/tasks/${TASK_ID}.md"
    if [ -f "$_TASK_FILE" ]; then
      TASK_TITLE="$(grep '^title:' "$_TASK_FILE" | head -1 | sed 's/^title:[[:space:]]*//' | sed 's/^"\(.*\)"$/\1/')"
    fi
  fi
fi

# --- Activity logging for Watchdog v2 ---
# Appends a timestamped entry to registry/activity/<AGENT_NAME>/<TASK_ID>.activity
# so that watchdog.py can detect live tool execution activity.
# Runs unconditionally (before Taskvia guard) so it works in standalone mode too.
_CURRENT_STEP="activity-log"
if [ -n "${AGENT_NAME:-}" ] && [ -n "${TASK_ID:-}" ]; then
  _ACTIVITY_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  ACTIVITY_DIR="${_ACTIVITY_REPO}/registry/activity/${AGENT_NAME}"
  mkdir -p "$ACTIVITY_DIR"
  echo "$(date +%s) tool=${CLAUDE_TOOL_NAME:-unknown}" >> "${ACTIVITY_DIR}/${TASK_ID}.activity"
fi

# --- Heartbeat ファイル更新 (task_162 P2是正) ---
# heartbeat は crewvia 内部の信号(watchdog.py が読む)であり Taskvia とは無関係。
# 以前は下の Taskvia ガードの下流にあり standalone モードでは一切書かれなかった
# (順序の事故であり設計判断ではない — task_162 Picard裁定)。activity と同じ扱いにし、
# ガードより前(無条件実行)へ移す。Taskvia への役割・スキル送信(/api/agents)自体は
# 引き続きガードの下流のままで変更していない(下記参照)。
_CURRENT_STEP="heartbeat"
if [[ -n "${AGENT_NAME:-}" ]]; then
  _HB_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  HEARTBEAT_DIR="${_HB_REPO}/registry/heartbeats"
  mkdir -p "$HEARTBEAT_DIR"
  date +%s > "${HEARTBEAT_DIR}/${AGENT_NAME}" 2>/dev/null || true
fi

# --- 同時死 backstop (t008) -------------------------------------------------
# 背景: dispatcher と watchdog の相互監視 (scripts/lib_daemon_watch.py) は
# 「相手を見る」仕組みなので、両方が同時に死ぬケース (herdr 再起動、OOM 等) は
# どちらも互いを起こせない。このケースだけ Director 側のこの hook が拾う。
#
# 対象: role が director のセッションのみ (Worker では毎ツール呼び出しに
# 発火してノイズになるうえ、Worker が自分で respawn/report できるわけでも
# ないので意味が無い)。
#
# 判定は registry/daemons/{dispatcher,watchdog}.heartbeat の mtime だけを見る
# 安価な処理 — ネットワーク I/O・mux 呼び出し・python サブプロセスは呼ばない
# (lib_daemon_watch.py の watch_peer() のような "process が本当に生きているか"
# の踏み込んだ検証はしない。あくまで最後の砦であり、誤検知しても Director が
# scripts/lib_daemon_watch.py status で確認するだけなので副作用は無い)。
# しきい値は config/crewvia.yaml の daemons.dispatcher_stale_seconds /
# watchdog_stale_seconds の既定値 (60 / 240) をハードコードする — YAML 解析は
# この hook の目的には重すぎるため、lib_daemon_watch.py と同じ env var
# (CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS 等) でだけ上書きを許す。
#
# throttle: 全ツール呼び出しのたびに判定すると使い物にならないため、
# マーカーファイルの mtime で 60 秒に 1 回に抑える。判定結果に関わらず
# マーカーを先に更新することで、同時に複数の PostToolUse が走っても
# 直後の呼び出しは早期リターンする (二重通知の窓を狭める。完全な排他では
# ないが、この hook にロックを持ち込むほどの重さではない)。
#
# 検出したら「1 行だけ出力して exit 2」で終える。PostToolUse hook が exit 2
# で終わると Claude Code は stderr を Claude (ここでは Director) にそのまま
# 見せる (ツールは既に実行済みなのでブロックはしない) — これが Director に
# ポーリングさせず「何か操作した拍子に勝手に届く」形の実現方法。
_CURRENT_STEP="daemon-backstop"
if [[ -n "${AGENT_NAME:-}" ]]; then
  _BS_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  _BS_WORKERS_YAML="${_BS_REPO}/registry/workers.yaml"
  _BS_IS_DIRECTOR=0
  if [[ -f "$_BS_WORKERS_YAML" ]]; then
    # O-1是正: 以前は grep -A3 の位置依存判定だったため、director エントリの
    # 直前の Worker がフィールド 1 つだけ (role/skills 欠落等) だと次の
    # `- name:` に届く前に role: 行を拾ってしまい、director と誤判定する
    # 恐れがあった (workers.yaml では director の直前が `- name: Ren` の
    # 1 行だけで、余裕が 1 行しかない)。下流の agents-heartbeat 送信で既に
    # 使っている「`- name:` で次エントリを検知して break する」Python
    # パーサに寄せる。
    _BS_ROLE="$(python3 - "$AGENT_NAME" "$_BS_WORKERS_YAML" <<'PYEOF' 2>/dev/null || echo "worker"
import re, sys
from pathlib import Path
agent_name, yaml_path = sys.argv[1], sys.argv[2]
content = Path(yaml_path).read_text()
in_target = False
role = "worker"
for line in content.splitlines():
    if re.match(r'\s*- name: ' + re.escape(agent_name) + r'\s*$', line):
        in_target = True
        continue
    if in_target:
        if re.match(r'\s*- name:', line):
            break
        m = re.match(r'\s*role:\s*(.+)', line)
        if m:
            role = m.group(1).strip()
            break
print(role)
PYEOF
)"
    if [[ "$_BS_ROLE" == "director" ]]; then
      _BS_IS_DIRECTOR=1
    fi
  fi

  if [[ "$_BS_IS_DIRECTOR" == "1" ]]; then
    _BS_DAEMONS_DIR="${_BS_REPO}/registry/daemons"
    # daemons/ が無い = 両デーモンとも一度も mutual watch の heartbeat を
    # 書いたことが無い (lib_daemon_watch.DaemonWatch が最初の beat() 時に
    # mkdir する)。standalone/inline 運用ではこの状態が正常なので、そのまま
    # 判定に入らずスキップする (常時 stale 誤検知を防ぐ)。
    if [[ -d "$_BS_DAEMONS_DIR" ]]; then
      _BS_THROTTLE="${_BS_DAEMONS_DIR}/backstop-notify.throttle"
      _BS_NOW="$(date +%s)"
      _BS_LAST=0
      if [[ -f "$_BS_THROTTLE" ]]; then
        # F-1是正: GNU 専用の `stat -c` は BSD/macOS に無く失敗する。
        # BSD の `stat -f %m` へフォールバックする (scripts/wait_for_plan_review.sh
        # の _mtime_of と同じイディオム)。両方失敗した場合の既定値は「判定不能を
        # 騒がしい側に倒さない」ため $_BS_NOW (= 今読んだばかり扱い) にする —
        # heartbeat 側の -1 (無限に stale) とは逆方向: throttle は読めないだけで
        # 誤発火させると要件 2 (毎ツール呼び出しの通知) と衝突する。
        _BS_LAST="$(stat -c %Y "$_BS_THROTTLE" 2>/dev/null || stat -f %m "$_BS_THROTTLE" 2>/dev/null || echo "$_BS_NOW")"
      fi
      _BS_ELAPSED=$(( _BS_NOW - _BS_LAST ))

      if [[ "$_BS_ELAPSED" -ge 60 ]]; then
        # 判定前にスロットル窓を更新する (判定結果に関わらず)。
        : > "$_BS_THROTTLE" 2>/dev/null || true

        _BS_DISPATCHER_STALE_S="${CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS:-60}"
        _BS_WATCHDOG_STALE_S="${CREWVIA_DAEMON_WATCHDOG_STALE_SECONDS:-240}"
        # O-2是正: 不正な (非数値の) env値は lib_daemon_watch.py の load_config()
        # と同じく「既定値を保って無視する」に揃える。以前はここで検証しておらず、
        # `[[ ... -ge "$_BS_DISPATCHER_STALE_S" ]]` に非数値が渡ると bash が
        # それを未束縛の変数参照として評価し `set -u` で hook 全体が異常終了
        # していた (crash guard が exit 0 に握り潰すため、意図した通知も出ない
        # まま黙って落ちる)。
        [[ "$_BS_DISPATCHER_STALE_S" =~ ^[0-9]+$ ]] || _BS_DISPATCHER_STALE_S=60
        [[ "$_BS_WATCHDOG_STALE_S" =~ ^[0-9]+$ ]] || _BS_WATCHDOG_STALE_S=240

        _BS_D_AGE=-1
        if [[ -f "${_BS_DAEMONS_DIR}/dispatcher.heartbeat" ]]; then
          _BS_D_MTIME="$(stat -c %Y "${_BS_DAEMONS_DIR}/dispatcher.heartbeat" 2>/dev/null || stat -f %m "${_BS_DAEMONS_DIR}/dispatcher.heartbeat" 2>/dev/null || echo "")"
          [[ -n "$_BS_D_MTIME" ]] && _BS_D_AGE=$(( _BS_NOW - _BS_D_MTIME ))
        fi
        _BS_W_AGE=-1
        if [[ -f "${_BS_DAEMONS_DIR}/watchdog.heartbeat" ]]; then
          _BS_W_MTIME="$(stat -c %Y "${_BS_DAEMONS_DIR}/watchdog.heartbeat" 2>/dev/null || stat -f %m "${_BS_DAEMONS_DIR}/watchdog.heartbeat" 2>/dev/null || echo "")"
          [[ -n "$_BS_W_MTIME" ]] && _BS_W_AGE=$(( _BS_NOW - _BS_W_MTIME ))
        fi

        # -1 (ファイル無し/読めない) は「無限に stale」として扱う。
        _BS_D_STALE=0
        if [[ "$_BS_D_AGE" -lt 0 ]] || [[ "$_BS_D_AGE" -ge "$_BS_DISPATCHER_STALE_S" ]]; then
          _BS_D_STALE=1
        fi
        _BS_W_STALE=0
        if [[ "$_BS_W_AGE" -lt 0 ]] || [[ "$_BS_W_AGE" -ge "$_BS_WATCHDOG_STALE_S" ]]; then
          _BS_W_STALE=1
        fi

        if [[ "$_BS_D_STALE" == "1" ]] && [[ "$_BS_W_STALE" == "1" ]]; then
          _BS_D_DESC="${_BS_D_AGE}s前"
          [[ "$_BS_D_AGE" -lt 0 ]] && _BS_D_DESC="heartbeat無し"
          _BS_W_DESC="${_BS_W_AGE}s前"
          [[ "$_BS_W_AGE" -lt 0 ]] && _BS_W_DESC="heartbeat無し"
          echo "[daemon-backstop] ⚠️ dispatcher と watchdog の heartbeat が両方 stale です (dispatcher: ${_BS_D_DESC}, watchdog: ${_BS_W_DESC})。相互監視は片方が生きていないと相手を起こせません。scripts/lib_daemon_watch.py status で確認し、両方が本当に死んでいれば両方を respawn してください (片方だけの respawn は knowledge/daemon-authority.md §5 の事故を再現します)。" >&2
          _INTENTIONAL_EXIT_CODE=2
        fi
      fi
    fi
  fi
fi

# Taskvia 無効モード: CREWVIA_TASKVIA=disabled または トークン未設定なら投稿スキップ
if [ "${CREWVIA_TASKVIA:-}" = "disabled" ] || [ -z "$TASKVIA_TOKEN" ]; then
  exit 0
fi

# stdin から hook の JSON ペイロードを読む
_CURRENT_STEP="read-input"
INPUT="$(cat)"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // "unknown"')"
TOOL_INPUT="$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')"

# tool_input の先頭80文字をサマリーとして使用
TOOL_INPUT_SUMMARY="$(echo "$TOOL_INPUT" | head -c 80)"

CONTENT="${TOOL_NAME}: ${TOOL_INPUT_SUMMARY}"

# ログペイロード構築
_CURRENT_STEP="build-payload"
PAYLOAD="$(jq -nc \
  --arg type    "work" \
  --arg content "$CONTENT" \
  --arg title   "${TASK_TITLE:-}" \
  --arg tid     "${TASK_ID:-}" \
  --arg agent   "$AGENT_NAME" \
  --arg proj    "${CREWVIA_PROJECT:-crewvia}" \
  '{type: $type, content: $content, task_title: $title, task_id: ($tid | if . == "" then null else . end), agent: $agent, project: $proj}')"

# curl 失敗でもエージェントを止めないため exit 0 で終了
curl -sf -X POST "${TASKVIA_URL}/api/log" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
  -d "$PAYLOAD" >/dev/null 2>&1 || true

# --- Taskvia /api/agents 送信(役割・スキル付きハートビート情報) ---
# heartbeat ファイル自体は上流(ガード前)で既に更新済み。ここは Taskvia への
# メタデータ送信のみ(TASKVIA_TOKEN が設定済みの場合のみここに到達)。
_CURRENT_STEP="agents-heartbeat"
if [[ -n "${AGENT_NAME:-}" ]]; then
  # task_160 F9是正: 汎用名 REPO_ROOT は外部から乗っ取り可能なため CREWVIA_REPO_ROOT を読む
  # (start.sh:248 が既に export 済み)。読み手(watchdog.py)側は変更しないこと — 向きが重要。
  _HB_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

  # workers.yaml からロール・スキルを取得
  _WORKERS_YAML="${_HB_REPO}/registry/workers.yaml"
  _HB_ROLE="worker"
  _HB_SKILLS_STR=""
  if [[ -f "$_WORKERS_YAML" ]]; then
    _HB_AGENT_META="$(python3 - "$AGENT_NAME" "$_WORKERS_YAML" <<'PYEOF' 2>/dev/null || echo "worker|"
import re, sys
from pathlib import Path
agent_name, yaml_path = sys.argv[1], sys.argv[2]
content = Path(yaml_path).read_text()
in_target = False
role = "worker"
skills = []
for line in content.splitlines():
    if re.match(r'\s*- name: ' + re.escape(agent_name) + r'\s*$', line):
        in_target = True
        continue
    if in_target:
        if re.match(r'\s*- name:', line):
            break
        m = re.match(r'\s*role:\s*(.+)', line)
        if m:
            role = m.group(1).strip()
        m = re.match(r'\s*skills:\s*\[(.+)\]', line)
        if m:
            skills = [s.strip() for s in m.group(1).split(",")]
print(f"{role}|{','.join(skills)}")
PYEOF
)"
    _HB_ROLE="${_HB_AGENT_META%%|*}"
    _HB_SKILLS_STR="${_HB_AGENT_META##*|}"
  fi

  # assignments から現在タスク情報を補完（env 未設定時のみ）
  _HB_TASK_ID="${TASK_ID:-}"
  _HB_TASK_TITLE="${TASK_TITLE:-}"
  if [[ -z "$_HB_TASK_ID" ]]; then
    _HB_ASSIGNMENT_FILE="${_HB_REPO}/queue/assignments/${AGENT_NAME}"
    if [[ -f "$_HB_ASSIGNMENT_FILE" ]]; then
      _HB_ASSIGNMENT="$(tr -d '\n' < "$_HB_ASSIGNMENT_FILE")"
      _HB_MISSION="${_HB_ASSIGNMENT%%:*}"
      _HB_TASK_ID="${_HB_ASSIGNMENT##*:}"
      _HB_TASK_FILE="${_HB_REPO}/queue/missions/${_HB_MISSION}/tasks/${_HB_TASK_ID}.md"
      if [[ -f "$_HB_TASK_FILE" ]]; then
        _HB_TASK_TITLE="$(grep '^title:' "$_HB_TASK_FILE" | head -1 | sed 's/^title:[[:space:]]*//' | sed 's/^"\(.*\)"$/\1/')"
      fi
    fi
  fi

  # Taskvia /api/agents にハートビートを送信（TASKVIA_TOKEN が設定済みの場合のみここに到達）
  _AGENTS_PAYLOAD="$(jq -nc \
    --arg name   "$AGENT_NAME" \
    --arg role   "$_HB_ROLE" \
    --arg skills "$_HB_SKILLS_STR" \
    --arg tid    "${_HB_TASK_ID:-}" \
    --arg ttitle "${_HB_TASK_TITLE:-}" \
    '{name: $name, role: $role, skills: ($skills | split(",") | map(select(. != ""))), current_task_id: ($tid | if . == "" then null else . end), current_task_title: ($ttitle | if . == "" then null else . end)}')"

  curl -sf -X POST "${TASKVIA_URL}/api/agents" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
    -d "$_AGENTS_PAYLOAD" >/dev/null 2>&1 || true
fi

_CURRENT_STEP="done"
exit 0
