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


# ---------------------------------------------------------------------------
# YAML helpers (minimal — mirrors plan.sh's parse_yaml subset)
# ---------------------------------------------------------------------------

def _parse_minimal_yaml(text: str) -> dict:
    """Parse a very narrow YAML subset (scalars, inline lists, block lists/maps)."""
    lines = text.splitlines()
    result: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        val = m.group(2).rstrip()
        if val == '':
            i += 1
            items: list = []
            sub: dict = {}
            while i < len(lines):
                lm = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lm:
                    items.append(_scalar(lm.group(1).strip()))
                    i += 1
                else:
                    mm = re.match(r'^  ([\w-]+):\s*(.*)$', lines[i])
                    if mm:
                        sub[mm.group(1)] = _scalar(mm.group(2).rstrip())
                        i += 1
                    else:
                        break
            result[key] = items if items else (sub if sub else None)
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in inner.split(',')] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def _scalar(s: str):
    if s in ('null', '~', ''):
        return None
    if s in ('true', 'yes'):
        return True
    if s in ('false', 'no'):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s.strip('"\'')


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError("missing frontmatter delimiter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        raise ValueError("unterminated frontmatter")
    meta_text = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    return _parse_minimal_yaml(meta_text), body


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
    """Extract skill names from the 'skills:' section of skill-permissions.yaml."""
    if not os.path.exists(skill_permissions_path):
        return set()
    with open(skill_permissions_path) as f:
        content = f.read()
    # Find the 'skills:' block and collect top-level keys (2-space indented)
    in_skills = False
    known: set[str] = set()
    for line in content.splitlines():
        if re.match(r'^skills:\s*$', line):
            in_skills = True
            continue
        if in_skills:
            # top-level key under skills: block (2-space indent)
            m = re.match(r'^  ([a-zA-Z_][a-zA-Z0-9_-]*):\s*$', line)
            if m:
                known.add(m.group(1))
            elif line and not line.startswith(' ') and not line.startswith('#'):
                in_skills = False  # left the skills block
    return known


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
    """Load profiles from timeout-profiles.yaml."""
    if not os.path.exists(timeout_profiles_path):
        return {}
    with open(timeout_profiles_path) as f:
        content = f.read()
    # Find 'profiles:' block
    profiles: dict = {}
    in_profiles = False
    current_profile: Optional[str] = None
    current_data: dict = {}
    for line in content.splitlines():
        if re.match(r'^profiles:\s*$', line):
            in_profiles = True
            continue
        if not in_profiles:
            continue
        # profile name (2-space indent)
        pm = re.match(r'^  ([a-zA-Z_][a-zA-Z0-9_-]*):\s*$', line)
        if pm:
            if current_profile:
                profiles[current_profile] = current_data
            current_profile = pm.group(1)
            current_data = {}
            continue
        # profile field (4-space indent)
        fm = re.match(r'^    (idle|max):\s*(\d+)', line)
        if fm and current_profile:
            current_data[fm.group(1)] = int(fm.group(2))
        elif line and not line.startswith(' ') and not line.startswith('#'):
            break
    if current_profile:
        profiles[current_profile] = current_data
    return profiles


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

#: `can_produce_deliverable` は厳密に小文字の `true` / `false` だけを真偽値として認める
#: (1 巡目 t055 の後も、2 巡目 (t057→t059) でコメント付きヘッダが `current` を前の skill の
#: まま残す形で同じ族が再発した — この欄をこれ以上正規表現の手当てで直さない。以後は本物の
#: YAML パーサ (PyYAML) で読む)。既定の YAML 1.1 bool resolver は `yes` / `no` / `on` / `off` /
#: `True` / `FALSE` 等も暗黙に真偽値へ丸め込むが、それは「はっきりしない綴り」を黙って
#: true/false のどちらかに倒す挙動であり、この欄が守りたい性質 (曖昧な値は「不正」として
#: 拒否し、既定の「作れる」側へは絶対に倒さない) と衝突する。resolver を差し替えて対象を
#: 狭める (引用符付きの値は元々 resolver の対象外 — 常に文字列なので影響しない)。
#: `yaml.SafeLoader` のサブクラスで、コンストラクタは何も足さない (`!!python/object` 等の
#: 任意型構築は不可能なまま) — 下の `yaml.load(..., Loader=_StrictBoolLoader)` は
#: `yaml.safe_load` と同じ安全性で、resolver だけを差し替えている。
if yaml is not None:
    class _StrictBoolLoader(yaml.SafeLoader):
        pass

    _StrictBoolLoader.yaml_implicit_resolvers = {
        first: [r for r in resolvers if r[0] != 'tag:yaml.org,2002:bool']
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    _StrictBoolLoader.add_implicit_resolver(
        'tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$'), list('tf'))


def _load_deliverable_capabilities(skill_permissions_path: str) -> tuple[dict, Optional[str]]:
    """`{skill: True | False | <不正な値の文字列>}` と、読めなかった理由 (読めたら None)。

    欄の無いスキルは辞書に載せない (= 呼び出し側は「作れる」と読む)。値は `true` / `false` の
    どちらかだけを受け入れ、それ以外は **文字列のまま** 返す (truthiness で False に潰さない —
    `flase` と書き間違えたスキルを「作れる」にも「作れない」にも黙って倒さないため)。
    空白を含む値 (文字列・リスト表記など) や空値も、欄自体は「ある」ものとして拾い、
    不正な値の文字列として返す (欄の有無と値の妥当性を別に扱う)。

    構造 (`skills:` セクション・各 skill・その下の欄) は本物の YAML パーサで読む。**PyYAML が
    無い環境では読めない扱いにする** (この欄専用の簡易フォールバックは書かない — 簡易パーサを
    書くたびにコメント・引用符・フロースタイルのどれかを見落として同じ族の欠陥を作ってきたため。
    `pip install pyyaml` は CI にも既定で入っている)。パーサが構造を理解できなかった (トップ
    レベルがマッピングでない・`skills` がマッピングでない・ある skill の値がマッピングでない)
    場合も、その skill だけを飛ばさず **読み込み全体を「読めない」として拒否する** (「宣言が
    無い」と「読めない」は別の状態 — 既定の「作れる」に静かに倒さない)。
    """
    try:
        with open(skill_permissions_path, encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {}, f"{skill_permissions_path}: {type(e).__name__}: {e}"

    if yaml is None:
        return {}, (f"{skill_permissions_path}: PyYAML が無いため読めません "
                     f"(この欄専用の簡易パーサは意図的に持たない — pip install pyyaml)")

    try:
        data = yaml.load(content, Loader=_StrictBoolLoader)
    except yaml.YAMLError as e:
        return {}, f"{skill_permissions_path}: YAML を解釈できません ({e})"

    if not isinstance(data, dict):
        return {}, f"{skill_permissions_path}: トップレベルがマッピングではありません ({type(data).__name__})"
    skills = data.get('skills')
    if skills is None:
        return {}, None                      # `skills:` セクション自体が無い = 宣言 0 件
    if not isinstance(skills, dict):
        return {}, f"{skill_permissions_path}: 'skills' がマッピングではありません ({type(skills).__name__})"

    caps: dict = {}
    for name, entry in skills.items():
        if not isinstance(name, str):
            return {}, f"{skill_permissions_path}: skills の下に文字列でないキーがあります ({name!r})"
        if entry is None:
            continue                         # 空のスキル定義。宣言なしと同じ
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
    """
    path = os.path.join(queue_dir, 'missions', slug, 'mission.yaml')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return False, None
    except (OSError, UnicodeDecodeError) as e:
        return False, f"{path}: {type(e).__name__}: {e}"
    value = _parse_minimal_yaml(text).get('deliverable_required')
    if value is None or value is False:
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

    if required_problem is not None and any(m.get('deliverable') is None for m in tasks):
        results.append(('FAIL', 'deliverable',
                        f"mission: deliverable が必須かどうか決められません — {required_problem}"))

    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        prefix = f"task/{tid}"
        declared = meta.get('deliverable')

        if declared is None:
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
    mission_dir = os.path.join(queue_dir, 'missions', slug)
    tasks_dir = os.path.join(mission_dir, 'tasks')
    if not os.path.isdir(tasks_dir):
        return []
    metas = []
    for fname in sorted(os.listdir(tasks_dir)):
        if not fname.endswith('.md'):
            continue
        path = os.path.join(tasks_dir, fname)
        with open(path) as f:
            text = f.read()
        try:
            meta, _ = _parse_frontmatter(text)
            metas.append(meta)
        except ValueError as e:
            metas.append({'id': fname, '_parse_error': str(e)})
    return metas


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

    # Parse errors first
    for meta in tasks:
        if '_parse_error' in meta:
            all_results.append(('FAIL', 'frontmatter', f"task/{meta['id']}: parse error — {meta['_parse_error']}"))

    valid_tasks = [m for m in tasks if '_parse_error' not in m]

    all_results += check_frontmatter(valid_tasks)
    all_results += check_dependency_graph(valid_tasks)
    all_results += check_skill_alignment(valid_tasks, skill_perm_path)
    all_results += check_timeout_validity(valid_tasks, timeout_path)
    # mission.yaml は、宣言の無い task があるときだけ読む (印が要るのはそのときだけ)。
    required, required_problem = (
        _mission_requires_deliverable(slug, queue_dir)
        if any(m.get('deliverable') is None for m in valid_tasks) else (False, None))
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
