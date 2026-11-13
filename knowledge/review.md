# review ナレッジベース

> このファイルは review を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## レビュープロトコル（2026-11-13 改訂 — merge を含まない）

**読む前に head を固定する。** `gh pr diff` は常に「今の」head を見せるので、先に head を控えてから読み、
承認の直前にもう一度確かめる。これをしないと、**A を読んで B を承認する**ことが起こりうる（#383 codex-review P1-2）。

```bash
PR={PR番号}
HEAD_BEFORE="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"   # ① 読む前に固定
gh pr view "$PR" --json title,body,files                             # ② 概要
gh pr diff "$PR"                                                     # ③ この head の diff

# ── ここでレビューする ──

HEAD_AFTER="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"    # ④ 承認の直前に確かめる
[ "$HEAD_AFTER" = "$HEAD_BEFORE" ] || echo "レビュー中に push された。承認しない"
```

1. ① で head を控える。**承認に書く sha はこの値**（④ で一致を確かめた後）
2. ③ でコード変更を精査する
3. **CI の完走を待たない。** CI を見るのは dispatcher で、結果は Director に通知される
   （`ci-failed` / `ci-green` / `merge-ready`）。diff は CI と並行して読んでよい
4. 所見を Result に書いて `plan.sh done` で完了する。**merge は試みない**
5. ④ が一致し、かつ承認するときだけ、Result の**最後の非空行**に
   `approved_head: <40 桁の小文字 sha>`（= `HEAD_BEFORE`）を 1 行置く

- **④ が食い違ったら承認しない。** 読んでいない head を承認したことになる。所見は残して done し、
  「レビュー中に push があったので再レビューが要る」と Director に伝える（新しい review task を切ってもらう）
- diff を commit に固定できる環境（worktree を持つ reviewer）なら、③ の代わりに
  `git fetch origin "+refs/pull/${PR}/head:refs/crewvia/pr-${PR}"` で取ってから
  `git diff "$(git merge-base "origin/$BASE_REF" "$HEAD_BEFORE")" "$HEAD_BEFORE"` を読む方が確実
  （`BASE_REF` は `gh pr view "$PR" --json baseRefName -q .baseRefName`。base を決め打たない）
- 要修正・条件付き・判断を Director に委ねるときは**書かない**。書かなければ `no_approval` として
  扱われ、Director には `merge-ready` ではなく `ci-green` が届く（Director が所見を読んで判断する）
- 行は末尾に置く。本文の途中に書いた sha は読まれない（`lib_ci_status.approved_head` は最終行の
  `fullmatch` だけを見る。推測しない作り）
- 承認した head の後に修正が push されると `approved_head_mismatch` になり、**未レビューの head が
  merge されることはない**。修正があれば再レビューの task を切る
- merge できないことは失敗ではない。review Worker の責務は「レビューを最後までやり切ること」であり、
  merge の実行は Director の責務になった（`agents/director.md` §12「merge の手順」）

設計: `knowledge/ci-wait-operations.md` §2.1（待ちの持ち主）・§2.5（承認 head）。

## 承認・merge の経路

**この手順書はかつて `gh-app-review approve` による Bot 承認を前提としていたが、現状と
合わない**（2026-11-02 に Director と QA Worker Finn が独立に確認。2026-11-09 時点も未解消）:

- `~/bin/gh-app-review` ごと存在せず、`command -v gh-app-review` も空
- 前提の `~/.key/elni-net-reviewer.*.private-key.pem`（GitHub App の秘密鍵）も無い
- `gh` の認証アカウントは PR の author（`tyz-works`）と同一のため、素の
  `gh pr review --approve` は GitHub がセルフ承認として拒否する

**迂回しないこと**（同一アカウントでの強制承認・権限昇格など）。承認経路が無い状態は
意図的な安全弁であり、崩す対象ではない。

**2026-11-13 以降、crewvia 側の承認は GitHub の承認レコードではなく Result の `approved_head` で表す**
（上のプロトコル 5）。GitHub にセルフ承認を通す話ではない — crewvia の中で「誰がどの head を読んだか」を
機械が照合できるようにしただけで、GitHub 側の経路は今も無いまま。`merge-ready` が出るのは
CI の門が通り、かつ最後の review の `approved_head` が現 head と一致したときだけ（設計 §2.5）。

一方で `main` に branch protection は掛かっていない（`gh api repos/.../branches/main/protection`
が 404）ため、承認レコードが無くても GitHub 側は merge を許可する。止まるとすれば
**Claude Code の classifier** だけで、しかも**一貫しない**（2026-11-02 PR #338 は拒否、
2026-11-03 PR #339・2026-11-06 PR #345・2026-11-09 PR #351 は通過。#345 は merge 自体は
通ったが merge 後の remote branch 削除だけ別途拒否された）。「前回通ったから今回も通る」を
当てにせず、止まる前提で見積もり、通ったら儲けものとして扱うこと。

**この拒否の不安定さが、merge を Director に寄せた理由のひとつである**（2026-11-13 / 設計
`knowledge/ci-wait-operations.md` §2.1）。review Worker が拒否されると `needs-director` で
Director に渡るしかなく、しかも「CI が緑になる瞬間まで Worker が生きている」ことが前提だった。
Worker が CI を待たなくなった今、merge できる瞬間に Worker はいない。最初から Director が
merge すれば経路は 1 本で、拒否されても Director がその場で対応できる。

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

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
