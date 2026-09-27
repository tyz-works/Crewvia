#!/usr/bin/env python3
"""lib_trust.py — claude の「Do you trust this folder?」を start.sh が踏まないための検査 (t021 / backlog #28)。

## なぜ要るのか

claude は **信頼されていない cwd** で起動すると trust ダイアログを出す。既定の選択は
`No, exit`。start.sh は起動の直後に kickoff メッセージ + Enter を送るので、その Enter が
`No, exit` を選んで claude が終了し、kickoff の残りの文字列が pane のシェルに落ちる
(`syntax error near unexpected token '('`)。しかも start.sh の「着弾検証」は入力行の
`❯` を見ていて、ダイアログの選択カーソルも `❯` なので `Kickoff message sent (verified)` と
言っていた。**失敗が不可視だったことが症状の本体**である。PR3 で dispatcher が TARGET_DIR 付きの
起動コマンドを出すようになり、初めて使う TARGET_DIR を踏む機会が増えた。

対策は 2 枚:

1. **事前検査** (`check`): claude を起動する前に `~/.claude.json` を読み、cwd に trust が
   記録されているかを見る。無ければ起動せず止める。
2. **最後の網** (`dialog`): 1 をすり抜けた場合 (git worktree・未知の継承規則・版差) のために、
   kickoff を送る直前と送った後の画面にダイアログの文言があれば失敗として止める。

## trust の判定 (実測した挙動に合わせる)

* 記録は `~/.claude.json` の `projects["<絶対パス>"].hasTrustDialogAccepted` (`CLAUDE_CONFIG_DIR` が
  あればその下の `.claude.json`)。
* **信頼済みの祖先の配下ではダイアログが出ない** (実測: memory `target-dir-trust-and-settings-local`)。
  だから cwd 自身だけでなく、親 dir を `/` まで辿って 1 つでも `true` があれば信頼済み。
* claude 自身が trust を種まきするとき (runner) は、パスと NFC 正規化したパスと realpath を
  別々のキーとして書く。だから **論理パス (`cd && pwd`) と物理パス (`realpath`) の両方**、
  それぞれ NFC の形も候補にし、どれかの祖先に `true` があれば信頼済み。末尾スラッシュ・`..` は
  キー側も候補側も `normpath` で畳む。正規化は「一致を見つけやすくする」ためだけに使い、
  一致しない側へ倒す (= 止める) 材料には使わない。
* `true` の判定は `is True`。`"true"` (文字列)・`1`・`null` を truthiness で信頼済みにしない。

## 3 つの結果 (終了コード)

| 結果 | 終了コード | 意味 | start.sh の扱い |
|---|---|---|---|
| trusted      | 0  | cwd か祖先に `hasTrustDialogAccepted: true` がある | 起動する |
| untrusted    | 10 | 設定は読めて、どこにも `true` が無い (`~/.claude.json` が**無い**場合も含む: 何も信頼していないのが事実) | 止める |
| unverifiable | 11 | 設定が読めない (権限・I/O・種類)・JSON でない・形が違う | **止める** |

**unverifiable を「信頼済み」に潰さない**。倒す向きは「止める」側: 進めた場合の被害は Worker の即死と
シェルへの文字列漏れ、止めた場合の被害は利用者が 1 行直すこと。ファイルが壊れているなら claude 自身も
同じ設定を読めないので、進んでも trust は確認できない。判断が割れる余地を残さないため、`~/.claude.json`
を crewvia が読み違えたときの退避用の env スイッチは付けない (trust は利用者の判断で、利用者が
`hasTrustDialogAccepted` を立てれば通る)。

一致した祖先とは別の、cwd に関係する記録が壊れていて (`projects[<path>]` が dict でない・
`hasTrustDialogAccepted` が bool でない) かつ他の祖先にも `true` が無い場合も unverifiable
(壊れた記録が「信頼しない」という意味なのか、読み違えなのかを決められない)。関係ない
プロジェクトの記録が壊れていても影響しない。

**`~/.claude.json` は書き換えない。** trust は利用者の判断で、crewvia が代わりに立てない。
止めるときは、利用者が `!` で打てるコマンドを出す。

## CLI (start.sh が使う)

    lib_trust.py check <dir>    # 信頼済みなら無出力で 0。それ以外は端末向けの説明を stdout に出して 10 / 11
    lib_trust.py dialog         # stdin (pane の capture) に trust ダイアログの文言があれば 0 / 無ければ 1 / 読めなければ 2
"""

