# cursor (Cursor CLI: cursor-agent) をレビュー先にする

`scripts/kai-review.sh` の「codex exec を走らせる段」を Cursor CLI に差し替えられる（agy と同じ作り。`knowledge/agy-review.md`）。
前後（pull・Execution ID の照合・findings の判定・needs-director / done の報告）は codex と共通。設定の読み口は `scripts/lib_review_engine.py` の 1 か所。

**判定に使う既定にはしない**（下の「worktree の外を読む」）。比較用（`both` + `compare`）か、内容を承知の上で単独で使う。

## 設定

`config/crewvia.yaml` の `review:`（既定は今までと同じ。merge しても挙動は変わらない）:

| 欄 | 既定 | 意味 |
|---|---|---|
| `engine` | `codex` | `codex` / `agy` / `cursor` / `both`。env `CREWVIA_REVIEW_ENGINE` が上書き（engine だけ） |
| `compare` | `[agy]` | `both` のときに Codex の横に併記するエンジンの一覧（`agy` / `cursor` の部分集合。空・重複・他の値は不正）。既定 = 従来の `both` と同じ |
| `cursor_model` | `grok-4.7-high` | `cursor-agent models` に出る名前。括弧付きパラメータ（`name[effort=high]`）も通す。`claude-fable-*` は拒否（下記） |
| `cursor_timeout_seconds` | `900` | `timeout` で包む秒数（cursor-agent にタイムアウトのフラグが無い） |

- 不正値・読めない config は codex に倒さず `needs-director`（固定コード `bad_compare` / `bad_cursor_model` / `bad_cursor_timeout`）。他は `knowledge/agy-review.md`「設定」と同じ。
- `compare: [agy, cursor]` の併記は Result に `### <engine> (比較用 — 判定は Codex。model=…)`。比較側の失敗は判定に使わず、失敗の理由と診断を併記する。

## 利用枠（2026-10-27 実測・このアカウント）

- サブスクのログインのまま（`agent login`）。API キーは使わない。**on-demand（従量課金）は Cursor の設定で切っておく**（切っていないと枠を超えた分が課金されうる）。
- **週の枠で使えるのは Grok 系だけ**。`gpt-5.6-sol-high` / `gemini-4-argon-high` は約 9 秒で exit 1・stdout 空・stderr
  `ActionRequiredError: <model> is not included in your weekly usage ...`（課金されず止まる）→ `CURSOR FAILURE: exit_1` と、診断に伏せ字後の stderr の行が出る。
- 1 回 3〜10 分（Grok 4.7 high で約 10.5 分の実測あり）。レビューのたびに枠を使う。
- **「NO ZDR」と付くモデル（`claude-fable-*`）はデータ保持の契約外なので使わない**。config の検証で拒否する（大文字小文字を問わず前方一致）。他に NO ZDR が増えたら `_CURSOR_DENIED_PREFIXES` に足す。

## 呼び方（cursor-agent 2026.10.22）

```
cd $CURSOR_WT && timeout --kill-after=10s <N>s cursor-agent -p --model <cursor_model> --mode plan --trust \
    --output-format json --add-dir <diff の dir> "<prompt>"
```

