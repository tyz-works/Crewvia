# model-per-skill — skill 別モデル選択の設計記録

> 実装: `scripts/lib_model.py` / `config/crewvia.yaml` (`model_per_skill` ブロック) / `scripts/start.sh`
> 追加日: 2026-09-07 (mission 20260907-skill-model-mapping)

---

## skill → モデル割り当て表

| skill | 割り当てモデル | 根拠 |
|---|---|---|
| `planning` | `claude-opus-5` | タスク分解・依存関係設計は誤判断のコストが高い |
| `plan_review` | `claude-opus-5` | プランレビューは深い推論が必要 |
| `review` | `claude-opus-5` | コードレビュー品質を最大化する |
| `research` | `claude-opus-5` | 情報収集・要約の精度が結果に直結する |
| `docs` | `claude-haiku-4-5-20251001` | 記述・整形タスクは軽量モデルで十分。コスト削減 |
| `qa` | `claude-haiku-4-5-20251001` | 動作検証は手順が明確。軽量化で thinking loop hang も回避 |
| `verify` | `claude-haiku-4-5-20251001` | smoke test・実機検証は同上 |
| `code` | `claude-sonnet-5` (worker_model) | 実装の精度とコストのバランス |
| `bash` | `claude-sonnet-5` (worker_model) | 同上 |
| `python` | `claude-sonnet-5` (worker_model) | 同上 |
| `typescript` | `claude-sonnet-5` (worker_model) | 同上 |
| `database` | `claude-sonnet-5` (worker_model) | 同上 |
| `cloud` | `claude-sonnet-5` (worker_model) | 同上 |
| `ops` | `claude-sonnet-5` (worker_model) | 同上 |

`worker_model` フォールバック = `config/crewvia.yaml` の `worker_model` 値（デフォルト `claude-sonnet-5`）。

---

## 複数 skill 指定時のルール

`bash scripts/start.sh worker docs qa` のように複数 skill を指定した場合、
**最も要求の高いモデル** (`opus > sonnet > haiku`) が選ばれる。

```
planning,code  → planning=opus(3), code→sonnet(2)  → claude-opus-5
docs,qa        → docs=haiku(1),   qa=haiku(1)       → claude-haiku-4-5-20251001
qa,bash        → qa=haiku(1),     bash→sonnet(2)    → claude-sonnet-5
code,python    → 両方 worker_model=sonnet(2)        → claude-sonnet-5
```

同ランク同士の場合は skill 名をソートして最初に出たモデルを採用（決定的な挙動にするため）。

ランク計算: モデル ID 文字列に `opus` を含む → 3、`sonnet` → 2、`haiku` → 1。
未知の ID → 2 (sonnet 相当) として stderr に警告を出す。

---

## 上書き手段

### 一時的な上書き（1回の Worker 起動のみ）

```bash
CREWVIA_WORKER_MODEL=claude-opus-5 bash scripts/start.sh worker docs
```

`CREWVIA_WORKER_MODEL` が起動前に設定されていると、`model_per_skill` より優先される（最優先）。
Director がこの env var を設定してから Worker を起動することで、特定タスクだけモデルを変えられる。

### 永続的な変更（設定ファイルを編集）

```yaml
# config/crewvia.yaml
model_per_skill:
  docs: claude-sonnet-5  # haiku → sonnet に変更
```

---

## 未定義 skill のフォールバック

`model_per_skill` に記載されていない skill は `worker_model` にフォールバックする。
`worker_model` も空の場合は `--model` なしで起動し、claude CLI のデフォルト（ユーザーの `/model` 設定）に従う。

---

## 実装の落とし穴: `WORKER_MODEL_EXPLICIT` フラグ

`scripts/start.sh` は config 読み込みブロックで `WORKER_MODEL_FROM_CONFIG` に
config の `worker_model` を**ローカル変数**として保持する（t007 fix: export を廃止して tmux env 汚染を排除）。
`SELECTED_MODEL` の解決ロジックに到達した時点で `CREWVIA_WORKER_MODEL` が非空かどうかは
「ユーザーが起動前に設定した」ことを確実に示す。

この問題を回避するため、**config 読み込みより前**に `WORKER_MODEL_EXPLICIT` フラグを記録する:

```bash
WORKER_MODEL_EXPLICIT=0
[[ -n "${CREWVIA_WORKER_MODEL:-}" ]] && WORKER_MODEL_EXPLICIT=1
```

`WORKER_MODEL_EXPLICIT=1` のときのみ env 優先を適用し、それ以外は `_resolve_worker_model()` helper
（内部で `lib_model.py` を呼ぶ）による skill 別解決を行う。dry-run (`CREWVIA_PRINT_MODEL=1`) も
同じ helper を使うため、dry-run と実際の起動で挙動が一致する。
これにより `model_per_skill` が dead code になる問題を回避している。

---

## dry-run モード: `CREWVIA_PRINT_MODEL=1`

```bash
CREWVIA_PRINT_MODEL=1 bash scripts/start.sh worker docs qa
# → claude-haiku-4-5-20251001 を stdout に出力して exit 0
```

モデル解決結果を確認するだけで claude は起動しない。`AGENT_NAME` 割り当てや registry 書き込みなど
副作用のある処理より前に exit するため、何度実行しても副作用ゼロ。

---

## Phase 2 予定

Codex Kai を `engine=codex` として skill matrix に統合する検討がある。
`model_per_skill` に `engine` キーを追加し、`lib_model.py` が `--model` ではなく
別の起動フラグを返す形が候補。詳細は別 mission で設計予定。
