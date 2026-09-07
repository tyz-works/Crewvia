#!/usr/bin/env python3
"""
tests/test_model_per_skill.py

lib_model.py の unit tests。
skill → モデル解決ロジックを全パターン検証する。

実行方法:
  python3 -m pytest tests/test_model_per_skill.py -v
"""

import sys
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

# scripts/ を sys.path に追加して lib_model をインポート
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_model  # noqa: E402


# ---------------------------------------------------------------------------
# フィクスチャ: 標準 config (実際の crewvia.yaml と同等の内容)
# ---------------------------------------------------------------------------
STANDARD_CONFIG = textwrap.dedent("""\
    worker_model: claude-sonnet-5

    model_per_skill:
      planning:    claude-opus-5
      plan_review: claude-opus-5
      review:      claude-opus-5
      research:    claude-opus-5
      docs:        claude-haiku-4-5-20251001
      qa:          claude-haiku-4-5-20251001
      verify:      claude-haiku-4-5-20251001
""")


@pytest.fixture()
def cfg(tmp_path):
    """標準 config ファイルを tmp_path に書き出して、そのパスを返す。"""
    p = tmp_path / "crewvia.yaml"
    p.write_text(STANDARD_CONFIG, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 単一 skill テスト
# ---------------------------------------------------------------------------
class TestSingleSkill:
    def test_planning_returns_opus(self, cfg):
        assert lib_model.resolve(cfg, "planning") == "claude-opus-5"

    def test_docs_returns_haiku(self, cfg):
        assert lib_model.resolve(cfg, "docs") == "claude-haiku-4-5-20251001"

    def test_code_falls_back_to_worker_model(self, cfg):
        # code は model_per_skill に未定義 → worker_model にフォールバック
        assert lib_model.resolve(cfg, "code") == "claude-sonnet-5"

    def test_bash_falls_back_to_worker_model(self, cfg):
        assert lib_model.resolve(cfg, "bash") == "claude-sonnet-5"

    def test_qa_returns_haiku(self, cfg):
        assert lib_model.resolve(cfg, "qa") == "claude-haiku-4-5-20251001"

    def test_verify_returns_haiku(self, cfg):
        assert lib_model.resolve(cfg, "verify") == "claude-haiku-4-5-20251001"

    def test_review_returns_opus(self, cfg):
        assert lib_model.resolve(cfg, "review") == "claude-opus-5"

    def test_research_returns_opus(self, cfg):
        assert lib_model.resolve(cfg, "research") == "claude-opus-5"


# ---------------------------------------------------------------------------
# 複数 skill: 最も要求の高いモデルを選ぶ (opus > sonnet > haiku)
# ---------------------------------------------------------------------------
class TestMultiSkill:
    def test_planning_and_code_returns_opus(self, cfg):
        # planning=opus(3), code→sonnet(2) → opus
        assert lib_model.resolve(cfg, "planning,code") == "claude-opus-5"

    def test_docs_and_qa_returns_haiku(self, cfg):
        # 両方 haiku(1) → haiku
        assert lib_model.resolve(cfg, "docs,qa") == "claude-haiku-4-5-20251001"

    def test_code_and_python_returns_sonnet(self, cfg):
        # 両方 worker_model=sonnet(2) → sonnet
        assert lib_model.resolve(cfg, "code,python") == "claude-sonnet-5"

    def test_qa_and_bash_returns_sonnet(self, cfg):
        # qa=haiku(1), bash→sonnet(2) → sonnet
        assert lib_model.resolve(cfg, "qa,bash") == "claude-sonnet-5"

    def test_planning_and_docs_and_code_returns_opus(self, cfg):
        # planning=opus > others → opus
        assert lib_model.resolve(cfg, "planning,docs,code") == "claude-opus-5"

    def test_same_rank_uses_sorted_first(self, cfg):
        # docs と qa は同ランク。ソート順: docs < qa → docs のモデルを採用 (両方同じ haiku なので実質同じ)
        result = lib_model.resolve(cfg, "qa,docs")
        assert result == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# エッジケース
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_skills_returns_worker_model(self, cfg):
        assert lib_model.resolve(cfg, "") == "claude-sonnet-5"

    def test_whitespace_skills_returns_worker_model(self, cfg):
        assert lib_model.resolve(cfg, "  ,  ") == "claude-sonnet-5"

    def test_undefined_skill_falls_back_to_worker_model(self, cfg):
        # 完全に未定義の skill
        assert lib_model.resolve(cfg, "nonexistent_skill") == "claude-sonnet-5"

    def test_config_without_model_per_skill(self, tmp_path):
        # model_per_skill ブロックが無い config (後方互換)
        p = tmp_path / "minimal.yaml"
        p.write_text("worker_model: claude-sonnet-5\n", encoding="utf-8")
        assert lib_model.resolve(str(p), "docs") == "claude-sonnet-5"

    def test_config_file_missing_returns_empty(self, tmp_path):
        # config ファイルが存在しない → 空文字 + exit 0 (例外なし)
        result = lib_model.resolve(str(tmp_path / "nonexistent.yaml"), "docs")
        assert result == ""

    def test_broken_yaml_returns_empty(self, tmp_path):
        # YAML parse 失敗 → 空文字 + exit 0
        p = tmp_path / "broken.yaml"
        p.write_text("worker_model: [unclosed\n", encoding="utf-8")
        result = lib_model.resolve(str(p), "docs")
        assert result == ""

    def test_unknown_model_id_treated_as_sonnet_rank(self, tmp_path, capsys):
        # opus/sonnet/haiku を含まない未知の ID は rank 2 (sonnet 相当) として扱い、
        # それより低いランクのモデルには勝つ
        p = tmp_path / "cfg.yaml"
        p.write_text(
            "worker_model: my-custom-model\n"
            "model_per_skill:\n"
            "  docs: claude-haiku-4-5-20251001\n"
            "  custom: my-custom-model\n",
            encoding="utf-8",
        )
        # custom(unknown→rank2) vs docs(haiku→rank1) → custom が勝つ
        result = lib_model.resolve(str(p), "custom,docs")
        assert result == "my-custom-model"
        # stderr に警告が出る
        captured = capsys.readouterr()
        assert "unknown model id" in captured.err

    def test_empty_worker_model_and_no_match_returns_empty(self, tmp_path):
        # worker_model が空 で skills も未定義 → 空文字
        p = tmp_path / "cfg.yaml"
        p.write_text("worker_model:\n", encoding="utf-8")
        result = lib_model.resolve(str(p), "code")
        assert result == ""


# ---------------------------------------------------------------------------
# _model_rank のユニットテスト
# ---------------------------------------------------------------------------
class TestModelRank:
    def test_opus_rank(self):
        assert lib_model._model_rank("claude-opus-5") == 3

    def test_sonnet_rank(self):
        assert lib_model._model_rank("claude-sonnet-5") == 2

    def test_haiku_rank(self):
        assert lib_model._model_rank("claude-haiku-4-5-20251001") == 1

    def test_unknown_rank_is_sonnet_level(self):
        assert lib_model._model_rank("gpt-4o") == 2

    def test_case_insensitive(self):
        assert lib_model._model_rank("Claude-Opus-5") == 3
