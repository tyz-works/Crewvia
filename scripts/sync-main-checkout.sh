#!/usr/bin/env bash
# sync-main-checkout.sh — 主 checkout を origin/main に合わせ、必要なら
# デーモンを restart する、1 コマンドの同期ツール (t005 / B2 / backlog #26)。
#
# 背景: merge のたびに Director が手で「主 checkout を ff → 変わったファイルに
# 応じて dispatcher / watchdog を restart」してきた (前 mission で 9 回)。
# 忘れると merge 済みの修正が本番で動かず、誤診断の元になる
# (knowledge/dispatcher-restart-after-merge.md)。dispatcher.sh の
# check_main_checkout_drift() が版ずれを検知して Director に知らせたら、
# このスクリプトを 1 本実行するだけで済むようにする。
#
# 使い方:
#   scripts/sync-main-checkout.sh              # fetch → ff merge → 必要な restart → status
#   scripts/sync-main-checkout.sh --dry-run    # 何をするか (どこまで進むか) を表示するだけ
#   scripts/sync-main-checkout.sh --repo-root <path>   # テスト用。既定はこのスクリプトの repo
#   scripts/sync-main-checkout.sh --restart-wait-seconds N   # restart 後、新しい世代の heartbeat を
#                                                            # 待つ上限 (既定 90 秒)
#   scripts/sync-main-checkout.sh --restart-wait-seconds N   # restart 後に新しい世代の
#                                                            # heartbeat を待つ上限 (既定 90)
#
# 何もしない条件 (exit 非 0、理由を出す):
#   - fetch に失敗した
#   - HEAD が origin/main の祖先でない (diverged — 手で解決すること)
#   - merge --ff-only 自体が失敗した (通常は起きないが、報告してそこで止まる)
#
# 主 checkout の未コミット変更 (registry/workers.yaml 等) があっても、それらと
# 衝突しない限り ff は進む — `git merge --ff-only` は素の git の判断に任せ、
# ここで追加のガードは入れない。
#
# **禁止**: 本番の主 checkout (/home/tkadmin/workspace/crewvia) に対してこの
# スクリプトを開発中に実行しない (--dry-run も含む)。動作確認は一時ディレクトリの
# bare origin + checkout でだけ行う。dispatcher / watchdog の restart は
# `scripts/lib_daemon_watch.py restart` を通す (pane の所有者確認・二重起動防止は
# そちらの責務。このスクリプトはそれを素で叩かない — 不変条件 4)。
#
# 失敗を成功に潰さない (族A の一覧。t112 / PR#246 Codex 1 巡目 P2):
#   箇所                          直す前                            直した後
#   ----------------------------  --------------------------------  ------------------------------
#   restart-targets (advisory)    process substitution の失敗が      出力を先に変数へ捕まえ、失敗を
#                                 while read に無音で吸われる        note_failure() に積む
#   restart-needed の比較         想定外の値 (コマンド失敗込み) は    true/false/unknown 以外・非0
#                                 case のどの枝にも当たらず無視      終了は note_failure() に積む
#   restart の実行                失敗しても WARNING を出すだけ      同じ WARNING に加え
#                                                                    note_failure() に積む
#   最後の status                 戻り値を見ない                     戻り値を見て失敗を積む
#   restart 直後の status         記録上の旧 pid が dead のまま      restart した daemon は新しい世代の
#                                 表示され失敗に見えた (09-28)      heartbeat を上限つきで待ってから表示。
#                                                                    上限までに無ければ note_failure() に積む
#   全体の終了コード              上記が何件あっても exit 0          FAILURES が 1 件でもあれば
#                                                                    非 0 で終わる (末尾の集計)
# `git fetch` / `git merge --ff-only` 自体の失敗は元から `fail()` (即 exit 1) で
# 正しく止まっており、この表には含めない。

set -uo pipefail

DRY_RUN=0
REPO_ROOT=""
RESTART_WAIT_SECONDS=90
RESTARTED=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    --restart-wait-seconds)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        echo "sync-main-checkout.sh: --restart-wait-seconds needs a whole number" >&2
        exit 2
      fi
      RESTART_WAIT_SECONDS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "sync-main-checkout.sh: unknown option: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
