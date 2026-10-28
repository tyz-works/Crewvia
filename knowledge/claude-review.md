# claude (Claude Code CLI) と ensemble (agy + claude の合議) をレビュー先にする

2026-10-28 のユーザー決定: Codex をやめて費用を下げる。すでに払っているサブスクの Claude と Gemini (agy) だけで
レビューの判定ができるようにする。`scripts/kai-review.sh` の「レビューを走らせる段」だけを差し替える。前後（pull・Execution ID の照合・
findings の判定・needs-director / done の報告）は codex / agy と共通。設定の読み口は `scripts/lib_review_engine.py` の 1 か所。
作りは agy（`knowledge/agy-review.md`）・cursor（`knowledge/cursor-review.md`）に揃えた。

## 設定

`config/crewvia.yaml` の `review:`（**既定は今までと同じ `engine: codex`・`compare: [agy]`。merge しても挙動は変わらない**）:

| 欄 | 既定 | 意味 |
|---|---|---|
| `engine` | `codex` | `codex` / `agy` / `claude` / `both` / `ensemble`。env `CREWVIA_REVIEW_ENGINE` が上書き（engine だけ） |
| `claude_model` | `claude-opus-5-5` | `claude --model` に渡す名前。形の検査は agy と同じ（英数字で始まる・64 字まで） |
| `claude_timeout_seconds` | `900` | `timeout --kill-after=10s <N>s` で包む（CLI にタイムアウトのフラグが無い） |
| `compare` | `[agy]` | `both` のときの併記の一覧に `claude` も置ける（agy / cursor / claude の部分集合） |

- 不正な値（`claude_model: 'a b'`・`claude_timeout_seconds: 0` 等）は `bad_claude_model` / `bad_claude_timeout` で止める（codex に倒さない）。
- `engine: claude` / `ensemble` では codex を要求しない（codex が無い machine でも動く）。

## claude の呼び方（実測: claude CLI 2.1.312）

```
printf '%s' "<prompt>" | ( cd $REVIEW_WT && <env を allowlist に絞る> && \
  timeout --kill-after=10s <N>s claude -p --output-format json --json-schema "<schema の中身>" \
    --permission-mode plan --model <claude_model> --setting-sources user --strict-mcp-config --no-session-persistence \
    --add-dir <diff の dir> --allowedTools Read Grep Glob \
    --disallowedTools Bash Write Edit MultiEdit NotebookEdit WebFetch WebSearch Task Agent ExitPlanMode )
```

- **prompt は stdin**。`--add-dir` / `--allowedTools` / `--disallowedTools` は可変長で、後ろに置いた位置引数を食う。
- diff は argv に載せない。worktree の外の `mktemp -d` に `review.diff` と `review.log`（`git log base..HEAD` の 1 行要約）を書き、`--add-dir` で読ませる。
- 出力（`--output-format json`）は JSON 1 個: `{"type":"result","subtype":"success","is_error":false,"permission_denials":[],"result":"<文>","structured_output":{"findings":[...]},...}`。
  判定に使うのは `structured_output` だけ（`result` の自由文は読まない）。CLI の検証に頼り切らず、`lib_review_engine.py extract-claude` が同じ schema でもう一度検証する。
- 実測（使い捨て repo・1 ファイルの 1 行 diff）: 約 10 秒・3 turn。`a+b → a-b` を P1 で拾い、body に再現（`add(2, 3)` が `-1`）を書いた。
- **`--bare` は使わない**（hooks と user の settings の deny を飛ばす）。

### 読み取り専用にする仕掛けと理由

| 仕掛け | 理由 |
|---|---|
| `--permission-mode plan` + `--allowedTools Read Grep Glob` | 書き込みの道具を許さない |
| **Bash を許さない**（`--disallowedTools Bash`） | 当初は読み取り系の Bash（git diff / git show / git log / cat / sed -n / grep）を許す案だったが、prefix の許可では `git diff --output=<file>`・`--ext-diff`（外部コマンドの起動）・`sed -n 'w <file>'` で書け、`cat` は Read の deny（秘密ファイル）を迂回する（commit 時の security review）。git の文脈は `review.log` で渡す |
| `--setting-sources user` | レビューする PR の `.claude/settings.json`（hooks・allow）を読まない。PR が足した hook がレビュー中に走らない。秘密ファイルの Read の deny は user の settings にあるのでそのまま効く |
| `--strict-mcp-config`（`--mcp-config` なし） | MCP server を 1 つも起動しない |
| `--no-session-persistence` | 会話を残さない |
| env の allowlist（`CLAUDE_ENV_KEEP`: PATH・HOME・USER・LANG・TMPDIR・TZ・`CLAUDE_CONFIG_DIR`・XDG_* 等） | 子に crewvia の env（`AGENT_NAME`・`TASK_ID`・`CREWVIA_*`・`TASKVIA_TOKEN`）と秘密の env を渡さない。subshell の中で残りを `unset`（値は argv に載らない）。Worker と誤認されない（hooks の承認・Taskvia へのログ投稿・assignment の補完が走らない） |
| 実行の前後の木の指紋（`wt_fingerprint` = HEAD + `status --porcelain -z --ignored` + 各パスの lstat） | 読み取り専用のはずの REVIEW_WT が変わっていたら結果を信用しない（commit・ignored のファイルの書き換えも拾う） |

残る穴（受け入れたもの）: Read / Grep は worktree と `--add-dir` の外を読める場合がある（plan モードの `-p` は許可の無い道具を拒否するが、
OS の sandbox ではない）。外への書き込みは道具が無いので起きない。OS の sandbox（bwrap 等）で包むのは別の課題。

## fail-closed（findings 無しの LGTM に倒さない）

