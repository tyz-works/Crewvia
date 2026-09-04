# QA ゲート機構 設計 Spec

> **ステータス**: Draft — Director レビュー待ち  
> **作成**: 2026-09-05, Minjun (t001)  
> **対象 mission**: 20260905-qa-gate  

---

## 背景と設計方針

### 問題の本質

mission 20260905-child-session-marker t002 QA での事故（Arjun が代替検証で PASS を出した件）の根本原因は **「Worker が嘘をついていない」** 点にある。「実施不可、代替で代替済み」と正直に書いても PASS に集約された。壊れていたのは集約ロジック（=人間の判断か、LLM の個体差に依存した判定）であり、プロンプト強化で対処しようとしても既知の idle Worker shutdown ブレと同型の問題で効果は頭打ちになる。

### 設計の 3 原則

1. **機械が判定する**: LLM に PASS/FAIL の判断を委ねない。ルールに基づく導出にする
2. **安い逃げ道を用意する**: ゲートを塞いでも別の抜け道を探すだけ。`needs-director` を代替検証より安くする
3. **壊れにくい形式**: LLM が書いた出力をパースする。形式が微妙にズレても壊れないパーサーを設計する

---

## ① 総合判定の機械的導出

### 設計: `## QA Gate` セクション + 行指向パーサー

**QA Worker が result に書く形式:**

```
## QA Gate

checkpoint: <名前> | required: yes | result: observed
checkpoint: <名前> | required: no  | result: not_run | note: <理由>
checkpoint: <名前> | required: yes | result: failed   | note: <詳細>
```

**ルール:**
- `result` は `observed` / `not_run` / `failed` の 3 値のみ
- `required: yes` かつ `result: observed` 以外 → **FAIL ゲート**（done 拒否）
- `required: no` は証跡として記録するが PASS/FAIL に影響しない

### パーサー方針（壊れにくさ重視）

```python
# plan.sh cmd_done 内での検証（QA タスク検出: 'qa' in skills）
def _validate_qa_gate(result_text: str) -> list[str]:
    """
    ## QA Gate セクションを探してチェックポイントを検証。
    FAIL 理由のリストを返す（空 = PASS）。
    """
    # セクションを探す（大文字小文字不問）
    section_match = re.search(r'^##\s+QA\s+Gate\s*$', result_text, re.MULTILINE | re.IGNORECASE)
    if not section_match:
        return []  # セクションなし = 旧来タスク互換（スキップ）

    # セクション以降のテキストを対象に行マッチ
    section_body = result_text[section_match.end():]
    
    fails = []
    # checkpoint: ... | required: yes | result: observed/not_run/failed
    pattern = re.compile(
        r'checkpoint:\s*(?P<name>[^|]+?)\s*\|\s*required:\s*(?P<req>yes|no)\s*\|\s*result:\s*(?P<res>observed|not_run|failed)',
        re.IGNORECASE
    )
    found_any = False
    for m in pattern.finditer(section_body):
        found_any = True
        req = m.group('req').lower()
        res = m.group('res').lower()
        name = m.group('name').strip()
        if req == 'yes' and res != 'observed':
            fails.append(f"  [{res}] {name}")
    
    return fails
```

**エラー時の挙動:**
```
[plan.sh] QA gate FAIL — 以下のチェックポイントが not_run / failed です:
  [not_run] /proc/PID/environ による env-var 確認
  [failed]  smoke-test: real binary 実行

plan.sh done をブロックします。
対処方法:
  1. チェックポイントを完遂して再度 plan.sh done を呼ぶ
  2. 完遂できない場合は plan.sh needs-director <task_id> "<理由>" で差し戻す
```

### frontmatter の変更

QA タスクに `qa_checkpoints` フィールドを追加（オプション）:

```yaml
---
id: t002
title: "QA: child-session-marker の動作確認"
skills: [qa]
qa_checkpoints:
  - name: "/proc/PID/environ による env-var 確認"
    required: true
  - name: "smoke-test: real binary 実行"
    required: true
  - name: "ログファイル出力確認"
    required: false
---
```

**`qa_checkpoints` の役割:**
- Director が「何を検証すべきか」を事前に宣言する
- QA Worker は `## QA Gate` セクションでこれらに対応するチェックポイントを記録する
- パーサーは `## QA Gate` 内容を検証する（frontmatter の `qa_checkpoints` はガイダンスであり、追加チェックポイントも許容）
- `qa_checkpoints` を省略した場合 → `## QA Gate` セクションが書かれていれば検証、なければスキップ（後方互換）

### `verified` ステータスとの統合

既存の `verified` は「Taskvia による外部 verification pass」を表す別の概念なので統合しない。  
QA ゲートは `done` への入口（`in_progress` → `done` の遷移をガードする）。Taskvia verification は `done` → `verified` の別フロー。

**ステータス遷移（QA タスク）:**
```
in_progress
  ↓ plan.sh done (QA gate PASS)
done
  ↓ plan.sh verify-result pass （Taskvia verification, 別フロー）
verified
```

---

## ② 証拠の提出必須化

### 設計: `required_evidence` frontmatter + 存在チェック

