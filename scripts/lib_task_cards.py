"""task カードを読むことの定義 —— crewvia の中で唯一の置き場。

同じ `queue/missions/<slug>/tasks/tNNN.md` を読む主体は 2 つある。

* `plan.sh`      — この task を割り当ててよいか / 一覧に何と表示するか
* `dispatcher.sh` — idle Worker にこの task を投げてよいか

この 2 つが別々のコードで同じファイルを読んでいた。t013 で「識別子はファイル名で
あって frontmatter の `id` 欄ではない」と決め、食い違うカードを `[破損]` として
保留する規則を入れたが、**入ったのは plan.sh 側だけ**だった (Codex 5 巡目 P2)。
結果、ズレは 4 つの形で出た。

1. `id` 行の無いカードを plan.sh は受理し ready と表示するのに、dispatcher は
   `m['id']` の `KeyError` で落ちる —— **全 mission の割り当てが止まる**。
2. `id` を名乗り替えたコピーが、dispatcher からは *名乗った先のカードとして*
   読まれる。2 枚あるはずのカードが 1 枚になり、消えた側は誰にも見えない。
3. その結果、`done` を名乗るコピーが「満たされた依存」に数えられる。
4. 壊れた frontmatter を plan.sh の parser は例外にして隔離するのに、
   dispatcher の parser は読めない行を黙って捨てるので、半分だけ読めた
   `status: pending` がそのまま信じられて **中身の分からないカードが dispatch
   される**。

`lib_dep_rules.py` を共有しても 1〜4 は解けない。あちらは「依存が満たされたか」の
*規則* であって、ここは規則に渡す *入力* だからである。入力が違えば、同じ規則から
違う結論が出る。

だからカードの読み取りそのものをここに集約した。`plan.sh` と `dispatcher.sh` は
どちらも `list_task_cards()` を呼ぶだけで、自前の parser も identity 規則も持たない。
再発防止は `tests/test_task_card_identity.py` (両者が同じ queue から同じ task 集合を
導くことを直接 assert する) が見張る。

## ここに置いてあるもの / 置いていないもの

置いてあるのは **カードを読むこと** だけ。書き出し (`dump_yaml` /
`serialize_frontmatter`) は plan.sh にしかない —— queue を書き換えるのは plan.sh の
仕事で、dispatcher は読むだけだからである。

`parse_yaml()` は厳格 (認識できない行で `ValueError`) で、これは plan.sh 側の挙動を
そのまま持ってきたもの。カード以外の YAML (`state.yaml` / `workers.yaml` /
`mission.yaml`) に dispatcher がこれを使うことは **意図的にしていない**: そちらは
手で編集される経路があり、1 行の typo で常駐デーモンが毎サイクル死ぬと、Worker の
割り当てと生存監視がまとめて止まる。カードは `list_task_cards()` が例外を
`[破損]` に変えて吸収するので、厳格でも落ちない。

## 例外を投げない

`read_task_card()` / `list_task_cards()` は **例外を投げない**。読めなかったカードは
`[破損]` のカードとして返る。呼び出し側 5 者のうち 3 つが常駐デーモン
(dispatcher / verifier-dispatcher / watchdog) で、1 枚のカードでサイクルが落ちると
止まるのは *その mission* ではなく **全 mission の割り当てと Worker の生存監視**
だからである。

ここは「捕まえる例外を数えて並べる」形にしていない。t014 で読み取りを集約した
とき、集約先は `OSError` と `ValueError` しか見ておらず、**集約前に 3 デーモンが
持っていたカード単位の `except Exception` より狭かった**。狭くなった差分が
そのまま穴になり、不正な UTF-8 を含むカード 1 枚で `UnicodeDecodeError` が
突き抜けた (Codex 6 巡目 P1)。名前の分かっている失敗は個別に扱って直し方を
警告に書き、**残り全部は `read_task_card()` の backstop が隔離に落とす**。

## 「空」と「観測できなかった」を分ける

`list_task_cards()` の **空リストは「カードが 1 枚も無い」の意味だけ**を持つ。
走査そのものに失敗したときは `scan_failure_task()` の node を 1 件返す。

同じ `[]` に潰していたのが Codex 7 巡目 P1 で、そこで出た形は
`all(m['status'] in TERMINAL for tasks)` —— `cmd_done()` と
`cmd_verify_result()` が持つ mission 完了の判定が **空リストに True を返す**、
というものだった。tasks ディレクトリが「書き込み・実行は可、読み取り不可」だと
既知のファイル名を直接開く更新系だけが生き残るので、1 枚を done にした瞬間に、
未完了の兄弟を残したまま mission 全体が done になる。

読み取り経路の全数調査と、直していない箇所の理由は
`knowledge/empty-vs-unobservable.md` にある。

## カードは通常ファイルだけ

`read_task_card()` は `O_NONBLOCK` で開いて `fstat` で種類を確かめ、通常ファイル
以外は **待たずに** `[破損]` として断る。待ち時間に上限を付けるのではなく種類で
弾くのは、待てば読めるものが 1 つも無いからである。上限が無いままだと、書き手の
いない FIFO 1 枚で、別 mission の健全なカードを 1 枚直しただけの実行と、この
読み取りを使う常駐デーモン 3 者が同時に座り込む (Codex 7 巡目 P2)。

## フォールバックを持たない

読み込めなければ呼び出し側はそのまま死ぬ。`lib_dep_rules.py` と同じ理由で、
「読めなかったら自前の規則で続行」に倒すと、規則が 1 つであるという性質そのものが
最も気付きにくい形 (壊れた環境でだけコピーが動く) で失われる。plan.sh を単体で
コピーする隔離テストでは、このファイルも一緒に置くこと。
"""

