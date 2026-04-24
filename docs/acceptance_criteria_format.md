# acceptance_criteria 正準フォーマット仕様

作成: 2026-04-23 / Data (task_090_p0_data)
最終化: 2026-04-24 / Troi (task_090_pf_troi)
Admiral 確定方針: **(R) ハイブリッド形式** — 構造化 YAML を推奨、自由記述 (markdown 箇条書き) は既存互換として許容

---

## 1. 概要

`acceptance_criteria` は Director が task 作成時に記述し、Plan Reviewer が「測定可能か」を検査し、Verifier が Worker の成果物を判定する際の共通契約。

同一フィールドが上流 (Plan Reviewer) と下流 (Verifier) の両方で使われるため、フォーマットの統一が判定精度に直結する。

---

## 2. フォーマット仕様

### 2.1 構造化形式（推奨）

task ファイルの frontmatter に YAML リストで記述する。

```yaml
acceptance_criteria:
  - id: ac-1
    description: "○○が △△ されること"
    measurable_check: "□□ を実行して ◇◇ が確認できること"
  - id: ac-2
    description: "…"
    measurable_check: "…"
```

**必須フィールド**:

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | string | AC の識別子 (例: `ac-1`, `ac-2`)。タスク内で一意 |
| `description` | string | 何が達成されるべきかを一文で記述 |
| `measurable_check` | string | Verifier が実際に確認できる具体的手順 or 観測可能事実 |

**任意フィールド**:

| フィールド | 型 | 説明 |
|---|---|---|
| `severity` | `required` \| `optional` | 省略時は `required`。全 `required` AC が pass で task pass |

### 2.2 自由記述形式（既存互換として許容）

frontmatter の `acceptance_criteria` フィールドに markdown 箇条書きを文字列として記述するか、Description 本文に記述する。

```yaml
# パターン A: frontmatter に inline リスト（既存コードが使用中）
acceptance_criteria: ["○○ が正常に動作すること", "テストが pass すること"]
```

```yaml
# パターン B: frontmatter に文字列ブロック
acceptance_criteria: |
  - ○○ が正常に動作すること
  - テストが pass すること
```

または Description 本文の箇条書き (frontmatter に `acceptance_criteria` なし):

```markdown
## Description

確認観点:
- ○○ が正常に動作すること
- テストが pass すること
```

**許容はするが非推奨**: 自由記述形式では `lint_plan.py` が `measurable_check` の有無を判定できない。新規タスク作成時は構造化形式を使うこと。

---

## 3. 切り替え条件（構造化 vs 自由記述）

| 状況 | 推奨フォーマット | 理由 |
|---|---|---|
| 新規タスク作成 | **構造化形式** | Verifier の判定精度向上 |
| 既存タスクの編集 | 既存形式を維持 (移行強制なし) | 一括移行は Out of Scope |
| verification.mode: strict | **構造化形式必須** | safety_review / rollback_review の判定に measurable_check が必要 |
| verification.mode: light | 自由記述形式も可 | 機械 check + 簡易 diff のみで判定 |
| リサーチ・ドキュメントタスク | 自由記述形式も可 | 観点箇条書きで十分 |

---

## 4. サンプル

### 4.1 light mode 用（2-3 criteria）

```markdown
---
id: t001
title: README に環境変数リストを追記する
skills: [docs]
priority: low
status: pending
verification:
  mode: light
acceptance_criteria:
  - id: ac-1
    description: "README.md の '## 環境変数' セクションに全変数が列挙されていること"
    measurable_check: "README.md を開き、NTFY_URL / NTFY_TOPIC / NTFY_USER / NTFY_PASS / APPROVAL_TOKEN_TTL_SECONDS が記載されていることを目視確認"
  - id: ac-2
    description: "追記後に README の既存コンテンツが壊れていないこと"
    measurable_check: "markdownlint README.md が exit 0 で終了すること"
---
```

### 4.2 standard mode 用（4-6 criteria）