**frontmatter 追加フィールド:**

```yaml
required_evidence:
  - ".jsonl"
  - "stat "
  - "/home/"
```

**検証方針:**
- `plan.sh done` は result 文字列に対し、各パターンが **部分文字列として含まれるか** を確認
- 正規表現は不使用（将来拡張余地はあるが、初期実装は単純な `in` チェック）
- **「文字列の存在確認で足りる」** — 過剰に厳密にすると正当な完了を弾く

```python
def _validate_required_evidence(result_text: str, required: list[str]) -> list[str]:
    """証拠パターンの存在チェック。見つからないパターンのリストを返す。"""
    missing = []
    for pattern in (required or []):
        if pattern and pattern not in result_text:
            missing.append(f"  '{pattern}'")
    return missing
```

**エラー時の挙動:**
```
[plan.sh] required_evidence が result に見つかりません:
  '/proc/'
  'stat '

plan.sh done をブロックします。
証拠を result に含めてから再度実行するか、
証拠が存在しない場合は plan.sh needs-director <task_id> "<理由>" を使用してください。
```

### 証拠が出せない正当なケースの扱い

**原則**: Director が `required_evidence: []` (空リスト) で作成 → 検証スキップ。

理由:
- 「証拠が出せない」の判断は **Director が事前にすべき** こと
- Worker が実行時に「証拠が出せないから例外扱い」を自己申告できてしまうと、ゲートが機能しない
- Worker が詰まった場合は ③ `plan.sh needs-director` で差し戻す

**例外として認める実装:**

```yaml
required_evidence_exemption: "integration test 環境が CI 上でのみ実行可能なため、ローカルでは証拠取得不可"
```

- Worker が result に `[EVIDENCE EXEMPTION: <理由>]` を書いた場合、パーサーは `required_evidence` チェックをスキップ
- ただし、**この抜け道の乱用を防ぐ**: Taskvia verification フラグで Director が確認

**今回の実装範囲**: 基本的な存在チェックのみ。exemption は設計に含めるが実装は t002 の判断に委ねる。

---

## ③ 「詰まった」を安く表明できる出口

### ステータス名: `needs_director`

**候補の比較:**

| 名前 | pros | cons |
|---|---|---|
| `blocked` | 短い | `blocked_by` の「依存未達」と混同。コード上 `is_blocked()` 関数と衝突リスク |
| `stuck` | 直感的 | 既存の STATUS_ICON テーブルへの追加のみで済む |
| `needs_director` | 意図が明確。`needs_human_review` と対称 | やや長い |
| `awaiting_director` | 意図が明確 | verbose |

**選択: `needs_director`**

理由:
- `needs_human_review`（Taskvia が使う既存ステータス）との一貫性
- Worker がどこに助けを求めているかが明確
- `blocked_by` との混同なし

### `plan.sh needs-director` コマンド

```bash
plan.sh needs-director <task_id> "<理由>"
```

**実装:**

```python
def cmd_needs_director(args):
    """
    plan.sh needs-director <task_id> "<理由>"
    
    in_progress タスクを needs_director 状態に遷移させる。
    Director の介入を求め、done を拒否する。
    Dispatcher はこのタスクを後続タスクの blocked_by 解除対象にしない。
    """
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) < 2:
        die("needs-director requires <task_id> and <reason>")
    task_id = positional[0]
    reason = positional[1]
    
    def _do():
        # ... slug 解決 ...
        meta, body = load_task(slug, task_id)
        if meta.get('status') not in ('in_progress',):
            die(f"needs-director requires in_progress task (current: {meta['status']})")
        
        meta['status'] = 'needs_director'
        meta['needs_director_reason'] = reason
        save_task(slug, task_id, meta, body)
        print(f"[plan.sh] Task {task_id} → needs_director")
        print(f"[plan.sh] Reason: {reason}")
        print(f"[plan.sh] Director への通知: plan.sh status で確認してください")
    
    with_lock(_do)
```

### `needs_director` の性質

- **非終端ステータス** — `TERMINAL_STATUSES` に追加しない
- **blocked_by 解除しない** — 後続タスクは `needs_director` タスクに依存していても解除されない（正しい挙動）
- **Dispatcher は割り当て対象にしない** — `pending` 以外の非終端ステータスと同じ扱い

**Director の対処フロー:**
1. `plan.sh status` で `needs_director` タスクを発見
2. reason を読んで対処方針を決定
3. 手順を補足した上で `plan.sh update <task_id> --status in_progress --reset` で差し戻し
4. Worker に `lib_mux send` で追加指示

### STATUS_ICON への追加

```python
STATUS_ICON = {
    ...
    'needs_director': '🆘',  # Director への SOS
}
```

### 代替検証より安い経路を確保

`plan.sh needs-director` を **「代替検証して done」より必ず安くする** ために:

- `plan.sh needs-director` は 1 コマンドで完了（説明文を書く手間が唯一のコスト）
- `plan.sh done` に QA gate チェックが入ることで、代替検証を done に押し込もうとするとエラーになる
- エラーメッセージに `plan.sh needs-director` の使い方を必ず明示する

