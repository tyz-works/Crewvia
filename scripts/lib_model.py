#!/usr/bin/env python3
"""
scripts/lib_model.py

skill → モデル ID の解決ヘルパー。
複数 skill が指定された場合は最も要求の高いモデル (opus > sonnet > haiku) を返す。

使い方:
  python3 scripts/lib_model.py resolve --config config/crewvia.yaml --skills "docs,qa"
  → sonnet  (t028 以降。docs / qa / verify は Sonnet)

終了コード:
  常に 0。呼び出し元の Worker 起動を絶対に止めない。
  エラー時は stderr に警告を出し、空文字を stdout に出力する。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_config  # noqa: E402
import lib_dep_rules as _dep  # noqa: E402
from lib_task_cards import (  # noqa: E402
    is_unreadable, read_regular_text_or_unreadable,
)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# PyYAML fallback: crewvia.yaml の必要部分だけ解析する簡易パーサー
# hooks/lib_skill_perms.py の _parse_yaml_fallback() と同パターン。
# ---------------------------------------------------------------------------
def _parse_yaml_fallback(path: str) -> dict:
    """PyYAML 不在時の簡易 YAML パーサー。

    crewvia.yaml の中から worker_model と model_per_skill だけを抽出する。
    対応するスキーマ:
      worker_model: <value>
      model_per_skill:
        <skill>: <value>   # 2-space indent
    """
    result: dict = {}
    in_model_per_skill = False

    # config も固定パスのガードを通す (t018)。置き違えた FIFO 1 枚で、
    # Worker の起動がモデル解決の途中から先へ進まなくなる。
    text = read_regular_text_or_unreadable(path)
    if is_unreadable(text):
        return result
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if indent == 0:
            in_model_per_skill = False
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                # インラインコメントを除去してから quotes を剥がす
                val = val.split("#")[0].strip().strip('"\'')
                if key == "worker_model" and val:
                    result["worker_model"] = val
                elif key == "model_per_skill":
                    result.setdefault("model_per_skill", {})
                    in_model_per_skill = True
        elif in_model_per_skill and indent >= 2 and ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"\'').split("#")[0].strip()  # strip inline comment
            if key and val:
                result.setdefault("model_per_skill", {})[key] = val

    return result


# ---------------------------------------------------------------------------
# モデルランク: 高い方が優先される
# ---------------------------------------------------------------------------
def _model_rank(model_id: str) -> int:
    """
    モデル ID 文字列からランクを返す。
    opus=3, sonnet=2, haiku=1。
    未知の ID (opus/sonnet/haiku を含まない) は 2 (sonnet 相当) として扱う。
    """
    m = model_id.lower()
    if "opus" in m:
        return 3
    if "sonnet" in m:
        return 2
    if "haiku" in m:
        return 1
    print(
        f"[lib_model] WARNING: unknown model id '{model_id}' treated as sonnet-rank (2)",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# メイン解決ロジック
# ---------------------------------------------------------------------------
def resolve(config_path: str, skills_str: str) -> str:
    """
    config_path の crewvia.yaml と skills_str から最適なモデル ID を返す。
    決定できない場合は空文字を返す (--model を付けない)。
    """
    # skills 解析 (空文字・空白は除外)
    skills = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []

    # config 読み込み
    config: dict = {}
    p = Path(config_path)
    if not p.exists():
        print(
            f"[lib_model] WARNING: config file not found: {config_path}",
            file=sys.stderr,
        )
        return ""

    try:
        if yaml is not None:
            # 通常 (PyYAML) 経路: 共通 + マシン固有の local を重ねて読む (lib_config。固定パスのガードは
            # lib_task_cards 経由)。壊れ (local を含む) は空 (= --model を付けない) に倒し、固定コードを出す。
            config = lib_config.load_for_path(str(p))
            if is_unreadable(config):
                print(
                    f"[lib_model] WARNING: could not read config: {config.reason}",
                    file=sys.stderr,
                )
                return ""
        else:
            # PyYAML 未導入時は簡易 fallback パーサーを使う
            print(
                "[lib_model] INFO: PyYAML not installed; using fallback parser",
                file=sys.stderr,
            )
            config = _parse_yaml_fallback(str(p))
    except Exception as exc:  # noqa: BLE001
        print(f"[lib_model] WARNING: failed to parse config: {exc}", file=sys.stderr)
        return ""

    worker_model: str = config.get("worker_model", "") or ""
    model_per_skill: dict = config.get("model_per_skill", {}) or {}

    if not skills:
        # skill 未指定 → worker_model フォールバック
        return worker_model

    # 各 skill のモデルを解決し、最高ランクを選ぶ
    best_model: str = ""
    best_rank: int = -1

    # 同ランクの場合は skill 名ソート順の最初のものを採用 (決定的挙動)
    for skill in sorted(skills):
        if skill in model_per_skill:
            candidate = model_per_skill[skill] or ""
        else:
            candidate = worker_model  # 未定義 skill → worker_model

        if not candidate:
            continue

        rank = _model_rank(candidate)
        if rank > best_rank:
            best_rank = rank
            best_model = candidate

    return best_model


# ---------------------------------------------------------------------------
# モデルの段上げ (PR-2。設計 knowledge/model-escalation.md §4.4・§4.5・§5)
# ---------------------------------------------------------------------------
#: 段の別名とその順 (低 → 高)。**ここに 1 回だけ書く**。`model_override` の検査 (plan.sh の update・lint・store-check・
#: dispatcher・pull) は全部これを import する。ランクは `_model_rank` と同じく部分文字列で見る (版つき ID も同じ段)。
ESCALATION_LADDER = ("haiku", "sonnet", "opus")


def is_alias(value) -> bool:
    """`model_override` に書いてよい値か: 別名だけ (小文字・完全一致。版つき ID・空・bool・None は不可)。"""
    return isinstance(value, str) and value in ESCALATION_LADDER


def alias_of(model):
    """モデル ID (別名でも版つきでも) が属する段の別名。段に無い (`fable` 等・空・文字列でない) は None。
    `_model_rank` と違って警告も出さず、未知を sonnet に倒さない (段上げの起点にしてよいかの判定に使う)。"""
    if not isinstance(model, str):
        return None
    m = model.lower()
    for alias in ESCALATION_LADDER:
        if alias in m:
            return alias
    return None


def escalation_base(*candidates):
    """段上げの起点: 候補 (閉じた試行の `execution_model` → 今の `model_override` → 既定モデルの順) の最初の
    **段の分かるもの** (§4.2)。どれも分からなければ None。起点が分からないことを理由に段上げを止めない
    ための順で、知らない名前は飛ばして次を見る。"""
    for c in candidates:
        if alias_of(c) is not None:
            return c
    return None


def escalate(base, override=None):
    """`base` の 1 段上の別名 (opus で止まる)。既に `override` があればそれより下げない (`max(override, base + 1)`)。
    `base` の段が分からず `override` も無ければ None (呼び出し側が「段上げしない」を決める)。"""
    cur = alias_of(base)
    ov = override if is_alias(override) else None
    if cur is None:
        return ov
    idx = min(ESCALATION_LADDER.index(cur) + 1, len(ESCALATION_LADDER) - 1)
    if ov is not None:
        idx = max(idx, ESCALATION_LADDER.index(ov))
    return ESCALATION_LADDER[idx]


def effective_model(config_path: str, skills, override=None) -> str:
    """その task を走らせるべきモデル (実効モデル。§5): `override` (別名) があればそれ、無ければ既定モデル (`resolve`)。
    env の明示 (`CREWVIA_WORKER_MODEL`) は Worker 側の事情なので含めない。`skills` は list か CSV。
    `override` が別名でない (壊れた値) ときは ValueError (既定に黙って倒さない)。"""
    if override is not None:
        if not is_alias(override):
            raise ValueError(f"model_override {override!r} is not one of {list(ESCALATION_LADDER)}")
        return override
    csv = ",".join(skills) if isinstance(skills, (list, tuple, set)) else (skills or "")
    return resolve(config_path, csv)


# ---------------------------------------------------------------------------
# 自動の段上げの判定 (PR-3。設計 knowledge/model-escalation.md §4.1・§4.6・§4.8)
# ---------------------------------------------------------------------------
#: 台帳の `model_escalation` の値 (固定の語彙。文面は読まない)
ESC_ESCALATED = 'escalated'                  # 1 段上げた (sonnet → opus 等)
ESC_AT_CAP = 'at_cap'                        # 起点がもう opus (上限のまま。`model_at_cap_count` を数える)
ESC_DECLINED = 'declined'                    # 上げない指定 (mechanical / --no-escalate / フラグ無しの retry)
ESC_NOT_APPLICABLE = 'not_applicable'        # review / qa / codex-review の task (`escalation_applicable` が偽)
ESC_SKIPPED_CONFIG = 'skipped:config_unreadable'   # 既定モデルが決められない (差し戻しは進める)
ESC_SKIPPED_NO_BASE = 'skipped:no_base'      # 起点の段が分からない

#: reopen の差し戻しの種類 (`plan.sh reopen --cause`)。quality = 設計の誤り・考え不足・要件の取り違え (1 段上げる)、
#: mechanical = CI の赤・構造ガード・テストの書き方・細かい境界値や入力検査 (上げない)
CAUSES = ('quality', 'mechanical')


def escalation_applicable(skills) -> bool:
    """段上げしてよい task か (§4.1)。skills が `CASCADE_REVIEW_SKILLS` (review / qa / codex-review) と交わらない実装 task だけ。
    reopen・verify-result fail・retry の 3 つの入口が全部これを通す (新しい集合を作らない)。"""
    if isinstance(skills, str):
        skills = [x.strip() for x in skills.split(',') if x.strip()]
    return not any(x in _dep.CASCADE_REVIEW_SKILLS for x in (skills or []))


def default_model(config_path: str, skills):
    """`(既定モデル, 問題)`。config が読めなければ `(None, 'config_unreadable')` (`resolve` と違い、空と壊れを区別する)。
    読めて何も決まらなければ `('', None)`。"""
    csv = ",".join(skills) if isinstance(skills, (list, tuple, set)) else (skills or "")
    p = Path(config_path)
    if not p.exists():
        return None, 'config_unreadable'
    try:
        if yaml is not None:
            config = lib_config.load_for_path(str(p))
            if is_unreadable(config):
                return None, 'config_unreadable'
        else:
            config = _parse_yaml_fallback(str(p))
    except Exception:  # noqa: BLE001
        return None, 'config_unreadable'
    wanted = [s.strip() for s in csv.split(",") if s.strip()]
    worker_model = config.get("worker_model", "") or ""
    per_skill = config.get("model_per_skill", {}) or {}
    if not wanted:
        return worker_model, None
    best, best_rank = "", -1
    for skill in sorted(wanted):
        cand = (per_skill[skill] or "") if skill in per_skill else worker_model
        if cand and _model_rank(cand) > best_rank:
            best, best_rank = cand, _model_rank(cand)
    return best, None


def want_escalation(command, *, cause=None, escalate_model=False, no_escalate=False) -> bool:
    """この差し戻しが段上げを**求めているか** (§4.1 の表。適用できるかは `decide_escalation` が `escalation_applicable` で見る)。
    reopen は `cause` (quality = 上げる / mechanical = 上げない)、verify-result fail は既定で上げる、retry は `--escalate-model` のときだけ。
    `--no-escalate` は常に勝つ (食い違い (mechanical + --escalate-model / quality + --no-escalate) は plan.sh が先に断る)。"""
    if no_escalate:
        return False
    if escalate_model:
        return True
    if command == 'reopen':
        return cause == 'quality'
    if command == 'verify_fail':
        return True
    return False


def decide_escalation(wants, *, skills, closed_model, current_override, config_path, closed_execution_id,
                      at_cap_count=0, at_cap_execution=None):
    """段上げの決定。`dict(code, escalate_to, model_from, at_cap_count, at_cap_execution, applied)`。
    `escalate_to` / `model_from` は別名か None。`at_cap_*` は card に書く新しい値 (変えないなら今の値)。

    起点は 閉じた試行の `execution_model` → 今の `model_override` → 既定モデル の順の、最初の**段の分かるもの** (§4.2)。
    既定モデルは前の 2 つが段を持たないときだけ読む。config が読めなければ `skipped:config_unreadable` (差し戻しは進める。§4.8)。
    起点が opus なら上がらず `at_cap` (`model_override` は opus のまま)。数は **at_cap のときだけ** +1 し、ちょうど 2 になった
    差し戻しの試行 ID だけを `at_cap_execution` にする (3 回目以降は書き換えない。§4.6)。"""
    out = dict(code=ESC_DECLINED, escalate_to=None, model_from=None, at_cap_count=at_cap_count,
               at_cap_execution=at_cap_execution, applied=False)
    if not wants:
        return out
    if not escalation_applicable(skills):
        out['code'] = ESC_NOT_APPLICABLE
        return out
    base = escalation_base(closed_model, current_override)
    if base is None:
        dflt, problem = default_model(config_path, skills)
        if problem:
            out['code'] = ESC_SKIPPED_CONFIG
            return out
        base = escalation_base(dflt)
        if base is None:
            out['code'] = ESC_SKIPPED_NO_BASE
            return out
    target = escalate(base, current_override)
    out.update(escalate_to=target, model_from=alias_of(base), applied=True)
    if alias_of(base) == ESCALATION_LADDER[-1]:
        count = at_cap_count + 1
        out.update(code=ESC_AT_CAP, at_cap_count=count)
        if count == 2:
            out['at_cap_execution'] = closed_execution_id
    else:
        out['code'] = ESC_ESCALATED
    return out


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lib_model.py",
        description="Resolve the best Claude model ID for the given skills.",
    )
    subparsers = parser.add_subparsers(dest="command")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve model for skills")
    resolve_parser.add_argument(
        "--config",
        default="config/crewvia.yaml",
        help="Path to crewvia.yaml (default: config/crewvia.yaml)",
    )
    resolve_parser.add_argument(
        "--skills",
        default="",
        help="Comma-separated skill list (e.g. 'docs,qa')",
    )

    args = parser.parse_args()

    if args.command == "resolve":
        result = resolve(args.config, args.skills)
        print(result)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