import os
import shlex
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_daemon_state import load_json_store  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402

TRUSTED = 0
UNTRUSTED = 10
UNVERIFIABLE = 11


def claude_json_path(env=None):
    """claude が trust を記録するファイル。`CLAUDE_CONFIG_DIR` があればその下 (claude 本体と同じ規則)。"""
    env = os.environ if env is None else env
    cfg = env.get('CLAUDE_CONFIG_DIR')
    if cfg:
        return os.path.join(cfg, '.claude.json')
    home = env.get('HOME') or os.path.expanduser('~')
    return os.path.join(home, '.claude.json')


def _norm(path):
    return os.path.normpath(unicodedata.normalize('NFC', path))


def candidate_dirs(directory):
    """cwd として claude に見えうるパス (論理 / 物理、それぞれ NFC) を、重複なしで順に返す。"""
    logical = os.path.abspath(directory)
    physical = os.path.realpath(directory)
    out = []
    for p in (logical, physical):
        for form in (p, unicodedata.normalize('NFC', p)):
            n = os.path.normpath(form)
            if n not in out:
                out.append(n)
    return out


def ancestors(path):
    """`path` 自身から `/` まで (両端を含む)。"""
    out = []
    cur = path
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


class Verdict:
    __slots__ = ('status', 'target', 'config', 'config_exists', 'via', 'reason')

    def __init__(self, status, target, config, via=None, reason='', config_exists=True):
        self.status = status
        self.target = target
        self.config = config
        #: ENOENT のときだけ False (`Path.exists()` は EACCES も False に潰すので使わない)。
        self.config_exists = config_exists
        self.via = via
        self.reason = reason


def _config_problem(data):
    """`load_json_store` の `check`: `projects` は object でなければ使えない (無いのは「1 件も無い」)。"""
    projects = data.get('projects')
    if projects is not None and not isinstance(projects, dict):
        return f"'projects' が object ではありません ({type(projects).__name__})"
    return None


def judge(directory, config_file=None, env=None):
    """`directory` を cwd にして claude を起動したとき、trust ダイアログが出ないか。"""
    config = config_file or claude_json_path(env)
    target = os.path.realpath(directory)

    # 読み取りの入口は lib_daemon_state.load_json_store (通常ファイルか・JSON か・最上位が object か・
    # `check` を通るか)。ENOENT だけが「本当に無い」で、それ以外の失敗は unverifiable。
    data = load_json_store(config, check=_config_problem, warn=lambda m: None)
    if is_missing(data):
        return Verdict(UNTRUSTED, target, config, config_exists=False,
                       reason=f'{config} が存在しません (どの dir も信頼済みとして記録されていない)')
    if is_unreadable(data):
        return Verdict(UNVERIFIABLE, target, config,
                       reason=f'{config} を読めない / 形が使えません: {data.reason}')

    projects = data.get('projects') or {}

    entries = {}
    for key, entry in projects.items():
        entries.setdefault(_norm(key), []).append((key, entry))

    malformed = None
    for cand in candidate_dirs(directory):
        for anc in ancestors(cand):
            for key, entry in entries.get(anc, ()):
                if not isinstance(entry, dict):
                    malformed = malformed or (
                        f"projects[{key!r}] が object ではありません ({type(entry).__name__})")
                    continue
                if 'hasTrustDialogAccepted' not in entry:
                    continue
                value = entry['hasTrustDialogAccepted']
                if value is True:
                    return Verdict(TRUSTED, target, config, via=key)
                if value is not False:
                    malformed = malformed or (
                        f"projects[{key!r}].hasTrustDialogAccepted が bool ではありません ({value!r})")
    if malformed:
        return Verdict(UNVERIFIABLE, target, config,
                       reason=f'{config}: {malformed}。他の祖先にも true が無いので信頼済みとは言えません')
    return Verdict(UNTRUSTED, target, config,
                   reason=(f"{config} の projects に、この dir にも親 dir にも "
                           f"hasTrustDialogAccepted: true の記録がありません"))


