# agy (Antigravity CLI + Gemini 4 Argon) をレビュー先にする (t080)

`scripts/kai-review.sh` の「codex exec を走らせる段」だけを差し替えられる。前後（pull・Execution ID の照合・findings の判定・
needs-director / done の報告）は codex と共通。設定の読み口は `scripts/lib_review_engine.py` の 1 か所。

## 設定

`config/crewvia.yaml` の `review:`（既定は今までと同じ `codex`。merge しても挙動は変わらない）:

| 欄 | 既定 | 意味 |
|---|---|---|
| `engine` | `codex` | `codex` / `agy` / `both`。env `CREWVIA_REVIEW_ENGINE` が上書き（engine だけ） |
| `agy_model` | `gemini-4-argon-high` | `agy models` に出る名前（`gemini-4-argon-{low,medium,high}`） |
| `agy_timeout_seconds` | `600` | `--print-timeout` に渡す |

- ブロックが無い・config ファイルが無い = 既定。**読めない・値が不正（env の空も）= 止める**（`needs-director`・固定コード）。codex に倒さない。
- `both`: 両方走らせ、**判定は codex のまま**。agy の findings は Result に「agy (比較用)」として併記する（agy が失敗しても判定は変えず、失敗の理由を併記）。

## agy の呼び方（実測: agy 1.4.7）

```
cd $REVIEW_WT && agy --mode plan --model <agy_model> --output-format json \
    --json-schema config/kai-review-findings.schema.json --add-dir <diff の dir> --print-timeout <N>s -p "<prompt>"
```

- サブスクのログインのまま。API キーは使わない。**`--dangerously-skip-permissions` は使わない**。
- **`-p` は最後**（前に置くと次の option を prompt として読み exit 2）。
- diff は argv に載せない（引数 1 個 128KB 上限・PR は 300KB まで通る）。worktree の外の `mktemp -d` に `review.diff` を書き、prompt でパスを読ませる。
  **`--add-dir` で足せば worktree 外のファイルを読める**（実機で確認）。prompt は「コマンドは実行せず、ファイルを読むだけ」と書く（`-p` は許可の無いコマンドを自動で拒否する）。
- 1 回 約 30〜60 秒・約 1.2 万 token。

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
増えすぎたら人が掃除する（crewvia は消さない）。

## 戻し方

`review.engine` を `codex` に戻す（または env を外す）。コードを戻すなら PR revert → `scripts/sync-main-checkout.sh`。`review:` ブロックは残ってよい（旧コードは読まない）。

テスト: `tests/test_kai_review_engine_agy.py`（agy は stub。正常・findings あり・各 fail-closed・`both`・設定の読み口）。
実機の agy を本番の PR に対して走らせる確認は QA task で行う。
