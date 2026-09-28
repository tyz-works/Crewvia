"""t021 (mission 20260927-mechanize-guards-b / B6, backlog #28):
start.sh が claude を起動する前に、cwd の trust を `~/.claude.json` で確かめて止める (scripts/lib_trust.py)。

信頼されていない cwd で claude を起動すると trust ダイアログが出る。既定の選択は "No, exit" で、
start.sh の kickoff の Enter がそれを選んで claude が終了する。この検査は 2 枚:

  * 事前検査 `judge()` / `lib_trust.py check` — 設定を読み、cwd か祖先に `hasTrustDialogAccepted: true` が
    あるか。**読めない・形が違う = unverifiable (止める)。「信頼済み」に潰さない**
  * 最後の網 `screen_shows_trust_dialog()` / `lib_trust.py dialog` — pane の画面にダイアログの文言があるか

start.sh を通した振る舞い (拒否・ログ・起動しないこと・最後の網) は tests/start-sh-trust-precheck.bats。

本物の `~/.claude.json` は読まない・書かない: 全テストが `config_file=` か `CLAUDE_CONFIG_DIR` で
使い捨ての設定を指す。

実行方法:
  python3 -m pytest tests/test_trust_precheck.py -v
"""

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_trust  # noqa: E402
from lib_trust import TRUSTED, UNTRUSTED, UNVERIFIABLE  # noqa: E402