def refusal_message(verdict, directory):
    """止めるときに端末とログへ出す説明。利用者が `!` で打てるコマンド付き。"""
    q = shlex.quote
    target = verdict.target
    lines = []
    if verdict.status == UNVERIFIABLE:
        lines += [
            f'[crewvia] ERROR: {directory} の trust を確認できないので、claude を起動しません。',
            f'          理由: {verdict.reason}',
            '          読めない状態を「信頼済み」とは扱いません (進めると、信頼されていない場合に kickoff の Enter が',
            '          trust ダイアログの既定 "No, exit" を選んで Worker が即終了します)。',
            f'          {verdict.config} を直してから起動し直してください。crewvia は書き換えません。',
        ]
        return '\n'.join(lines)

    lines += [
        f'[crewvia] ERROR: {directory} は claude に信頼されていないので、起動しません。',
        f'          理由: {verdict.reason}',
        '          このまま起動すると claude の trust ダイアログ (既定 "No, exit") を kickoff の Enter が選び、',
        '          Worker は即終了して残りの文字列がシェルに落ちます。',
        '          trust は利用者の判断です。crewvia は ~/.claude.json を書き換えません。',
        '          信頼してよい dir なら、次のどちらかを実行してから起動し直してください:',
        f'            ! cd {q(target)} && claude      # ダイアログで "Yes, I trust this folder" を選び、/exit',
    ]
    if verdict.config_exists:
        cfg = q(verdict.config)
        lines.append(
            "            ! tmp=$(mktemp) && jq --arg k " + q(target)
            + " '.projects[$k].hasTrustDialogAccepted = true' " + cfg
            + ' > "$tmp" && chmod 600 "$tmp" && mv "$tmp" ' + cfg)
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 最後の網: pane の画面に trust ダイアログが出ているか
# ---------------------------------------------------------------------------
#
# 文言は claude 2.1.283 のバンドルで確認したもの (`Quick safety check: Is this a project you
# created or one you trust?` / `Yes, I trust this folder` / `No, exit`) と、それ以前の版の
# `Do you trust the files in this folder?`。画面は折り返されるので、空白を畳んでから見る。
# 単独の `No, exit` は他の画面にも出うるので、決定の操作案内 (`Enter to confirm`) と揃ったときだけ数える。

_DIALOG_PHRASES = (
    'quick safety check',
    'yes, i trust this folder',
    'do you trust the files in this folder',
    'is this a project you created or one you trust',
)


def screen_shows_trust_dialog(screen):
    flat = ' '.join(str(screen).lower().split())
    if any(p in flat for p in _DIALOG_PHRASES):
        return True
    return 'no, exit' in flat and 'enter to confirm' in flat


def main(argv):
    if len(argv) >= 3 and argv[1] == 'check':
        directory = argv[2]
        verdict = judge(directory)
        if verdict.status == TRUSTED:
            return TRUSTED
        print(refusal_message(verdict, directory))
        return verdict.status
    if len(argv) == 2 and argv[1] == 'dialog':
        try:
            screen = sys.stdin.buffer.read().decode('utf-8', errors='replace')
        except OSError as e:
            print(f'[lib_trust] cannot read stdin: {e}', file=sys.stderr)
            return 2
        return 0 if screen_shows_trust_dialog(screen) else 1
    print('usage: lib_trust.py check <dir> | dialog  (stdin: pane capture)', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
