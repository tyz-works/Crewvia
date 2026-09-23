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

## フォールバックを持たない

読み込めなければ呼び出し側はそのまま死ぬ。`lib_dep_rules.py` と同じ理由で、
「読めなかったら自前の規則で続行」に倒すと、規則が 1 つであるという性質そのものが
最も気付きにくい形 (壊れた環境でだけコピーが動く) で失われる。plan.sh を単体で
コピーする隔離テストでは、このファイルも一緒に置くこと。
"""

from __future__ import annotations

import os
import re

#: 読めない / 信用できないカードに与える疑似ステータス。pending でも終端でもない。
#: だから pull も dispatch もこのカードを拾わず、同時に「mission が完了した」とも
#: 数えられない。t009 (1 枚の壊れたカードが mission 全体を凍らせた) の解き方。
CORRUPT_TASK_STATUS = 'corrupted'

#: task ファイルの名前。この正規表現に合うものだけが task であり、`tNNN` の
#: 部分がその task の識別子になる。
TASK_FILENAME_RE = re.compile(r't(\d+)\.md')


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

    `warn` は `callable(str)` か None。None なら黙る (同じ実行の中で 2 回目以降に
    読む呼び出し用 —— 同じ警告が二重に出ると、読む側は 2 件壊れていると誤読する)。
    """
    def _warn(msg):
        if warn is not None:
            warn(msg)

    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        # 権限 / 消えた途中 / I/O。dispatcher は従来 per-file の try/except で
        # これを吸収していたので、共有に寄せる際にその耐性を落とさない。
        _warn(
            f"failed to read {path}: {e}\n"
            f"  holding it as a [破損] task; other tasks are unaffected."
        )
        return isolated_task(task_id, 'read error', str(e))

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

    ディレクトリが無ければ空リスト (mission が作られた直後・archive 済みなど、
    普通に通る状態なので例外にはしない)。
    """
    if not os.path.isdir(tasks_dir):
        return []
    entries = []
    try:
        names = os.listdir(tasks_dir)
    except OSError as e:
        if warn is not None:
            warn(f"failed to list {tasks_dir}: {e}")
        return []
    for fn in names:
        m = TASK_FILENAME_RE.fullmatch(fn)
        if not m:
            continue
        entries.append((int(m.group(1)), fn))
    entries.sort()
    return [read_task_card(os.path.join(tasks_dir, fn), fn[:-len('.md')], warn=warn)
            for _, fn in entries]