def write_config(tmp_path, projects, name=".claude.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return path


def trusted(value=True):
    return {"hasTrustDialogAccepted": value}


@pytest.fixture
def work(tmp_path):
    """信頼の検査対象になる dir: <tmp>/proj/sub/leaf。"""
    leaf = tmp_path / "proj" / "sub" / "leaf"
    leaf.mkdir(parents=True)
    return leaf


# ---------------------------------------------------------------------------
# 信頼済み / 未信頼
# ---------------------------------------------------------------------------

def test_the_dir_itself_recorded_as_trusted(tmp_path, work):
    cfg = write_config(tmp_path, {str(work): trusted()})
    v = lib_trust.judge(str(work), config_file=str(cfg))
    assert v.status == TRUSTED
    assert v.via == str(work)


def test_a_trusted_ancestor_covers_everything_below_it(tmp_path, work):
    """実測: 信頼済みの祖先の配下ではダイアログが出ない。"""
    cfg = write_config(tmp_path, {str(work.parent.parent): trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == TRUSTED


def test_the_filesystem_root_trusted_covers_everything(tmp_path, work):
    cfg = write_config(tmp_path, {"/": trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == TRUSTED


def test_a_sibling_or_a_child_does_not_cover_the_dir(tmp_path, work):
    """信頼は下へ継承されるだけ。兄弟・子の記録は cwd の信頼にならない。"""
    sibling = work.parent / "other"
    sibling.mkdir()
    child = work / "child"
    child.mkdir()
    cfg = write_config(tmp_path, {str(sibling): trusted(), str(child): trusted()})
    v = lib_trust.judge(str(work), config_file=str(cfg))
    assert v.status == UNTRUSTED


def test_a_dir_never_recorded_is_untrusted(tmp_path, work):
    cfg = write_config(tmp_path, {"/somewhere/else": trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == UNTRUSTED


def test_explicit_false_is_untrusted_and_an_entry_without_the_field_is_no_evidence(tmp_path, work):
    cfg = write_config(tmp_path, {str(work): trusted(False), str(work.parent): {"allowedTools": []}})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == UNTRUSTED


def test_a_false_on_the_dir_does_not_override_a_trusted_ancestor(tmp_path, work):
    """claude の判定は「祖先のどれかが true」。近い false が遠い true を打ち消さない。"""
    cfg = write_config(tmp_path, {str(work): trusted(False), str(work.parent.parent): trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == TRUSTED


def test_no_projects_key_at_all_is_untrusted_not_unverifiable(tmp_path, work):
    """設定が読めて `projects` が無い = 何も信頼していない、が事実。読めない (11) とは別。"""
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"numStartups": 3}), encoding="utf-8")
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == UNTRUSTED


# ---------------------------------------------------------------------------
# 「読めない」を「信頼済み」に潰さない (unverifiable = 止める)
# ---------------------------------------------------------------------------

def test_a_missing_config_is_untrusted_and_says_so(tmp_path, work):
    v = lib_trust.judge(str(work), config_file=str(tmp_path / "absent.json"))
    assert v.status == UNTRUSTED
    assert v.config_exists is False


@pytest.mark.parametrize("content", [
    "",                              # 途中で切れた・空
    "{not json",
    "[]",                            # 最上位が object でない
    "null",
    '{"projects": []}',              # projects が object でない
    '{"projects": "x"}',
])
def test_a_config_that_cannot_be_used_is_unverifiable(tmp_path, work, content):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(content, encoding="utf-8")
    v = lib_trust.judge(str(work), config_file=str(cfg))
    assert v.status == UNVERIFIABLE, v.reason
    assert v.reason


def test_a_config_that_is_a_directory_is_unverifiable(tmp_path, work):
    """読めなかった (通常ファイルでない) を「無い」にも「信頼済み」にも読まない。"""
    d = tmp_path / "dir.json"
    d.mkdir()
    assert lib_trust.judge(str(work), config_file=str(d)).status == UNVERIFIABLE


def test_a_config_that_cannot_be_decoded_is_unverifiable(tmp_path, work):
    cfg = tmp_path / ".claude.json"
    cfg.write_bytes(b'{"projects": {"\xff\xfe": {}}}')
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == UNVERIFIABLE


@pytest.mark.parametrize("value", ["true", 1, None, "yes", [], {}])
def test_only_the_boolean_true_trusts_a_dir(tmp_path, work, value):
    """truthiness で信頼済みにしない: 文字列 "true"・1・null は bool ではない = 決められない = 止める。"""
    cfg = write_config(tmp_path, {str(work): trusted(value)})
    v = lib_trust.judge(str(work), config_file=str(cfg))
    assert v.status == UNVERIFIABLE, (value, v.reason)


def test_a_relevant_entry_that_is_not_an_object_is_unverifiable(tmp_path, work):
    cfg = write_config(tmp_path, {str(work.parent): "trusted"})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == UNVERIFIABLE


def test_a_malformed_entry_for_an_unrelated_project_changes_nothing(tmp_path, work):
    """関係ないプロジェクトの記録が壊れていても、判定は他の記録だけで決まる。"""
    cfg = write_config(tmp_path, {"/unrelated": "garbage", str(work): trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == TRUSTED
    cfg2 = write_config(tmp_path, {"/unrelated": "garbage"}, name="two.json")
    assert lib_trust.judge(str(work), config_file=str(cfg2)).status == UNTRUSTED


def test_a_trusted_ancestor_wins_over_a_malformed_nearer_entry(tmp_path, work):
    cfg = write_config(tmp_path, {str(work): trusted("true"), str(work.parent.parent): trusted()})
    assert lib_trust.judge(str(work), config_file=str(cfg)).status == TRUSTED


# ---------------------------------------------------------------------------
# パスの正規化
# ---------------------------------------------------------------------------

def test_a_symlinked_dir_is_trusted_by_its_physical_path(tmp_path, work):
    link = tmp_path / "link"
    link.symlink_to(work)
    cfg = write_config(tmp_path, {str(work.resolve()): trusted()})
    assert lib_trust.judge(str(link), config_file=str(cfg)).status == TRUSTED


def test_a_symlinked_dir_is_trusted_by_its_logical_path(tmp_path, work):
    """論理パスで信頼された (物理パスへ畳むと一致を失う) 記録も落とさない。"""
    link = tmp_path / "link"
    link.symlink_to(work)
    cfg = write_config(tmp_path, {str(link): trusted()})
    assert lib_trust.judge(str(link), config_file=str(cfg)).status == TRUSTED


def test_a_symlinked_ancestor_of_the_dir_is_followed_both_ways(tmp_path, work):
    link = tmp_path / "link"
    link.symlink_to(work.parent.parent)          # link -> proj
    cfg = write_config(tmp_path, {str(work.parent.parent.resolve()): trusted()})
    assert lib_trust.judge(str(link / "sub" / "leaf"), config_file=str(cfg)).status == TRUSTED


def test_trailing_slash_and_dotdot_do_not_hide_a_match(tmp_path, work):
    cfg = write_config(tmp_path, {str(work) + "/": trusted()})
    assert lib_trust.judge(str(work) + "/", config_file=str(cfg)).status == TRUSTED
    assert lib_trust.judge(str(work / ".." / "leaf"), config_file=str(cfg)).status == TRUSTED


def test_unicode_normalisation_form_does_not_hide_a_match(tmp_path):
    """macOS 由来の NFD で記録された key も、NFC の dir に一致する (claude 自身が両方を種まきする)。"""
    nfc = unicodedata.normalize("NFC", "プロジェクトé")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    d = tmp_path / nfc
    d.mkdir()
    cfg = write_config(tmp_path, {str(tmp_path / nfd): trusted()})
    assert lib_trust.judge(str(d), config_file=str(cfg)).status == TRUSTED


def test_a_prefix_of_the_name_is_not_an_ancestor(tmp_path):
    """/a/proj は /a/proj-2 の祖先ではない (文字列の前方一致でなく、ディレクトリ境界で見る)。"""
    (tmp_path / "proj").mkdir()
    other = tmp_path / "proj-2"
    other.mkdir()
    cfg = write_config(tmp_path, {str(tmp_path / "proj"): trusted()})
    assert lib_trust.judge(str(other), config_file=str(cfg)).status == UNTRUSTED


# ---------------------------------------------------------------------------
# どの設定を読むか / 書き換えないこと
# ---------------------------------------------------------------------------

def test_claude_config_dir_wins_over_home(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "HOME": str(tmp_path / "home")}
    assert lib_trust.claude_json_path(env) == str(tmp_path / "cfg" / ".claude.json")


def test_without_claude_config_dir_it_is_dot_claude_json_in_home(tmp_path):
    assert lib_trust.claude_json_path({"HOME": str(tmp_path)}) == str(tmp_path / ".claude.json")
    assert lib_trust.claude_json_path({"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": ""}) == str(
        tmp_path / ".claude.json")


def test_the_config_is_never_modified(tmp_path, work):
    """trust は利用者の判断。判定も拒否メッセージの生成も、設定を 1 バイトも変えない。"""
    cfg = write_config(tmp_path, {"/elsewhere": trusted()})
    before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
    v = lib_trust.judge(str(work), config_file=str(cfg))
    lib_trust.refusal_message(v, str(work))
    assert (cfg.read_bytes(), cfg.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [".claude.json"]


# ---------------------------------------------------------------------------
# 拒否メッセージ
# ---------------------------------------------------------------------------

def test_the_untrusted_message_gives_commands_the_user_can_type(tmp_path):
    spaced = tmp_path / "my project"
    spaced.mkdir()
    cfg = write_config(tmp_path, {})
    v = lib_trust.judge(str(spaced), config_file=str(cfg))
    msg = lib_trust.refusal_message(v, str(spaced))
    assert "claude に信頼されていない" in msg
    assert f"cd '{spaced}' && claude" in msg                    # 空白入りのパスは引用される
    assert "jq --arg k '" + str(spaced) + "'" in msg
    assert "hasTrustDialogAccepted = true" in msg
    assert "書き換えません" in msg                                # crewvia は書かない、と明示


def test_the_message_offers_no_jq_command_when_there_is_no_config_to_edit(tmp_path, work):
    v = lib_trust.judge(str(work), config_file=str(tmp_path / "absent.json"))
    msg = lib_trust.refusal_message(v, str(work))
    assert "&& claude" in msg and "jq --arg" not in msg


def test_the_unverifiable_message_says_why_and_does_not_offer_to_trust(tmp_path, work):
    cfg = tmp_path / ".claude.json"
    cfg.write_text("{broken", encoding="utf-8")
    v = lib_trust.judge(str(work), config_file=str(cfg))
    msg = lib_trust.refusal_message(v, str(work))
    assert "確認できない" in msg and str(cfg) in msg
    assert "jq --arg" not in msg                                  # 壊れたファイルに jq で書かせない


# ---------------------------------------------------------------------------
# CLI (start.sh が呼ぶ形)
# ---------------------------------------------------------------------------

def run_cli(*args, config_dir=None, stdin=None):
    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return subprocess.run([sys.executable, str(SCRIPTS / "lib_trust.py"), *args],
                          capture_output=True, text=True, env=env, input=stdin)


def test_cli_check_trusted_is_silent_and_exits_0(tmp_path, work):
    write_config(tmp_path, {str(work): trusted()})
    r = run_cli("check", str(work), config_dir=tmp_path)
    assert (r.returncode, r.stdout) == (0, "")


def test_cli_check_untrusted_exits_10_with_the_message_on_stdout(tmp_path, work):
    write_config(tmp_path, {})
    r = run_cli("check", str(work), config_dir=tmp_path)
    assert r.returncode == 10
    assert "信頼されていない" in r.stdout


def test_cli_check_unverifiable_exits_11(tmp_path, work):
    (tmp_path / ".claude.json").write_text("{broken", encoding="utf-8")
    r = run_cli("check", str(work), config_dir=tmp_path)
    assert r.returncode == 11
    assert "確認できない" in r.stdout


def test_cli_usage_errors_are_not_a_trusted_answer(tmp_path):
    for args in ((), ("check",), ("bogus",)):
        r = run_cli(*args, config_dir=tmp_path)
        assert r.returncode == 2, args


# ---------------------------------------------------------------------------
# 最後の網: pane の画面
# ---------------------------------------------------------------------------

# claude 2.1.283 のバンドルにある文言 (Quick safety check / Yes, I trust this folder / No, exit)。
DIALOG_2_1_283 = """\
 Accessing workspace:

 /home/user/newproj

 Quick safety check: Is this a project you created or one you trust? (Like your own
 code, a well-known open source project, or work from your team). If not, take a
 moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

DIALOG_OLDER = """\
 Do you trust the files in this folder?

 /home/user/newproj

 ❯ 1. Yes, proceed
   2. No, exit
"""

# t099 (P2-2): DIALOG_OLDER と同じ画面だが、カーソルが選択肢 2 (拒否) にある。フッタ (Enter to
# confirm) も無い旧版の形。カーソル位置に関わらず、選択肢の組そのものがダイアログの証拠になる。
DIALOG_OLDER_CURSOR_ON_DECLINE = """\
 Do you trust the files in this folder?

 /home/user/newproj

   1. Yes, proceed
 ❯ 2. No, exit
"""


@pytest.mark.parametrize("screen", [
    DIALOG_2_1_283,
    DIALOG_OLDER,
    DIALOG_OLDER_CURSOR_ON_DECLINE,
    # 折り返しで文言が行をまたいでも見つける (実物と同じく、選択肢の構造も画面に乗っている)
    "Quick safety check: Is this a project you\n  created or one you trust?"
    "\n\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n\n Enter to confirm · Esc to cancel",
    "❯ 1. Yes, I\n    trust this folder",
    # 大文字小文字・余分な空白
    "QUICK   SAFETY\nCHECK\n\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n\n Enter to confirm · Esc to cancel",
    # 単独の "No, exit" は決定の操作案内と揃ったときだけ数える
    "  2. No, exit\n\n Enter to confirm · Esc to cancel",
])
def test_the_trust_dialog_is_recognised(screen):
    assert lib_trust.screen_shows_trust_dialog(screen)


@pytest.mark.parametrize("screen", [
    "",
    "❯ ",
    "user@host:~/proj$ claude\n",
    # 普通の claude の入力画面 (`❯` が入力行の目印として出る)
    "╭────────────────────╮\n│ ❯                  │\n╰────────────────────╯\n  ? for shortcuts",
    # 単独の "No, exit" は別の画面にも出うる
    "  2. No, exit",
    "Enter to confirm",
    # ダイアログ以外の選択ダイアログ (permission プロンプト)
    "Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently",
    # --- 族B: 文言の部分一致だけでは同定にならない (t078) ---
    # (1) 信頼済みディレクトリのパスに文言を含む
    "user@host:/tmp/quick safety check$ claude\n╭──────╮\n│ ❯    │\n╰──────╯",
    "[crewvia] WORK_DIR=/tmp/quick safety check\n╭──────╮\n│ ❯    │\n╰──────╯",
    # (2) 送信後の画面に、task の出力として文言が引用されている
    "❯ echo 'Quick safety check: looks fine to me'\nQuick safety check: looks fine to me\n❯ ",
    # (3) ダイアログの文言を含むファイル名・ブランチ名がステータス行に出ている
    "On branch feat/quick safety check\nnothing to commit, working tree clean\n❯ ",
    "modified: docs/quick safety check.md\n❯ ",
    # --- 族B (続き, t089 / PR#237 4巡目 P2-1): 文言と選択肢の構造を「画面のどこかに独立に」
    # 拾うだけでは、無関係な 2 箇所の組み合わせでも一致してしまう (t078 はここまでしか塞がなかった)。
    # パスの文言 (Working directory: .../quick safety check) と、trust ダイアログとは無関係な
    # 普通の権限確認メニュー (Bash 実行の確認。選択肢自身の文言は "Yes" / "No, and tell Claude ..."
    # であり、trust ダイアログの選択肢 "Yes, I trust this folder" / "No, exit" ではない) が
    # 同じ画面に乗ると、旧実装 (文言 = 部分一致 / 構造 = カーソル+数字 or confirm、を独立に判定) は
    # 誤って True を返す (直接確認済み)。文言と選択肢は「同じダイアログの枠」になければならない。
    "[crewvia] Working directory: /tmp/quick safety check\n\n"
    "Bash command\nnpm test\n\n"
    "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently\n\n"
    "Enter to confirm · Esc to cancel",
    # 同じ族: フレーズが「No, exit」ではなく他の phrase (quick safety check) 由来でも同様に誤検出しない。
    "user@host:/tmp/quick safety check$ claude\n\n"
    "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently\n\n"
    "Enter to confirm · Esc to cancel",
    # --- 族B (続き, t099 / PR#237 5巡目 P2-1): 単独の "No, exit" フォールバックが画面のどこにあっても
    # よい独立部分一致だったための再発。cwd のパスに文言そのもの ("No, exit") が入っており、離れた場所に
    # ある無関係な権限確認メニューの Enter to confirm フッタと組み合わさって誤って一致してしまう。
    "[crewvia] Working directory: /tmp/No, exit\n\n"
    "Bash command\nnpm test\n\n"
    "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently\n\n"
    "Enter to confirm · Esc to cancel",
    "user@host:/tmp/No, exit$ claude\n\n"
    "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently\n\n"
    "Enter to confirm · Esc to cancel",
])
def test_ordinary_screens_are_not_mistaken_for_the_dialog(screen):
    assert not lib_trust.screen_shows_trust_dialog(screen)


def test_cli_dialog_exit_codes():
    assert run_cli("dialog", stdin=DIALOG_2_1_283).returncode == 0
    assert run_cli("dialog", stdin="❯ ").returncode == 1
    assert run_cli("dialog", stdin="").returncode == 1


def test_cli_dialog_survives_bytes_that_are_not_utf8():
    r = run_cli("dialog", stdin=None)
    assert r.returncode in (0, 1)
    raw = subprocess.run([sys.executable, str(SCRIPTS / "lib_trust.py"), "dialog"],
                         input=b"\xff\xfe Quick safety check\n \xe2\x9d\xaf 1. Yes, I trust this folder"
                               b"\n   2. No, exit",
                         capture_output=True,
                         env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"})
    assert raw.returncode == 0
