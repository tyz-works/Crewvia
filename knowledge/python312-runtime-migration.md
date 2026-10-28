# 本番機ツールチェーン下限の不一致と Python 3.12 切替 (20261028-python312-runtime)

## 何が起きたか

CI は `.github/workflows/ci.yml:133` で `ubuntu-latest` + Python 3.12 固定。一方、本番機（Worker/Director
が実際に動くホスト）は Ubuntu 22.04 / Python 3.10 / git 2.34 だった。この差分がどこにも文書化されておらず、
CI を素通りしたコードの前提が本番で崩れて複数の実害が出た。すべて実測済み:

1. **`scripts/plan.sh` の全サブコマンドが死んだ（システム全体停止）**
   `scripts/plan.sh:3940` の入れ子 f-string（`f"{[f"{s}/{m.get('id')}" for s, m, _b in mine]}"`）は
   PEP 701（Python 3.12 以降の構文緩和）で、本番機の既定 `python3`（3.10.12）では
   `SyntaxError: f-string: unmatched '['` になる。commit `d0cd944`（2026-10-02）から本ミッション以前より main に
   存在していた既存コードで、CI（3.12）は通るが本番（3.10）は構文エラーで `plan.sh` そのものが起動しない。
   PR #317 で 3.10 互換の書き方に直した。

2. **テスト 31 件が本番機だけで FAILED**
   同じ PEP 701 由来で `tests/test_task_graph.py` / `tests/test_task_graph_plugin_contract.py` の 31 件が
   本番機（3.10 の `python3`）では SyntaxError で FAILED していた（t004 の Ingrid が shim 有無で実測:
   shim なし `31 failed, 546 passed` → shim あり 全 PASS）。CI では再現しない（CI は 3.12 なので）。

3. **git 2.34.1 で worktree の再利用判定が構造的に壊れていた**
   `scripts/git-helpers.sh:131` の `_crewvia_worktree_lookup` は `git worktree list --porcelain -z`
   （`-z` は NUL 区切り。改行を含む path の worktree 偽装を防ぐ意図的な設計）を使うが、`-z` は git 2.36 以降の
   フラグ。本番機の git 2.34.1 では `error: unknown switch `z'` で失敗し、既存 worktree パスの再利用判定が
   常に失敗 → `WORKSPACE_CREATE_FAILED`（W4）になる。**既存 worktree が残っている状態からの pull だけで再現**
   （新規作成パスは `-z` を使わないため影響なし）。t002 の Elena が 2 回目の pull 試行で実際に踏んだ
   （詳細: `queue/missions/20261028-python312-runtime/tasks/t002.md` の Result「補足」節）。
   git 2.56.0 への更新で解消。`-z` を諦める／フォールバックを足す方向ではなく git を上げる方向を選んだ
   （`-z` の安全設計を本番用に弱めない）。

4. **GitHub 認証が無く push / PR / CI 取得が不可能だった**
   `gh auth login` + `gh auth setup-git` で解消。

5. **codex CLI も node/npm も本番機に無く、codex-review タスクが全滅**
   本ミッションでは codex-review を skip し、review Worker（人間相当のレビュー）に一本化した。

## 根本原因

CI のバージョン（`ubuntu-latest` = 直近の Ubuntu LTS、Python 3.12 固定）と本番機の実際のバージョンの対応関係が
どこにも書かれていなかった。そのため「CI が緑 = 本番で動く」という前提が暗黙に信じられていたが、
本番機は CI より古い Ubuntu 22.04 / Python 3.10 / git 2.34 で、CI が検出できない構文・フラグの非互換が
本番でだけ顕在化した。

## 取った対策

- 本番機に Python 3.12 を導入（deadsnakes PPA。t002）。`/usr/bin/python3` ＝ 3.10.12 のシステム既定には
  一切触れていない（apt が依存するため）。詳細: `scripts/bin/python3` のコメント、[CLAUDE.md](../CLAUDE.md) の不変条件。
- pyyaml / pytest を 3.12 向けに再導入（t002）。
  手段は `python3.12 -m pip install --user --ignore-installed pyyaml pytest`（**venv ではない**。
  `/usr/lib/python3/dist-packages` の Debian パッケージ（3.10 用）が 3.12 の `sys.path` に残っていて
  「already satisfied」と誤判定されるため `--ignore-installed` が必要だった）。
  入れたバージョン: PyYAML 6.0.3 / pytest 9.2.0（他は依存: iniconfig 2.3.1 / packaging 26.3 /
  pip 25.0.1 / pluggy 1.7.0 / Pygments 2.21.0）。
