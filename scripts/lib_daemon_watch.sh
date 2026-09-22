#!/usr/bin/env bash
# lib_daemon_watch.sh — デーモン相互監視の bash 側 (t005)
#
# dispatcher の heartbeat を **bash から** 書くための最小ヘルパー。
#
# なぜ python 側 (lib_daemon_watch.py の DaemonWatch.beat) ではなく bash なのか:
#
#   dispatcher.sh は 1 サイクルごとに python3 を起動し直す使い捨て構造なので、
#   実際に生き続けているプロセスは **bash のラッパーだけ** である。相手
#   (watchdog) が probe するのはこの bash の PID でなければならない。
#
#   さらに重要なのは、heartbeat が dispatch サイクルの成否から独立している
#   必要があること。python 側が毎回例外で落ちるような状態 (壊れた card、
#   中途半端な deploy) では、bash ループは元気に回り続けているのに heartbeat
#   だけが止まる。相手から見ると「生きているのに死んで見える」= respawn =
#   dispatcher が 2 つ、という最悪の壊れ方になる。書き手を分けておけば、
#   python が全滅しても生存表明だけは続く。
#
# 形式は lib_daemon_watch.py の read_heartbeat() が読むものと同一。
# 契約は tests/test_daemon_mutual_watch.py::test_bash_written_heartbeat_is_readable_by_python
# が固定している。

# 起動時刻は最初の 1 回だけ記録する (以降の beat では更新しない)。
CREWVIA_DAEMON_STARTED_AT="${CREWVIA_DAEMON_STARTED_AT:-}"

# _daemon_generation <pid> — "<pid>:<starttime>"、読めなければ "<pid>:"
#
# /proc/<pid>/stat の 22 番目のフィールド (starttime) を取る。comm (2 番目) は
# 括弧付きで空白や ')' を含みうるので、**最後の ')' より後ろ**を切り出してから
# 数える。切り出し後の先頭は 3 番目 (state) なので、22 番目は 20 トークン目。
# lib_daemon_watch.py の process_generation() と同じ数え方。
_daemon_generation() {
  local pid="$1" starttime=""
  if [[ -r "/proc/${pid}/stat" ]]; then
    starttime="$(sed 's/^.*) //' "/proc/${pid}/stat" 2>/dev/null | awk '{print $20}')"
  fi
  printf '%s:%s' "$pid" "$starttime"
}

# daemon_beat <name> <repo_root> [registry_dir] [pid]
#
# 失敗しても必ず 0 で返る: heartbeat が書けないことは dispatch を止める理由に
# ならないし、`set -e` の下で呼ばれるのでここで非ゼロを返すとデーモンごと
# 落ちてしまう (監視のために監視対象を殺すことになる)。
daemon_beat() {
  local name="$1"
  local repo_root="$2"
  local registry_dir="${3:-${repo_root}/registry}"
  local pid="${4:-$$}"

  local dir="${registry_dir}/daemons"
  mkdir -p "$dir" 2>/dev/null || return 0

  local now
  now="$(date +%s.%N 2>/dev/null)" || return 0
  [[ -n "$CREWVIA_DAEMON_STARTED_AT" ]] || CREWVIA_DAEMON_STARTED_AT="$now"

  local generation
  generation="$(_daemon_generation "$pid")"

  local tmp="${dir}/.${name}.heartbeat.tmp.$$"
  if printf '{"daemon":"%s","generation":"%s","pid":%d,"repo_root":"%s","started_at":%s,"updated_at":%s,"version":1,"window":"%s"}\n' \
      "$name" "$generation" "$pid" "$repo_root" \
      "$CREWVIA_DAEMON_STARTED_AT" "$now" "$name" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "${dir}/${name}.heartbeat" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
  fi
  return 0
}
