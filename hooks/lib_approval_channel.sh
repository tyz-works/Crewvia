#!/usr/bin/env bash
# hooks/lib_approval_channel.sh
# 承認チャネル共通ライブラリ。pre-tool-use.sh から source して使う。
#
# 提供する関数:
#   get_approval_channel_mode  — 有効モードを返す (taskvia|ntfy|both)
#   load_ntfy_config           — ntfy 設定を config / env から読み込む
#
# α方針 (task_089): ntfy publish は Taskvia が担当。crewvia は POST /api/request に
#   { notify: true } を渡すのみ。ntfy_publish / parse_token_urls は削除済み。

# 二重 source 防止
[[ "${_LIB_APPROVAL_CHANNEL_LOADED:-}" == "1" ]] && return 0
readonly _LIB_APPROVAL_CHANNEL_LOADED=1

_CREWVIA_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_APPROVAL_CONFIG_FILE="${_CREWVIA_REPO}/config/crewvia.yaml"

# ---------------------------------------------------------------------------
# _read_approval_yaml <key_path>
#   effective config (config/crewvia.yaml + config/crewvia.local.yaml。lib_config) の approval_channel セクションから値を読み出す。
#   key_path: "mode" | "ntfy.url" | "ntfy.topic" | "ntfy.user" | "ntfy.pass" | "ntfy.token_ttl_seconds"
#   stdout に値を出力。見つからなければ空出力。
# ---------------------------------------------------------------------------
_read_approval_yaml() {
  local key_path="$1"
  # PreToolUse は Worker の全 tool 呼び出しに乗るので、python は effective config を dump する 1 回だけ起動し、
  # 各キーは jq で切り出す (PR-2 レビュー P3-1。以前は 6 回起動で 1 回 約 0.5 秒増)。
  _approval_load_cfg
  [[ "${_APPROVAL_CFG_RC}" -eq 0 ]] || return 0   # 壊れ: 呼び出し側 (get_approval_channel_mode) が config-unreadable を返す
  local out
  out="$(printf '%s' "${_APPROVAL_CFG_JSON}" | jq -r --arg k "${key_path}" \
    'getpath(("approval_channel." + $k) | split(".")) // empty | if type == "object" or type == "array" then tojson else tostring end' 2>/dev/null)" || return 0
  [[ -n "$out" ]] && printf '%s\n' "$out"
  return 0
}

# effective config (共通 + local) を 1 回だけ読む。_APPROVAL_CFG_RC: 0=読めた / 1=壊れ。
_approval_load_cfg() {
  [[ -n "${_APPROVAL_CFG_RC:-}" ]] && return 0
  # 壊れた local / 共通。承認を黙って通さない・共通の値に黙って戻さない (get_approval_channel_mode → deny)。
  # worktree の hook から呼んでも local は主 checkout のものを見る (lib_config.find_main_checkout)。
  if _APPROVAL_CFG_JSON="$(python3 "${_CREWVIA_REPO}/scripts/lib_config.py" --root "${_CREWVIA_REPO}" dump --json 2>/dev/null)"; then
    _APPROVAL_CFG_RC=0
  else
    _APPROVAL_CFG_RC=1
    _APPROVAL_CFG_JSON=""
  fi
}

# ---------------------------------------------------------------------------
# get_approval_channel_mode
#   優先順位: env CREWVIA_APPROVAL_CHANNEL > config > デフォルト(taskvia)
#   stdout にモード文字列を出力する
# ---------------------------------------------------------------------------
get_approval_channel_mode() {
  if [[ -n "${CREWVIA_APPROVAL_CHANNEL:-}" ]]; then
    echo "${CREWVIA_APPROVAL_CHANNEL}"
    return
  fi

  local mode
  _approval_load_cfg
  mode=$(_read_approval_yaml "mode")
  if [[ "${_APPROVAL_CFG_RC:-1}" -eq 0 ]]; then
    echo "${mode:-taskvia}"
  else
    echo "config-unreadable"
  fi
}

# ---------------------------------------------------------------------------
# load_ntfy_config
#   ntfy 設定を config から読み込み、env var として export する。
#   env var が既にセットされている場合は config より優先される。
#   設定後に使える変数: NTFY_URL, NTFY_TOPIC, NTFY_USER, NTFY_PASS,
#                        APPROVAL_TOKEN_TTL_SECONDS
# ---------------------------------------------------------------------------
load_ntfy_config() {
  local cfg_url cfg_topic cfg_user cfg_pass cfg_ttl
  _approval_load_cfg   # 親シェルで 1 回 (下の $(...) は subshell なので、ここで読んだ値を引き継ぐ)
  cfg_url=$(_read_approval_yaml "ntfy.url")
  cfg_topic=$(_read_approval_yaml "ntfy.topic")
  cfg_user=$(_read_approval_yaml "ntfy.user")
  cfg_pass=$(_read_approval_yaml "ntfy.pass")
  cfg_ttl=$(_read_approval_yaml "ntfy.token_ttl_seconds")

  [[ -z "${NTFY_URL:-}" ]]                   && export NTFY_URL="${cfg_url:-}"
  [[ -z "${NTFY_TOPIC:-}" ]]                 && export NTFY_TOPIC="${cfg_topic:-}"
  [[ -z "${NTFY_USER:-}" ]]                  && export NTFY_USER="${cfg_user:-}"
  [[ -z "${NTFY_PASS:-}" ]]                  && export NTFY_PASS="${cfg_pass:-}"
  [[ -z "${APPROVAL_TOKEN_TTL_SECONDS:-}" ]] && export APPROVAL_TOKEN_TTL_SECONDS="${cfg_ttl:-900}"

  # デフォルト補完
  export NTFY_URL="${NTFY_URL:-}"
  export NTFY_TOPIC="${NTFY_TOPIC:-}"
  export NTFY_USER="${NTFY_USER:-}"
  export NTFY_PASS="${NTFY_PASS:-}"
  export APPROVAL_TOKEN_TTL_SECONDS="${APPROVAL_TOKEN_TTL_SECONDS:-900}"
}

