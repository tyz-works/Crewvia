# Codex Reviewer (Kai-codex) ナレッジベース

> Codex CLI を reviewer として使う際の手順・制約・運用ノウハウ。
> Phase 1 (review 限定) で導入 (mission: 20260907-codex-reviewer-phase1)。
> **Phase 2 (2026-09-08)**: Dispatcher が `codex-review` skill task を自動 spawn するようになった。
> Kai-codex は registry/workers.yaml に登録された正規 worker で、plan.sh pull/done 経由で Taskvia 同期と task_count bump が自動で走る。
> **Phase 3 (2026-09-08, mission: 20260908-codex-reviewer-phase3, PR#180)**: Codex 自身の self-review で見つかった
> kai-review.sh の欠陥 (主 working tree 汚染・stale branch review・findings 判定の fail-open・固定 /tmp パス衝突) を
> まとめて修正。詳細は下記「Phase 3: kai-review.sh 安定化」を参照。
> **R-1 (2026-09-09, mission: 20260909-safety-gate-hardening, t001)**: Phase 3 (F-3) で導入した散文
> critical キーワード safety net が、無関係な否定語で本物の critical finding を見逃す欠陥を持っていた
> (4 度目の「自動承認側に倒れる」欠陥)。safety net を全廃し、構造化シグナル (JSON findings 配列 or
> [P#] タグ) が一切無い場合は無条件で needs-director に倒す fail-closed 方式に変更。
> トレードオフとして clean review もタグ無しの限り needs-director 相当になる。詳細は下記
> 「findings 判定ロジックの変遷」の R-1 節を参照。
> **t006 (2026-09-09, mission: 20260909-safety-gate-hardening)**: `codex exec review` サブコマンドの
> 使用をやめ、diff を自前取得して `codex exec --output-schema` に渡す方式に移行した。これにより
> R-1 が抱えていた制約 (clean review でも構造化シグナルを強制する手段が無い) を解消し、clean review
> でも信頼できる空配列 `{"findings":[]}` を実機で確認できるようになった (fail-closed の設計原則自体は
> 維持)。あわせて Seo 指摘の F-B (JSONL 出力が bash 数値比較のエラーで握りつぶされ auto-done する
> 欠陥) を修正し、diff の非空性・サイズ上限・取得失敗を codex 呼び出し前にゲートする受け入れ基準を
> 実装した。詳細は下記「t006: `codex exec review` → `codex exec --output-schema` 移行」を参照。
> **F-A (2026-09-09, mission: 20260909-safety-gate-hardening, t007)**: `[P#]` タグ抽出が出力全体への
> 無アンカー grep だったため、レビュー対象の diff/コードが `[P3]` 等の文字列を含んでいて codex が
> それを地の文で引用しただけで `HAD_SIGNAL=1` が立ち、同じ出力中の散文 critical finding が丸ごと
> 無視される欠陥があった (`kai-review.sh`/`test_kai_review.sh` 自身を触る PR の diff には
> `[P0]`-`[P3]` のリテラルが実際に含まれるため机上の話ではない)。タグ抽出を finding 行
> (行頭の箇条書きマーカーに続くタグ) にアンカーして修正。詳細は下記「F-A: `[P#]` タグ抽出の
> アンカー化」を参照。

---

## Kai-codex の起動手順

### Phase 2: Priya が plan に codex-review task を積むだけ（常用パス）

```bash
plan.sh add "PR#<N> Codex review (Kai)" \
  --skills codex-review \
  --blocked-by t<impl_task_id> \
  --pr-number <N> \
  --priority medium
```

- `--skills codex-review` — 専用 skill 名。Dispatcher がこの skill を検知して `kai-review.sh` を自動 spawn する
- `--pr-number <N>` — frontmatter に PR 番号を刻む。Dispatcher が spawn 時に `--pr` として渡す。**必須**（無いと spawn せず warning）
- `--blocked-by` — 実装 task の完了後に review を走らせる典型パターン

Dispatcher spawn 後のフロー: `kai-review.sh` が `plan.sh pull` → `codex exec review` → `plan.sh done` を実行。Director の手動介入は不要。

### 手動起動（フォールバック）

自動 spawn が失敗したとき、または smoke test で個別に呼ぶとき:

```bash
bash scripts/kai-review.sh \
  --pr <PR番号> \
  --task <task_id> \
  --mission <mission-slug> \
  [--agent Kai-codex] \
  [--model o3] \
  [--skip-pull]                  # task が既に in_progress の場合
```

### モデル選択の目安

| モデル | 用途 |
|---|---|
| デフォルト（codex CLI が決定） | 通常 review・速度重視 |
| `o3` | 重要 mission・大きな diff・クリティカルバグ疑い |

### 前提確認

```bash
# codex CLI が利用可能か確認
which codex   # → /home/tkadmin/.nvm/versions/node/v24.18.0/bin/codex
codex --version
```

---

## 2 人体制での verdict 突合フロー

Phase 1 では Claude (Seo) と Codex (Kai) の **2 人体制**で review する。

```
[Director が review task を 2 つ積む]
  ├─ Seo (Claude)  → plan.sh done "LGTM" / needs-director "NEEDS FIX"
  └─ Kai (Codex)   → kai-review.sh 経由で plan.sh done / needs-director

[2 人の verdict 突合]
  両方 LGTM                → merge 承認（Director が Seo に gh pr merge 指示）
  どちらか NEEDS FIX       → Director が findings を統合して判断
  BRANCH MISMATCH 検出     → 優先度最高、即 Director 判断
  両方 NEEDS FIX (一致)    → 修正タスクを積んで fix → re-review
```

### いつ 2 人体制を使うか

- **重要 mission**（main / staging に影響する code 変更）: 2 人体制推奨
- **軽量 mission**（docs-only / MEMORY 更新 / minor fix）: Seo 1 人で十分
- **特に有効な場面**:
  - Claude 生成コードの review（同一モデル bias 回避）
  - critical bug 疑いのある大きな diff
  - LLM 特有の誤りパターン（hallucination / edge case 見落とし）

**使い方の流れ**: Seo の review task が完了したタイミングで Director が手動で `kai-review.sh` を呼ぶ。Kai review は plan task には載せない（Dispatcher の誤 assign を防ぐため）。

---

## Codex CLI の特徴

**t006 (2026-09-09) で呼び出し形式を変更した。** 現行 (t006 以降) の実際の呼び出し形式:

```bash
git diff <origin/main>...HEAD | codex exec -C <review-worktree> \
  --sandbox read-only \
  --output-schema config/kai-review-findings.schema.json \
  -m <model> \
  --ephemeral \            # セッション保存を省略
  -o <output-file> \
  "<review prompt>"
```

- **`exec` (review サブコマンドではない方)**: `[PROMPT]` 位置引数・stdin パイプ・
  `--output-schema`・`-o` を同時に受け付ける。旧 `exec review` サブコマンドは
  `--base <BRANCH>` とカスタム `[PROMPT]` を同時指定できない制約があった
  (詳細は「t006」節参照)
- **diff は stdin で渡す**: `<stdin>` ブロックとして prompt に追記される
- **`--output-schema`**: 最終応答の JSON Schema を強制する (`config/kai-review-findings.schema.json`)。
  clean review でも `{"findings":[]}` という信頼できる空配列を返させることができる
- **`--sandbox read-only`**: レビューは書き込みを必要としないため明示的に読み取り専用にする
- **`--ephemeral`**: セッション状態を保存しない（CI 相当の使い方）
- **出力先 `-o`**: 最終応答 (JSON) を指定ファイルに書き出す

旧呼び出し形式 (Phase 1〜R-1, `codex exec review --base main ...`) は git blame 参照。

### codex rescue との違い

| 用途 | コマンド |
|---|---|
| Codex Reviewer (Kai) | `codex exec --output-schema ... "<prompt>"` (diff は stdin, t006 以降) |
| Codex Rescue (既存 skill) | `codex:codex-rescue` skill 経由の対話的セッション |

---

## hook 不足の制約と workaround

Codex CLI は Claude Code の pre/post-tool-use hook を**持たない**。Phase 2 では plan.sh + dispatcher でカバーできる箇所を全て埋めた:

| 機能 | Claude Worker | Kai-codex (Phase 2) |
|---|---|---|
| Taskvia approval リクエスト | 自動（hook） | **なし** — Codex CLI に該当機構が無い（Phase 3 検討） |
| Taskvia カンバン表示 | あり（hook） | **あり** — plan.sh pull が taskvia_sync_pull を発火 |
| Taskvia agents 表示 | あり（heartbeat） | **あり** — kai-review.sh が起動時に registry/heartbeats/Kai-codex を touch |
| knowledge ログ自動投稿 | あり（hook） | **なし** — findings は plan.sh 経由で task ファイル Result に残る |
| heartbeat / watchdog 連携 | あり | **限定的** — 起動時 touch のみ（review 中は更新なし） |
| registry task_count 更新 | 自動（plan.sh done 内） | **自動** — Kai-codex が registry 登録済みなので同経路 |

**運用上の対処**:
- Dispatcher の spawn ログ: `logs/kai-spawn/<slug>-<task_id>-<epoch>.log`
- review findings は `/tmp/kai-review-output.txt` と task の Result セクションに残る
- Kai-codex がハングしたら:
  ```bash
  rm queue/assignments/Kai-codex   # spawn ロック解除
  plan.sh update <task_id> --status pending --reset --mission <slug>   # task 復旧
  ```

---

## Phase 3: kai-review.sh 安定化 (2026-09-08, PR#180)

Kai-codex 自身に PR#173 (kai-review.sh 初版) を self-review させたところ、以下の欠陥が見つかり、
まとめて修正した (`scripts/kai-review.sh` のヘッダーコメントに実装レベルの詳細あり)。

### 主 working tree 非破壊化 + stale branch review 対策 (F1/F1b)

- **旧実装の問題**: `$CREWVIA_REPO_ROOT`（主リポジトリ）で直接 `git checkout` していたため、fetch 失敗時に
  古い local branch のまま review してしまい (F1)、review 後も主 WT の HEAD が PR branch に切り替わったまま
  残っていた (F1b、他 Worker の worktree 運用と衝突しうる)。
- **対処**: `refs/pull/<PR#>/head` を origin から一意な local ref (`refs/kai-review-fetch/pr-<N>-$$`) へ
  fetch し、そこから専用の使い捨て git worktree (`mktemp -d` + `git worktree add --detach`) を作り、
  その中で `codex exec -C <worktree> review` を実行する。主 WT の HEAD は一切動かさない。fetch は
  fork PR や削除済み branch でも `refs/pull/<PR#>/head` が GitHub 上に残っている限り機能する
  (旧実装の `origin/<branch>` 参照は fork PR や削除済み branch で必ず失敗していた)。
- worktree・fetch した一時 ref は `trap cleanup EXIT` で成功/失敗どちらの経路でも必ず削除する。

### findings 判定ロジックの変遷 (F2 → F-1/F-3 → [P2] → R-1)

現物確認 (codex-cli 0.144.5) の結果、`codex exec review` の実際の出力は JSON ではなく自然文 +
`- [P0]`〜`- [P3]` タグ付き箇条書きだった。判定ロジックは QA re-review を重ねて堅牢化した:

1. **F2**: 「LGTM キーワードが無ければ needs-director」という旧ルールは、clean な review でも
   LGTM と言わないため常に誤発火していた。`[P#]` タグの有無を主判定に切り替えた。
2. **F-1 / F-3 (QA FAIL)**: タグ抽出が `[P1-3]` のみで `[P0]` を拾えていなかった (自動 done 側に
   倒れる危険な欠陥)。また critical キーワードの safety net が "No critical issues found." のような
   **否定文脈の健全な報告**まで拾って誤発火していた。→ `[P0]` を判定対象に含め、構造化シグナル
   ([P#] タグ or JSON) が得られた場合は keyword fallback を使わない (`HAD_SIGNAL`) ように変更。
3. **[P2] (Seo 最終レビュー, t018)**: JSON 経路の入口ゲート (`jq -e '.findings | length'`) が
   `.findings` 欠損/null でも `length` が `0` を返し `-e` が exit 0 になる fail-open だった
   (**3 回連続で同じ「自動承認側に倒れる」構造の欠陥**: [P0] 抜け → JSON 内側の allowlist →
   JSON 入口ゲート)。`.findings | arrays | length` に変更し、真の配列でない限り JSON 経路に
   入らないようにした。
4. **R-1 (t001, mission 20260909-safety-gate-hardening, 4 度目)**: F-3 で導入した「critical
   キーワード + 同一行否定語除外」の散文 safety net が、否定語が critical な指摘と無関係な箇所を
   否定しているだけの行 (実測例: "This introduces a critical race condition ... that does not
   have a workaround, and callers cannot recover once it triggers.") まで行ごと除外し、本物の
   critical finding を見逃して auto-done してしまう欠陥を持っていた (実測: Phase 3 QA t010)。
   「同一行のどこかに否定語があれば安全」という default-safe な構造自体が
   allowlist/denylist の原則に反していたため、個別のキーワード調整では終わらないと判断し、
   散文 safety net を全廃した。代わりに **JSON findings 配列も [P#] タグも一切見つからない
   (`HAD_SIGNAL=0`) 場合は、内容に関わらず無条件で needs-director に倒す** (fail-closed) 方式に
   変更した。
   - 実機検証: 代替案として「codex に必ず [P#] タグを出力させるカスタム prompt を渡す」ことを
     検討したが、`codex exec review --base <BRANCH>` は `--base` と `[PROMPT]` を同時指定できない
     (CLI が拒否する) ことを確認した。`--output-schema` を付けても review サブコマンドの最終出力
     書式は変化しないことも確認済み。つまり現行 codex-cli は `--base` を使う限り「clean な
     review でもタグを出す」ことを強制できない。
   - **トレードオフ**: 副作用として clean な review (タグ無し) も一律 needs-director 相当になる
     — これは F2 が解消した「LGTM キーワードが無ければ needs-director」問題を実質的に部分的に
     復活させる。safety-gate-hardening mission の意図 (危険な方向への誤判定を繰り返さない) を
     優先して意図的に選択した。改善余地: `--commit` + 手動 diff 取得 + `--output-schema` の
     組み合わせで custom prompt と構造化出力の両立を図る、等。

**教訓**: レビューゲートの判定ロジックは、迷ったら「修正必要」側に倒す（fail-closed）ことを
毎回明示的に確認すること。denylist（危険と確認できないものは全部危険側）で組むほうが、
allowlist（安全と確認できたものだけ安全側）より事故りにくい。**「危険パターンに一致しなければ
安全」という default-safe な構造は、そのパターンがどれだけ精緻でも denylist ではなく
allowlist 違反であり、同種の欠陥を再発させる** (F-3 → R-1 で実証済み)。auto-done のような
危険な結論は、構造化シグナルによる積極的な確認が取れた場合のみ許可すること。

## t006: `codex exec review` → `codex exec --output-schema` 移行 (2026-09-09, mission 20260909-safety-gate-hardening)

R-1 は「`review` サブコマンドの `--base` はカスタム `[PROMPT]` と併用できず、clean review に
構造化シグナルを強制する手段が無い」ことを根拠に fail-closed 方式を採ったが、これは
「clean review もタグ無し = 一律 needs-director」という副作用を伴っていた (F2 が解消した問題の
部分的な復活)。t006 で `review` サブコマンドの使用そのものをやめ、次の方式に移行した:

1. `git diff <origin/main>...HEAD` で diff を自前取得する (`main` ローカルブランチではなく、
   都度 `origin` から一意な local ref に fetch した最新の main を base にする。**実機で
   local main が origin/main より 2 commit 遅れているケースを観測した** — Director が
   review/自動化を頻繁に回す一方で明示的な `git pull` は都度行わないため、ローカル main は
   容易に陳腐化する。診断できない不完全な base を使うと受け入れ基準(i)の趣旨に反するため、
   PR head の fetch と同じパターンで base も都度 fetch するようにした)
2. 取得した diff を stdin で `codex exec -C <worktree> --output-schema <schema> -o <file>
   "<prompt>"` に渡す。`--output-schema` (`config/kai-review-findings.schema.json`) が
   最終応答を `{"findings":[...]}` 形式に強制する。OpenAI の strict structured outputs は
   「`additionalProperties:false` の場合、`required` は `properties` の全キーを含まなければ
   ならない」制約があるため、`body`/`file` のような「省略したい」フィールドは
   `required` に含めた上で型を `["string","null"]` にして null 許容にする必要がある
   (最初 `body`/`file` を optional のまま `required` から外して実機で
   `invalid_json_schema: ... 'required' ... Missing 'body'` エラーを実際に踏んだ)。

### 受け入れ基準(i): 空配列を信用する前に diff の健全性を検証する

diff 取得の失敗 / 空 diff / 巨大すぎる diff (context 切り詰めリスク) のいずれでも、
codex が返す空配列 `{"findings":[]}` は「clean」と「レビューできていない」を区別できない。
kai-review.sh は codex を呼ぶ**前**に以下を検証し、満たさなければ codex を一切呼ばず
needs-director に倒す:

- diff が非空であること (空 diff は base 解決ミス等の兆候であり、レビュー対象が無いことを
  意味する。`git diff` の三点記法は共通祖先が無い場合 `fatal: ...: no merge base` で
  exit 128 になることを実機で確認済み — fork 元が全く異なる場合などに起こりうる)
- diff サイズが `MAX_DIFF_BYTES` (300KB) 以下であること (context 切り詰めを直接検知する
  手段が無いための実測ベースの安全マージン。切り詰めが疑われるほど巨大な diff は
  そもそも自動レビューに向かないと判断し、needs-director で人間判断に委ねる)

いずれのケースも、regression test (`scripts/test_kai_review.sh` の「受け入れ基準(i)」節) で
「codex 側に空配列を返す fixture を用意していても、codex が実際には一度も呼ばれないこと」を
negative test として確認している (診断できない diff は codex の応答内容を一切信用しない
という設計を、fixture レベルでも裏付ける)。

### F-B (Seo 指摘): JSON が複数ドキュメント (JSONL) の場合の fail-open

`--output-schema` 移行で JSON 経路が本番の主経路になったことで、Seo が隔離ハーネスで
実測した潜在欠陥が現実的なリスクになった: 出力が 2 つ以上の JSON ドキュメントの場合、
旧実装の `jq -e '.findings | arrays | length'` は複数行を返し、後続の
`[[ "$FINDINGS_COUNT" -gt 0 ]]` が bash の構文エラー (`syntax error in expression`) になって
false 扱いになり、**`NEEDS_FIX=0` のまま `HAD_SIGNAL=1` が立って auto-done してしまう**
(実機で `{"findings":[]}` + `{"findings":[{"severity":"critical"}]}` の 2 行入力を使い、
旧ロジックを単体で再現して確認済み)。JSON 経路に入る前に「出力がちょうど 1 つの JSON
ドキュメントであること」を `jq -c '.' | grep -c .` でゲートし、2 つ以上 (または 0、パース
不能) の場合は JSON 経路そのものをスキップして `[P#]` タグ判定 → fail-closed にフォール
バックするよう修正した。

### 実機確認 (2026-09-09, codex-cli 0.153.4)

- **基本動作**: 未クォート変数展開 (`mv $SRC/* $DEST`) を含む合成の脆弱な `deploy.sh` に対し
  実行した結果、P0 1件 (引数未検証によるルート削除リスク) + P1 2件 (word splitting / エラー
  握り潰し) + P2 1件 (hidden files 除外) を実際に検出。無害な README 見出し変更の diff では
  `{"findings": []}` を実際に返した (fixture として `scripts/test_kai_review.sh` に採用)。
- **短い出力の誤判定を発見・修正**: 実測 `{"findings": []}` はわずか 16 文字であり、旧来の
  「出力が20文字未満なら要確認」という safety net (元は `review` サブコマンドの自然文出力を
  前提にしたもの) に誤ってひっかかり needs-director に倒れることを regression test で
  検出した。JSON 経路で確定判定できた場合はこの長さチェックを適用しないよう修正した
  (JSON パース成功時点で構造的な信頼性は担保済みであり、文字数は無関係なため)。
- **受け入れ基準(ii) (検出力の比較検証)**: crewvia 本体の実 PR (#191,
  `hooks/pre-tool-use.sh` の task file write guard 修正, diff 5150 bytes) に対し、
  新方式 (`kai-review.sh --pr 191 --dry-run`) と旧方式 (`codex exec review --base main`,
  同一 worktree/diff) を両方実際に実行して比較した:
  - 旧方式: [P2] 1件 (mixed quote での quote-stripping bypass)
  - 新方式: P1 1件 (escaped quote での quote-stripping bypass) + P2 1件
    (`-lc` 等の combined flags でのインタプリタ判定バイパス)
  - 新方式は旧方式が見つけた種類の指摘 (quote-stripping の頑健性問題) を再現できただけでなく、
    旧方式が見つけていない追加の実指摘 (combined flags バイパス) も検出した。新方式が
    codex にワーキングディレクトリ内のファイルを読ませ、実際に python で正規表現の挙動を
    検証するコマンドを実行させて確証を取っていたことも観測した (単純な diff 読解を超えた
    調査を行っている)。この結果から、少なくともこのケースでは検出力の低下は無く、
    むしろ向上したと判断した。

### 教訓

- **strict JSON Schema の "optional" フィールドは `required` + nullable型で表現する**。
  `additionalProperties:false` 下で `required` から漏れたフィールドがあると
  `invalid_json_schema` エラーで即座に失敗する (実機で踏んだ)。
- **判定ロジックの前提を変えたら、既存の safety net が新しい正常系と衝突しないか
  実機フィクスチャで確認すること**。「20文字未満は疑わしい」という heuristic は自然文が
  前提なら妥当だが、正当な最小 JSON 応答 (`{"findings": []}`) の方が短いことがあるとは
  想定されていなかった。
- **ローカルブランチの参照は陳腐化しうる**。`--base main` (ローカル) をそのまま信用せず、
  自動化スクリプトが diff base に使う ref は都度 origin から fetch する方が安全。

## F-A: `[P#]` タグ抽出のアンカー化 (2026-09-09, mission 20260909-safety-gate-hardening, t007)

t006 で JSON 経路が主経路になったが、`[P#]` タグ判定は依然として fallback (JSON 判定が
成立しない場合の forward/backward-compat 経路) として残っている。この fallback 自体に
Seo が隔離ハーネスで発見した欠陥があった:

**欠陥**: タグ抽出が `grep -oiE '\[P[0-3]\]'` という**出力全体への無アンカー grep**
だったため、レビュー対象の diff やコードそのものが `[P0]`-`[P3]` という文字列を含んで
いて codex がそれを地の文で引用しただけで `HAD_SIGNAL=1` が立ってしまう。実測:

```
入力:
  The diff adds a comment mentioning [P3] priority tags to kai-review.sh.
  Separately, this introduces a critical data-loss bug in the queue writer.
結果 (修正前): method=tags needs_fix=0   (= auto-done)
```

散文で述べられた本物の critical finding が丸ごと無視される。`kai-review.sh` /
`test_kai_review.sh` 自身を触る PR の diff には `[P0]`-`[P3]` のリテラルが実際に含まれる
ため (このファイル自身がまさにそれ)、机上の想定ではなく実際に起こりうる入力である。
R-1 (t001) で auto-done 経路が「JSON 空配列」と「`[P#]` タグ」の 2 本に絞られたことで、
この偽造されうるトークンが auto-done への最安経路になっていた。

**対処**: タグ抽出を **finding 行** (行頭の箇条書きマーカーに続くタグ) にアンカーした。
実測 fixture (`p1_findings.txt` / `p0_findings.txt` / `tag_and_critical_keyword.txt`) は
いずれも `- [P#] <title> — <file>:<line>` という形式だったため、行頭の任意の空白 + 任意の
`-` 箇条書きマーカー + 任意の空白 + `[P#]` にアンカーする正規表現 (`^[[:space:]]*-?[[:space:]]*\[P[0-3]\]`)
に変更し、行として一致したものだけからタグを抽出するようにした。地の文での引用は
行頭に来ないため拾われなくなる。

**倒れる方向**: アンカーに一致しない場合は `HAD_SIGNAL=0` のまま R-1 の fail-closed
分岐 (無条件で needs-director) に委ねられるため、取りこぼしは安全側に倒れる。「引用され
た `[P#]` を拾う」方向の緩さのみを塞ぎ、本物の finding 行 (行頭アンカーに一致するもの) は
本文中に無関係な `[P#]` 引用が同居していても引き続き検出されることを regression test で
確認済み (`scripts/test_kai_review.sh` の F-A 節)。

**設計原則との対応**: このミッション (`safety-gate-hardening`) の中心テーマである
「危険な結論 (auto-done) は allowlist、かつ判定 unit (どこを見るか) も 1 つに絞る」の
実例。F-A は「allowlist にはしていたが scope を絞らなかった」ことによる欠陥だった —
出力全体を走査して「どれか 1 つでも `[P#]` があれば構造化シグナルあり」と見なしていたのを、
判定 unit を「finding 行」に絞ることで閉じた。`plan_review.md` の verdict 正規化で同種の
欠陥を 3 回踏んで「判定 unit を最初の中身のある unit 1 つに絞る」に落ち着いたのと同じ構造。

### `--dry-run` (F6, Director 指示)

`--skip-pull` は plan.sh pull だけを飛ばす（task が既に in_progress な場合の再実行用）のに対し、
`--dry-run` は plan.sh への書き込み（pull/done/needs-director）を一切行わず、判定結果を stdout に
表示するだけ。実 PR に対する smoke test・動作確認で実タスクの status を壊さないための区別。

### 一時ファイルの mktemp 化 (F3)

固定 `/tmp/kai-review-output.txt` は codex-review task が並列実行された場合（手動起動との衝突含む）に
相互上書きする事故を招くため、出力ファイル・stderr ファイルとも `mktemp` で一意化し、cleanup trap で
削除する。

### `codex-review` skill の正式登録

`plan.sh lint` が「skill 'codex-review' not in skill-permissions.yaml」警告を出していた積み残しを解消
するため、`config/skill-permissions.yaml` に `codex-review: {allow: [], deny: []}` として登録した。
Codex CLI プロセスは Claude Code の PreToolUse hook を経由しないため、この allow/deny は実行時には
適用されない（known skill 一覧に載せるためだけの登録）。

---

## 運用上の注意（Phase 3 で判明）

### codex-review の鶏と卵問題

Dispatcher は常に **main 版の `scripts/kai-review.sh`** を起動する
（`dispatcher.sh` の `KAI_REVIEW_SH` は `$CREWVIA_REPO_ROOT/scripts/kai-review.sh` を指す絶対パス）。
そのため **kai-review.sh 自体を修正する PR は、merge されるまで自分自身で dogfood できない**
（Phase 3 で実際に 4 回空振りした）。

- kai-review.sh を修正する task の `codex-review` review task（自己レビュー）は、
  **fix PR が main に merge されてから** 積むこと。merge 前に積んでも旧コードでレビューされる。
- Director は §12 の PR 運用同様、「常駐プロセスが読み込むファイルの fix は merge 後に
  効果を確認する」原則を codex-review にも適用する。

### Result の記録方法

`plan.sh done` は複数行を安全に扱う（`build_task_body` 経由で body に書くだけで frontmatter には
触れない）。1 行制約が必要なのは `plan.sh needs-director` の reason だけ
（frontmatter の `needs_director_reason` に直接書かれるため）。task ファイルの直接編集
（heredoc 等）は禁止 — Worker が長時間ハングする事故につながる（詳細: `agents/worker.md` §5）。

---

## Phase 4 候補（未着手）

Phase 2/3 の実運用結果を見て、以下は着手していない検討事項として残っている:

- `start.sh --engine codex` で bash / code / docs / qa の Codex 化
- kai-review.sh 内で Taskvia の /api/request (approval) と /api/knowledge を直接呼ぶ
- `worker-names.yaml` に engine 情報（codex / claude）を含める
- LGTM 一致時の自動 merge 承認
- heartbeat 継続更新（長時間 review のタイムアウト管理）

---

## 参考ファイル

- `agents/worker-codex.md` — Kai の identity・操作手順（Director 向け詳細版）
- `scripts/kai-review.sh` — Kai 起動スクリプト実体
- `codex:codex-cli-runtime` skill — Codex CLI の詳細 syntax
- `codex:gpt-5-4-prompting` skill — Codex prompt 設計指針
- `knowledge/review.md` — Claude Seo の review ナレッジ（比較参考）
