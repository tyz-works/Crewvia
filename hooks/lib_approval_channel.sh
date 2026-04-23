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
#   config/crewvia.yaml の approval_channel セクションから値を読み出す。
#   key_path: "mode" | "ntfy.url" | "ntfy.topic" | "ntfy.user" | "ntfy.pass" | "ntfy.token_ttl_seconds"
#   stdout に値を出力。見つからなければ空出力。
# ---------------------------------------------------------------------------
_read_approval_yaml() {
  local key_path="$1"
  [[ ! -f "$_APPROVAL_CONFIG_FILE" ]] && return

  awk -v key_path="$key_path" '
    BEGIN {
      in_ac=0; in_ntfy=0
      n = split(key_path, kp, ".")
      tk = kp[1]
      sk = (n > 1) ? kp[2] : ""
    }
    /^approval_channel:/ { in_ac=1; next }
    in_ac && /^[a-zA-Z_]/ { in_ac=0; in_ntfy=0 }
    in_ac {
      if (/^  ntfy:/) { in_ntfy=1; next }
      if (in_ntfy && /^  [a-zA-Z_]/ && !/^    /) { in_ntfy=0 }

      if (tk == "mode" && sk == "" && /^  mode:/) {
        val = $0; gsub(/^  mode:[[:space:]]*/, "", val)
        gsub(/^['"'"'"]|['"'"'"]$/, "", val); print val; exit
      }
      if (tk == "ntfy" && in_ntfy) {
        pfx = "    " sk ":"
        if (index($0, pfx) == 1) {
          val = substr($0, length(pfx) + 1)
          gsub(/^[[:space:]]+/, "", val)
          gsub(/^['"'"'"]|['"'"'"]$/, "", val); print val; exit
        }
      }
    }
  ' "$_APPROVAL_CONFIG_FILE"
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
  mode=$(_read_approval_yaml "mode")
  echo "${mode:-taskvia}"
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