REGISTRY_DIR="${REPO_ROOT}/registry"
LIB_DAEMON_WATCH="${SCRIPT_DIR}/lib_daemon_watch.py"

say() { echo "[sync-main-checkout] $*"; }
fail() { echo "[sync-main-checkout] $*" >&2; exit 1; }

# 個別には致命的でない (即 exit しない) が、最終的な終了コードには反映する失敗の集計。
# 上の族A の表を参照。
FAILURES=()
note_failure() { FAILURES+=("$1"); echo "[sync-main-checkout] FAILURE: $1" >&2; }

cd "$REPO_ROOT" || fail "cannot cd to repo root: $REPO_ROOT"

# --- 1. fetch (dry-run でも行う — 差分を知るには要る。working tree には触れない) ---
say "git fetch origin main ..."
if ! FETCH_OUT="$(git fetch origin main 2>&1)"; then
  echo "$FETCH_OUT" >&2
  fail "git fetch origin main failed — network か remote の設定を確認してください。何もしていません。"
fi

LOCAL_HEAD="$(git rev-parse HEAD)" || fail "git rev-parse HEAD failed"
REMOTE_HEAD="$(git rev-parse origin/main)" || fail "git rev-parse origin/main failed"

if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
  say "already up to date (HEAD == origin/main == ${LOCAL_HEAD:0:12})"
  CHANGED_FILES=()
elif git merge-base --is-ancestor "$LOCAL_HEAD" "$REMOTE_HEAD"; then
  N_BEHIND="$(git rev-list --count "${LOCAL_HEAD}..${REMOTE_HEAD}")"
  say "origin/main is ${N_BEHIND} commit(s) ahead (${LOCAL_HEAD:0:12} -> ${REMOTE_HEAD:0:12})"
  mapfile -t CHANGED_FILES < <(git diff --name-only "$LOCAL_HEAD" "$REMOTE_HEAD")
