# Multi-Agent System - CLAUDE.md

公開前提のマルチエージェントシステム。
カンバン駆動でタスクを管理し、Taskvia（WebUI）と連携して承認フローを実現する。

## コンセプト

- **タスクが主役**。エージェントはカードをこなすワーカー
- **Star Trek非依存**。汎用的・ポータブルな設計
- **Taskvia連携**。WebUIでカンバン可視化・承認・ナレッジログ
- **公開前提**。tmux依存を排除し、誰でもセットアップできる

---

## ロール設計

### Director（1名）

- タスクの分解・カード生成・Worker割り当て・全体管理
- Taskviaのカンバンに対してBacklog→In Progressの遷移を管理
- Worker完了報告を受けてDoneに遷移させる
- 名前はworker-names.yamlのプールから選ぶ（カスタマイズで固定可能）

### Worker（複数）

- Directorから割り当てられたカードを実行する
- ツール実行が必要な場合はPreToolUse hookでTaskviaに承認リクエストを送る
- 完了・気づき・改善案をTaskviaのログAPIに投稿する
- **名前の継承ルール**: 同じスキルセットのWorkerは同じ名前を引き継ぐ
  - 例: `skills: [ops, bash]` のWorkerは歴代 "Kai" を名乗る
  - ナレッジログに「Kai発見:...」と残り、次のKaiが引き継ぐ
  - これによりWorkerに継続性・愛着が生まれる

---

## 名前システム

設定ファイル: `config/worker-names.yaml`

**デフォルト動作**
- 全ての名前がDirectorにもWorkerにもなれる
- スキルセットをキーとして名前をハッシュ割り当て（同スキル→同名前）
- プール内の名前が足りなければランダムに組み合わせ

**カスタマイズ**
```yaml
customizations:
  - name: Kai
    role: director    # Director固定

  - name: Luca
    role: worker
    skills: [code, python]  # スキル固定

  - name: Sora
    disabled: true          # 除外
```

**名前プール**: 世界各国のファーストネーム50個
アジア東・南・中東・ヨーロッパ・南北アメリカ・アフリカ・スラブ圏をカバー。

---

## スキルタグ

| タグ | 内容 |
|---|---|
| `ops` | インフラ・サーバー操作 |
| `bash` | シェルスクリプト・コマンド実行 |
| `code` | コーディング全般 |
| `python` | Python |
| `typescript` | TypeScript / JavaScript |
| `research` | 調査・情報収集 |
| `database` | DB操作・クエリ |
| `cloud` | クラウド（AWS / OCI） |
| `docs` | ドキュメント作成 |

---

## Taskvia連携

リポジトリ: `tyz-works/taskvia`
WebUI URL: `taskvia.vercel.app`

### 承認フロー（PreToolUse hook）

```bash
#!/bin/bash
TASKVIA_URL="https://taskvia.vercel.app"

CARD_ID=$(curl -s -X POST "$TASKVIA_URL/api/request" \
  -H "Authorization: Bearer $TASKVIA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"tool\": \"$TOOL_NAME\",
    \"agent\": \"$AGENT_NAME\",
    \"task_title\": \"$TASK_TITLE\",
    \"task_id\": \"$TASK_ID\",
    \"priority\": \"$PRIORITY\"
  }" | jq -r .id)

for i in $(seq 600); do
  STATUS=$(curl -s \
    -H "Authorization: Bearer $TASKVIA_TOKEN" \
    "$TASKVIA_URL/api/status/$CARD_ID" | jq -r .status)
  [ "$STATUS" = "approved" ] && exit 0
  [ "$STATUS" = "denied" ]   && exit 1
  sleep 1
done
exit 1
```

### ナレッジログ投稿（PostToolUse or 自発的）

```bash
curl -s -X POST "$TASKVIA_URL/api/log" \
  -H "Authorization: Bearer $TASKVIA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"knowledge\",
    \"content\": \"気づいた内容\",
    \"task_title\": \"$TASK_TITLE\",
    \"task_id\": \"$TASK_ID\",
    \"agent\": \"$AGENT_NAME\"
  }"
```

ログのtype:
- `knowledge` : 気づき・パターン・注意点 → Obsidianにpush
- `improvement` : 改善案 → Obsidianにpush
- `work` : 作業ログ → 一時保存後に破棄

---

## カンバンのカード構造

```json
{
  "card_id": "card-042",
  "column": "backlog",
  "assigned_to": "Kai",
  "priority": "high",
  "task": "タスク内容",
  "skills_required": ["ops", "bash"],
  "tool": "Bash(oci db ...)",
  "blocked_by": ["card-038"]
}
```

カラム遷移:
```
Backlog → In Progress → Awaiting Approval → Done
```

---

## ディレクトリ構成

