"""checkpoint の redaction は値の長さで切らない (PR #307 Codex 1 巡目 P1-1)。

全パターン × 値の長さ (旧上限 -1 / ちょうど / +1 / 1MB) × 引用符あり・なしで、保存物に入力の秘密の
**どの 8 文字以上の部分文字列も**残らないこと。扱える上限を超える値は「保存しない」側に倒れること。
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_checkpoint as ck  # noqa: E402

# 旧実装の上限の前後 (assign 1000 / bearer 512 / token 255 / op 300) と大きい値
LENGTHS = [15, 16, 19, 20, 254, 255, 256, 299, 300, 301, 511, 512, 513, 999, 1000, 1001, 5000, 1_000_000]


def secret_body(n, seed="Zq7"):
    """文字が繰り返しにならない n 文字 (部分文字列の照合が意味を持つ)。区切りになる文字は含めない。"""
    out, i = [], 0
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    x = sum(map(ord, seed))
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out.append(alphabet[x % len(alphabet)])
        i += 1
    return "".join(out)


def windows(secret, k=8):
    """secret の k 文字以上の部分文字列のうち、全部を調べると重いので全 k 文字窓 (これが無ければ長いものも無い)。"""
    step = max(1, len(secret) // 4000)
    return [secret[i:i + k] for i in range(0, len(secret) - k + 1, step)]


def assert_no_leak(secret, redacted):
    for w in windows(secret):
        assert w not in redacted, "秘密の部分文字列が残った"
    # 末尾は必ず確かめる (間引いた窓が末尾を飛ばさない)
    assert secret[-8:] not in redacted and secret[:8] not in redacted


# (名前, 入力の組み立て, 値の最小長)。{v} が秘密の本体
ASSIGN_KEYS = ["PASSWORD", "TOKEN", "SECRET", "API_KEY", "passwd", "DB_PASSWORD", "Secret_Value"]


@pytest.mark.parametrize("n", LENGTHS)
@pytest.mark.parametrize("key", ASSIGN_KEYS)
@pytest.mark.parametrize("quote", ["", '"', "'"], ids=["bare", "dq", "sq"])
def test_assignment_values_of_any_length_are_replaced_whole(key, quote, n):
    v = secret_body(n)
    for sep in ("=", ": "):
        text = f"before {key}{sep}{quote}{v}{quote} after\nnext line\n"
        red, count = ck.redact(text)
        assert count >= 1
        assert_no_leak(v, red)
        if quote:
            assert red.endswith("after\nnext line\n") or "after" in red


@pytest.mark.parametrize("n", LENGTHS)
def test_unterminated_quote_redacts_the_rest_of_the_line(n):
    v = secret_body(n)
    red, _ = ck.redact(f'PASSWORD="{v}\nsecond line stays\n')
    assert_no_leak(v, red)
    assert "second line stays" in red


@pytest.mark.parametrize("n", LENGTHS)
def test_bearer_values_of_any_length(n):
    v = secret_body(n)
    red, count = ck.redact(f"Authorization: Bearer {v}\nnext\n")
    assert count == 1
    assert_no_leak(v, red)
    assert "Authorization: Bearer " in red and red.endswith("next\n")


TOKEN_MAKERS = {
    "github": lambda b: "gh" + "p_" + b,
    "github_pat": lambda b: "github_" + "pat_" + b,
    "openai": lambda b: "sk" + "-" + b,
    "aws": lambda b: "AK" + "IA" + b.upper(),
    "slack": lambda b: "xox" + "b-" + b,
    "op": lambda b: "op" + "://" + b,
}


@pytest.mark.parametrize("n", LENGTHS)
@pytest.mark.parametrize("kind", sorted(TOKEN_MAKERS))
@pytest.mark.parametrize("quote", ["", '"', "'"], ids=["bare", "dq", "sq"])
def test_prefixed_tokens_of_any_length(kind, quote, n):
    body = secret_body(n)
    tok = TOKEN_MAKERS[kind](body)
    if len(body) < {"github": 20, "github_pat": 20}.get(kind, 16):   # 印の後ろの最低長に満たない短い値は対象外 (旧実装と同じ)
        return
    red, count = ck.redact(f"x {quote}{tok}{quote} y\n")
    assert count >= 1
    assert_no_leak(tok, red)


def test_token_with_characters_outside_the_old_class_is_replaced_to_the_delimiter():
    tok = "sk" + "-" + "abcdefghijklmnop" + ".qrstuvwx+yz/0123456789=="
    red, _ = ck.redact(f"v {tok} z")
    assert_no_leak(tok, red)
    assert red.endswith(" z")


def test_long_key_names_do_not_hide_the_assignment():
    v = secret_body(40)
    red, count = ck.redact("A" * 100 + "_PASSWORD" + "B" * 100 + "=" + v + "\n")
    assert count == 1
    assert_no_leak(v, red)


def test_json_style_and_escaped_quotes():
    v = secret_body(300)
    red, _ = ck.redact('{"password": "' + v[:150] + '\\"' + v[150:] + '", "next": 1}')
    assert_no_leak(v, red)
    assert '"next": 1' in red


def test_private_key_without_end_is_redacted_to_the_end():
    body = secret_body(5000)
    red, count = ck.redact(f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n")
    assert count == 1 and body[:20] not in red


def test_a_value_over_the_scan_limit_raises_so_the_file_is_not_saved(monkeypatch):
    monkeypatch.setattr(ck, "VALUE_SCAN_LIMIT", 1000)
    with pytest.raises(ck.RedactionError):
        ck.redact("PASSWORD=" + "x" * 1001)
    with pytest.raises(ck.RedactionError):
        ck.redact("Bearer " + "y" * 1001)
    # _save_text は例外を「保存しない」に倒す
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        info, n = ck._save_text(d, "f.txt", "PASSWORD=" + "x" * 1001, limit=10_000)
        assert info is None and not pathlib.Path(d, "f.txt").exists()


def test_the_saved_file_never_holds_the_tail_of_a_long_value(tmp_path):
    v = secret_body(1_000_000)
    with ck.SafeDir.open_root(tmp_path) as out:                 # 書き込みは dir fd 基点 (fix 4)
        info, count = ck._save_text(out, "f.txt", f'PASSWORD="{v}TAIL_SECRET"\n', limit=ck.PATCH_LIMIT)
    saved = (tmp_path / "f.txt").read_text()
    assert info is not None and count == 1
    assert "TAIL_SECRET" not in saved and v[-12:] not in saved


@pytest.mark.parametrize("text_of", [
    lambda: "sk-" * 400_000,
    lambda: "TOKEN" * 200_000,
    lambda: "Bearer " * 150_000,
    lambda: "PASSWORD:" + " " * 1_000_000,
    lambda: "PASSWORD=" * 100_000,
    lambda: "gh" + "p_" * 300_000,
    lambda: ("a=b " * 250_000),
    lambda: 'PASSWORD="' + "\\" * 200_000,
], ids=["sk", "TOKEN", "Bearer", "spaces", "assign-repeat", "ghp", "benign", "backslashes"])
def test_pathological_1mb_inputs_stay_linear(text_of):
    text = text_of()
    t0 = time.monotonic()
    ck.redact(text)
    assert time.monotonic() - t0 < 10, "redaction が線形でない"


def test_task_graph_style_names_are_not_mistaken_for_tokens():
    assert ck.redact("tasks/task-graph-notes-for-later.md")[1] == 0
    assert ck.path_is_secret("scripts/task-graph-notes-for-later.md") is False