---

## crewvia-qa Skill 配置方針

### 現状の問題

`crewvia-qa` skill の実体は `~/.claude/skills/crewvia-qa/SKILL.md` にあり:
- PR で変更できない（リポジトリ外）
- 複数 WSL 運用で他マシンに伝播しない
- MEMORY.md にも「複数 WSL 運用で移植性を意識する」と記録されている

### 推奨案: **(a) リポジトリ内に移す + symlink インストール**

**理由:**
1. **複数 WSL で伝播できる** — `git pull` するだけで最新版が手に入る
2. **PR でレビュー可能** — QA ゲートの定義変更がレビュー対象になる
3. **変更履歴が追跡できる** — `git blame` / `git log` で変更の経緯が残る
4. オプション (b) の「`agents/worker.md` 側に寄せる」は worker.md が肥大化する。QA 手順の詳細はスキルファイルが適切

**実装方針:**

```
crewvia リポジトリ内:
  skills/
    crewvia-qa/
      SKILL.md    ← ここが正典（現行 ~/.claude/skills/crewvia-qa/SKILL.md の内容）

インストール（README.md / scripts/setup.sh に記述）:
  ln -sf "$(pwd)/skills/crewvia-qa" ~/.claude/skills/crewvia-qa
```

**インストール手順の位置:**
- `README.md` のセットアップセクションに追加
- `scripts/setup.sh` が存在すれば自動化（なければ手動手順のみ）

**今後の拡張**: 他の crewvia 専用 skills（`crewvia-plan-review` 等）も同じ方針でリポジトリ内に取り込める

### 今回のスコープと次ミッションへの送り

| 項目 | t001（本タスク） | t002（実装タスク） |
|---|---|---|
| 設計 spec 作成 | ✅ 本文書 | — |
| `plan.sh` への QA gate + required_evidence + needs-director 実装 | — | ✅ |
| `skills/crewvia-qa/` のリポジトリ内移行 | — | ✅ |
| `crewvia-qa` SKILL.md の内容更新（QA Gate 記述形式を反映） | — | ✅ |
| `agents/director.md` §3 のガイドライン更新 | — | ✅ |
| `agents/worker.md` §5 Pre-Done への needs-director 言及追加 | — | ✅ |

---

## 影響ファイル一覧

| ファイル | 影響内容 |
|---|---|
| `scripts/plan.sh` | `cmd_done` に QA gate / required_evidence 検証を追加。`cmd_needs_director` を新設。`TERMINAL_STATUSES` は変更なし。`STATUS_ICON` に `needs_director: 🆘` を追加。`TASK_META_KEY_ORDER` に `qa_checkpoints`, `required_evidence`, `needs_director_reason` を追加 |
| `agents/worker.md` §5 Pre-Done チェックリスト | `plan.sh needs-director` の使い方を追加 |
| `agents/director.md` §3 QA タスク記述ガイドライン | `qa_checkpoints` / `required_evidence` フィールドの記述方法を追加 |
| `~/.claude/skills/crewvia-qa/SKILL.md` | リポジトリ内 `skills/crewvia-qa/SKILL.md` へ移行 + Step 8 に `## QA Gate` 記述形式を追加 |
| `README.md` | symlink インストール手順を追加 |

---

## 設計上のトレードオフ整理

### なぜ frontmatter ではなく Result 内の `## QA Gate` セクションか？

frontmatter でチェックポイント結果を持たせる案（例: `qa_results.checkpoint_name: observed`）も検討したが:
- frontmatter の YAML は LLM が書くとインデントやエスケープでミスりやすい
- `plan.sh done` は result を引数として受け取る（ファイルに事前書き込みではない）
- Result 内の `## QA Gate` セクションは「QA Worker が書くレポートの一部」として自然

### なぜ YAML fence ではなく行指向形式か？

YAML fence（```yaml ... ```）も検討したが:
- バッククォートのエスケープが引数渡しで壊れやすい
- ネストの深い YAML は LLM が壊しやすい
- `checkpoint: ... | required: yes | result: observed` の行形式は記述量が少なく、1 行単位でパースできる（他の行が壊れても影響が隔離される）

### なぜ `required_evidence` の検証を「存在チェックのみ」にするか？

過剰に厳密な検証（例: 正規表現マッチ、行数確認）は正当な完了を弾くリスクがある。「`.jsonl` という文字列が result に含まれているか」だけで「実パスが言及されている」ことの十分な証拠になる。検証の厳密さよりも、「証拠を書く習慣を強制する」ことが目的。

---

## 実装優先順位

t002 担当 Worker へのガイダンス:

1. **最優先**: `plan.sh needs-director` コマンド（既存インフラへの影響最小、効果最大）
2. **高優先**: `## QA Gate` セクションの検証（`cmd_done` への追加）
3. **中優先**: `required_evidence` 検証（frontmatter フィールド追加を伴う）
4. **別 PR 推奨**: `skills/crewvia-qa/` のリポジトリ内移行（影響範囲が独立している）

各機能は独立して実装・PR 可能。stacked PR または並列 PR として進めることを推奨。