- crewvia の文脈でだけ `python3` を 3.12 に向ける PATH shim を導入（`scripts/bin/python3`。t004。PR #318）。
  全 call site が bare `python3` / `#!/usr/bin/env python3`（149 箇所）なので、`scripts/bin` を PATH の先頭に
  置くだけで一括切替できる設計。`start.sh` の LAUNCH_CMD に加えて、`scripts/lib_daemon_watch.py` の
  `spawn_command()`（dispatcher/watchdog の起動・相互監視の respawn が通る唯一の関数）と、トップレベルの
  `crewvia` launcher（herdr preflight）の 2 箇所に PATH export の穴があり、塞いだ。
- git を 2.34.1 → 2.56.0 に更新（`-z` 対応）。
- `gh auth login` / `gh auth setup-git` で GitHub 認証を設定。
- 実機で切替後の crewvia 全体を検証（t006）: 全サブコマンド正常・`pytest tests/` が `9482 passed, 5 skipped`
  （失敗 0 件）・red_proof 6 件が compile OK・デーモンが `/proc/<pid>/exe` 実測で `python3.12` を直接 exec し
  `PATH` 先頭が `scripts/bin` であることを確認。異常なし、revert 不要と判定。

## 戻し方

- `scripts/bin/python3` を消す（`scripts/bin` には他に plan wrapper しか残らない）。PR #318 の revert で
  `lib_daemon_watch.py` / `crewvia` launcher の PATH export も一緒に消える。
- t002 で入れたパッケージ（sudo 不要）: `python3.12 -m pip uninstall -y pyyaml pytest iniconfig packaging pluggy pygments`
  （または `~/.local/lib/python3.12/site-packages/` を削除）。
- python3.12 本体（sudo 要。Director が別途実施。このミッションの Worker タスクでは実行していない）:
  `sudo apt remove --purge python3.12 python3.12-venv python3.12-dev && sudo add-apt-repository --remove ppa:deadsnakes/ppa`
- git 2.56.0 → 2.34.1 への戻しは本ミッションでは扱っていない（ppa:git-core/ppa を使った場合は同 PPA の remove +
  旧パッケージの再導入。`-z` に依存するコードを戻す必要はない — `_crewvia_worktree_lookup` 側を変えていないため）。
- `/usr/bin/python3` は一度も変更していない（3.10.12 のまま）。

いずれの対策も revert 済みの事故対応ではなく、2026-10-28 時点で本番に入っている構成。

## 補足: レビュー基盤の現状（本ミッションの時点と現在の違い）

本ミッションが codex-review を skip したのは、その時点で codex CLI も代替エンジンも本番機に無かったため。
2026-10-28 に別ミッション `20261028-codex-free-review`（PR #316）が merge され、`scripts/kai-review.sh` に
`claude`（Claude Code CLI 自身をレビュー先にする）と `ensemble`（agy + claude の合議・fail-closed）が追加された。
**現在は「codex が無いので自動レビュー不可」ではない** — `config/crewvia.yaml` の `review.engine` を
`claude` / `ensemble` に設定すれば codex なしでも自動レビューが動く（既定は今までと同じ `engine: codex` の
まま。merge しても既存の挙動は変わらない）。レビュー基盤の設定・現状は `config/crewvia.yaml` の `review:`
ブロックと `knowledge/claude-review.md` を参照。

## 関連

- README「Prerequisites」: 必要な Python / git バージョンと Ubuntu 22.04 での PPA 導入手順
- [CLAUDE.md](../CLAUDE.md) 不変条件: `python3` の shim 解決と `/usr/bin/python3` 不可侵の原則
- `queue/missions/20261028-python312-runtime/tasks/t002.md`（Python 3.12 導入・パッケージ・戻し方の実測）
- `queue/missions/20261028-python312-runtime/tasks/t004.md`（shim の設計・塞いだ穴・PEP 701 の発見。PR #318）
- `queue/missions/20261028-python312-runtime/tasks/t006.md`（切替後の実機検証。9482 passed / 0 failed）
- `knowledge/git-policy.md` §1（`_crewvia_worktree_lookup` が `-z` に依存する設計意図）