```markdown
---
id: t002
title: POST /api/request に approve_url / deny_url を追加する
skills: [code, typescript]
priority: high
status: pending
verification:
  mode: standard
  checks: [lint, typecheck, diff_review, acceptance_review]
acceptance_criteria:
  - id: ac-1
    description: "POST /api/request のレスポンスに approve_url と deny_url が含まれること"
    measurable_check: "curl -X POST /api/request で返る JSON に 'approve_url' と 'deny_url' キーが存在することを確認"
  - id: ac-2
    description: "approve_url は https://<TASKVIA_BASE_URL>/api/approve-token/<token> 形式であること"
    measurable_check: "レスポンスの approve_url を正規表現 /https?://.*\/api\/approve-token\/[A-Za-z0-9_-]{32}/ でマッチ確認"
  - id: ac-3
    description: "NTFY_URL が未設定の場合でも id のみ返し、approve_url / deny_url は null になること"
    measurable_check: "NTFY_URL を unset した状態でリクエストし、レスポンスの approve_url が null であることを確認"
  - id: ac-4
    description: "TypeScript の型チェックが通ること"
    measurable_check: "npx tsc --noEmit が exit 0 で終了すること"
  - id: ac-5
    description: "既存の POST /api/request のレスポンス { id } が必ず含まれること（後退がないこと）"
    measurable_check: "curl レスポンスに 'id' キーが存在し、nanoid 形式の文字列であることを確認"
---
```

### 4.3 strict mode 用（7 criteria 以上、measurable_check 厳密）

```markdown
---
id: t003
title: ntfy トークン発行と approve/deny エンドポイントの整合修正
skills: [code, typescript, security]
priority: high
status: pending
verification:
  mode: strict
  checks: [lint, typecheck, unit_test, integration_test, diff_review, acceptance_review, safety_review, rollback_review]
  escalate_on_fail: true
acceptance_criteria:
  - id: ac-1
    description: "approval_token:{token} が Redis に TTL 付きで格納されること"
    measurable_check: "テスト後に redis-cli TTL approval_token:<token> が 900 以下の正数を返すことを確認"
  - id: ac-2
    description: "POST /api/approve-token/{token} が consumed_at 済みトークンに 409 を返すこと"
    measurable_check: "同一トークンを 2 回 POST し、2 回目が HTTP 409 + { error: 'token_already_used' } を返すことを curl で確認"
  - id: ac-3
    description: "POST /api/deny-token/{token} が存在しないトークンに 404 を返すこと"
    measurable_check: "存在しないトークン文字列で POST し、HTTP 404 + { error: 'invalid_or_expired_token' } を確認"
  - id: ac-4
    description: "トークン消費後に approval:{id} の status が 'approved' / 'denied' に更新されること"
    measurable_check: "approve 後に GET /api/status/{id} を呼び、{ status: 'approved' } が返ることを確認"
  - id: ac-5
    description: "TypeScript 型チェックが通ること"
    measurable_check: "npx tsc --noEmit が exit 0 で終了すること"
  - id: ac-6
    description: "lint エラーがないこと"
    measurable_check: "npm run lint が exit 0 で終了すること"
  - id: ac-7
    description: "既存の GET /api/status/{id} エンドポイントが認証必須のまま維持されること（後退なし）"
    measurable_check: "Authorization ヘッダなしの GET が HTTP 401 を返すことを確認"
  - id: ac-8
    description: "変更が NTFY_URL 未設定環境でも Taskvia の既存承認フロー (approval:{id} / status polling) を破壊しないこと"
    measurable_check: "NTFY_URL を unset した状態でエンドツーエンドの approval フロー（request → status polling → approve → status=approved）が完走することを確認"
---
```

---

## 5. lint_plan.py への影響（新旧両対応仕様）

### 5.1 現状の AC 検査状況

`lint_plan.py` の `REQUIRED_FIELDS = ['id', 'title', 'skills', 'status', 'priority']` に `acceptance_criteria` は**含まれていない**。
AC 不在でも現状では lint が pass する。

### 5.2 追加すべき検査ロジック（偽陽性・偽陰性を作らない）