- `--mode plan` = 読み取り専用（ファイルを書かない）。**`--force` / `--yolo` / `--approve-mcps` / `--sandbox disabled` は使わない**（テストが argv で確かめる）。
- `--trust` はヘッドレスで worktree を信頼するため（Director の実測の形。無しの挙動は利用枠を使うので確かめていない）。PR の中身を信頼することになるので、**レビュー用 worktree の木のどこかに `.cursor/`（MCP・権限・hook・rules）か `.cursorrules` があれば走らせない**（`workspace_has_cursor_config`。`git ls-files` で見る = symlink も含む）。crewvia には無い。さらに **PR が agent の設定（`.claude/` の settings・hooks・skills、`.mcp.json`、`.codex/`、`.vscode/`、`AGENTS.md`）を変えていたら走らせない**（`pr_changes_agent_config`。base…HEAD の変更ファイル名で見る）。crewvia 自身が `.claude/` を持つので「ある」では拒否できない — base（main = レビュー済み）の中身は信頼し、PR の変更だけを信頼しない。PR が足した・変えた **symlink は行き先を問わず拒否**（名前が `.claude` で終わる link・設定の場所へ向ける link）。`.cursorignore`（レビュー対象を隠せる）も木にあれば拒否。**`CLAUDE.md` の変更は拒否しない**: crewvia の PR はよく `CLAUDE.md` を変えるので拒否すると cursor がほぼ走らない。効くのは prompt への影響だけで、diff 本文からの prompt injection と同じ種類（cursor は `--mode plan` で書かず、判定の既定にもしない）。MCP は `--approve-mcps` を付けないので承認されない。
- prompt は位置引数で最後。diff は argv に載せず、worktree の外の `mktemp -d` に `review.diff` を書いて prompt でパスを示す（`--add-dir` で足す）。prompt は「ファイルを読むだけ・コマンドは実行しない・書かない」。
- `--json-schema` は無い。stdout は JSON 1 個 `{"type":"result","subtype":"success","is_error":false,"result":"<自由文>",...}` で、`result` は**前置きの文の後ろに JSON**（` ```json ` の囲みのこともある）。
  `lib_review_engine.py extract-cursor` が「末尾の `{` 空白 `"findings"` 空白 `:` から始まる JSON（後ろは空白か ``` だけ。整形された `{\n  "findings": …}` も拾う）」を取り出し、`config/kai-review-findings.schema.json` で検証する（additionalProperties・enum・required・型）。

## fail-closed（findings 無しの LGTM に倒さない）

次のどれかなら `needs-director`（`CURSOR FAILURE: <理由>`）:

| 理由 | 条件 |
|---|---|
| `cursor_agent_not_found` | `cursor-agent` が PATH に無い |
| `workspace_has_cursor_config` | レビュー用 worktree の木に `.cursor` / `.cursorrules` / `.cursorignore` がある（PR が持ち込んだ設定を `--trust` で読ませない） |
| `pr_changes_agent_config` | PR が `.claude` / `.mcp.json` / `.codex` / `.vscode` / `AGENTS.md` を変えている、または symlink を足した・変えた |
| `timeout` | `cursor_timeout_seconds` を超えた（exit 124） |
| `timeout_killed` | 超えた後の SIGTERM を無視し、10 秒後の SIGKILL（`--kill-after`）で落とした（exit 137）。`--kill-after` が無いと SIGTERM を無視する cursor-agent を永遠に待ち、needs-director も後始末も走らず `both` で Codex も始まらない（Codex #313 r2 P2） |
| `exit_<N>` | exit が 0 以外（枠外のモデルは `exit_1` + `ActionRequiredError`） |
| `unparseable_output` | stdout が JSON 1 個でない |
| `is_error` | `is_error` が `false` でない（欠けも含む） |
| `subtype` | `type` が `result` でない / `subtype` が `success` でない |
| `no_findings_json` | `result` の末尾に findings の JSON が無い（後ろに文が続く場合も） |
| `schema_invalid` | 末尾の JSON が schema に通らない |
| `redaction_failed` | findings の伏せ字が失敗した（中身を出さない） |
| `worktree_modified` / `worktree_status_failed` | cursor 専用 worktree（`kai-review-cursor-wt.*`）の**指紋**が実行の前後で違う / 取れない。指紋 = `git status --porcelain=v1 -z --ignored=matching --untracked-files=all` + 出た各パスの lstat（`lib_review_engine.py fingerprint`）。`--porcelain` だけだと ignored のファイル（`.claude/settings.local.json` 等。PR が ignore を足すことも可）の追加・書き換えを見ない（Codex #313 r2 P2） |
| `worktree_create_failed` | cursor 専用 worktree を作れなかった |

失敗の診断（`### cursor diagnostics (redacted)`）は `lib_review_engine.diagnose --engine cursor`: stdout の `type` / `subtype` / `is_error` だけ（**`result` 本文は出さない**）と stderr の末尾。lib_checkpoint の伏せ字を通してから切る。

## worktree の外を読む（重要）

- cursor-agent は **`--mode plan` でも worktree の外を読む**。実測で Grok は主 repo の `.git`・別 task の worktree を、Composer は Director の scratchpad にあった前回の Grok の出力を Read して丸写しした
  （証拠: `~/.cursor/projects/<ws>/agent-transcripts/*.jsonl` の Read / Glob / Grep の path）。
- **`--sandbox enabled` でも止まらない**（attempt 2 の実測: `enabled` / `disabled` のどちらでも、worktree の外に置いた目印ファイルを読んで返した）。
- したがって: 秘密（HOME 配下の設定・鍵）や、他 engine の出力・同時に走る別のレビューの一時物が**読まれうる**。判定に使う既定にしない。
- kai-review.sh 側の手当て:
  - cursor は **専用の使い捨て worktree**（PR head と同じ commit。`mktemp -d kai-review-cursor-wt.*` に `git worktree add --detach`）で走らせ、成否を問わず終わったら消す。Codex・agy が読む REVIEW_WT では走らせない（書き換え・途中失敗の汚れを他 engine が読む。Codex #313 r1 P1）。`both` では cursor の前後で REVIEW_WT の指紋（ignored を含む。上の表）を比べ、違えば・取れなければ（cursor が外を書いた）`REVIEW WORKTREE MODIFIED` で needs-director。
  - `both` では cursor を **Codex・agy より先に**走らせる（その時点で他 engine の出力はまだ無い）。agy・cursor の一時物は走り終えたらすぐ消す。併記の順は `compare` の順のまま。
  - 取り出した findings は Result に入る前に lib_checkpoint の伏せ字を通す（外で読んだ秘密を Result に写さない）。直列化した JSON でなく**値ごと**（title / body / file）に通す（`\"` のエスケープで伏せ字の規則が外れない）。
  - Result の cursor の節に「worktree の外も読める」の注意を付ける。
- 実行後に transcript の path を検査して「汚染の可能性」を出す案は入れていない（transcript の形は版で変わりうる・実測 1 版だけで検査器を作ると黙って外れる）。疑わしいときは上の transcript を人が見る。

## 戻し方

`review.engine` を `codex` に、`compare` を `[agy]` に戻す（または env を外す）。コードを戻すなら PR revert → `scripts/sync-main-checkout.sh`。

テスト: `tests/test_kai_review_engine_cursor.py`（cursor-agent は stub。設定・取り出し・各 fail-closed・枠外の `ActionRequiredError`・伏せ字・`.cursor/` の拒否・`both` + `compare` の順序と併記・既定の `compare`）。