```
/
  config/
    worker-names.yaml   名前プール・カスタマイズ設定
    skills.yaml         スキルタグ定義
  hooks/
    pre-tool-use.sh     PreToolUse hook（Taskvia承認）
    post-tool-use.sh    PostToolUse hook（ログ投稿）
  agents/
    director.md     Directorのシステムプロンプト
    worker.md           Workerのシステムプロンプト
  scripts/
    start.sh            マルチエージェント起動スクリプト
    plan.sh             タスクプラン管理 CLI（per-task / multi-mission）
    taskvia-sync.sh     queue → Taskvia 同期
  queue/                プラン置き場（plan.sh が管理）
    state.yaml          active mission slug + default_mission
    missions/<slug>/
      mission.yaml      title / status / next_task_id
      tasks/tNNN.md     frontmatter + Description / Result
    archive/            完了 mission の退避先
  registry/
    workers.yaml        Worker のスキル・経験値
    heartbeats/         watchdog 監視用
  CLAUDE.md             このファイル
  README.md             公開向けセットアップガイド
```

---

## 環境変数

| 変数名 | 用途 |
|---|---|
| `TASKVIA_URL` | Taskvia WebUIのURL |
| `TASKVIA_TOKEN` | Taskvia API認証トークン |
| `AGENT_NAME` | 起動時に設定されるエージェント名 |
| `TASK_TITLE` | 現在担当中のタスクタイトル |
| `TASK_ID` | 現在担当中のカードID |

---

## 今後の拡張余地

- [ ] Planner ロール（mission spec → タスク分解の自動化）。skill タグ `planning` を予約済み。
- [ ] `plan.sh status` の JSON 出力サポート（WIP 計測の grep を置き換える）
- [ ] task frontmatter にブランチ名を持たせて Worker に伝達
- [ ] mission 間の優先度設定（現状は default_mission 優先のみ）

---

## 設計原則

1. **tmux非依存** - tmuxがなくても動く。tmuxはオプション
2. **Taskvia非依存** - Taskvia未接続でもスタンドアロンで動作可能
3. **名前はポジション** - 同スキルWorkerは同名前を引き継ぐ
4. **公開前提** - ドメイン固有設定を外に出し、設定ファイルで全カスタマイズ可能

---

## 自律改善システム

Workerが改善案を発見したとき、自律的にフィードバックループを回す仕組み。

### 改善案の分類

Workerは改善案を発見したら以下の基準で自己判断する：

```
自律実行OK（承認不要）    → Directorに提案 → Backlogに自動追加
要確認（リスクあり）      → improvementとしてログ投稿 → ユーザーに確認
```

### 自律実行してよい改善の範囲

`config/autonomous-improvement.yaml` で設定する（ユーザーが自由に編集可能）。

**デフォルトでOKな改善:**
- ドキュメント・コメントの更新
- スクリプトのリファクタリング（動作変更なし）
- ログ・コメントの追加
- テストの追加

**デフォルトで要確認な改善:**
- 外部サービスへの変更
- 設定ファイルの変更
- 削除操作
- 新規ファイルの作成
- パッケージ・依存関係の変更

### フロー

```
Worker が改善案を発見
   ↓
autonomous-improvement.yaml の基準で自己判断
   ↓
自律実行OK → Directorに「改善提案: xxx」を報告
             Directorがカードを作成してBacklogに積む
             優先度低めで自動実行
   ↓
要確認     → type: "improvement" でTaskviaにログ投稿
             Obsidianにも蓄積される
             ユーザーが確認して明示的に依頼した場合のみ実行
```

### autonomous-improvement.yaml（設定ファイル）

```yaml
# 自律改善の設定
# ユーザーが自由に編集できる

autonomous:
  # 承認なしで自律実行してよい改善の種類
  allowed:
    - docs          # ドキュメント更新
    - refactor      # リファクタリング（動作変更なし）
    - comment       # コメント・ログ追加
    - test          # テスト追加

  # 必ずユーザー確認が必要な改善
  requires_approval:
    - external      # 外部サービスへの変更
    - config        # 設定ファイルの変更
    - delete        # 削除操作
    - dependency    # パッケージ・依存関係の変更
    - new_file      # 新規ファイル作成

  # 自律改善の1日あたり最大実行数（暴走防止）
  max_per_day: 5
```

### Workerのシステムプロンプトへの組み込み

Workerは以下の判断フローを持つ：

1. タスク実行中に改善案を発見
2. `autonomous-improvement.yaml` のallowedリストと照合
3. OK → Directorに「改善提案」として報告（自分でタスク化しない）
4. NG → `type: improvement` でTaskvia `/api/log` に投稿して終了
5. DirectorがBacklogに積むかどうかを判断する

※ Workerが自分でBacklogにカードを追加しないこと。必ずDirectorを経由する。