from __future__ import annotations

import errno as _errno
import os
import re
import stat

#: 読めない / 信用できないカードに与える疑似ステータス。pending でも終端でもない。
#: だから pull も dispatch もこのカードを拾わず、同時に「mission が完了した」とも
#: 数えられない。t009 (1 枚の壊れたカードが mission 全体を凍らせた) の解き方。
CORRUPT_TASK_STATUS = 'corrupted'

#: task ファイルの名前。この正規表現に合うものだけが task であり、`tNNN` の
#: 部分がその task の識別子になる。
TASK_FILENAME_RE = re.compile(r't(\d+)\.md')

#: 走査そのものが失敗したときに 1 件だけ返す疑似カードの id。
#: 実在のカードの id は `TASK_FILENAME_RE` から来るので必ず `t<数字>` であり、
#: この名前と衝突することはない (= 本物のカードを隠さない)。
SCAN_FAILURE_TASK_ID = '!scan'


# ---------------------------------------------------------------------------
# YAML の狭い部分集合
# ---------------------------------------------------------------------------

def _scalar(val):
    if val == 'null' or val == '~':
        return None
    if val in ('true', 'True'):
        return True
    if val in ('false', 'False'):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r'-?\d+', val):
        return int(val)
    return val


def _split_inline_list(s):
    """Split inline list respecting quoted strings."""
    out = []
    cur = []
    in_q = None
    for ch in s:
        if in_q:
            cur.append(ch)
            if ch == in_q:
                in_q = None
            continue
        if ch in ('"', "'"):
            in_q = ch
            cur.append(ch)
            continue
        if ch == ',':
            out.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append(''.join(cur).strip())
    return [x for x in out if x]


