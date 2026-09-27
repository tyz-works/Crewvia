#!/usr/bin/env python3
"""worktree_gc.py — 古い Worker worktree を安全に片付ける (t033 / backlog #33)。

主 checkout の `git worktree list` は、archive 済み mission・merge 済みブランチの worktree が
溜まり続けて 398 個 (2026-09-27) になっていた。手で消すと「今使っている Worker の worktree」や
「push していないコミットのある worktree」を巻き込みうるので、**理由付きで keep / remove を出す**
仕組みにする。

    python3 scripts/worktree_gc.py                 # dry-run (既定。何も変えない・何も書かない)
    python3 scripts/worktree_gc.py --apply         # remove と判定したものだけを隔離する (削除しない)
    python3 scripts/worktree_gc.py --json          # 機械可読
    python3 scripts/worktree_gc.py --list-quarantine   # 隔離済みの一覧
    python3 scripts/worktree_gc.py --restore <隔離先 or 元のパス>   # 隔離を元に戻す

## `--apply` は削除ではなく隔離する (t071 / PR#239 3巡目)

Codex review で 3 巡連続、「削除してよいかの判定が何かを見落とす → 唯一のコピーごと消える」P1 が出た
(t057: ignored ファイル、t064: `GIT_DIR` 継承、t071: `assume-unchanged`/`skip-worktree` の index フラグ)。
見落とし方を列挙して塞ぐやり方は 4 巡目がありうる (`core.ignoreStat=true` は新しいエントリに
assume-unchanged を自動で付けるので、設定 1 つで大量に目隠しされる、等)。**判定が見落としても
復旧できるようにする**ため、`--apply` は remove と判定したものを `git worktree remove` で消す代わりに、
`git worktree move` で `.claude/worktrees/.quarantine/<YYYYMMDD-HHMMSS>/<元の相対パス>` へ移し、
`git worktree lock` を付ける。**`git worktree remove` / `git branch -d` / `git worktree prune` は
一切呼ばない。実際の削除はこのツールの外、人間が手で行う。** 元に戻すには `--restore`。

判定 (`classify()`) の条件そのものは変えていない — 隔離は復旧できるが、clean でないものを隔離候補に
しないことは引き続き重要 (隔離に気付かず作業を失ったと思わせないため)。

## remove にしてよいのは、次を **すべて** 満たすものだけ

1. `<主 checkout>/.claude/worktrees/<mission_slug>/<name>` 配下 (主 checkout 自身・その外は keep)
2. その mission が active でない (`queue/state.yaml` の `active_missions` に無く、`queue/missions/<slug>` も
   残っていない = archive 済み)。state.yaml が読めなければ **全部 keep**
3. Worker が今使っていない: `registry/workers/*/target_dir.json` の TARGET_DIR がその worktree を指さず、
   どのプロセスの cwd (Worker の claude・pane のシェル・この実行自身) もその worktree の中に無い
4. 未コミット・untracked の変更が無い (`git status --porcelain --untracked-files=all` が空)
5. ignore されている内容も無い (`git status --ignored=matching` が空)。「ignore されている = 捨ててよい」
   は成り立たない (`.env` 等の secrets・ローカル状態を ignore しているこの repo では、`git worktree remove`
   は `--force` 無しでも ignored ファイルの削除を許すため)。捨ててよいと明示的に確立できていない限り keep
6. `assume-unchanged` / `skip-worktree` の index フラグが付いた tracked ファイルが無い (`git ls-files -v`
   が空)。どちらのフラグも付いたファイルへの編集は `git status` に **一切出ない** — 空の status は
   「tracked ファイルが変わっていない」を示さない。1 つでもあれば keep (t071 / PR#239 3巡目)
7. `core.ignoreStat` が有効でない。有効だと、以後の checkout 等で触れたファイルへ自動で
   assume-unchanged が付き、6 の観測が今後の編集を拾えなくなる (t071 / PR#239 3巡目)
8. HEAD から辿れるコミットがすべて `origin/*` にある (merge 済み、または remote branch に push 済み)

## 判定できないものは keep (破棄ではなく保留)

読めない・git が失敗・プロセス表を取れない、はどれも **remove の根拠にしない** (memory
`fail-closed-discard-vs-hold` / `evidence-for-destructive-decisions`)。ENOENT (本当に無い) だけを
「無い」と読む。

## `--apply` の安全策

- **`git worktree remove` は呼ばない。** 代わりに `git worktree move <candidate> <隔離先>` で
  `.claude/worktrees/.quarantine/<timestamp>/<元の相対パス>` へ移し、直後に
  `git worktree lock --reason "quarantined by worktree_gc <timestamp> orig=<元の絶対パス>"` を付ける。
  `git worktree move` は登録・ブランチ・未コミットの変更をすべて保ったまま移動し、`lock` された
  worktree は `git worktree prune` でも消えない。**`git branch -d` / `-D` も呼ばない** — 隔離では
  branch に触れる理由が無い (branch は移動した worktree にそのまま残る)。`rm -rf` は使わない。
  remote branch は触らない。
- **repository-wide の `git worktree prune` は呼ばない。** (t057 で削除済み。隔離は個別の対象だけを
  `git worktree move` で動かすので、この制約は変わらず有効)
- 隔離先が既に存在する場合は **`git worktree move` を呼ばずに失敗として報告する** — 空ディレクトリで
  あっても `git worktree move` は「その中へ移す」(Unix の `mv` と同じ挙動) ので、既存の何かの上に
  上書きすることはないが、意図しない場所への配置になりうる (族A: `git worktree move` 自身は成功
  ("成功" というエラー無し)を返すため、これを見落とすと誤配置が起きたことに気付けない)。
- `git worktree move` が失敗する場合 (submodule を含む worktree 等) は、**削除にフォールバックせず
  keep** にする (族A — 安全な操作の失敗を、より危険な操作へのフォールバックの合図にしない)。
- 隔離する直前に、その worktree の判定を **もう一度** やり直し、remove でなくなっていたら隔離しない
  (dry-run から --apply までに Worker が起動した・変更が入った、を拾う)。
- 1 件の失敗で止めない (失敗は報告して次へ。終了コード 1)。
- **実際の削除はこのツールの外。** 隔離された worktree を最終的に消すかどうかは人間が判断する。

## 限界 (知っておくこと)

- 「origin にある」は **手元の remote-tracking ref** で見る (fetch しない = 読み取り専用)。`--fetch` を付けると
  `git fetch --prune origin` してから判定する (merge 後に remote branch が消えていれば、squash merge の
  ブランチは「push 済みと確かめられない」= keep になる。それが保留の向き)。
- プロセスの cwd は Linux の `/proc`、無ければ `lsof` で見る。どちらも使えなければ全部 keep。
- **戻せない操作はこのツールには無い。** `--apply` がやるのは `git worktree move` + `git worktree lock`
  だけで、どちらも `--restore` で完全に戻せる (unlock + move back)。実際に消す操作 (`git worktree remove`
  や `rm -rf`) はこのツールの外、人間の判断で行う。

## 停止スイッチは無い

この判定は dry-run が既定で、`--apply` を付けない限り何も変えない。env で判定を曲げる口は作らない
(共有規則に env 停止スイッチを付けない — memory `no-env-killswitch-for-shared-rule`)。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_task_cards import (  # noqa: E402
    is_missing, is_unreadable, parse_yaml, read_regular_text_or_unreadable,
)
import lib_worker_target  # noqa: E402

REMOVE = 'remove'
KEEP = 'keep'

#: 判定の理由コード (出力・集計・テストが使う。文言ではなくこの名前で突き合わせる)。
R_REMOVE = 'all-conditions-met'
R_MAIN = 'main-checkout'
R_OUTSIDE = 'outside-managed-dir'
R_LOCKED = 'locked'
R_PRUNABLE = 'prunable'
R_STATE_UNREADABLE = 'state-unreadable'
R_MISSION_ACTIVE = 'mission-active'
R_MISSION_NOT_ARCHIVED = 'mission-not-archived'
R_MISSION_UNOBSERVABLE = 'mission-dir-unobservable'
R_REGISTRY_UNREADABLE = 'registry-unreadable'
R_PROCESS_SCAN_FAILED = 'process-scan-failed'
R_IN_USE_TARGET = 'in-use-target-dir'
R_IN_USE_PROCESS = 'in-use-process'
R_STATUS_FAILED = 'git-status-failed'
R_DIRTY = 'dirty'
R_IGNORED = 'ignored-files-present'
R_INDEX_FLAGS = 'index-flags-present'
R_LS_FILES_FAILED = 'ls-files-failed'
R_IGNORE_STAT = 'core-ignorestat-enabled'
R_CONFIG_CHECK_FAILED = 'config-check-failed'
R_HEAD_UNRESOLVED = 'head-unverifiable'
R_UNPUSHED = 'unpushed-commits'
R_QUARANTINED = 'already-quarantined'

GIT_TIMEOUT_SECONDS = 120

#: プロセス表の場所 (テストが偽の /proc に向ける)。
PROC_ROOT = Path('/proc')

#: 隔離領域の名前 (`<managed_root>/.quarantine/<timestamp>/<元の相対パス>`)。
QUARANTINE_DIRNAME = '.quarantine'

#: `git worktree lock --reason` に埋め込む印。`--restore` はこれを手がかりに元のパスへ戻す。
#: タイムスタンプ・元のパスのどちらにも空白を含みうるので、`orig=` は行の最後に置き、そこから先を
#: まるごと元のパスとして読む (`re.match` の `(.*)$` — パスの途中にどんな文字があっても崩れない)。
_QUARANTINE_REASON_RE = re.compile(r'^quarantined by worktree_gc (\S+) orig=(.*)$')


def _quarantine_reason(timestamp: str, original_abs_path: str) -> str:
    return f'quarantined by worktree_gc {timestamp} orig={original_abs_path}'


def _parse_quarantine_reason(reason: str) -> Optional[tuple[str, str]]:
    """`(timestamp, 元の絶対パス)`。このツールが付けた印でなければ None。"""
    m = _QUARANTINE_REASON_RE.match(reason)
    return (m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

#: `git -C <path>` の指定を上書きしうる環境変数。呼び出し元のシェルにこれらが残っていると、
#: `-C` で指定した候補 worktree ではなく env の指す別リポジトリ/worktree/index を検査してしまう
#: (族B — 検査した対象と実際に作用する対象が違う。PR#239 2巡目 P1、t058 QA が実機で再現:
#: GIT_DIR/GIT_WORK_TREE を dirty な candidate とは別の clean リポジトリに向けると、
#: candidate への `git -C candidate status` の出力が空文字列になり dirty が検出できなくなった)。
_GIT_REPO_LOCATION_ENV_VARS = (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_COMMON_DIR', 'GIT_NAMESPACE',
)


def _git_env() -> dict:
    env = dict(os.environ)
    for key in _GIT_REPO_LOCATION_ENV_VARS:
        env.pop(key, None)
    env['LC_ALL'] = 'C'
    # 読み取り専用の判定が index.lock を取りに行かない (status の opportunistic refresh を止める)。
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


def run_git(cwd, *args) -> tuple[Optional[int], str, str]:
    """`(returncode, stdout, stderr)`。起動できない・時間切れは returncode=None (= 判定不能)。"""
    try:
        proc = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                              env=_git_env(), timeout=GIT_TIMEOUT_SECONDS,
                              encoding='utf-8', errors='surrogateescape')
    except (OSError, subprocess.SubprocessError) as e:
        return None, '', f'{type(e).__name__}: {e}'
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class Worktree:
    path: str
    head: Optional[str] = None
    branch: Optional[str] = None       # refs/heads/<name> (detached なら None)
    detached: bool = False
    bare: bool = False
    locked: bool = False
    locked_reason: Optional[str] = None   # `locked` の値部分。reason 無しの lock は '' のまま
    prunable: bool = False


def parse_worktree_list(raw: str) -> list[Worktree]:
    """`git worktree list --porcelain -z` の出力。record は NUL 2 つ (空の欄) で区切られる。"""
    out: list[Worktree] = []
    cur: Optional[Worktree] = None
    for field_ in raw.split('\0'):
        if field_ == '':
            if cur is not None:
                out.append(cur)
                cur = None
            continue
        key, _, value = field_.partition(' ')
        if key == 'worktree':
            if cur is not None:
                out.append(cur)
            cur = Worktree(path=value)
        elif cur is None:
            continue
        elif key == 'HEAD':
            cur.head = value
        elif key == 'branch':
            cur.branch = value
        elif key == 'detached':
            cur.detached = True
        elif key == 'bare':
            cur.bare = True
        elif key == 'locked':
            cur.locked = True
            cur.locked_reason = value
        elif key == 'prunable':
            cur.prunable = True
    if cur is not None:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 判定の材料 (Context): 1 回の走査で集める
# ---------------------------------------------------------------------------

@dataclass
class Context:
    repo: str                                   # 主 checkout の realpath
    queue: str
    active_missions: Optional[set] = None       # None = 読めなかった
    state_problem: str = ''
    registry_problem: str = ''
    target_dirs: list = field(default_factory=list)   # [(agent, realpath)]
    process_problem: str = ''
    process_cwds: list = field(default_factory=list)  # [(pid, realpath)]

    @property
    def managed_root(self) -> str:
        return os.path.join(self.repo, '.claude', 'worktrees')


def load_active_missions(queue: str) -> tuple[Optional[set], str]:
    """`queue/state.yaml` の active_missions。読めない・形が違うなら `(None, 理由)`。

    state.yaml が **無い** (ENOENT) のも「読めない」に含める: plan.sh は無ければ active ゼロと読むが、
    片付けは「active でないと確かめられた mission」だけが対象で、確かめられないなら消さない。
    """
    path = os.path.join(queue, 'state.yaml')
    text = read_regular_text_or_unreadable(path)
    if is_unreadable(text):
        return None, ('state.yaml が無い' if is_missing(text) else f'state.yaml が読めない ({text.reason})')
    try:
        data = parse_yaml(text, source=path)
    except ValueError as e:
        return None, f'state.yaml を解釈できない ({e})'
    missions = data.get('active_missions')
    if not isinstance(missions, list) or not all(isinstance(m, str) and m for m in missions):
        return None, f"state.yaml の active_missions が文字列のリストではない ({missions!r})"
    return set(missions), ''


def load_target_dirs(registry: str) -> tuple[list, str]:
    """`registry/workers/<Name>/target_dir.json` の TARGET_DIR。`([(agent, realpath)], 読めなかった理由)`。

    `registry/workers` が無い (ENOENT) のは「記録が 1 つも無い」= 普通の状態。それ以外の失敗と、
    読めない記録は、その Worker が worktree を使っているかどうか決められないので理由を返す
    (呼び出し側は全部 keep にする)。`target_dir: null` は「crewvia 本体で起動した」事実で、worktree を指さない。
    """
    workers = os.path.join(registry, 'workers')
    try:
        names = sorted(os.listdir(workers))
    except FileNotFoundError:
        return [], ''
    except OSError as e:
        return [], f'{workers} を列挙できない ({e})'
    found = []
    for name in names:
        rec = lib_worker_target.load_record(registry, name)
        if is_missing(rec):
            continue                      # Worker のディレクトリはあるが記録は書かれていない
        if is_unreadable(rec):
            return [], f'{name} の target_dir 記録が読めない ({rec.reason})'
        if rec['target_dir'] is not None:
            found.append((name, os.path.realpath(rec['target_dir'])))
    return found, ''


#: cwd を読めなくても Worker ではないと言える、ユーザー自身の常駐プロセス (comm)。カーネルは dumpable でない
#: プロセス (ssh-agent は自分で切る・sshd / systemd --user は権限の切り替えで切れる) の
#: `/proc/<pid>/cwd` を同じ uid にも読ませない。Worker (claude と pane のシェル) は dumpable なので読める。
#: **allowlist** — 未知の名前で読めないものは「取れなかった」に倒す (足したいときはここに理由付きで足す)。
UNOBSERVABLE_BUT_NOT_A_WORKER = frozenset({
    'ssh-agent', 'sshd', 'sshd-session', 'systemd', '(sd-pam)', 'gpg-agent',
})


def _cwd_unreadable_but_harmless(pid: str) -> Optional[str]:
    """同じ uid のプロセスの cwd を読めなかったとき、それが worktree を掴んでいないと言える理由。言えなければ None。

    * 消えた (`/proc/<pid>` が無い) / zombie・終了処理中 (state Z / X、または cmdline が空 = mm が無い):
      もう cwd を持たない
    * allowlist の常駐プロセス (`UNOBSERVABLE_BUT_NOT_A_WORKER`)
    """
    base = PROC_ROOT / pid
    try:
        stat = (base / 'stat').read_text()
        state = stat[stat.rindex(')') + 2:].split(' ', 1)[0]
        if state in ('Z', 'X'):
            return f'state {state}'
        if not (base / 'cmdline').read_bytes():
            return 'cmdline が空 (終了処理中)'
        comm = (base / 'comm').read_text().strip()
    except FileNotFoundError:
        return '消えた'
    except (OSError, ValueError):
        return None
    return f'comm {comm}' if comm in UNOBSERVABLE_BUT_NOT_A_WORKER else None


def scan_process_cwds() -> tuple[list, str]:
    """全プロセスの cwd。`([(pid, realpath)], 取れなかった理由)`。

    Linux は `/proc/<pid>/cwd`。消えたプロセス (ENOENT / ESRCH) は無視してよい。他の uid のプロセスは
    Worker になりえないので飛ばす。**自分と同じ uid のプロセスを読めなかった**ときは、その cwd が
    worktree の中かもしれないので取れなかった扱い (ただし `_cwd_unreadable_but_harmless()` が
    「掴んでいない」と言えるものは除く)。`/proc` が無ければ `lsof`。
    """
    proc = PROC_ROOT
    if not proc.is_dir():
        return _scan_with_lsof()
    me = os.getuid()
    found = []
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError as e:
        return [], f'/proc を列挙できない ({e})'
    for pid in entries:
        try:
            cwd = os.readlink(proc / pid / 'cwd')
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ESRCH):
                continue
            try:
                st_uid = os.stat(proc / pid).st_uid
            except OSError as stat_e:
                if stat_e.errno in (errno.ENOENT, errno.ESRCH):
                    continue          # プロセスが消えた
                # EACCES / EIO 等は消滅の証拠ではない (族A — 観測の失敗を「不在」に潰さない。
                # PR#239 2巡目 P2、t058 QA が実機で再現: readlink・stat の両方が EACCES を返す
                # read-only probe で `([], '')` = スキャン成功として報告されていた)。
                return [], f'/proc/{pid} を stat できない ({stat_e})'
            if st_uid != me:
                continue
            if _cwd_unreadable_but_harmless(pid) is not None:
                continue
            try:
                comm = (proc / pid / 'comm').read_text().strip()
            except OSError:
                comm = '?'
            return [], f'/proc/{pid}/cwd ({comm}) を読めない ({e})'
        found.append((int(pid), os.path.realpath(cwd)))
    return found, ''


def _scan_with_lsof() -> tuple[list, str]:
    try:
        proc = subprocess.run(['lsof', '-a', '-d', 'cwd', '-Fpn'], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f'/proc も lsof も使えない ({type(e).__name__}: {e})'
    # lsof は一部のプロセスの検査に失敗しても部分的な stdout を出しつつ非 0 を返すことがある。
    # 出力があっても不完全なスキャンでしかない = 「使われていない証拠」にならないので、成功 (rc=0) だけを完全とみなす。
    if proc.returncode != 0:
        return [], f'lsof が不完全 (rc={proc.returncode}): {(proc.stderr or "").strip()[:200]}'
    # 出力が空なら「見えなかった」であって「無い」ではない。
    if not proc.stdout.strip():
        return [], f'lsof の出力が空 (rc={proc.returncode})'
    found, pid = [], None
    for line in proc.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n') and pid is not None:
            found.append((pid, os.path.realpath(line[1:])))
    return found, ''


def build_context(repo: str, queue: str) -> Context:
    ctx = Context(repo=os.path.realpath(repo), queue=queue)
    ctx.active_missions, ctx.state_problem = load_active_missions(queue)
    registry = os.path.join(os.path.dirname(os.path.abspath(queue)), 'registry')
    ctx.target_dirs, ctx.registry_problem = load_target_dirs(registry)
    ctx.process_cwds, ctx.process_problem = scan_process_cwds()
    return ctx


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    path: str
    action: str               # REMOVE | KEEP
    reason: str
    detail: str = ''
    branch: Optional[str] = None

    def as_dict(self) -> dict:
        return {'path': self.path, 'action': self.action, 'reason': self.reason,
                'detail': self.detail, 'branch': self.branch}


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def mission_slug_of(wt_real: str, managed_root: str) -> Optional[str]:
    """`<managed_root>/<slug>/<name>[/...]` の <slug>。その形でなければ None。"""
    if not _within(wt_real, managed_root) or wt_real == managed_root:
        return None
    parts = Path(os.path.relpath(wt_real, managed_root)).parts
    if len(parts) < 2 or parts[0] in ('', '.', '..'):
        return None
    return parts[0]


def quarantine_root_of(managed_root: str) -> str:
    return os.path.join(managed_root, QUARANTINE_DIRNAME)


def _is_our_quarantine_entry(wt: Worktree, wt_real: str, managed_root: str) -> bool:
    """すでにこのツールが隔離した worktree か。

    **根拠は 2 つの一致 (族B: 対象の同定)** — パスが `.quarantine/` 配下「だけ」でも、lock の reason が
    このツールの印「だけ」でも判定しない。片方だけずれている (パスは隔離領域だが reason が無い/別物、
    または reason はこのツールの印だが `.quarantine/` の外) のは、通常の運用では起きない組み合わせで
    あり、そのまま「隔離済みで安全」と信用せず通常の分類に進ませる (何かを黙って見落とすより、
    もう一度普通に判定させる方が安全な向き)。
    """
    under_quarantine = _within(wt_real, quarantine_root_of(managed_root))
    marked = bool(wt.locked and wt.locked_reason and _parse_quarantine_reason(wt.locked_reason))
    return under_quarantine and marked


def classify(wt: Worktree, ctx: Context, is_main: bool = False) -> Verdict:
    """1 つの worktree を分類する。最初に当たった keep の理由を返し、全部通れば remove。

    **順序は安いものから**: 構造 (main / 管理外 / lock) → mission → 使用中 → git の中身。
    どの keep も「remove の根拠を 1 つ欠いた」という意味で、読めなかった・判定できなかったものは
    すべてここで keep に落ちる。
    """
    def keep(reason, detail=''):
        return Verdict(wt.path, KEEP, reason, detail, wt.branch)

    wt_real = os.path.realpath(wt.path)
    if is_main or wt_real == ctx.repo or wt.bare:
        return keep(R_MAIN)
    if _is_our_quarantine_entry(wt, wt_real, ctx.managed_root):
        return keep(R_QUARANTINED, '`--restore` で元に戻すか、`--list-quarantine` で一覧を見る')
    slug = mission_slug_of(wt_real, ctx.managed_root)
    if slug is None:
        return keep(R_OUTSIDE, f'{ctx.managed_root}/<mission>/<name> の外')
    if wt.locked:
        return keep(R_LOCKED)
    if wt.prunable or not os.path.isdir(wt.path):
        return keep(R_PRUNABLE, 'ディレクトリが無い (`git worktree prune` の対象)')

    # --- mission ---
    if ctx.active_missions is None:
        return keep(R_STATE_UNREADABLE, ctx.state_problem)
    if slug in ctx.active_missions:
        return keep(R_MISSION_ACTIVE, slug)
    mission_dir = os.path.join(ctx.queue, 'missions', slug)
    try:
        os.lstat(mission_dir)
    except FileNotFoundError:
        pass                                      # archive 済み (queue/missions に無い)
    except OSError as e:
        return keep(R_MISSION_UNOBSERVABLE, f'{mission_dir}: {e}')
    else:
        return keep(R_MISSION_NOT_ARCHIVED, f'{mission_dir} が残っている')

    # --- 使用中 ---
    if ctx.registry_problem:
        return keep(R_REGISTRY_UNREADABLE, ctx.registry_problem)
    for agent, target in ctx.target_dirs:
        if _within(target, wt_real):
            return keep(R_IN_USE_TARGET, f'{agent} の TARGET_DIR = {target}')
    if ctx.process_problem:
        return keep(R_PROCESS_SCAN_FAILED, ctx.process_problem)
    for pid, cwd in ctx.process_cwds:
        if _within(cwd, wt_real):
            return keep(R_IN_USE_PROCESS, f'pid {pid} の cwd = {cwd}')

    # --- git の中身 ---
    # `core.ignoreStat=true` は以後の checkout 等で触れたファイルへ自動で assume-unchanged を付ける。
    # 効いていれば、この時点で 1 件もフラグが無くても今後の編集が git status から見えなくなりうる
    # ので keep にする (t071 / PR#239 3巡目)。`git config` は system/global/local/worktree の実効値を
    # 見る (`--local` を付けない) — このツールが実際に信頼する git 呼び出しが読む値と一致させるため。
    rc, out, err = run_git(wt.path, 'config', '--bool', 'core.ignoreStat')
    if rc == 0:
        if out.strip() == 'true':
            return keep(R_IGNORE_STAT, 'core.ignoreStat=true (今後の編集が status から見えなくなりうる)')
    elif rc != 1:                                  # rc=1 は「未設定」(既定 false) — 失敗ではない
        return keep(R_CONFIG_CHECK_FAILED, (err or 'git を実行できない').strip()[:200])

    # assume-unchanged (小文字) / skip-worktree ('S') が付いた tracked ファイルへの編集は
    # `git status` に一切出ない。空の status は「変わっていない」を示さない (t071 / PR#239 3巡目)。
    rc, out, err = run_git(wt.path, 'ls-files', '-v')
    if rc != 0:
        return keep(R_LS_FILES_FAILED, (err or 'git を実行できない').strip()[:200])
    flagged = [line for line in out.splitlines() if line and (line[0].islower() or line[0] == 'S')]
    if flagged:
        return keep(R_INDEX_FLAGS, f'{len(flagged)} 件 (先頭: {flagged[0]})')

    rc, out, err = run_git(wt.path, 'status', '--porcelain=v1', '--untracked-files=all', '--ignored=matching')
    if rc != 0:
        return keep(R_STATUS_FAILED, (err or 'git を実行できない').strip()[:200])
    lines = out.splitlines()
    dirty_lines = [line for line in lines if not line.startswith('!! ')]
    if dirty_lines:
        return keep(R_DIRTY, f'{len(dirty_lines)} 件の変更 (先頭: {dirty_lines[0]})')
    ignored_lines = [line for line in lines if line.startswith('!! ')]
    if ignored_lines:
        return keep(R_IGNORED, f'{len(ignored_lines)} 件の ignored ファイル (先頭: {ignored_lines[0][3:]})')
    rc, out, err = run_git(wt.path, 'rev-list', '--max-count=1', 'HEAD', '--not', '--remotes=origin')
    if rc != 0:
        return keep(R_HEAD_UNRESOLVED, (err or 'git を実行できない').strip()[:200])
    if out.strip():
        return keep(R_UNPUSHED, f'origin に無いコミット {out.strip()[:12]} から')

    return Verdict(wt.path, REMOVE, R_REMOVE, '', wt.branch)


def list_worktrees(repo: str) -> tuple[Optional[list[Worktree]], str]:
    rc, out, err = run_git(repo, 'worktree', 'list', '--porcelain', '-z')
    if rc != 0:
        return None, (err or 'git を実行できない').strip()
    return parse_worktree_list(out), ''


def classify_all(repo: str, queue: str) -> tuple[list[Verdict], Optional[str]]:
    """`(verdict のリスト, worktree 一覧を取れなかった理由)`。"""
    worktrees, problem = list_worktrees(repo)
    if worktrees is None:
        return [], problem
    ctx = build_context(repo, queue)
    return [classify(wt, ctx, is_main=(i == 0)) for i, wt in enumerate(worktrees)], None


# ---------------------------------------------------------------------------
# --apply (隔離。削除しない)
# ---------------------------------------------------------------------------

def _quarantine_destination(managed_root: str, timestamp: str, wt_real: str) -> str:
    rel = os.path.relpath(wt_real, managed_root)
    return os.path.join(quarantine_root_of(managed_root), timestamp, rel)


def apply_quarantine(repo: str, queue: str, verdicts: list[Verdict]) -> list[dict]:
    """remove と判定したものを 1 件ずつ、**判定をやり直してから** 隔離する (削除しない)。

    `git worktree move` + `git worktree lock` だけを使う。`git worktree remove` / `git branch -d` /
    `rm -rf` は呼ばない — branch は移動した worktree にそのまま残る。隔離先が既に存在する・
    `git worktree move` 自体が失敗する、はどちらも削除にフォールバックせず `failed` にする (族A)。
    1 回の `--apply` で隔離したものは全部同じタイムスタンプの下に入る。
    """
    results = []
    managed_root = os.path.join(os.path.realpath(repo), '.claude', 'worktrees')
    # 秒精度だと、同じ元パスを 2 回に分けて隔離する 2 回の `--apply` が同じ秒に収まった瞬間、隔離先が
    # 文字列として一致し、2 回目が「隔離先が既に存在する」で failed になる (quarantine が世代として
    # 突き合わせる値である以上、衝突してはいけない。crewvia の `now_generation()` と同じ理由)。
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    for v in verdicts:
        if v.action != REMOVE:
            continue
        worktrees, problem = list_worktrees(repo)
        if worktrees is None:
            results.append({'path': v.path, 'status': 'skipped', 'detail': f'一覧を取れない: {problem}'})
            continue
        current = next((w for i, w in enumerate(worktrees) if i > 0 and w.path == v.path), None)
        if current is None:
            results.append({'path': v.path, 'status': 'skipped', 'detail': 'もう worktree 一覧に無い'})
            continue
        again = classify(current, build_context(repo, queue))
        if again.action != REMOVE:
            results.append({'path': v.path, 'status': 'skipped',
                            'detail': f'再判定で keep になった ({again.reason}: {again.detail})'})
            continue
        wt_real = os.path.realpath(current.path)
        dest = _quarantine_destination(managed_root, timestamp, wt_real)
        if os.path.exists(dest):
            # `git worktree move` は既存のディレクトリ (空でも) を「その中へ移す」— 上書きはしないが
            # 意図しない場所への配置になる。ここで先に拒否し、move 自体を呼ばない (族A)。
            results.append({'path': v.path, 'status': 'failed',
                            'detail': f'隔離先が既に存在するので隔離しない: {dest}'})
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except OSError as e:
            results.append({'path': v.path, 'status': 'failed', 'detail': f'隔離先を作れない: {e}'})
            continue
        rc, _out, err = run_git(repo, 'worktree', 'move', current.path, dest)
        if rc != 0:
            # submodule を含む worktree 等で move が失敗しうる。削除にフォールバックせず keep (族A)。
            results.append({'path': v.path, 'status': 'failed',
                            'detail': f'git worktree move: {(err or "実行できない").strip()[:300]}'})
            continue
        reason = _quarantine_reason(timestamp, wt_real)
        rc, _out, err = run_git(repo, 'worktree', 'lock', '--reason', reason, dest)
        if rc != 0:
            results.append({'path': v.path, 'status': 'failed',
                            'detail': (f'{dest} へ移動したが lock に失敗した (手で `git worktree lock` '
                                       f'すること): {(err or "実行できない").strip()[:300]}')})
            continue
        results.append({'path': v.path, 'status': 'quarantined', 'detail': dest})
    return results


# ---------------------------------------------------------------------------
# --restore / --list-quarantine
# ---------------------------------------------------------------------------

def find_quarantine_entries(repo: str) -> tuple[Optional[list[tuple[Worktree, str, str]]], str]:
    """`([(worktree, 隔離日時, 元の絶対パス), ...], 問題)`。`list_worktrees` が失敗したら `(None, 理由)`。"""
    worktrees, problem = list_worktrees(repo)
    if worktrees is None:
        return None, problem
    managed_root = os.path.join(os.path.realpath(repo), '.claude', 'worktrees')
    out = []
    for wt in worktrees:
        wt_real = os.path.realpath(wt.path)
        if not _is_our_quarantine_entry(wt, wt_real, managed_root):
            continue
        parsed = _parse_quarantine_reason(wt.locked_reason)
        out.append((wt, parsed[0], parsed[1]))
    return out, ''


def cmd_list_quarantine(repo: str, as_json: bool) -> int:
    entries, problem = find_quarantine_entries(repo)
    if entries is None:
        print(f'worktree 一覧を取れませんでした: {problem}', file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps([{'path': wt.path, 'quarantined_at': ts, 'original_path': orig}
                          for wt, ts, orig in entries], ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print('隔離されている worktree はありません')
        return 0
    for wt, ts, orig in entries:
        print(f'{wt.path}\n  隔離日時: {ts}\n  元の場所: {orig}')
    return 0


def cmd_restore(repo: str, given_path: str, as_json: bool) -> int:
    """隔離を元に戻す。引数は隔離先のパス、または隔離される前の元のパスのどちらでもよい。

    元の場所に既に何かあれば **上書きせず拒否する** (`git worktree move` は既存ディレクトリを
    「その中へ移す」ので、素通しすると誤配置になる)。
    """
    entries, problem = find_quarantine_entries(repo)
    if entries is None:
        print(f'worktree 一覧を取れませんでした: {problem}', file=sys.stderr)
        return 1
    given_real = os.path.realpath(given_path)
    by_quarantine_path = [e for e in entries if os.path.realpath(e[0].path) == given_real]
    by_original_path = [e for e in entries if os.path.realpath(e[2]) == given_real]
    candidates = by_quarantine_path or by_original_path
    if not candidates:
        print(f'{given_path}: 隔離エントリが見つかりません '
              f'(隔離先のパス、または隔離される前の元のパスを指定すること。--list-quarantine で一覧)',
              file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(f'{given_path}: 複数の隔離エントリが該当します。隔離先のパスを直接指定すること:', file=sys.stderr)
        for wt, ts, _orig in candidates:
            print(f'  {wt.path} (隔離日時: {ts})', file=sys.stderr)
        return 1
    wt, _ts, orig = candidates[0]
    if os.path.exists(orig):
        print(f'{orig}: 既に何かある。上書きしないので中止する (隔離先 {wt.path} は変更していません)',
              file=sys.stderr)
        return 1
    try:
        os.makedirs(os.path.dirname(orig), exist_ok=True)
    except OSError as e:
        print(f'{orig} の親ディレクトリを作れません (隔離先は変更していません): {e}', file=sys.stderr)
        return 1
    rc, _out, err = run_git(repo, 'worktree', 'unlock', wt.path)
    if rc != 0:
        print(f'{wt.path} の unlock に失敗しました (何も動かしていません): '
              f'{(err or "実行できない").strip()[:300]}', file=sys.stderr)
        return 1
    rc, _out, err = run_git(repo, 'worktree', 'move', wt.path, orig)
    if rc != 0:
        # unlock は済んだが move できなかった。保護を失った状態で放置しないよう再ロックを試みる
        # (ベストエフォート — 失敗してもその旨を報告するだけで、これ以上は何もしない)。
        relock_rc, _o, relock_err = run_git(repo, 'worktree', 'lock', '--reason', wt.locked_reason, wt.path)
        relock_note = '' if relock_rc == 0 else f' (再ロックにも失敗: {(relock_err or "").strip()[:200]})'
        print(f'{wt.path} を {orig} へ move できませんでした{relock_note}: '
              f'{(err or "実行できない").strip()[:300]}', file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({'restored': wt.path, 'to': orig}, ensure_ascii=False, indent=2))
    else:
        print(f'{wt.path} を {orig} へ復元しました')
    return 0


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def summarize(verdicts: list[Verdict]) -> dict:
    removes = [v for v in verdicts if v.action == REMOVE]
    keeps = [v for v in verdicts if v.action == KEEP]
    by_reason = Counter(v.reason for v in keeps)
    return {'total': len(verdicts), 'remove': len(removes), 'keep': len(keeps),
            'keep_by_reason': dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])))}


def format_text(verdicts: list[Verdict], applied: Optional[list[dict]], quiet: bool) -> str:
    lines = []
    if not quiet:
        for v in verdicts:
            detail = f'  ({v.detail})' if v.detail else ''
            lines.append(f'{v.action:<6} {v.reason:<26} {v.path}{detail}')
    s = summarize(verdicts)
    lines.append('')
    lines.append(f"worktree {s['total']} 件: remove {s['remove']} / keep {s['keep']}")
    for reason, n in s['keep_by_reason'].items():
        lines.append(f'  keep {reason}: {n}')
    if applied is None:
        lines.append('dry-run (何も変えていません)。remove と判定したものを隔離するには --apply')
    else:
        counts = Counter(r['status'] for r in applied)
        lines.append(f"apply: quarantined {counts.get('quarantined', 0)} / skipped {counts.get('skipped', 0)}"
                     f" / failed {counts.get('failed', 0)}")
        for r in applied:
            if r['status'] == 'quarantined':
                lines.append(f"  quarantined: {r['path']} -> {r['detail']}")
            else:
                lines.append(f"  {r['status']}: {r['path']} — {r['detail']}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='古い Worker worktree を安全に片付ける (既定は dry-run。--apply は削除ではなく隔離)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='remove と判定したものを隔離する (削除しない)')
    mode.add_argument('--restore', metavar='PATH', default=None,
                      help='隔離を元に戻す。PATH は隔離先・隔離される前の元のパスのどちらでもよい')
    mode.add_argument('--list-quarantine', action='store_true', help='隔離済みの worktree の一覧を出す')
    ap.add_argument('--repo', default='.', help='主 checkout (または任意の worktree) のパス。既定: カレント')
    ap.add_argument('--queue', default=None,
                    help='queue ディレクトリ。既定: $CREWVIA_QUEUE、無ければ <主 checkout>/queue')
    ap.add_argument('--fetch', action='store_true', help='判定の前に `git fetch --prune origin` する')
    ap.add_argument('--json', action='store_true', help='JSON で出力する')
    ap.add_argument('--quiet', action='store_true', help='1 件ずつの行を出さず集計だけ')
    args = ap.parse_args(argv)

    worktrees, problem = list_worktrees(args.repo)
    if worktrees is None or not worktrees:
        print(f'worktree 一覧を取れませんでした: {problem or "空"}', file=sys.stderr)
        return 1
    repo = worktrees[0].path            # 先頭は常に主 checkout

    if args.list_quarantine:
        return cmd_list_quarantine(repo, args.json)
    if args.restore is not None:
        return cmd_restore(repo, args.restore, args.json)

    queue = args.queue or os.environ.get('CREWVIA_QUEUE') or os.path.join(repo, 'queue')

    if args.fetch:
        rc, _out, err = run_git(repo, 'fetch', '--prune', 'origin')
        if rc != 0:
            print(f'git fetch --prune origin に失敗しました (判定は手元の ref のままにせず中止): '
                  f'{(err or "実行できない").strip()[:300]}', file=sys.stderr)
            return 1

    verdicts, problem = classify_all(repo, queue)
    if problem is not None:
        print(f'worktree 一覧を取れませんでした: {problem}', file=sys.stderr)
        return 1

    applied = apply_quarantine(repo, queue, verdicts) if args.apply else None

    if args.json:
        print(json.dumps({'summary': summarize(verdicts), 'applied': applied,
                          'worktrees': [v.as_dict() for v in verdicts]}, ensure_ascii=False, indent=2))
    else:
        print(format_text(verdicts, applied, args.quiet))
    failed = any(r['status'] == 'failed' for r in (applied or []))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
