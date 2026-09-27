#!/usr/bin/env python3
"""lint_plan.py — Static analysis for Crewvia plan.sh missions.

Output format (grep-friendly):
  [OK]   <category>: <message>
  [WARN] <category>: <message>
  [FAIL] <category>: <message>

Exit codes:
  0 — no FAIL (and no WARN in strict mode)
  1 — at least one FAIL (or WARN in strict mode)
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None


def _load_scripts_module(name: str):
    """Load `scripts/<name>.py` by path, same pattern as plan.sh's own
    `_load_scripts_module()`.

    lint_plan.py is loaded three different ways (plan.sh's `_load_lint_module()`
    via `importlib.util.spec_from_file_location` from a `python3 -` stdin
    interpreter whose `sys.path` does *not* include `scripts/`; its own
    `if __name__ == '__main__':` entry point when run directly; and tests via
    `sys.path.insert(0, SCRIPTS_DIR); import lint_plan`). Resolving relative to
    `__file__` works in all three, unlike a plain `import lib_task_cards` that
    would only work in the last case.
    """
    import importlib.util
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, f'{name}.py')
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# task カード (queue/missions/<slug>/tasks/tNNN.md) の読み取りは、識別子・
# parser・隔離の規則を持つ唯一の入口 (CLAUDE.md 不変条件 #1)。plan.sh /
# dispatcher.sh と同じものを読む — lint だけが別の緩い parser で読んでいると、
# 「lint は OK と言うのに plan.sh は [破損] として保留する」食い違いが起きる
# (t072 / PR#236 3巡目)。
lib_task_cards = _load_scripts_module('lib_task_cards')


# ---------------------------------------------------------------------------
# Module 1: Frontmatter schema check
# ---------------------------------------------------------------------------

VALID_PRIORITIES = {'high', 'medium', 'low'}
VALID_STATUSES = {
    'pending', 'in_progress', 'done', 'verified', 'failed', 'skipped',
    'ready_for_verification', 'verifying', 'verification_failed', 'needs_human_review',
    # t009 / #24: drafting の段階から止めておける (Director 専用 / PR 番号待ちの task)。
    # `blocked_reason` が必須 (check_frontmatter)。理由の無い停止は、あとで誰も解けない。
    'blocked',
}
REQUIRED_FIELDS = ['id', 'title', 'skills', 'status', 'priority']


def check_frontmatter(tasks: list[dict]) -> list[tuple[str, str, str]]:
    """Check required fields, types, and valid enum values.

    Returns list of (level, category, message).
    """
    results = []
    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        prefix = f"task/{tid}"

        for field in REQUIRED_FIELDS:
            if field not in meta or meta[field] is None:
                results.append(('FAIL', 'frontmatter', f"{prefix}: missing required field '{field}'"))

        # Type checks
        if 'skills' in meta and meta['skills'] is not None:
            if not isinstance(meta['skills'], list):
                results.append(('FAIL', 'frontmatter', f"{prefix}: 'skills' must be a list, got {type(meta['skills']).__name__}"))

        if 'blocked_by' in meta and meta['blocked_by'] is not None:
            if not isinstance(meta['blocked_by'], list):
                results.append(('FAIL', 'frontmatter', f"{prefix}: 'blocked_by' must be a list"))

        # Enum checks
        priority = meta.get('priority')
        if priority is not None and priority not in VALID_PRIORITIES:
            results.append(('FAIL', 'frontmatter', f"{prefix}: unknown priority '{priority}' (valid: {sorted(VALID_PRIORITIES)})"))

        status = meta.get('status')
        if status is not None and status not in VALID_STATUSES:
            results.append(('FAIL', 'frontmatter', f"{prefix}: unknown status '{status}' (valid: {sorted(VALID_STATUSES)})"))

        # `blocked` は明示的な停止で、理由 (blocked_reason) が要る。理由が無いと、
        # 何を待っているのか (PR 番号か・Director の判断か) が誰にも分からず、解くのも
        # `plan.sh done --pr` のような機械の解除に任せられない。
        if status == 'blocked':
            reason = meta.get('blocked_reason')
            if not isinstance(reason, str) or not reason.strip():
                results.append(('FAIL', 'frontmatter',
                                f"{prefix}: status 'blocked' requires a non-empty 'blocked_reason'"))

        if not results or all(level != 'FAIL' for level, *_ in results):
            pass  # OK entries added by caller

    return results


# ---------------------------------------------------------------------------
# Module 2: Dependency graph check
# ---------------------------------------------------------------------------

def check_dependency_graph(tasks: list[dict]) -> list[tuple[str, str, str]]:
    """Detect cycles and undefined task references in blocked_by.

    Returns list of (level, category, message).
    """
    results = []
    task_ids = {m.get('id') for m in tasks if m.get('id')}

    # Undefined reference check
    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        for dep in (meta.get('blocked_by') or []):
            if dep not in task_ids:
                results.append(('FAIL', 'dependency', f"task/{tid}: blocked_by '{dep}' does not exist"))

    # Cycle detection via DFS
    graph: dict[str, list[str]] = {m.get('id', ''): list(m.get('blocked_by') or []) for m in tasks}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(node: str, path: list[str]) -> Optional[list[str]]:
        if node in in_stack:
            cycle_start = path.index(node)
            return path[cycle_start:] + [node]
        if node in visited:
            return None
        visited.add(node)
        in_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor in graph:
                found = dfs(neighbor, path + [neighbor])
                if found:
                    return found
        in_stack.discard(node)
        return None

    reported_cycles: set[frozenset] = set()
    for tid in graph:
        cycle = dfs(tid, [tid])
        if cycle:
            key = frozenset(cycle)
            if key not in reported_cycles:
                reported_cycles.add(key)
                results.append(('FAIL', 'dependency', f"circular dependency detected: {' → '.join(cycle)}"))

    return results


# ---------------------------------------------------------------------------
# Module 3: Skill alignment check
# ---------------------------------------------------------------------------

def _load_known_skills(skill_permissions_path: str) -> set[str]:
    """`skills:` セクション直下の skill 名の集合。読めない・空・形が不正なら空集合
    (呼び出し側の `check_skill_alignment` が WARN にする — 「未知の skill 0 件」に
    見えても実害は無い、という判定なので FAIL にはしない)。

    旧実装は「2 マス下げの `name:` 行」を正規表現で拾う手書きパーサで、
    コメント付きヘッダ (`skills: # permissions`) で `in_skills` に入れず全滅する
    族Aの欠陥を t059 の改善提案として残していた (`_load_deliverable_capabilities`
    と同じ族)。構造的な YAML 読み込みに寄せて解消する。
    """
    data, problem = _load_yaml_document(skill_permissions_path)
    if problem is not None:
        return set()
    skills = data.get('skills')
    if not isinstance(skills, dict):
        return set()
    return {name for name in skills if isinstance(name, str)}


def check_skill_alignment(tasks: list[dict], skill_permissions_path: str) -> list[tuple[str, str, str]]:
    """Check that task skills exist in skill-permissions.yaml.

    Returns list of (level, category, message).
    """
    results = []
    known_skills = _load_known_skills(skill_permissions_path)

    if not known_skills:
        results.append(('WARN', 'skill', f"skill-permissions.yaml not found or empty: {skill_permissions_path}"))
        return results

    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        for skill in (meta.get('skills') or []):
            if skill not in known_skills:
                results.append(('WARN', 'skill', f"task/{tid}: skill '{skill}' not in skill-permissions.yaml (known: {sorted(known_skills)})"))

    return results


# ---------------------------------------------------------------------------
# Module 4: Timeout validity check
# ---------------------------------------------------------------------------

def _load_timeout_profiles(timeout_profiles_path: str) -> dict:
    """`profiles:` セクションの `{name: {idle: int, max: int}}`。読めない・空・
    形が不正なら空 dict (呼び出し側の `check_timeout_validity` が WARN にする)。

    旧実装は 2 段の手書き状態機械 (2 マス下げでプロファイル名、4 マス下げで
    `idle`/`max` の数字だけを正規表現で拾う) で、コメント付きヘッダやフロー
    スタイルを同じ理由で見落としうる族Aの欠陥だった。構造的な読み込みに寄せる。
    """
    data, problem = _load_yaml_document(timeout_profiles_path)
    if problem is not None:
        return {}
    profiles = data.get('profiles')
    if not isinstance(profiles, dict):
        return {}
    result: dict = {}
    for name, entry in profiles.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        result[name] = {
            k: v for k, v in entry.items()
            if k in ('idle', 'max') and isinstance(v, int) and not isinstance(v, bool)
        }
    return result


def check_timeout_validity(tasks: list[dict], timeout_profiles_path: str) -> list[tuple[str, str, str]]:
    """Check task timeout values against known profile ranges.

    Returns list of (level, category, message).
    """
    results = []
    profiles = _load_timeout_profiles(timeout_profiles_path)

    if not profiles:
        results.append(('WARN', 'timeout', f"timeout-profiles.yaml not found or unreadable: {timeout_profiles_path}"))
        return results

    # Aggregate profile bounds for comparison
    all_idles = [p['idle'] for p in profiles.values() if 'idle' in p]
    all_maxes = [p['max'] for p in profiles.values() if 'max' in p]
    min_idle, max_idle = (min(all_idles), max(all_idles)) if all_idles else (0, 99999)
    min_max, max_max = (min(all_maxes), max(all_maxes)) if all_maxes else (0, 99999)

    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        timeout = meta.get('timeout')
        if timeout is None:
            continue  # optional — OK

        if not isinstance(timeout, dict):
            results.append(('WARN', 'timeout', f"task/{tid}: 'timeout' must be a dict with idle/max keys"))
            continue

        idle = timeout.get('idle')
        max_t = timeout.get('max')

        if idle is not None:
            if not isinstance(idle, int) or idle <= 0:
                results.append(('WARN', 'timeout', f"task/{tid}: timeout.idle must be a positive integer, got {idle!r}"))
            elif idle < min_idle or idle > max_idle:
                results.append(('WARN', 'timeout', f"task/{tid}: timeout.idle={idle} outside profile range [{min_idle}, {max_idle}]"))

        if max_t is not None:
            if not isinstance(max_t, int) or max_t <= 0:
                results.append(('WARN', 'timeout', f"task/{tid}: timeout.max must be a positive integer, got {max_t!r}"))
            elif max_t < min_max or max_t > max_max:
                results.append(('WARN', 'timeout', f"task/{tid}: timeout.max={max_t} outside profile range [{min_max}, {max_max}]"))

        if idle is not None and max_t is not None and isinstance(idle, int) and isinstance(max_t, int):
            if idle >= max_t:
                results.append(('WARN', 'timeout', f"task/{tid}: timeout.idle ({idle}) >= timeout.max ({max_t})"))

    return results


# ---------------------------------------------------------------------------
# Module 5: Deliverable declaration check (t013 / backlog #31)
# ---------------------------------------------------------------------------
#
# 「PR を作る task に Write 禁止の skill を付けた」を、スキル名からの推測ではなく task の
# **宣言** (`deliverable: pr|file|none`) と、config/skill-permissions.yaml の
# `can_produce_deliverable` 欄の突き合わせで落とす。このファイルにスキル名は書かない
# (判定の情報源は config の 1 箇所だけ。スキルを足したら config の欄を足す)。

VALID_DELIVERABLES = ('pr', 'file', 'none')

#: 成果物 (PR / file) を宣言した task にだけ、skills との突き合わせが効く。
DELIVERABLES_THAT_NEED_A_WRITER = ('pr', 'file')

#: `can_produce_deliverable` / `deliverable_required` は厳密に小文字の `true` / `false` だけを
#: 真偽値として認める (1 巡目 t055 の後も、2 巡目 (t057→t059) でコメント付きヘッダが `current` を
#: 前の skill のまま残す形で同じ族が再発し、3 巡目 (t072) では `deliverable_required: no` が
#: 黙って `false` になる形で mission.yaml 側にも同じ族が出た — この欄をこれ以上正規表現の手当てで
#: 直さない。以後は本物の YAML パーサ (PyYAML) で読む)。既定の YAML 1.1 bool resolver は
#: `yes` / `no` / `on` / `off` / `True` / `FALSE` 等も暗黙に真偽値へ丸め込むが、それは
#: 「はっきりしない綴り」を黙って true/false のどちらかに倒す挙動であり、この 2 つの欄が守りたい
#: 性質 (曖昧な値は「不正」として拒否し、既定の安全側 (「作れる」/「必須でない」) へは絶対に
#: 倒さない) と衝突する。resolver を差し替えて対象を狭める (引用符付きの値は元々 resolver の
#: 対象外 — 常に文字列なので影響しない)。
#:
#: **4 巡目 (t076) の finding**: resolver は「タグの無い値をどのタグと見なすか」だけを決め、
#: `!!bool no` のように **タグを明示した値には効かない** — 明示タグは resolver を経由せず、
#: PyYAML 既定の緩い bool コンストラクタ (`yes`/`no`/`on`/`off`/大文字小文字混在まで真偽値に
#: 丸める) にそのまま渡る。`can_produce_deliverable: !!bool yes` が PR を作れない skill を
#: 「作れる」に通してしまうことを読み取り専用の probe で確認済み。resolver に加えて
#: **`tag:yaml.org,2002:bool` のコンストラクタ自体も** 厳密化する (`true`/`false` の 2 語しか
#: 受け付けず、それ以外は `yaml.YAMLError` を送出する) — 暗黙・明示のどちらの経路で
#: `bool` タグに辿り着いても同じ既定になる。`yaml.SafeLoader` のサブクラスで、`bool` 以外の
#: コンストラクタは何も足さない (`!!python/object` 等の任意型構築は不可能なまま) — 下の
#: `yaml.load(..., Loader=_StrictBoolLoader)` は `yaml.safe_load` と同じ安全性で、
#: `bool` の resolver とコンストラクタだけを差し替えている。
if yaml is not None:
    class _StrictBoolLoader(yaml.SafeLoader):
        pass

    _StrictBoolLoader.yaml_implicit_resolvers = {
        first: [r for r in resolvers if r[0] != 'tag:yaml.org,2002:bool']
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    _StrictBoolLoader.add_implicit_resolver(
        'tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$'), list('tf'))

    def _construct_strict_bool(loader: 'yaml.SafeLoader', node: 'yaml.Node') -> bool:
        """`tag:yaml.org,2002:bool` の構築を `true` / `false` の 2 語だけに絞る。

        暗黙 resolver の絞り込み (上) は、タグの無い値にしか効かない。`!!bool no` のように
        タグを明示した値は resolver を経由せず直接この constructor に来るため、resolver だけ
        差し替えても `!!bool` 経由の抜け道が残る (t076 finding)。既定の
        `SafeConstructor.bool_values` (`yes`/`no`/`on`/`off`/大文字小文字混在) を使わず、
        ここで `true`/`false` 以外を明示的に拒否する。
        """
        value = loader.construct_scalar(node)
        if value == 'true':
            return True
        if value == 'false':
            return False
        raise yaml.constructor.ConstructorError(
            None, None,
            f"bool は true / false のどちらかだけ (got {value!r})",
            node.start_mark)

    _StrictBoolLoader.add_constructor('tag:yaml.org,2002:bool', _construct_strict_bool)


def _load_yaml_document(path: str, *, missing_is_ok: bool = True) -> tuple[dict, Optional[str]]:
    """queue / config の YAML ファイル 1 つを、構造的に (本物の YAML パーサで) 読む。

    `(data, problem)`。ファイルの**オープン**は `lib_task_cards.read_regular_text_or_unreadable()`
    を通す (CLAUDE.md 不変条件 #1 — 種類の確認・ENOENT とそれ以外の区別を、この 1 箇所とだけ共有する)。
    それ以外の読み取り失敗・YAML 構文エラー・トップレベルがマッピングでない場合は
    `({}, <理由>)` を返し、呼び出し側に「決められない」として扱わせる
    (「読めない」を「無い」「空」に潰さない)。空ファイル (`yaml.load` が `None` を返す) は
    「中身が無い」として `({}, None)`。

    `missing_is_ok` (既定 True): 本当に無い (ENOENT) を「印が無い」として `({}, None)` に
    するかどうか。呼び出し側で意味が違う —— `mission.yaml` の `deliverable_required` や
    プロファイル集の各セクションのように、**印自体が無いのが普通の状態**なら既定のままでよい。
    `config/skill-permissions.yaml` の `can_produce_deliverable` のように、**ファイルは常に
    存在すべき前提**で、無いことを黙って安全側の既定 (「作れる」) に倒したくない呼び出し側は
    `missing_is_ok=False` を渡す (`_load_deliverable_capabilities` 参照)。

    構造は本物の YAML パーサ (`_StrictBoolLoader` — PyYAML の SafeLoader で bool resolver だけ
    厳密化したもの) で読む。**PyYAML が無い環境では読めない扱いにする** (この用途専用の簡易
    フォールバックは書かない — 簡易パーサを書くたびにコメント・引用符・フロースタイルのどれかを
    見落として同じ族の欠陥を作ってきたため。`pip install pyyaml` は CI にも既定で入っている)。
    """
    text = lib_task_cards.read_regular_text_or_unreadable(path)
    if lib_task_cards.is_unreadable(text):
        if missing_is_ok and lib_task_cards.is_missing(text):
            return {}, None
        return {}, f"{path}: {text.reason}"

    if yaml is None:
        return {}, (f"{path}: PyYAML が無いため読めません "
                     f"(この用途専用の簡易パーサは意図的に持たない — pip install pyyaml)")

    try:
        data = yaml.load(text, Loader=_StrictBoolLoader)
    except yaml.YAMLError as e:
        return {}, f"{path}: YAML を解釈できません ({e})"

    if data is None:
        return {}, None                      # 空ファイル = 中身なし
    if not isinstance(data, dict):
        return {}, f"{path}: トップレベルがマッピングではありません ({type(data).__name__})"
    return data, None


def _load_deliverable_capabilities(skill_permissions_path: str) -> tuple[dict, Optional[str]]:
    """`{skill: True | False | <不正な値の文字列>}` と、読めなかった理由 (読めたら None)。

    欄の無いスキルは辞書に載せない (= 呼び出し側は「作れる」と読む)。値は `true` / `false` の
    どちらかだけを受け入れ、それ以外は **文字列のまま** 返す (truthiness で False に潰さない —
    `flase` と書き間違えたスキルを「作れる」にも「作れない」にも黙って倒さないため)。
    空白を含む値 (文字列・リスト表記など) や空値も、欄自体は「ある」ものとして拾い、
    不正な値の文字列として返す (欄の有無と値の妥当性を別に扱う)。

    構造 (`skills:` セクション・各 skill・その下の欄) はパーサが理解できなかった (`skills` が
    マッピングでない・ある skill の値がマッピングでない) 場合も、その skill だけを飛ばさず
    **読み込み全体を「読めない」として拒否する** (「宣言が無い」と「読めない」は別の状態 —
    既定の「作れる」に静かに倒さない)。ファイル自体が無い場合も同様に問題として返す
    (`missing_is_ok=False`) — この config は常に存在すべき前提で、無いことを「全 skill が
    作れる」に静かに倒さない。

    5 巡目 (t076→t081) の finding と同族: `skills:` **キーが無い** (この config がまだ
    1 件も書かれていない、素の状態) は「宣言 0 件」の正当な既定として扱うが、**キーがあって
    値が明示的に `null`** (セクションの中身が丸ごと消えた・編集事故) は同じ意味に潰さず
    「読めない」として拒否する — `can_produce_deliverable` を守る欄そのものが消えたのに
    気付かず「全 skill が作れる」へ静かに倒れるのを防ぐ。同じ理由で、各 skill の値が
    `null` (`research:` の直後に何も書かれていない等) も「宣言なし」に潰さず拒否する
    (`research: {}` という明示的な空マッピングなら「宣言なし」として通す —
    「触ったが空にした」と「意図的に空だと書いた」を区別する)。
    """
    data, problem = _load_yaml_document(skill_permissions_path, missing_is_ok=False)
    if problem is not None:
        return {}, problem
    if 'skills' not in data:
        return {}, None                      # `skills:` セクション自体が無い = 宣言 0 件
    skills = data['skills']
    if skills is None:
        return {}, f"{skill_permissions_path}: 'skills' が null です (セクションごと消さず、書かないなら削除すること)"
    if not isinstance(skills, dict):
        return {}, f"{skill_permissions_path}: 'skills' がマッピングではありません ({type(skills).__name__})"

    caps: dict = {}
    for name, entry in skills.items():
        if not isinstance(name, str):
            return {}, f"{skill_permissions_path}: skills の下に文字列でないキーがあります ({name!r})"
        if entry is None:
            return {}, (f"{skill_permissions_path}: skill {name!r} の値が null です "
                        f"(何も宣言しないなら {{}} と書くこと)")
        if not isinstance(entry, dict):
            return {}, f"{skill_permissions_path}: skill {name!r} の値がマッピングではありません ({type(entry).__name__})"
        if 'can_produce_deliverable' not in entry:
            continue
        raw = entry['can_produce_deliverable']
        caps[name] = raw if isinstance(raw, bool) else str(raw)
    return caps, None


def _mission_requires_deliverable(slug: str, queue_dir: str) -> tuple[bool, Optional[str]]:
    """mission.yaml の `deliverable_required` を読む。`(必須か, 読めなかった理由)`。

    印が無い (キーが無い・mission.yaml 自体が無い = ENOENT だけ) mission は「必須でない」。
    それ以外の読み取り失敗・`true` / `false` 以外の値は、必須かどうか決められないので理由を返す
    (呼び出し側が FAIL にする — 「読めない」を「印が無い」に潰さない)。

    3 巡目 (t072) の finding 2 件を、手書きパーサではなく `_load_yaml_document()` (本物の
    YAML パーサ) に寄せることで解消する: `deliverable_required: no` は (YAML 1.1 の bool
    ではなく) 文字列 `'no'` のまま読め、下の `value is True` / `is False` の同一性判定に
    引っかからず「true / false のどちらかだけ」の FAIL になる (黙って False にしない)。
    引用符付きのキー `"deliverable_required": true` も、本物のパーサはキーの引用符を
    構文として扱うので、通常のキーと同じに読める (丸ごと無視しない)。

    5 巡目 (t076→t081) の finding: `dict.get()` は「キーが無い」と「キーはあるが値が
    null」を同じ `None` に潰す。**キーが無い** (mission.yaml がこの機能より前に書かれた・
    そもそも `deliverable_required` に触れたことが無い) のは「必須でない」の正当な既定。
    だが **キーがあって値が明示的に `null` / `~` / 何も書かれていない** のは、誰かがこの欄を
    触ったのに空にした (書き忘れ・誤消去) 可能性が高く、「必須でない」に黙って倒さない —
    `in` 演算子でキーの有無を別に確かめてから値を読む。
    """
    path = os.path.join(queue_dir, 'missions', slug, 'mission.yaml')
    data, problem = _load_yaml_document(path)
    if problem is not None:
        return False, problem
    if 'deliverable_required' not in data:
        return False, None
    value = data['deliverable_required']
    if value is False:
        return False, None
    if value is True:
        return True, None
    return False, f"{path}: deliverable_required は true / false のどちらかだけ (got {value!r})"


def check_deliverable(tasks: list[dict], skill_permissions_path: str,
                      required: bool = False,
                      required_problem: Optional[str] = None) -> list[tuple[str, str, str]]:
    """`deliverable` 宣言の検査。Returns list of (level, category, message).

    * 値は pr / file / none のどれか (印の有無にかかわらず、書かれていれば検査する)。
    * `required` (この mission が deliverable 必須の印を持つ) なら、全 task が宣言を持つ。
    * `pr` / `file` を宣言した task の skills が **すべて** `can_produce_deliverable: false`
      なら FAIL (印の有無にかかわらず効く)。欄の無い・config に載っていないスキルは
      「作れる」側 (未知のスキルは別に skill 検査が WARN する)。
    """
    results: list[tuple[str, str, str]] = []
    caps: Optional[dict] = None
    caps_problem: Optional[str] = None

    def capabilities() -> tuple[dict, Optional[str]]:
        nonlocal caps, caps_problem
        if caps is None:
            caps, caps_problem = _load_deliverable_capabilities(skill_permissions_path)
        return caps, caps_problem

    # required の値が要るのは「キーが無い (未宣言)」task だけ —— 「キーはあるが値が null」の
    # task は required に関わらず下のループが無条件で FAIL する (unknown deliverable) ので、
    # ここで `.get() is None` のまま広く数えると、null だけの mission でも「必須かどうか
    # 決められません」という無関係な追加メッセージが出る (t084)。
    if required_problem is not None and any('deliverable' not in m for m in tasks):
        results.append(('FAIL', 'deliverable',
                        f"mission: deliverable が必須かどうか決められません — {required_problem}"))

    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        prefix = f"task/{tid}"
        # t084 (Codex 6巡目 P2 と同族): `meta.get('deliverable')` は「キーが無い」と「キーは
        # あるが値が明示的に null」を同じ None に潰す。**キーが無い** (この機能より前に書かれた
        # task) は「未宣言」の正当な既定 (required でなければ何も言わない)。だが **キーがあって
        # 値が null** は誰かがこの欄を触ったのに空にした可能性が高く、「未宣言」に潰さず
        # 下の「unknown deliverable」で拒否する (`_mission_requires_deliverable` / t081 と同じ型)。
        has_declaration = 'deliverable' in meta
        declared = meta.get('deliverable')

        if not has_declaration:
            if required:
                results.append(('FAIL', 'deliverable',
                                f"{prefix}: 'deliverable' が未宣言です (この mission は必須) — "
                                f"plan.sh update {tid} --deliverable {'|'.join(VALID_DELIVERABLES)}"))
            continue

        if declared not in VALID_DELIVERABLES:
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: unknown deliverable {declared!r} (valid: {list(VALID_DELIVERABLES)})"))
            continue

        if declared not in DELIVERABLES_THAT_NEED_A_WRITER:
            continue

        skills = meta.get('skills')
        if not isinstance(skills, list) or not skills:
            # skills が無い・空リストの task は producer skill を 1 つも持てない。
            # check_frontmatter は `skills: []` を有効な frontmatter として通すので、
            # ここで前提を預けず自分で閉じる (欠落・空リストのどちらも FAIL)。
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を宣言していますが 'skills' が空です "
                            f"(skills={skills!r}) — 成果物を作れる skill を足すか、deliverable を none にする"))
            continue
        table, problem = capabilities()
        if problem is not None:
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を skills と突き合わせられません — {problem}"))
            continue
        bad = sorted({s for s in skills if s in table and table[s] not in (True, False)})
        if bad:
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: skill {bad} の can_produce_deliverable が true / false ではありません "
                            f"({skill_permissions_path})"))
            continue
        if all(table.get(s) is False for s in skills):
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を宣言していますが、skills {skills} は"
                            f" すべて can_produce_deliverable: false で、成果物を作れません "
                            f"(成果物を作れる skill を足すか、deliverable を none にする)"))
    return results


# ---------------------------------------------------------------------------
# Task loader
# ---------------------------------------------------------------------------

def _load_tasks_from_mission(slug: str, queue_dir: str) -> list[dict]:
    """`lib_task_cards.list_task_cards()` を通す (CLAUDE.md 不変条件 #1)。

    旧実装は `os.listdir` + 素の `open()` + 自前の frontmatter パーサで、
    plan.sh / dispatcher.sh が読むのと**別の parser**でカードを読んでいた
    (t072 / PR#236 3巡目)。食い違うと「lint は OK と言うのに plan.sh は
    [破損] として保留する」(またはその逆) が起きうる。加えて、旧実装は
    `os.listdir` 自体が失敗した場合の処理が無く未捕捉の例外で落ちていた
    (`list_task_cards()` は `scan_failure_task()` の保留ノードを返す)。

    読めなかったカードは `status: lib_task_cards.CORRUPT_TASK_STATUS` の
    擬似カードとして返る (`lint_mission()` が FAIL に変換する)。
    """
    tasks_dir = os.path.join(queue_dir, 'missions', slug, 'tasks')
    return [meta for meta, _body in lib_task_cards.list_task_cards(tasks_dir)]


# ---------------------------------------------------------------------------
# Main lint function
# ---------------------------------------------------------------------------

def lint_mission(slug: str, queue_dir: str, config_dir: str, strict: bool = False) -> int:
    """Run all lint checks on a mission. Returns 0 (pass) or 1 (fail)."""
    tasks = _load_tasks_from_mission(slug, queue_dir)
    if not tasks:
        print(f"[WARN] mission: no tasks found in mission '{slug}'")
        return 0

    skill_perm_path = os.path.join(config_dir, 'skill-permissions.yaml')
    timeout_path = os.path.join(config_dir, 'timeout-profiles.yaml')

    all_results: list[tuple[str, str, str]] = []

    # Parse errors first (lib_task_cards.list_task_cards() holds unreadable
    # cards as a CORRUPT_TASK_STATUS pseudo-task rather than raising — see
    # _load_tasks_from_mission()).
    for meta in tasks:
        if meta.get('status') == lib_task_cards.CORRUPT_TASK_STATUS:
            all_results.append(('FAIL', 'frontmatter',
                                f"task/{meta.get('id', '<unknown>')}: "
                                f"{meta.get('parse_error', lib_task_cards.CORRUPT_TASK_STATUS)}"))

    valid_tasks = [m for m in tasks if m.get('status') != lib_task_cards.CORRUPT_TASK_STATUS]

    all_results += check_frontmatter(valid_tasks)
    all_results += check_dependency_graph(valid_tasks)
    all_results += check_skill_alignment(valid_tasks, skill_perm_path)
    all_results += check_timeout_validity(valid_tasks, timeout_path)
    # mission.yaml は、宣言の無い (キーが無い) task があるときだけ読む (required が要るのは
    # そのときだけ —— 「キーはあるが値が null」の task は required に関わらず check_deliverable()
    # が無条件で FAIL するので、ここで required を知る必要が無い。t084: `.get() is None` のまま
    # 広く数えると、null だけの mission でも不要に mission.yaml を読みに行く)。
    required, required_problem = (
        _mission_requires_deliverable(slug, queue_dir)
        if any('deliverable' not in m for m in valid_tasks) else (False, None))
    all_results += check_deliverable(valid_tasks, skill_perm_path, required, required_problem)

    # Print results
    has_fail = False
    for level, category, message in all_results:
        effective = level
        if strict and level == 'WARN':
            effective = 'FAIL'
        if effective == 'FAIL':
            has_fail = True
        print(f"[{effective}] {category}: {message}")

    # Summary OK lines for passing tasks
    task_ids = [m.get('id', '?') for m in valid_tasks]
    # Bug A fix: respect strict mode (WARN promoted to FAIL counts as FAIL)
    # Bug B fix: extract task IDs from circular dependency messages too
    fail_ids: set[str] = set()
    for lvl, cat, msg in all_results:
        effective_lvl = 'FAIL' if (strict and lvl == 'WARN') else lvl
        if effective_lvl != 'FAIL':
            continue
        if msg.startswith('task/'):
            fail_ids.add(msg.split('/')[1].split(':')[0])
        elif cat == 'dependency' and 'circular dependency' in msg:
            # Extract all task IDs from "circular dependency detected: a → b → a"
            for tid in task_ids:
                if tid in msg:
                    fail_ids.add(tid)
    for tid in task_ids:
        if tid not in fail_ids:
            print(f"[OK]   task/{tid}: all checks passed")

    if not has_fail:
        print(f"[OK]   mission/{slug}: lint passed ({len(valid_tasks)} task(s))")

    return 1 if has_fail else 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Lint a Crewvia mission plan.')
    parser.add_argument('slug', help='Mission slug to lint')
    parser.add_argument('--queue-dir', default=None, help='Path to queue directory')
    parser.add_argument('--config-dir', default=None, help='Path to config directory')
    parser.add_argument('--strict', action='store_true', help='Treat WARN as FAIL')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    queue_dir = args.queue_dir or os.environ.get('CREWVIA_QUEUE', os.path.join(repo_root, 'queue'))
    config_dir = args.config_dir or os.path.join(repo_root, 'config')

    sys.exit(lint_mission(args.slug, queue_dir, config_dir, strict=args.strict))