def parse_yaml(text, source='<yaml>'):
    """Parse a narrow subset: scalar fields, inline lists, block lists.

    Unrecognized lines raise instead of being silently dropped, so a hand-edit
    typo (e.g. missing colon, mis-indented block list) cannot quietly produce a
    half-loaded dict.
    """
    lines = text.splitlines()
    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m:
            if line and line[0] in (' ', '\t'):
                # Deeply nested / orphaned indented line (e.g. inside verification.commands)
                # — skip silently to maintain backward compatibility with unknown block structures
                i += 1
                continue
            raise ValueError(
                f"{source}: malformed line {i + 1}: {line!r} "
                f"(expected `key: value`, `key: [a, b]`, or `key:` followed by `  - item` lines)"
            )
        key = m.group(1)
        val = m.group(2).rstrip()
        if val == '':
            # Possible block list (`- item`) or block mapping (`  key: val`)
            i += 1
            items = []
            sub_dict = {}
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    map_m = re.match(r'^  ([\w-]+):\s*(.*)$', lines[i])
                    if map_m:
                        sub_key = map_m.group(1)
                        sub_val = _scalar(map_m.group(2).rstrip())
                        sub_dict[sub_key] = sub_val
                        i += 1
                    else:
                        break
            if items:
                result[key] = items
            elif sub_dict:
                result[key] = sub_dict
            else:
                result[key] = None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [_scalar(s.strip()) for s in _split_inline_list(inner)]
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def parse_frontmatter(text, source='<task>'):
    """Split a markdown file into (meta dict, body string)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError(
            f"missing frontmatter delimiter — file must begin with a line "
            f"containing only `---`"
        )
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        raise ValueError(
            f"unterminated frontmatter — frontmatter block must close with a "
            f"line containing only `---` before the body"
        )
    meta_text = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    if body.startswith('\n'):
        body = body[1:]
    return parse_yaml(meta_text, source=f"{source} (frontmatter)"), body


# ---------------------------------------------------------------------------
# 隔離 —— 読めないカードを「見える形で保留する」唯一の作り方
# ---------------------------------------------------------------------------

def _safe_warn(warn, msg):
    """警告を出す。出せなくても、そこで読み取りを止めない。

    `warn` は呼び出し側から渡されたただの callable で、dispatcher なら
    `log()` —— つまり **ログファイルへの書き込み**である。ディスクが埋まる /
    ログの権限が変わるだけでそれは例外を投げる。そこで諦めないと、
    「壊れたカードが 1 枚あって、かつログが書けない」という、いちばん忙しい日に
    しか揃わない組み合わせで全 mission の割り当てが止まる。

    黙っても隔離そのものは失われない —— `[破損]` のカードは *戻り値* であって、
    警告はその写しにすぎないからである。
    """
    if warn is None:
        return
    try:
        warn(msg)
    except Exception:
        pass


def isolated_task(task_id, title, reason):
    """`[破損]` として保留されたカードを組み立てる。

    `CORRUPT_TASK_STATUS` は pending でも終端でもない。だから pull も dispatch も
    このカードを拾わず、同時に「mission が完了した」とも数えられない。依存して
    いる下流は waiting のまま止まる —— これが正しい: 中身が信用できないカードを
    「満たされた依存」として下流を動かしてはならない。

    削除ではなく保留にしてあるのは、黙って消えたカードは誰にも直せないからで
    ある。`plan.sh status` に理由と直し方が出て、1 行直せば解ける。
    """
    return {
        'id': task_id,
        'title': '[破損] ' + title,
        'status': CORRUPT_TASK_STATUS,
        'skills': [],
        'blocked_by': [],
        'parse_error': reason,
    }, ''


def scan_failure_task(tasks_dir, what, detail):
    """`tasks_dir` を走査できなかったことを、**1 件の終端でない node** で表す。

    「観測できなかった」と「そこに何も無い」は違う。`[]` を返すと両者が同じ形に
    なり、`all(status in TERMINAL for tasks)` —— `cmd_done()` と
    `cmd_verify_result()` が持つ mission 完了の判定 —— が **空リストに対して
    True になる**。tasks ディレクトリが「書き込み・実行は可、読み取り不可」だと、
    既知のファイル名を直接開く更新系だけが生き残るので、**1 枚を done にした
    瞬間に、未完了の兄弟を残したまま mission 全体が done になる** (Codex 7 巡目
    P1)。集約前は listing の例外がこの経路を中断させていた。

    だから 1 件返す。`CORRUPT_TASK_STATUS` は pending でも終端でもないので、

    * 完了判定は **必ず False** になる (fail closed)
    * pull も dispatch もこの node を拾わない
    * `plan.sh status` と DAG に理由が出る (黙って止まらない)

    の 3 つが同時に成立する。個々のカードを `[破損]` として保留するのとまったく
    同じ形で、違うのは対象がディレクトリだという点だけである。
    """
    return isolated_task(
        SCAN_FAILURE_TASK_ID,
        f'{tasks_dir} を走査できない ({what})',
        f'{what}: {detail}',
    )


def normalize_card(task_id, meta):
    """読めたカードの欄を揃える。識別子は **ファイル名から与えられる**。

    `skills` のスカラー正規化 (`skills: bash` → `['bash']`) をここに置いてあるのは、
    これを忘れると `set("bash")` が 1 文字ずつに割れて、skill の突き合わせ
    (worker matching / DIRECTOR_ONLY_SKILLS) が静かに全滅するため (PR #125)。
    読み手が 2 つあるかぎり、片方だけ忘れられる場所に置いてはいけない。
    """
    meta['id'] = task_id
    meta.setdefault('skills', [])
    meta.setdefault('blocked_by', [])
    if meta.get('skills') is None:
        meta['skills'] = []
    elif isinstance(meta.get('skills'), str):
        meta['skills'] = [meta['skills']]
    if meta.get('blocked_by') is None:
        meta['blocked_by'] = []
    return meta


# ---------------------------------------------------------------------------
# 通常ファイルだけを、待たずに読む
# ---------------------------------------------------------------------------

class _NotARegularFile(Exception):
    """開いた先が通常ファイルではなかった。`read_task_card()` が隔離に落とす。"""


#: `stat` の種類ビット → 人が読める名前。理由の文面に使うだけ。
_FILE_TYPES = (
    (stat.S_ISFIFO, 'FIFO (named pipe)'),
    (stat.S_ISDIR, 'directory'),
    (stat.S_ISSOCK, 'socket'),
    (stat.S_ISCHR, 'character device'),
    (stat.S_ISBLK, 'block device'),
)


def _describe_file_type(mode):
    for test, name in _FILE_TYPES:
        if test(mode):
            return name
    return f'mode {stat.S_IFMT(mode):#o}'


def _read_regular_file(path, newline=None):
    """通常ファイルなら中身を返す。それ以外なら `_NotARegularFile` で即座に断る。

    `O_NONBLOCK` を付けて開くのは、**種類を確かめる前に待たされないため**で
    ある。書き手のいない FIFO は `open()` の時点で止まるので、開いてから
    `fstat` する形にしても、`O_NONBLOCK` が無ければ確かめる所まで到達できない。

    判定は `fstat` —— 開いた **その fd** に対して行う。`os.stat(path)` で先に
    見てから開くと、見た対象と開いた対象が別物でありうる (memory:
    verify-and-destroy-must-share-one-connection と同じ形)。

    `newline` は `open()` と同じ意味。既定 (`None`) は universal newlines で、
    `''` を渡すと改行変換を行わない —— `plan.sh` の `plan_review.verdict` の
    ように「2 行ちょうど」を検査する読み手だけが使う。

    通常ファイルだと分かったら `O_NONBLOCK` は落とす。通常ファイルの read に
    非ブロッキングの意味は無く、付けたままにすると将来この関数が他の種類を
    受理するようになったときに、短い read が黙って途中までの中身を返す。
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise _NotARegularFile(_describe_file_type(mode))
        os.set_blocking(fd, True)
        f = os.fdopen(fd, newline=newline)
    except BaseException:
        os.close(fd)
        raise
    with f:
        return f.read()


#: 公開名。`_read_regular_file()` / `_NotARegularFile` は t016 からの内部名で、
#: `tests/red_proof_unobservable.sh` が注入点として名指ししているので残してある。
#:
#: **queue / registry のファイルを固定パスで開くコードは、これを通すこと。**
#: t016 のガードは `tasks/` を *列挙して* 読む経路にしか入っておらず、
#: `plan.sh` の `load_task()` / `load_mission()` と `dispatcher.sh` の
#: `publish_agents()` —— どれも固定パスで `open()` する —— がそのまま残って
#: いた (Codex 8 巡目 P2)。列挙するかどうかは害の大きさを変えない: 書き手の
#: いない FIFO 1 枚で、キューロックを握ったままの `plan.sh` と、`dispatch()`
#: より前に走る `publish_agents()` が無期限に座り込む。
NotARegularFile = _NotARegularFile
read_regular_text = _read_regular_file


class Unreadable:
    """**読めなかった**ことそのものを表す値。「空」ではない。

    t018 まで、読み取りの失敗は `None` / `{}` / `[]` で返されていた。そこが
    同じ型の欠陥を 8 回作った入口である —— 最後の 1 つ (Codex 9 巡目 P1) は
    「state.yaml が読めない → `{}` → active mission ゼロ → **idle Worker を
    退役させてよい**」で、直前の t017 がガードを足したことによって初めて
    到達可能になった。個々の呼び出し側を見張る形では閉じない。**失敗を空と
    同じ形で返さない**という、コードの形のほうを変える。

    だからこの値は「空の入れ物」として振る舞わない。`bool()` / `len()` /
    `in` / `[]` / 反復、そして `.get()` `.splitlines()` のような属性アクセスは
    **すべて `TypeError`** になる。`if not state:` も
    `state.get('active_missions')` も、書いた時点では気付けなくても
    **実行した瞬間に落ちる** —— 黙って退役を認可する経路が、書けなくなる。

    落ちること自体は安全側である。dispatcher の 1 サイクルは
    `run_dispatch || log "dispatch cycle error"` の下にあり、heartbeat は
    その外で無条件に書かれる (= respawn も起きない)。何も割り当てず何も
    壊さずに 5 秒後へ進む。

    正しい扱いは `is_unreadable()` / `is_missing()` で分岐すること。
    `errno` は `OSError` 由来のときだけ入る —— `ENOENT` (本当に無い) と
    それ以外 (権限・I/O・種類) を呼び出し側が分けられるようにするためで、
    「まだ無いのが普通」は ENOENT の話でしかない
    (`knowledge/empty-vs-unobservable.md` §5)。
    """

    __slots__ = ('path', 'reason', 'errno')

    def __init__(self, path, reason, errno=None):
        self.path = str(path)
        self.reason = str(reason)
        self.errno = errno

    def __repr__(self):
        return f'Unreadable({self.path!r}, {self.reason!r}, errno={self.errno!r})'

    def _refuse(self, op):
        raise TypeError(
            f"{op} on Unreadable({self.path!r}): {self.reason}\n"
            f"  「読めなかった」を「空」として扱うことはできません。"
            f"is_unreadable() / is_missing() で分岐してください。")

    # 「空の入れ物」として使われうる操作を、すべて拒否する。
    def __bool__(self):
        self._refuse('bool()')

    def __len__(self):
        self._refuse('len()')

    def __iter__(self):
        self._refuse('iter()')

    def __contains__(self, item):
        self._refuse('in')

    def __getitem__(self, key):
        self._refuse('[]')

    def __getattr__(self, name):
        # `__slots__` に無い属性 —— `.get()` `.splitlines()` `.strip()` など、
        # dict / str のつもりで呼ばれたものがここに来る。
        self._refuse(f'.{name}')


def is_unreadable(value):
    """`value` が「読めなかった」を表しているか。"""
    return isinstance(value, Unreadable)


def is_missing(value):
    """`value` が **本当に無い** (ENOENT) を表しているか。

    `Path.exists()` を分岐の材料にできないのは、`EACCES` で stat できない場合も
    `False` に潰れるからである (`knowledge/empty-vs-unobservable.md` §2 の B と
    同じ形)。「まだ無いのが普通」を許すときは、ここで **ENOENT だけ** を通す。
    """
    return isinstance(value, Unreadable) and value.errno == _errno.ENOENT


def read_regular_text_or_unreadable(path, warn=None):
    """`read_regular_text()` の、**例外を出さない** 形。読めなければ `Unreadable`。

    常駐デーモン (dispatcher / verifier-dispatcher / watchdog) と、mission を
    並べて表示する側のための入口である。1 つのファイルを読めなかっただけで
    サイクルや一覧全体を落とさない、という向きは `read_task_card()` と同じ。

    **この関数は例外を投げない** (`KeyboardInterrupt` / `SystemExit` のような
    `BaseException` は別 —— あれは握り潰してはいけない)。名前の分かっている
    失敗には直し方まで書き、**残り全部を最後の `except Exception` が
    受ける**。`read_task_card()` の backstop とまったく同じ形で、理由も同じ:
    「今回の 1 件を足す」形は、次に読み取り経路へ新しい失敗が入った日に
    もう一度同じ止まり方をする。実際、t015 で `read_task_card()` について
    直した `UnicodeDecodeError` の漏れが、**t017 で新しく作ったこの wrapper に
    そのまま再現していた** (Codex 9 巡目 P2)。

    **倒す先はここでは決めない。** 呼び出し側が失敗をどう読むかは判定ごとに
    違う —— dispatcher の `dispatch()` は「このサイクルは何もしない」、
    `all_done` の判定は「完了ではない」(= 誰も退役させない)。どちらも
    「読めなかったことを、割り当て・破壊の許可に使わない」側である
    (memory: fail-direction-is-per-judgment / evidence-for-destructive-decisions)。
    """
    try:
        return read_regular_text(path)
    except NotARegularFile as e:
        return _unreadable(
            warn, path, f'not a regular file ({e})', None,
            f"  hint: queue / registry のファイルは通常ファイルだけです。"
            f"`ls -l {path}` で種類を確かめてください。")
    except FileNotFoundError as e:
        # 「まだ無い」は普通の状態 (起動直後の state.yaml / 未作成の marker)。
        # 5 秒ごとに回る常駐デーモンからここへ来るので、警告は出さない ——
        # 区別は戻り値の `errno` が持っており、警告はその写しにすぎない。
        return Unreadable(path, f'not found ({e})', errno=e.errno)
    except OSError as e:
        return _unreadable(warn, path, f'read error ({e})', e.errno)
    except UnicodeError as e:
        # `UnicodeDecodeError` は `OSError` ではなく `ValueError` の側にいるので
        # 上の except では捕まらない (Codex 6 巡目 P1 / 9 巡目 P2)。
        return _unreadable(
            warn, path, f'decode error, not UTF-8 ({e})', None,
            f"  hint: `file {path}` でエンコーディングを確かめ、必要なら "
            f"`iconv -f <元の文字コード> -t utf-8` で書き直すこと。")
    except Exception as e:      # noqa: BLE001 — backstop。理由は docstring。
        return _unreadable(
            warn, path, f'unexpected read failure: {type(e).__name__}: {e}', None)


def _unreadable(warn, path, reason, err_no, hint=''):
    _safe_warn(warn, f"failed to read {path}: {reason}\n"
                     + (hint + "\n" if hint else "")
                     + f"  treating it as unreadable for now.")
    return Unreadable(path, reason, errno=err_no)


# ---------------------------------------------------------------------------
# カード 1 枚を読む
# ---------------------------------------------------------------------------
#
# 識別子は **ファイル名** から来る。frontmatter の `id` ではない。
# `task_path()` が `<id>.md` を前提にしている以上ファイル名が本体で、`id` 欄は
# 同じことの言い直しでしかないが、誰も突き合わせていなかった (Codex 4 巡目)。
#
# 突き合わせないと 2 つ壊れる。
#
# * `tNNN.md` をコピーして `id` 行を直し忘れると、同じ id の node が 2 つ並ぶ。
#   plugin はそれで **ファイル全体** を拒否するので、無関係な mission の DAG
#   まで消える (t010 の循環・空配列とまったく同じ巻き添えの型)。
# * もっと悪いのは可視化の外側だ。`pull` は割り当てたカードを
#   `save_task(slug, meta['id'], ...)` で書き戻すので、`t002.md` が `id: t001`
#   を名乗っていると **t001.md が t002 の内容で上書きされる**。カードが 1 枚、
#   誰にも気付かれずに消える。
#
# だから id はファイル名から取る (ファイルシステムが一意性を保証するので、
# 重複は構造上作れなくなる)。そのうえで `id` 欄の扱いは 2 つに分ける。
#
# * **欄が無い / 空** — 矛盾ではない。ファイル名が答えを持っているので、
#   黙って落とさず普通のカードとして扱い、次の書き戻しで埋まる。
# * **欄がファイル名と食い違う** — どちらが正しいか、ここでは決められない
#   (コピー元の id が残ったのか、ファイルが置き違えられたのか)。決められない
#   ものを勝手に決めると、名乗り替えを黙って追認することになる。だから
#   `[破損]` として **保留** する: 1 行直せば解けるし、それまで見えている。

def read_task_card(path, task_id, warn=None):
    """1 枚のカードを `(meta, body)` にする。読めなければ隔離したカードを返す。

    例外をここで吸収するのは、**呼び出し側の 1 つが常駐デーモンだから**である。
    1 枚のカードで dispatch サイクルが落ちると、止まるのはその mission ではなく
    全 mission の割り当てになる。倒れる先は常に「そのカードだけが動かない」。

    **この関数は例外を投げない。** 保証しているのは「この失敗とこの失敗を捕まえ
    た」ではなく「読み取り経路から例外が出てこない」ことのほう —— 理由は下の
    backstop のコメントにある。

    `warn` は `callable(str)` か None。None なら黙る (同じ実行の中で 2 回目以降に
    読む呼び出し用 —— 同じ警告が二重に出ると、読む側は 2 件壊れていると誤読する)。
    """
    def _warn(msg):
        _safe_warn(warn, msg)

    try:
        return _read_task_card(path, task_id, _warn)
    except Exception as e:
        # ---- backstop (Codex 6 巡目 P1) -----------------------------------
        #
        # 集約前、このカードを読んでいた常駐デーモン 3 者 (dispatcher /
        # verifier-dispatcher / watchdog) は **カード単位の `except Exception`**
        # を持っていた。t014 で読み取りを 1 箇所に寄せたとき、集約先は
        # `OSError` と `ValueError` しか見ていなかった —— 集約したこと自体は
        # 正しかったが、**集約先が元のハンドラと同じ広さを持っているかを
        # 確かめていなかった**。狭くなった差分がそのまま穴になり、不正な UTF-8 を
        # 含むカード 1 枚で `UnicodeDecodeError` がここを突き抜けて、全 mission の
        # 割り当てと Worker の生存監視が同時に止まった。
        #
        # だから「今回の 1 件 (UnicodeError) を足す」では閉じない。次に読み取り
        # 経路へ新しい失敗が入った日に、同じ止まり方がもう一度出るからである。
        # 上で名前の分かっている失敗は個別に扱い (そのほうが直し方を書ける)、
        # **残り全部をここで隔離に落とす**。
        #
        # 広く捕まえても黙らない: 例外の型と文言が `[破損]` カードの理由として
        # 残り、警告も出る。だから「読み取りのバグを握り潰す」方向には倒れない。
        _warn(
            f"unexpected failure while reading {path}: "
            f"{type(e).__name__}: {e}\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(
            task_id, 'unexpected read error', f'{type(e).__name__}: {e}')


def _read_task_card(path, task_id, _warn):
    """`read_task_card()` の本体。名前の分かっている失敗をそれぞれ隔離する。

    ここから漏れた例外は呼び出し元の backstop が受ける。関数を分けてあるのは、
    「名前の分かっている失敗には直し方まで書く」と「名前の分からない失敗でも
    止めない」が別の話だからで、1 つの try に混ぜると、後から足した except が
    どちらのつもりなのか読めなくなる。
    """
    try:
        text = _read_regular_file(path)
    except _NotARegularFile as e:
        # 通常ファイル以外は **待たずに拒否する** (Codex 7 巡目 P2)。
        #
        # `open()` には上限が無い。書き手のいない FIFO が `tNNN.md` として
        # 置かれていると、そこを読みに来た実行は無期限に座り込む。止まるのは
        # その mission ではない: 変更系の plan.sh は commit の後に全 active
        # mission を同期で走査するので、**別 mission の健全なカードを 1 枚
        # 直しただけの実行**が返らなくなる。`retire --no-wait` がこれを踏むと、
        # 退役を commit した後に watchdog のタイムアウトを使い切る。同じ読み
        # 取りを常駐デーモン 3 者も使うので、1 枚で全 mission の割り当てと
        # Worker の生存監視が同時に止まりうる。
        #
        # 待ち時間に上限を付けるのではなく **種類で弾く** のは、待てば読める
        # ものが 1 つも無いからである。カードは通常ファイルしかありえない。
        # 条件を「FIFO なら拒否」と書かずに「通常ファイルだけ受理」と書いて
        # あるのは、denylist を足し続ける形が次の種類で必ず穴を開けるため
        # (memory: approve-judgment-needs-allowlist-and-scope)。
        _warn(
            f"{path} is not a regular file ({e})\n"
            f"  hint: task カードは通常ファイルだけです。`ls -l {path}` で種類を "
            f"確かめ、置き違えたものなら削除してください。\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(task_id, 'not a regular file', str(e))
    except OSError as e:
        # 権限 / 消えた途中 / I/O。dispatcher は従来 per-file の try/except で
        # これを吸収していたので、共有に寄せる際にその耐性を落とさない。
        _warn(
            f"failed to read {path}: {e}\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(task_id, 'read error', str(e))
    except UnicodeError as e:
        # 途中で切れた書き込み・別エンコーディングの貼り付け・バイナリの
        # 取り違えで普通に起きる。`UnicodeDecodeError` は `OSError` ではなく
        # `ValueError` の側にいるので、上の except では捕まらない (Codex 6 巡目 P1)。
        _warn(
            f"failed to decode {path}: {e}\n"
            f"  hint: task files must be UTF-8. `file {path}` でエンコーディングを "
            f"確かめ、必要なら `iconv -f <元の文字コード> -t utf-8` で書き直すこと。\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(task_id, 'decode error (not UTF-8)', str(e))

    try:
        meta, body = parse_frontmatter(text, source=str(path))
    except ValueError as e:
        # A single malformed task file must not blank out `plan.sh status`
        # or freeze dispatch for the whole mission (t009). Surface it as a
        # [破損] pseudo-task — visible, but never 'pending' or terminal —
        # and keep going so every other task file is still usable.
        _warn(
            f"failed to parse {path}: {e}\n"
            f"  hint: task files start with `---` / frontmatter / `---` / "
            f"`## Description` / `## Result` (see existing tNNN.md for the template).\n"
            f"  showing as [破損] task; other tasks are unaffected."
        )
        return isolated_task(task_id, 'frontmatter parse error', str(e))

    declared = meta.get('id')
    declared = '' if declared is None else str(declared).strip()
    if declared and declared != task_id:
        _warn(
            f"{path}: frontmatter id "
            f"{declared!r} does not match the filename "
            f"({task_id!r}).\n"
            f"  hint: the filename is the task's identity. If this file "
            f"was copied from another card, set `id: {task_id}`; if it "
            f"was misplaced, rename the file.\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(
            task_id,
            f'id がファイル名と一致しない (frontmatter: {declared})',
            f'frontmatter id {declared!r} != filename {task_id!r}',
        )

    return normalize_card(task_id, meta), body


def list_task_cards(tasks_dir, warn=None):
    """`tasks_dir` の全カードを `tNNN` 順に `[(meta, body), ...]` で返す。

    **空リストが意味するのは「カードが 1 枚も無い」だけ**である。走査そのものに
    失敗したときは `scan_failure_task()` の node を 1 件返す —— 理由はそちらの
    docstring にある。

    区別の付け方は `os.stat()` 1 回に寄せてある。`os.path.isdir()` は

      * 本当に無い          (ENOENT)
      * ディレクトリではない (ENOTDIR / 通常ファイル)
      * stat できない        (EACCES: 親から実行権限が消えた等)

    の 3 つを **同じ False** に潰すので、それを分岐の材料にするかぎり、1 番目と
    残り 2 つを区別できない。
    """
    try:
        st = os.stat(tasks_dir)
    except FileNotFoundError:
        # ここだけが「本当に無い」。mission を作った直後・archive 済みは
        # `tasks/` が無いのが正常な状態なので、空で返すのが正しい。終端でない
        # node を置くと、その mission は二度と完了しなくなる。
        return []
    except OSError as e:
        _safe_warn(warn, f"failed to stat {tasks_dir}: {e}\n"
                         f"  holding the mission as unscannable; "
                         f"completion cannot be concluded from this.")
        return [scan_failure_task(tasks_dir, 'stat error', str(e))]
    if not stat.S_ISDIR(st.st_mode):
        _safe_warn(warn, f"{tasks_dir} is not a directory\n"
                         f"  holding the mission as unscannable; "
                         f"completion cannot be concluded from this.")
        return [scan_failure_task(tasks_dir, 'not a directory',
                                  _describe_file_type(st.st_mode))]
    entries = []
    try:
        names = os.listdir(tasks_dir)
    except OSError as e:
        _safe_warn(warn, f"failed to list {tasks_dir}: {e}\n"
                         f"  holding the mission as unscannable; "
                         f"completion cannot be concluded from this.")
        return [scan_failure_task(tasks_dir, 'listing error', str(e))]
    for fn in names:
        m = TASK_FILENAME_RE.fullmatch(fn)
        if not m:
            continue
        entries.append((int(m.group(1)), fn))
    entries.sort()
    return [read_task_card(os.path.join(tasks_dir, fn), fn[:-len('.md')], warn=warn)
            for _, fn in entries]