次のどれかなら `needs-director`（`CLAUDE FAILURE: <理由>`）。理由は固定語彙:

| 理由 | 条件 |
|---|---|
| `claude_not_found` | `claude` が PATH に無い |
| `timeout` / `timeout_killed` | `timeout` の 124 / SIGTERM を無視して `--kill-after` で落とした 137 |
| `exit_<N>` | exit が 0 以外 |
| `unparseable_output` | stdout が JSON 1 個でない（2 個以上・壊れた・object でない）・`permission_denials` が list でない |
| `duplicate_key` | 同じキーが 2 回（json は後勝ちで黙って読むので拒否） |
| `is_error` | `is_error` が `false` 以外（欠けも） |
| `subtype` | `type != result` か `subtype != success`（`error_max_turns` 等） |
| `permission_denials` | 拒否された道具の呼び出しがあった（確かめたかったものを確かめられていない。agy の `denied_actions` と同じ扱い） |
| `no_structured_output` | `structured_output` が無い・null・object でない |
| `schema_invalid` | `structured_output` が findings の schema に合わない |
| `worktree_modified` / `worktree_status_failed` | 実行の前後で REVIEW_WT の指紋が違う / 指紋が取れない |

失敗の診断（`lib_review_engine.py diagnose --engine claude`）は stdout の `type` / `subtype` / `is_error` と拒否された道具の名前（`denied tool: Bash`）、
stderr の末尾を伏せ字（`lib_checkpoint.redact`）に通して Result に残す。`result`（レビュー本文・コード片）と道具の入力（コマンド・パス）は出さない。

## prompt の方針

- 共通の prompt（codex / agy と同じ: 正しさ・安全・人が見るべきもの、P0〜P3、空の findings は読んだうえでだけ）に足す:
  - Worker とは別の、**攻める役**のレビュアー。壊れる入力・状態・手順を具体的に作る。
  - **各 finding の body に「再現手順」か「落ちるテストの形」（入力・コマンド・期待値と実際）を必ず書く。書けないものは出さない**（指摘の水増しと、確かめられない往復を減らす）。
  - checkout に `knowledge/review-attack-checklist.md` があれば最初に読んで使う（別 task t002 が作る。無くても動く）。
  - シェルは使えない・ファイルを作らない・schema の JSON だけを返す。

## ensemble（agy + claude の合議）

- agy → claude の順に**両方**走らせる（agy が失敗しても claude は走らせて、Result に両方の状態を残す）。
- **両方が成功したときだけ**、両者の findings を 1 つの `{"findings":[...]}` に合わせる（各 finding に `"engine": "agy" | "claude"` を足す・**重複はそのまま**）。
  判定は共通の経路: P0〜P2 が 1 つでもあれば NEEDS FIX、P3 だけ・空なら LGTM（どちらも Result に併記）。
- **片方でも失敗したら needs-director**（`ENSEMBLE FAILURE: agy=<ok|failed:理由> claude=<ok|failed:理由>` + 失敗した側の診断）。もう片方だけで LGTM にしない。
- agy の結果は claude を走らせる前にシェル変数に取ってファイルを消す（claude に判定の入力を読ませ・書き換えさせる余地を作らない）。
- Result: 先頭行 → `### Findings`（合わせたもの。`- engine:` 付き）→ `### agy raw output (model=…)` → `### claude raw output (model=…)`。

| agy | claude | 判定 |
|---|---|---|
| 空 | 空 | LGTM |
| P2 | 空 | NEEDS FIX |
| 空 | P2 | NEEDS FIX |
| P3 だけ | 空 / P3 だけ | LGTM（併記） |
| 失敗 | 何でも | needs-director |
| 何でも | 失敗 | needs-director |

## both + compare: [claude]

判定は codex のまま。claude は**判定の結果をシェル変数に取り、codex の一時ファイルを消した後に**走らせる（cursor と同じ位置）。失敗は併記だけ。

## 枠（利用量）は Worker と共有する

- 子の `claude` は Worker と**同じアカウントのサブスクの枠**を使う（ログインは HOME 下の Claude Code の認証。API キーは渡さない）。
  5 時間枠・週の枠はアカウント単位なので、レビューが Worker の作業時間を削る。小さな diff の実測で 1 回 約 7 万 token の入力（大半は system prompt の cache）。
- 長いミッションで枠が 80% を超えたら、`engine` を `agy` に寄せるか、レビューの回数（往復）を減らす。
- 往復の上限の運用: 同じ PR で claude / ensemble の NEEDS FIX が **3 巡**続いたら、Director が指摘の系統を見て「設計で閉じる」か「打ち切って未解決の課題に書く」を決める
  （Codex の往復の教訓。`knowledge/codex-reviewer.md`）。kai-review.sh 自体は回数を数えない。

## テスト

`tests/test_kai_review_engine_claude.py`（claude / agy は stub）: 設定の読み口・`extract-claude` の fail-closed の各条件・argv（plan・`--setting-sources user`・
Bash なし・diff が argv に無い）・prompt（stdin・再現の要求・チェックリスト）・子の env が allowlist・各失敗の理由・timeout・ensemble の組み合わせ表・
重複の併記・`both` の compare・既定（codex）で claude を呼ばない。stub への指示は env ではなく `$TMPDIR/claude_ctl.sh`（子の env は allowlist で落ちる）。

## 戻し方

PR revert → `scripts/sync-main-checkout.sh`。config の `claude_model` / `claude_timeout_seconds` は旧コードでは未知のキー（無視される）。
`engine: claude` / `ensemble` を設定していたら、revert の前に `codex` か `agy` に戻す（旧コードは `bad_engine` で needs-director にする）。
