#!/usr/bin/env python3
"""
scripts/lib_model.py

skill → モデル ID の解決ヘルパー。
複数 skill が指定された場合は最も要求の高いモデル (opus > sonnet > haiku) を返す。

使い方:
  python3 scripts/lib_model.py resolve --config config/crewvia.yaml --skills "docs,qa"
  → claude-haiku-4-5-20251001

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
            # 通常 (PyYAML) 経路も固定パスのガードを通す (t019)。
            # t018 でガードを足したのは fallback 側だけで、**PyYAML が入って
            # いる本番の経路は素の `p.open()` のまま**だった。置き違えた
            # FIFO 1 枚で、Worker 起動も plan review もモデル解決の途中から
            # 先へ進まなくなる。属性形式の open だったため、
            # tests/test_queue_reads_go_through_the_guard.py の機械検出から
            # も見えていなかった (Codex 10 巡目 P2-1)。
            text = read_regular_text_or_unreadable(str(p))
            if is_unreadable(text):
                print(
                    f"[lib_model] WARNING: could not read config "
                    f"{text.path}: {text.reason}",
                    file=sys.stderr,
                )
                return ""
            config = yaml.safe_load(text) or {}
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
