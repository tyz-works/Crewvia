# review ナレッジベース

> このファイルは review を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## レビュープロトコル

**読む前に head を控える。** `gh pr diff` は常に「今の」head を見せるので、読み終えてから
`headRefOid` を取ると **A を読んで B を承認する**ことが起こりうる（PR #383 codex-review P1）。

```bash
PR={PR番号}
HEAD_BEFORE="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"   # ① 読む前に控える
gh pr view "$PR" --json title,body,files                             # ② 概要
gh pr diff "$PR"                                                     # ③ この head の diff

# ── ここでレビューする ──

HEAD_AFTER="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"    # ④ 承認の直前に確かめる
[ "$HEAD_AFTER" = "$HEAD_BEFORE" ] || echo "レビュー中に push された。承認しない"
```

1. ① で head を控える。**承認に書く sha はこの値**（`HEAD_BEFORE`）
2. ③ でコード変更を精査する（CI の完了は待たない。CI は dispatcher が見る）
3. 所見を Result に残して `plan.sh done`。**merge しない**（merge は Director。2026-11-13 の
   「Worker は CI を待たない・merge は Director」への変更。設計: `knowledge/ci-wait-operations.md` §2）
4. ④ が一致し、かつ**承認するときだけ** Result の**最後の行**に
   `approved_head: <HEAD_BEFORE の 40 桁の sha>` を 1 行書く。要修正・条件付きのときは書かない。
   dispatcher の `merge-ready` 通知と `plan.sh ci-gate` はこの head が PR の現 head と一致するときだけ承認とみなす。
   **④ が食い違ったら承認しない** — 読んでいない head を承認したことになる。所見は残して done し、
   「レビュー中に push があったので再レビューが要る」と Director に伝える
   （diff を commit に固定できるなら、③ の代わりに
   `git fetch origin "+refs/pull/${PR}/head:refs/crewvia/pr-${PR}"` で取って
   `git diff "$(git merge-base "origin/$BASE_REF" "$HEAD_BEFORE")" "$HEAD_BEFORE"` を読む方が確実。
   `BASE_REF` は `gh pr view "$PR" --json baseRefName -q .baseRefName`。base を決め打たない）
5. **`approved_head:` 行は Result の最後の非空行にする。** 後ろに所見・ログを足さない。
   行頭 `## ` の見出しは `plan.sh done` が保存時に `### ` へ自動で下げる（#382 の
   `demote_section_headings`）ので、本文に見出しを書いても Result 節は途中で切れない。
   自動で下がる前は `lib_history.result_section` が最初の行頭 `## ` で節を切っていて、
   `approved_head:` 行が節の外に出て `no_approval` になっていた
   （実例: mission 20261113-ci-wait-ops-3 t002 の再レビュー）

> 旧手順（`gh pr merge --squash --delete-branch` を review Worker が試み、拒否されたら `needs-director`）は廃止。
> 「CI が緑になるまで Worker が待つ」形をやめたので、merge できる瞬間に Worker がいない。
> 稼働中の古いプロンプトの Worker は旧手順のまま完走する（拒否されたら `needs-director` に行くだけ）。

## 承認・merge の経路

> **2026-11-13 以降は merge するのは Director**（下は経緯と、classifier が一貫しない事実の記録。review Worker は merge しない）。

**この手順書はかつて `gh-app-review approve` による Bot 承認を前提としていたが、現状と
合わない**（2026-11-02 に Director と QA Worker Finn が独立に確認。2026-11-09 時点も未解消）:

- `~/bin/gh-app-review` ごと存在せず、`command -v gh-app-review` も空
- 前提の `~/.key/elni-net-reviewer.*.private-key.pem`（GitHub App の秘密鍵）も無い
- `gh` の認証アカウントは PR の author（`tyz-works`）と同一のため、素の
  `gh pr review --approve` は GitHub がセルフ承認として拒否する

**迂回しないこと**（同一アカウントでの強制承認・権限昇格など）。承認経路が無い状態は
意図的な安全弁であり、崩す対象ではない。

一方で `main` に branch protection は掛かっていない（`gh api repos/.../branches/main/protection`
が 404）ため、承認レコードが無くても GitHub 側は merge を許可する。止まるとすれば
**Claude Code の classifier** だけで、しかも**一貫しない**（2026-11-02 PR #338 は拒否、
2026-11-03 PR #339・2026-11-06 PR #345・2026-11-09 PR #351 は通過。#345 は merge 自体は
通ったが merge 後の remote branch 削除だけ別途拒否された）。「前回通ったから今回も通る」を
当てにせず、止まる前提で見積もり、通ったら儲けものとして扱うこと。

`--delete-branch` は local branch が他の worktree で checkout 中だと失敗し、gh はそこで
中断して remote branch を削除せずに終わる。merge 自体は成功しているので、その場合は
remote branch だけ `gh api -X DELETE repos/.../git/refs/heads/<branch>` で後片付けする
（local branch と他 task の worktree には触れない）。

**gh-app-review を将来再導入する場合に要るもの（現在は採っていない選択肢）**: GitHub App
の登録とインストール、秘密鍵（`~/.key/elni-net-reviewer.*.private-key.pem` 相当）の配置、
`~/bin/gh-app-review` 本体。2026-11-09 時点ではユーザー判断により、承認・merge の可否は
Director / ユーザーの判断に委ねる前提を採っている。

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

- **kai-review.sh 自身の `approved_head:` 実装 (t001, mission 20261113-approval-gate-holes, PR #389)**:
  `HEAD_BEFORE` は review worktree を作った直後（`git worktree add` の後・`DIFF_CONTENT` を読む前）に
  `git rev-parse --verify "$FETCH_LOCAL_REF"` で控える（`gh pr view --json headRefOid` ではなく、既に
  fetch 済みのローカル ref から取る —— 追加の gh 呼び出しを避けつつ、fetch した実体と確実に一致する）。
  再確認は Result 全文を組み立て終えた直後・`plan.sh done` を呼ぶ `if/elif/else` 分岐の直前
  （LGTM = `NEEDS_FIX -eq 0` のときだけ）で `gh pr view --json headRefOid` を呼び直す。一致すれば
  `approved_head: <sha>` を、食い違う・確認できない（gh 失敗・形が 40 桁小文字 sha でない）場合は
  再レビューが要る旨の日本語 1 文を、どちらも `>> "$RESULT_FILE"` で**末尾に追記**する
  （approved_head: 行が Result の最後の非空行、という不変条件を保つため、構築済みの全文の後に足す）。

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
