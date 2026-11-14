# agy (Antigravity CLI + Gemini) をレビュー先にする (t080)

`scripts/kai-review.sh` の「codex exec を走らせる段」だけを差し替えられる。前後（pull・Execution ID の照合・findings の判定・
needs-director / done の報告）は codex と共通。設定の読み口は `scripts/lib_review_engine.py` の 1 か所。

## 設定

`config/crewvia.yaml` の `review:`（既定は今までと同じ `codex`。merge しても挙動は変わらない）:

| 欄 | 既定 | 意味 |
|---|---|---|
| `engine` | `codex` | `codex` / `agy` / `claude` / `both` / `ensemble`。env `CREWVIA_REVIEW_ENGINE` が上書き（engine だけ）。`cursor` は比較専用で `compare` にだけ置ける（`knowledge/cursor-review.md`）。`claude` と `ensemble`（agy + claude の合議）は `knowledge/claude-review.md` |
| `agy_model` | `gemini-3.8-flash-high` | `agy models` に出る名前。agy 1.4.10 の 6 モデル: `gemini-3.8-flash-{high,medium,low}` / `claude-sonnet-4-6` / `claude-opus-4-6-thinking` / `gpt-oss-120b-medium` |
| `agy_timeout_seconds` | `600` | `--print-timeout` に渡す |

- **キー（`review:` / `engine` 等）が無い・config ファイルが無い = 既定**。**キーがあって値が null（`review:` / `review: null` / `engine:`）は不正**。**読めない・値が不正（env の空も）= 止める**（`needs-director`・固定コード）。codex に倒さない。
- 読み口は PyYAML。**PyYAML が無く config ファイルがあれば止める**（`yaml_unavailable`。regex で「review: が無い」と決めると `"review":` や flow 形式を見落として黙って codex になる）。CI は `pip install pytest pyyaml`（`.github/workflows/ci.yml`）、本番の `python3` は PyYAML 6.0.1 が入っていることを確認済み（t084）。
- `both`: codex と `compare`（既定 `[agy]`）の各エンジンを走らせ、**判定は codex のまま**。agy の findings は Result に「agy (比較用)」として併記する（agy が失敗しても判定は変えず、失敗の理由を併記）。`compare: [agy, cursor]` で cursor も併記できる（`knowledge/cursor-review.md`）。

## agy の呼び方（実測: agy 1.4.10）

```
cd $REVIEW_WT && agy --mode plan --model <agy_model> --output-format json \
    --json-schema config/kai-review-findings.schema.json --add-dir <diff の dir> --print-timeout <N>s \
    --settings '{"permissions":{"allow":["read_file(<diff の dir>/**)","read_file(<REVIEW_WT>/**)","read_file(<REVIEW_WT の gitdir>/**)"]}}' -p "<prompt>"
```

- サブスクのログインのまま。API キーは使わない。**`--dangerously-skip-permissions` は使わない**。
- **`--add-dir` だけでは headless (`-p`) で `read_file` が自動拒否される**（確認プロンプトを出せない）。`status:SUCCESS` のまま `denied_actions` だけが載った空応答になり、kai-review.sh は fail-closed で needs-director に倒れる。だから `--settings` で `read_file` の allow-rule を 2 本（diff の dir と review worktree）だけ明示する。**ただしこれで拒否が無くなるわけではない**（下の「既知」の節: 2026-11-07 の実測で小さい diff 2/4 が拒否）。`command` は許さない（read-only review の前提）。組み立てに失敗したら `AGY_FAIL=settings_build_failed`。
- **`-p` は最後**（前に置くと次の option を prompt として読み exit 2）。
- diff は argv に載せない（引数 1 個 128KB 上限・PR は 300KB まで通る）。worktree の外の `mktemp -d` に `review.diff` を書き、prompt でパスを読ませる。
  **`--add-dir` で足せば worktree 外のファイルを読める**（実機で確認）。prompt は「コマンドは実行せず、ファイルを読むだけ」と書く（`-p` は許可の無いコマンドを自動で拒否する）。
- 実測（t084 / #309 / #305）: kai-review 経由で 1 回 約 170 秒、直接実行で 70〜124 秒・2.0〜2.4 万 token。

## fail-closed（findings 無しの LGTM に倒さない）

次のどれかなら `needs-director`（`AGY FAILURE: <理由>`）。理由は固定語彙:

| 理由 | 条件 |
|---|---|
| `exit_<N>` | exit が 0 以外（API の失敗は exit 3 + stderr に `AGY_ERROR: {...}`） |
| `status` | `status` が `SUCCESS` 以外 |
| `denied_actions` | `denied_actions` が空でない（このとき `structured_output` は null・exit は 0 になる） |
| `no_structured_output` | `structured_output` が null か欠けている |
| `unparseable_output` | stdout が JSON ドキュメント 1 つでない |
| `worktree_modified` / `worktree_status_failed` | 実行後の `git -C $REVIEW_WT status --porcelain` が空でない / 取れない |

`structured_output` が取れたら、そのあとは codex と同じ判定（`findings` が配列・P3 だけなら通す・それ以外は needs-director）。

## 残るもの

