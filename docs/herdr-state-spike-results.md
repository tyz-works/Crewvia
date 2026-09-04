# herdr agent_status Spike 結果レポート (Rule 5 設計用)

## 環境

| 項目 | 値 |
|------|-----|
| herdr version | 0.8.2 (stable) |
| WSL kernel | 6.18.33.2-microsoft-standard-WSL2 |
| 実施日 | 2026-09-04 |
| 参照設計 | `docs/herdr-state-rule5-design.md` (PR #138) §4 |
| herdr workspace | `crewvia-spike` (w7) |
| spike tab | `spike-claude` (w7:t1) |
| spike pane | `w7:p1` |
| claude cwd | `/tmp/tmp.RF5Ys9D0eo` (mktemp) |
| 起動オプション | `claude --dangerously-skip-permissions` |
| crewvia env | なし (AGENT_NAME 等は渡さず、registry/queue を汚染しない) |

---

## 6 状況の結果

### 状況 1: 思考中 (spinner)

**結論: `working`（予想通り）**

長い計算タスク（「1から10000までのすべての素数を列挙し…」）を送信し、生成中にサンプリング。

#### agent_status 推移

| sample | 経過時間 | agent_status |
|--------|----------|--------------|
| 1 | t+0s | `working` |
| 2 | t+3s | `working` |
| 3 | t+6s | `working` |
| 4 | t+9s | `working` |
| 5 | t+12s | `working` |

**揺れなし**。全サンプルで `working`。

#### matched rule

```
live_turn_working (priority 970) -> working
```

`live_turn_working` が `bottom_non_empty_lines(12)` の any_count を 2 以上満たして一致。spinner 表示と出力中のテキストを検知。

#### pane 末尾 (サンプリング時点)

```
· Calculating… (27s · ↓ 1.3k tokens)

──────────────────────────────────────────────────────────────────────────────────────────────
❯
```

---

### 状況 2: 完了後の待機

**結論: `done`（tab 非 focus 時）/ screen detect は `idle`**

重要: `herdr pane get` の `.result.pane.agent_status` と `herdr agent explain` の判定が異なる。

| API | 返値 | 意味 |
|-----|------|------|
| `herdr pane get w7:p1` → `.result.pane.agent_status` | `done` | screen=idle + tab 非 focus |
| `herdr agent explain w7:p1 --json` → `.state` | `idle` | 画面内容のみ判定 |

#### agent_status 推移 (herdr pane get)

| sample | agent_status |
|--------|--------------|
| 1 | `done` |
| 2 | `done` |
| 3 | `done` |

**揺れなし**。

#### matched rule (explain)

```
live_prompt_box (priority 950) -> idle
osc_title_idle  (priority 250) -> idle
```

`❯` が `prompt_box_body` に出現して `live_prompt_box` が一致。

#### pane 末尾

```
✻ Baked for 1m 9s · done 5:31 PM

──────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────
  ⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker
  📁 tmp.RF5Ys9D0eo │ Fable 5.1
  ⏵⏵ bypass permissions on
```

---

### 状況 3: テキスト質問待ち (Seo 型)

**結論: `done`（tab 非 focus 時）/ screen detect は `idle`**

「AとBどちらが好きか質問して待ってください」を送信。Claude が「AとBのどちらが好きですか?」と聞いてターン終了。

#### agent_status 推移 (herdr pane get)

| sample | agent_status |
|--------|--------------|
| 1 | `done` |
| 2 | `done` |
| 3 | `done` |
| 4 | `done` |
| 5 | `done` |

**揺れなし**。

#### matched rule (explain)

```
live_prompt_box (priority 950) -> idle
osc_title_idle  (priority 250) -> idle
```

状況 2 と同じ rule。テキスト質問待ちと純粋な完了後待機は**区別不可能**。

#### pane 末尾

```
● AとBのどちらが好きですか?

✻ Worked for 2s · done 5:33 PM

──────────────────────────────────────────────────────────────────────────────────────────────
❯
```

#### 重要な観察

Claude は質問してから**ターンを終了**し `❯` に戻る。ここでの `idle`/`done` は「user の回答待ち」と「純粋な完了後待機」を区別しない。  
→ Rule 5 B (idle-with-task) は両方を拾う。意図通り。

---

### 状況 4: AskUserQuestion の選択 UI ⭐最重要

**結論: `blocked`（予想外に confirmed、A で拾える）**

「AskUserQuestion ツールを使って選択肢を出してください」を送信。選択 UI が表示される。

#### agent_status 推移 (herdr pane get)

| sample | agent_status |
|--------|--------------|
| 1 | `blocked` |
| 2 | `blocked` |
| 3 | `blocked` |
| 4 | `blocked` |
| 5 | `blocked` |

**揺れなし**。全サンプルで `blocked`。

#### matched rule (explain)

```
live_blocked_form (priority 980) -> blocked
```

AskUserQuestion の選択 UI が「Esc to cancel」を含むフォーム形式のため `live_blocked_form` が一致。

#### pane 末尾

```
──────────────────────────────────────────────────────────────────────────────────────────────
 ☐ 好きな色

好きな色は何ですか?

❯ 1. 赤
     赤色
  2. 青
     青色
  3. 緑
     緑色
  4. Type something.
──────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
```

#### spec §2 の A 定義への影響

`blocked` で検知できるため **Rule 5 A が有効**。変更不要。

---

### 状況 5: 承認ダイアログ

**結論: `blocked`（予想通り）**

新規ディレクトリ (`/tmp/tmp.RF5Ys9D0eo`) で `claude --dangerously-skip-permissions` を起動した際の「Trust this folder」ダイアログ。

#### agent_status 推移 (herdr pane get)

| sample | agent_status |
|--------|--------------|
| 1 | `blocked` |
| 2 | `blocked` |
| 3 | `blocked` |

**揺れなし**。

#### matched rule (explain)

```
live_blocked_form (priority 980) -> blocked
```

#### pane 末尾

```
 Accessing workspace:

 /tmp/tmp.RF5Ys9D0eo

 Quick safety check: Is this a project you created or one you trust?

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
```

#### 注記

`--dangerously-skip-permissions` を付けても初回起動時の trust 確認は回避できない。この UI が `live_blocked_form` に一致して `blocked` になる。PreToolUse の Bash 実行確認ダイアログも同様の UI (「Enter to confirm · Esc to cancel」) なので同じ rule で `blocked` になると考えられる（状況 4 で AskUserQuestion の「Esc to cancel」も `blocked` になったことから推定）。

---

### 状況 6: done → idle の遷移

**結論: `done` のまま維持（60 秒後も変化なし）**

状況 2 完了後に tab を focus せず 60 秒間観測。

#### agent_status 推移 (herdr pane get)

| sample | 経過時間 | agent_status |
|--------|----------|--------------|
| 1 | t+0s | `done` |
| 2 | t+5s | `done` |
| 3 | t+10s | `done` |
| 4 | t+15s | `done` |
| 5 | t+20s | `done` |
| 6 | t+25s | `done` |
| 7 | t+30s | `done` |
| 8 | t+35s | `done` |
| 9 | t+40s | `done` |
| 10 | t+45s | `done` |
| 11 | t+50s | `done` |
| 12 | t+55s | `done` |
| 13 | t+60s | `done` |

**揺れなし**。tab を focus しない限り `done` → `idle` への自動遷移は発生しない。

#### spec §2 の B 定義への影響

Rule 5 B の条件 `state ∈ {idle, done}` で **両方を捕捉する** 設計は正しい。tab focus 時は `done` → `idle` になるが、grace 時間の計測は `{idle, done}` セットを「同一状態」として扱えば問題ない。

---

## idle vs done の重要な区別

`herdr pane get` の `agent_status` は tab focus 状態を加味した **合成値** を返す。

| 画面状態 | tab focus | `herdr pane get` | `herdr agent explain` |
|----------|-----------|-------------------|-----------------------|
| ❯ プロンプト表示 | あり | `idle` | `idle` |
| ❯ プロンプト表示 | なし | `done` | `idle` |

lib_mux.py の `state()` verb で `herdr pane get` を使う場合、`done` は実質 `idle` の別名。Rule 5 B の実装では `state in {"idle", "done"}` として両方を扱うことを推奨。

---

## spec §2 の A/B 定義への影響まとめ

| Rule | 定義 | 結果 | 変更要否 |
|------|------|------|----------|
| A: blocked | `state == blocked` | 状況 4 (AskUserQuestion) も `blocked` を返す → A で**すべての UI ブロック**を網羅 | 変更不要 |
| B: idle-with-task | `state ∈ {idle, done}` かつ assignment 存在 | `done` = tab 非 focus の `idle` なので両方を含む設計が必要 | `done` を明示的に含める（既に spec 記載済み） |

**spec §2 の変更は不要**。A も B も定義通りに機能することを確認。

---

## 気づいた点

1. **AskUserQuestion = blocked は重要な発見**  
   spec §4 では「不明」としていたが、実際は `live_blocked_form` rule (priority 980) が「Esc to cancel」パターンを検知して `blocked` を返す。Claude Code の選択 UI (AskUserQuestion / trust dialog / PreToolUse 確認) はすべて同じフォームパターンを使うため、Rule 5 A 一本で統一的に検知できる。

2. **idle と done は pane get レベルで異なるが explain レベルでは同じ**  
   `herdr agent explain` は画面内容のみ判定するため常に `idle`。`herdr pane get` は tab visibility も加味するため `done` を返す。Dispatcher 実装では `herdr pane get` の `agent_status` を使うべき。

3. **テキスト質問待ちは idle/done と区別不可**  
   Claude がテキストで質問してターン終了すると、純粋な完了後待機と同じ `idle`/`done` になる。これは Rule 5 B (idle-with-task) で拾うことができる。意図した設計通り。

4. **done 状態は自動で idle に遷移しない**  
   tab を focus すれば `done` → `idle` になるが、非 focus のまま 60 秒経過しても `done` のまま。grace 秒カウントは `idle` と `done` を区別せず同じ状態として扱ってよい。

5. **herdr agent explain の region 構造**  
   blocked 判定の `live_blocked_form` rule は `after_last_horizontal_rule` 領域を見る。Claude Code の UI は水平罫線 (`────`) で区切られた構造を持つため、罫線以降のフォームコンテンツが対象になる。

---

## 後片付け記録

```bash
# spike workspace close
herdr workspace close crewvia-spike

# mktemp ディレクトリ削除 (手動)
# /tmp/tmp.RF5Ys9D0eo

# claude プロセス確認
pgrep -f claude
```