```python
# check_acceptance_criteria 関数として追加 (check_frontmatter の後に呼ぶ)

def _detect_ac_format(ac_value) -> str:
    """Returns: 'structured' | 'freetext' | 'inline_list' | 'empty' | 'missing'"""
    if ac_value is None:
        return 'missing'
    if isinstance(ac_value, list):
        if len(ac_value) == 0:
            return 'empty'
        if isinstance(ac_value[0], dict):
            return 'structured'
        return 'inline_list'   # ["text1", "text2"] 形式
    if isinstance(ac_value, str):
        stripped = ac_value.strip()
        if not stripped:
            return 'empty'
        # Detect markdown bullets: lines starting with - or *
        if re.search(r'(?m)^[ \t]*[-*]\s+.+', stripped):
            return 'freetext'
        return 'freetext'      # plain text も自由記述扱い
    return 'missing'


def _check_structured_ac(ac_list, task_id) -> list[tuple[str, str, str]]:
    """Check each structured AC entry has required fields."""
    results = []
    for i, entry in enumerate(ac_list):
        if not isinstance(entry, dict):
            results.append(('WARN', 'acceptance_criteria',
                f"task/{task_id}: ac[{i}] is not a dict in structured format"))
            continue
        for field in ('id', 'description', 'measurable_check'):
            if not entry.get(field):
                results.append(('WARN', 'acceptance_criteria',
                    f"task/{task_id}: ac[{i}] missing field '{field}'"))
    return results


def check_acceptance_criteria(tasks: list[dict]) -> list[tuple[str, str, str]]:
    results = []
    for meta in tasks:
        tid = meta.get('id', '<unknown>')
        ac = meta.get('acceptance_criteria')
        fmt = _detect_ac_format(ac)

        if fmt == 'missing':
            results.append(('WARN', 'acceptance_criteria',
                f"task/{tid}: acceptance_criteria not found — add to frontmatter (structured format recommended)"))
        elif fmt == 'empty':
            results.append(('WARN', 'acceptance_criteria',
                f"task/{tid}: acceptance_criteria is empty"))
        elif fmt == 'inline_list':
            results.append(('WARN', 'acceptance_criteria',
                f"task/{tid}: acceptance_criteria uses inline list format — consider structured format {{ id, description, measurable_check }}"))
        elif fmt == 'structured':
            results += _check_structured_ac(ac, tid)
        # fmt == 'freetext': allowed, no warning

    return results
```

### 5.3 検出ロジック正規表現まとめ

| フォーマット判定 | 検出方法 |
|---|---|
| 構造化形式 | AC がリスト型 かつ 各要素が `dict` |
| inline リスト | AC がリスト型 かつ 各要素が `str` |
| 自由記述 (本文) | AC が `str` 型、または frontmatter に AC なし + Description に `(?m)^[ \t]*[-*]\s+.+` マッチ |
| 空 | AC がリスト型で長さ 0、または AC が空文字列 |

### 5.4 lint 影響サマリー

| 状況 | 現状 | 追加後 |
|---|---|---|
| AC フィールドなし | 検査なし (pass) | WARN |
| AC = 空リスト | 検査なし (pass) | WARN |
| AC = inline list `["text"]` | 検査なし (pass) | WARN (推奨形式への移行促進) |
| AC = 自由記述文字列 | 検査なし (pass) | pass (既存互換) |
| AC = 構造化形式、フィールド完備 | 検査なし (pass) | pass |
| AC = 構造化形式、フィールド欠如 | 検査なし (pass) | WARN |
| strict モード | — | WARN が FAIL に昇格 |

**偽陽性なし**: 既存タスクの大多数 (AC なし) は WARN のみ、FAIL にはならない。
**偽陰性なし**: 空・欠如は必ず WARN/FAIL で検出される。

---

## 6. 付録: 既存タスクの AC 記述揺れパターン（参考）

*移行強制なし。新規作成時の参考資料。*

| タスク | パターン | 備考 |
|---|---|---|
| queue/archive/test-beverly-qa/tasks/t003.md | `acceptance_criteria: ["test 1", "test 2"]` | inline リスト形式。lint が WARN になる候補 |
| queue/archive/20260413-taskvia-hook-review/tasks/t001.md | frontmatter に AC なし、Description に確認観点を `-` 箇条書きで記述 | 自由記述形式。Verifier は Description を読んで判定 |
| queue/archive/20260411-infra-backlog/tasks/t001.md | frontmatter に AC なし、Description に問題説明のみ (観点なし) | Verifier は Result を読んで推測判定 — 判定精度が最も低い |
| queue/archive/strict-test/tasks/t001.md | frontmatter に AC なし、Description も空 | テスト用データのため空白、実務タスクでは不可 |
| queue/archive/qa4-test (Verifier notes) | AC 3/3 充足確認 — frontmatter 構造化形式の参照 | Verifier が前提として構造化 AC を期待している実証 |

**観察**: 現状の大多数のタスクは AC を持たず、Verifier が Description / Result から意図を読み取る形になっている。構造化 AC の導入により Verifier の判定ブレを削減できる。

---

*本仕様は task_090 Phase 0 (Data) が策定し、Phase F (Troi) が文面整形した正準ドキュメントです。仕様変更は Admiral 承認が必要。*
