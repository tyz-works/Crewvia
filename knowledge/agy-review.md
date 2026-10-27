# agy (Antigravity CLI + Gemini 4 Argon) をレビュー先にする (t080)

`scripts/kai-review.sh` の「codex exec を走らせる段」だけを差し替えられる。前後（pull・Execution ID の照合・findings の判定・
needs-director / done の報告）は codex と共通。設定の読み口は `scripts/lib_review_engine.py` の 1 か所。

## 設定

`config/crewvia.yaml` の `review:`（既定は今までと同じ `codex`。merge しても挙動は変わらない）:

| 欄 | 既定 | 意味 |
|---|---|---|
| `engine` | `codex` | `codex` / `agy` / `cursor` / `both`。env `CREWVIA_REVIEW_ENGINE` が上書き（engine だけ）。cursor は `knowledge/cursor-review.md` |
| `agy_model` | `gemini-4-argon-high` | `agy models` に出る名前（`gemini-4-argon-{low,medium,high}`） |
| `agy_timeout_seconds` | `600` | `--print-timeout` に渡す |

- **キー（`review:` / `engine` 等）が無い・config ファイルが無い = 既定**。**キーがあって値が null（`review:` / `review: null` / `engine:`）は不正**。**読めない・値が不正（env の空も）= 止める**（`needs-director`・固定コード）。codex に倒さない。
- 読み口は PyYAML。**PyYAML が無く config ファイルがあれば止める**（`yaml_unavailable`。regex で「review: が無い」と決めると `"review":` や flow 形式を見落として黙って codex になる）。CI は `pip install pytest pyyaml`（`.github/workflows/ci.yml`）、本番の `python3` は PyYAML 6.0.1 が入っていることを確認済み（t084）。
- `both`: codex と `compare`（既定 `[agy]`）の各エンジンを走らせ、**判定は codex のまま**。agy の findings は Result に「agy (比較用)」として併記する（agy が失敗しても判定は変えず、失敗の理由を併記）。`compare: [agy, cursor]` で cursor も併記できる（`knowledge/cursor-review.md`）。

## agy の呼び方（実測: agy 1.4.7）

```
cd $REVIEW_WT && agy --mode plan --model <agy_model> --output-format json \
    --json-schema config/kai-review-findings.schema.json --add-dir <diff の dir> --print-timeout <N>s -p "<prompt>"
```

- サブスクのログインのまま。API キーは使わない。**`--dangerously-skip-permissions` は使わない**。
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

## 既知: `read_file` 拒否で `structured_output` が null になる run（t084 で調査・再現せず）

QA t082 で、通常の prompt でも 1 回だけ `read_file` が拒否され `structured_output` が null になった run があった。fail-closed なので安全側（`denied_actions` → `needs-director`）だが、agy を既定にすると誤爆になる。
t084 で同じ形（worktree を cwd・一時 dir を `--add-dir`・5KB と 200KB の diff）を 5 回走らせたが **0/5 で再現せず**（全部 `SUCCESS`・`denied_actions` なし）。原因は特定できていない。
`permissions.allow` で読み取りだけ許可する設定は、再現しないものを検証できないので足していない（書き込み・コマンドは許可しない方針は変えない）。
再発したら needs-director の Result 末尾の `### agy diagnostics (redacted)` を見る（失敗のときだけ入る。`AGY_ERROR line:`＝stderr の API エラー、`stdout status:`、`denied: action=… path=…`＝拒否された action と path、`stderr tail:`＝末尾 8 行。lib_checkpoint の伏せ字を通し、全体 2000 字・各行 300 字で切る。伏せ字が失敗したら中身は出ない。`both` では Result の agy 比較の節に同じ内容が付く）。denied の path を見る: 一時 dir 側ならタイミング（`--add-dir` の効き）、worktree 側なら cwd の読み。当面は再実行で通る見込み。

## 戻し方

`review.engine` を `codex` に戻す（または env を外す）。コードを戻すなら PR revert → `scripts/sync-main-checkout.sh`。`review:` ブロックは残ってよい（旧コードは読まない）。

テスト: `tests/test_kai_review_engine_agy.py`（agy は stub。正常・findings あり・各 fail-closed・`both`・設定の読み口）。
実機の agy を本番の PR に対して走らせる確認は QA task で行う。
