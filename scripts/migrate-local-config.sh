#!/usr/bin/env bash
# scripts/migrate-local-config.sh — config/crewvia.yaml の手元変更を config/crewvia.local.yaml へ移す
# (machine-local-config の移行。設計: knowledge/machine-local-config.md §7 の下の「移行スクリプト」)。
#
# 使い方: crewvia の主 checkout で `scripts/migrate-local-config.sh` (どこから呼んでもスクリプトの親が対象)。
#   main が PR-2 (lib_config) 以降であること。crewvia は止めなくてよい。
#
# やること:
#   1. config/crewvia.yaml の HEAD との差を、config/crewvia.local.yaml (mode: 0600) に書く。
#   2. mode は共通にコミットされていて差に出ない機があるので、**必ず local に明示する**。
#   3. lib_config.load_effective で移行前後の実効 config が一致することを確かめる。
#      不一致なら元に戻して止める。
#   4. 共通の config/crewvia.yaml を HEAD に戻す (git checkout)。
# 止まる場合 (何も変えない): lib_config.py が無い / 既に local がある / 共通が HEAD に無い /
#   キーを消した手元変更がある / 許可外キー (lib_config.ALLOWED_KEYS 以外) の手元変更がある。
# 退避: ~/crewvia-local-migrate-<時刻>/ (700)。戻し方:
#   cp "<退避先>/crewvia.yaml.local-edited" config/crewvia.yaml && rm config/crewvia.local.yaml
# 秘密: 標準出力に出すのはキー名と SAME / 不一致だけ。値は出さない (退避先のファイルには入る。700)。
#
# 終了コード: 0 = 移行した / 1 = 前提を満たさず何も変えていない / 2 = 手元変更を移せず何も変えていない /
#   3 = local を作ったが移行前後が一致しない (リポジトリは何も変えていない) / 4 = 適用後に不一致 (元に戻した)。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 移行前後の比較は local を読んだ実効値でなければ意味が無い。テスト隔離の印が立っていると
# lib_config は local を読まないので、ここでは外す。
unset CREWVIA_MUX_TEST_ISOLATION

[ -f scripts/lib_config.py ] || { echo "scripts/lib_config.py が無い (主 checkout で・main を最新に)"; exit 1; }
# local は lib_config が主 checkout から読む。worktree 等で実行すると比べる相手がずれる (exit 4 になる) ので先に止める。
MAIN_CHECKOUT="$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import lib_config; print(lib_config.find_main_checkout(sys.argv[1]))' "$ROOT" 2>/dev/null || true)"
if [ "$MAIN_CHECKOUT" != "$ROOT" ]; then
  echo "ここは主 checkout ではない (主 checkout: ${MAIN_CHECKOUT:-不明} / ここ: $ROOT)。主 checkout で実行する。中止 (何も変えていない)"
  exit 1
fi
[ -e config/crewvia.local.yaml ] && { echo "config/crewvia.local.yaml が既にある。中止 (何も変えていない)"; exit 1; }
git show HEAD:config/crewvia.yaml > /dev/null 2>&1 || { echo "config/crewvia.yaml が HEAD に無い。中止 (何も変えていない)"; exit 1; }

B="$HOME/crewvia-local-migrate-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$B"
chmod 700 "$B"
cp config/crewvia.yaml "$B/crewvia.yaml.local-edited"
git diff HEAD -- config/crewvia.yaml > "$B/crewvia.yaml.diff"
git show HEAD:config/crewvia.yaml > "$B/common.yaml"
python3 scripts/lib_config.py dump --json > "$B/before.json"
echo "退避先: $B"

rc=0
python3 - "$B" <<'PY' || rc=$?
import json
import sys

import yaml

sys.path.insert(0, 'scripts')
import lib_config as c

B = sys.argv[1]
SAME = object()


def load(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def diff(x, y):
    """共通 x に対する手元 y の差。変わらなければ SAME。null への変更は None のまま残す。"""
    if isinstance(x, dict) and isinstance(y, dict):
        removed = [k for k in x if k not in y]
        if removed:
            print('キーを消した手元変更がある (手で見て): %s' % sorted(map(str, removed)))
            sys.exit(2)
        out = {}
        for k in y:
            d = y[k] if k not in x else diff(x[k], y[k])
            if d is not SAME:
                out[k] = d
        return out if out else SAME
    return SAME if x == y else y


common = load('%s/common.yaml' % B)
edited = load('%s/crewvia.yaml.local-edited' % B)
if not isinstance(common, dict) or not isinstance(edited, dict):
    print('config/crewvia.yaml が dict でない。中止')
    sys.exit(2)
d = diff(common, edited)
d = {} if d is SAME else d
if 'mode' not in edited:
    print('config/crewvia.yaml に mode が無い。中止')
    sys.exit(2)
d['mode'] = edited['mode']          # 差に出なくても必ず明示する
bad = sorted(str(k) for k in d if k not in c.ALLOWED_KEYS)
if bad:
    print('local に移せないキーの手元変更がある: %s (共通に残す変更はコミットで。何も変えていない)' % bad)
    sys.exit(2)
hdr = '# このマシン固有の設定。git 管理外。秘密の値は書かない (op:// の参照だけ)\n'
with open('%s/crewvia.local.yaml' % B, 'w', encoding='utf-8') as f:
    f.write(hdr + yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
eff = c.load_effective(common_path='%s/common.yaml' % B, local_path='%s/crewvia.local.yaml' % B)
if not isinstance(eff, dict):
    print('local を重ねた設定が読めない (固定コード: %s)' % getattr(eff, 'reason', 'unreadable'))
    sys.exit(3)
ok = json.loads(json.dumps(eff)) == json.load(open('%s/before.json' % B, encoding='utf-8'))
print('local のトップキー: %s / 移行前と一致: %s' % (list(d), ok))
sys.exit(0 if ok else 3)
PY
if [ "$rc" -ne 0 ]; then
  echo "中止 (exit $rc)。リポジトリは何も変えていない。退避先: $B"
  exit "$rc"
fi

install -m 600 "$B/crewvia.local.yaml" config/crewvia.local.yaml
git checkout HEAD -- config/crewvia.yaml
if python3 scripts/lib_config.py dump --json > "$B/after.json" && cmp -s "$B/before.json" "$B/after.json"; then
  echo SAME
else
  echo "不一致 → 元に戻す"
  cp "$B/crewvia.yaml.local-edited" config/crewvia.yaml
  rm -f config/crewvia.local.yaml
  exit 4
fi
echo "mode = $(python3 scripts/lib_config.py get mode)"
git status --short