`--mode plan` でも、agy は計画のメモを `~/.gemini/antigravity-cli/brain/<id>/` に置く（worktree には書かない）。レビューのたびに溜まるので、
増えすぎたら人が掃除する（crewvia は消さない）。1 run で +1 dir。

## `--settings` があっても `read_file` が拒否される run があった（2026-11-14 に原因を特定・修正）

**原因: モデルが allow の外のパスを読もうとしていた。** allow-rule の書き方（`/*` と `/**` の違い・worktree の `.git` の指す先）の問題で、agy の不具合ではない。

### 1.5.3 での再現と切り分け（2026-11-14・`gemini-3.8-flash-high`・合計 10 run）

| 条件 | diff | 結果 |
|---|---|---|
| 旧 allow（`<diff dir>/*` + `<wt>/**`）・簡略 prompt | #340 ×2 / #317 ×2 | 4/4 成功 |
| 旧 allow・本番の REVIEW_PROMPT（kai-review.sh と同じ文面） | #340 ×1 | 成功 |
| 旧 allow・本番の REVIEW_PROMPT | #317 ×1 | **拒否**（`denied_actions` に `read_file`・`structured_output` null） |
| 新 allow（3 本とも `/**`）・本番の REVIEW_PROMPT | #340 ×2 / #317 ×2 | 4/4 成功 |

- **拒否は確率的**（同じ形で成功も拒否も出る）。簡略 prompt では 0/4、本番 prompt では #317 で 1/2。「diff の中身に依存」ではなく、**モデルがたまたま allow の外を読みに行くか**で決まる。前回の「#340 は毎回拒否・#317 は 0 回」は標本が小さかった。
- 1.4.12 のときと違い、1.5.3 の旧 allow でも拒否は起きる（版は無関係）。
- 新 allow の 4/4 成功は、拒否率が 1/2 より十分低いことの示唆であって、ゼロの証明ではない（標本 4）。

### 拒否されたパスの特定

拒否された run の会話記録（`~/.gemini/antigravity-cli/conversations/<id>.db` を `strings` して `Path` の行だけ抽出。認証情報のファイルは開いていない）に、allow の外のパスへの `read_file` が 2 種類あった:

| 読みに行ったパス | 旧 allow で外れる理由 |
|---|---|
| `<diff dir>/scripts/plan.sh`（diff に出てくる相対パスを diff dir 基準で解決した） | `<diff dir>/*` は直下 1 階層だけ。2 階層目は外 |
| `<主 repo>/.git/worktrees/<wt 名>/HEAD` | review worktree の `.git` は `gitdir: <主 repo>/.git/worktrees/<名>` を指すファイル。worktree の外 |

log（`~/.gemini/antigravity-cli/log/cli-*.log`）には `soft-denying tool confirmation "ReadFile" at step N` としか出ず、パスは出ない。会話記録で見るしかない。
どちらが拒否の直接の原因かは 1 run では分けていない（両方を塞いだ）。

### 直し（kai-review.sh の `run_agy_review`・bench/review-bench/run.sh も同じ）

allow は 3 本、すべて `/**`、**読み取り（`read_file`）だけ**:

```
read_file(<AGY_DIFF_DIR>/**)
read_file(<REVIEW_WT>/**)
read_file(<REVIEW_WT の gitdir（git rev-parse --absolute-git-dir）>/**)
```

- `command` は許さない・`--dangerously-skip-permissions` は使わない（read-only review の前提は同じ）。
- gitdir は **その worktree の分だけ**（主 repo の `.git` 全体は許さない）。`rev-parse` が失敗したら `settings_build_failed` で fail-closed。
- テスト: `tests/test_kai_review_engine_agy.py` が 3 本・すべて `/**`・gitdir が `worktrees/` を含むことを検査する。

### 診断のコツ

- `denied_actions` にパスは出ない。再発したら Result 末尾の `### agy diagnostics (redacted)` の `denied:` が空でも、**会話記録を `strings` して `"Path":"…"` を見る**と、読みに行ったパスが分かる。
- allow に無いパスを読もうとするのはモデルの癖（diff の相対パスを別の基準で解決する等）で、版が変わっても出うる。新しい種類のパスが出たら、allow を `**` に広げるのではなく、そのパスだけ足す。

### 当時の記録（t082 / t084・`--settings` を足す前）

QA t082 で 1 回だけ `read_file` が拒否された。t084 で同じ形（5KB と 200KB の diff）を 5 回走らせたが 0/5 で再現せず、「再現しないものを検証できない」として `permissions.allow` は足さなかった。2026-10-29（#322）に agy 1.4.10 で 3/4 拒否を観測し、`--settings` の allow-rule を足した（#348）。

## 戻し方

`review.engine` を `codex` に戻す（または env を外す）。コードを戻すなら PR revert → `scripts/sync-main-checkout.sh`。`review:` ブロックは残ってよい（旧コードは読まない）。

テスト: `tests/test_kai_review_engine_agy.py`（agy は stub。正常・findings あり・各 fail-closed・`both`・設定の読み口）。
実機の agy を本番の PR に対して走らせる確認は QA task で行う。