else
  fail "HEAD (${LOCAL_HEAD:0:12}) is not an ancestor of origin/main (${REMOTE_HEAD:0:12}) — diverged. 手で解決してください。何もしていません。"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  say "--dry-run: merge も restart もしません。"
  if [[ ${#CHANGED_FILES[@]} -gt 0 ]]; then
    say "pull されたら変わるファイル (${#CHANGED_FILES[@]} 件):"
    printf '  %s\n' "${CHANGED_FILES[@]}"
    if RESTART_TARGETS_OUT="$(printf '%s\n' "${CHANGED_FILES[@]}" | python3 "$LIB_DAEMON_WATCH" restart-targets --repo-root "$REPO_ROOT")"; then
      while IFS=' ' read -r kind name; do
        [[ -z "$kind" ]] && continue
        if [[ "$kind" == "daemon" ]]; then
          say "  -> restart 対象: $name"
        else
          say "  -> restart 推奨 (手動。このスクリプトは実行しません): $name"
        fi
      done <<< "$RESTART_TARGETS_OUT"
    else
      note_failure "restart-targets が失敗しました (dry-run の対象一覧は不完全です)"
    fi
  fi
  for name in dispatcher watchdog; do
    if v="$(python3 "$LIB_DAEMON_WATCH" restart-needed "$name" --repo-root "$REPO_ROOT")"; then
      say "restart-needed($name) [pull 前の現在の disk 基準] = $v"
    else
      note_failure "restart-needed($name) が失敗しました"
    fi
  done
  if [[ ${#FAILURES[@]} -gt 0 ]]; then
    say "--dry-run: ${#FAILURES[@]} 件の失敗があり、上の表示は不完全な可能性があります。"
    exit 1
  fi
  exit 0
fi

# --- 2. merge --ff-only (pull すべきものが無ければ no-op) ---
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  say "git merge --ff-only origin/main ..."
  if ! git merge --ff-only origin/main; then
    fail "git merge --ff-only failed — 上のエラーを確認してください。何も restart していません。"
  fi
fi

# --- 3. 変わったファイルから advisory (worker / director) を報告 ---
if [[ ${#CHANGED_FILES[@]} -gt 0 ]]; then
  if RESTART_TARGETS_OUT="$(printf '%s\n' "${CHANGED_FILES[@]}" | python3 "$LIB_DAEMON_WATCH" restart-targets --repo-root "$REPO_ROOT")"; then
    while IFS=' ' read -r kind name; do
      [[ "$kind" == "advisory" ]] || continue
      say "restart 推奨 (このスクリプトは実行しません — 手動で対応してください): $name"
    done <<< "$RESTART_TARGETS_OUT"
  else
    note_failure "restart-targets が失敗しました (advisory 報告は不完全です)"
  fi
fi

# --- 4. dispatcher / watchdog: 記録された版と disk の現在地を比較して restart ---
# 「変わったファイル一覧との突合」ではなく「稼働中デーモンの記録との突合」を restart
# 判定の根拠にする — こちらは pull がここで起きたかどうかに関係なく、記録し忘れ
# (record-version が古い) やこのスクリプト以外の経路で HEAD が動いた場合もカバーする。
ANY_RESTARTED=0
for name in dispatcher watchdog; do
  if ! v="$(python3 "$LIB_DAEMON_WATCH" restart-needed "$name" --repo-root "$REPO_ROOT")"; then
    note_failure "restart-needed($name) が非 0 で終了しました (出力: ${v:-<なし>})"
    continue
  fi
  case "$v" in
    true)
      say "restart-needed($name) = true -> restart します"
      # restart 前の heartbeat の同一性と時刻。restart 後、これと違う世代の
      # heartbeat が記録されるまで status を出さない (下の 5.)。
      if ! HB_BEFORE="$(python3 "$LIB_DAEMON_WATCH" heartbeat-id "$name" --repo-root "$REPO_ROOT")"; then
        note_failure "heartbeat-id($name) が失敗しました (restart 後の世代確認ができません)"
        HB_BEFORE="none"
      fi
      RESTART_SINCE="$(date +%s)"
      if python3 "$LIB_DAEMON_WATCH" restart "$name" --repo-root "$REPO_ROOT"; then
        ANY_RESTARTED=1
        RESTARTED+=("$name|$HB_BEFORE|$RESTART_SINCE")
      else
        note_failure "restart($name) が失敗しました (理由は上に出ています)。手動で確認してください。"
      fi
      ;;
    false)
      say "restart-needed($name) = false (最新の版で稼働中)"
      ;;
    unknown)
      say "restart-needed($name) = unknown (版の記録が無く比較できません — 自動 restart はしません。念のため restart するなら手で: python3 $LIB_DAEMON_WATCH restart $name)"
      ;;
    *)
      note_failure "restart-needed($name) が想定外の値を返しました: '$v'"
      ;;
  esac
done

# --- 5. 確認 ---
# restart した daemon は、新しい世代の heartbeat が記録されるまで (上限つき) 待つ。待たずに
# status を出すと、記録上まだ旧 pid で recorded_instance_alive=False と表示され、失敗に見える
# (2026-09-28 21:11)。上限までに記録されなければ、明示的に失敗として積む。
for entry in "${RESTARTED[@]+"${RESTARTED[@]}"}"; do
  IFS='|' read -r r_name r_before r_since <<< "$entry"
  say "$r_name: 新しい世代の heartbeat を待っています (上限 ${RESTART_WAIT_SECONDS} 秒) ..."
  if python3 "$LIB_DAEMON_WATCH" wait-heartbeat "$r_name" --repo-root "$REPO_ROOT" \
       --before "$r_before" --since "$r_since" --timeout "$RESTART_WAIT_SECONDS"; then
    :
  else
    note_failure "restart($r_name) 後 ${RESTART_WAIT_SECONDS} 秒以内に新しい世代の heartbeat が記録されませんでした (下の status は旧世代の記録を含みうる)"
  fi
done
if [[ "$ANY_RESTARTED" -eq 1 ]]; then
  say "restart 後の状態:"
fi
if ! python3 "$LIB_DAEMON_WATCH" status --repo-root "$REPO_ROOT"; then
  note_failure "status サブコマンドが失敗しました"
fi

if [[ ${#FAILURES[@]} -gt 0 ]]; then
  say "failed: ${#FAILURES[@]} 件の失敗があります (詳細は上の FAILURE 行)。HEAD=$(git rev-parse --short HEAD)"
  exit 1
fi

say "done. HEAD=$(git rev-parse --short HEAD)"
