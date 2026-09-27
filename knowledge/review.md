# review ナレッジベース

> このファイルは review を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## レビュープロトコル

1. `gh pr view {PR_URL}` で概要確認
2. `gh pr diff {PR_URL}` でコード変更を精査する
3. 問題なければ Bot として承認: `gh-app-review approve {PR番号}`
4. マージ: `gh pr merge {PR番号} --squash --delete-branch`
   - `gh-app-review merge` は "Resource not accessible by integration" エラーが出ることがあるため、通常の `gh pr merge` を使う
5. マージ完了後、Director に報告する

## 注意事項

- 承認は必ず `gh-app-review approve` を使うこと（同一アカウントのセルフ承認は GitHub が拒否する）
- `~/bin/gh-app-review` と `~/.key/elni-net-reviewer.*.private-key.pem` が存在することが前提
- トークン取得に失敗する場合は `gh-app-review token` でデバッグ

## Verification Push 運用

QA レイヤーが verification 結果を Taskvia に push するフロー。

```
plan.sh verify-result <task_id> <verdict> [rework_count]
    → scripts/taskvia-verification-sync.sh
    → POST $TASKVIA_URL/api/verification
    → Taskvia Board: バッジ更新 (5s polling で自動反映)
```

**前提**: Taskvia Board にバッジを表示するには、先に `POST /api/request` で approval card が作成されている必要がある（task 実行開始時に hooks が自動実行）。`POST /api/verification` だけではバッジは表示されない。

**リクエストボディ**:
```json
{
  "task_id": "<task_id>",
  "mission_slug": "<slug>",
  "verdict": "pass" | "fail",
  "rework_count": 0,
  "mode": "standard",
  "verifier": "<agent_name>"
}
```

**TTL**:
- `verification:{task_id}`: 7 日
- `verification:index:{slug}`: TTL なし（lazy cleanup）
- `approval:{id}`: 600 秒 — verification push は approval card 作成後 5 分以内に実施すること

**no-op 条件**: `TASKVIA_TOKEN` 未設定 or `CREWVIA_TASKVIA=disabled` 時はスキップ。

---

## レビュー観点: queue / registry の読み取り (構造テストで閉じない形)

`tests/test_queue_reads_go_through_the_guard.py` が、対象モジュールの
`open()` / `.read_text()` / 外部プロセス呼び出しを AST で全部拾い、
allowlist に無ければ落とす。**そこで機械的に止まらない形だけ**、レビューで
見ること。

**この検出器は「うっかり直接読み取りを足す」ことを止める補助であって、
敵対的なすり抜けを防ぐ境界ではない。** 下の 2 群は性質が違うので分けて
書く —— 片方は原理的に閉じないが、**もう片方は静的には読めるのに現実装が
見ていないだけ** である (t021 で Director 判断により backlog)。
どちらも「検出器が見ていないから安全」ではない。

PR の diff に次のどれかが現れたら、**queue / registry / config のファイルを
読んでいないか**を明示的に確かめる:

### (A) 検出はするが、許可表が通してしまう

1. **外部プロセスへの argv —— リテラルでも通る**。
   `ALLOWED_SUBPROCESS_CALLS` の鍵は `(script, 関数, program)` だけで、
   **引数を縛っていない**。だから
   `subprocess.run(["bash", "-c", "cat queue/state.yaml"])` は program を
   `bash` として拾われていても、`cmd_review` の既存の `bash` 許可に一致して
   素通りする。**「動的に組んだ argv だけが危ない」のではない** ——
   完全にリテラルな shell 読み取りも同じように通る。argv が変数
   (`subprocess.run(cmd)`) の場合も、表は `cmd` という式のまま 1 行を持つので
   中身が `cat queue/...` に変わっても当たり続ける。
   → **argv の変更は、それ自体をレビュー対象にすること**

### (B) 静的には読めるのに、現実装が見ていない (backlog)

いずれも「意図的に分かりにくく書いたコード」を要求する形だが、**diff に
現れたら読むこと**:

2. **別名 import した subprocess 関数** ——
   `from subprocess import check_output as capture; capture([...])`
3. **変数に取り置いた subprocess 関数** ——
   `run = subprocess.check_output; run([...])`
4. **定数 `getattr` の subprocess 版** ——
   `getattr(subprocess, "check_output")([...])`
5. **再束縛された `O_*` 定数** —— `O_WRONLY = 0` としてから
   `os.open(path, O_WRONLY)`。**この 1 件だけは倒れる方向が危険** ——
   他は「見えない」だけだが、これは読み取りを書き込みだと誤判定して
   積極的に検出から外す
6. **裸の名前を変数に取り置く形** —— `reader = open; reader(path).read()`
7. **import した `os.read`** —— `from os import read as read_fd; read_fd(fd, 100)`

### (C) 原理的に閉じないもの

8. **`exec()` / `eval()` / `ctypes` / C 拡張経由のファイルアクセス**
9. **完全に動的な属性名** —— `getattr(p, verb)()` の `verb` が変数
10. **`AUDITED_MODULES` に無いファイルからの読み取り** —— `hooks/` 配下、
   新しく足した `scripts/*.py`。新しいモジュールを足したら
   `AUDITED_MODULES` にも足すこと
11. **`.sh` の 2 つ目以降の `<<'PYEOF'` ブロック** —— `_python_source()` は
   1 つ目しか読まない

いずれも「直接 `open()` していないから安全」ではない。書き手のいない FIFO を
1 枚置かれるだけで、キューロックを握ったままのプロセスが無期限に止まる。
読む必要があるなら python 側で `lib_task_cards` のガードを通すこと。

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
