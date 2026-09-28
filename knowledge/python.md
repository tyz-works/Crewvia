# python ナレッジベース

> このファイルは python（Python 開発）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

## 2026-05-03 Claude Code JSONL セッションログの構造

`.claude/projects/<slug>/*.jsonl` の各行は独立した JSON オブジェクト。重要な型:

- `type=assistant`: AI ターン。`message.usage` に token 情報 (input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens)
- `type=system, subtype=compact_boundary`: コンパクションイベント。`compactMetadata.durationMs` / `preTokens` / `postTokens` を持つ
- `type=user`: ユーザーターン（メッセージ境界の検出に使える）
- `timestamp` フィールドは ISO 8601 (Z suffix)、`datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()` でパース可能

`usage.input_tokens` は実際には非常に小さい（3〜数十）こともある — キャッシュヒット時は cache_read_input_tokens がほぼ全て。

## 2026-05-03 Python -c で sys.exit() しても複数 print の出力が残る問題

`python3 -c "... ; print(fname); sys.exit(0)"` の結果を bash variable に収めようとすると、
sys.exit() が効いても前の print 出力が改行なしで連結されることがある。
`sys.stdout.write(fname)` + `> /tmp/file.txt` 経由でも同じ現象が起きた。
安全策: ファイル名を直接ハードコードするか `head -1` で最初の行だけ取り出す:
```bash
COMPACT_FILE=$(python3 -c "..." | head -1)
```

## 2026-09-27 `git status --porcelain` は既定で ignored ファイルを数えない

`git status --porcelain=v1 --untracked-files=all` は ignored なファイルを一切含まない。
`.env` 等を `.gitignore` している repo で「クリーンかどうか」を破壊操作の判定材料にするなら、
`--ignored=matching` も足して `!! ` prefix の行を別扱いすること（`worktree_gc.py` t057 / PR#239 F1）。
`git worktree remove` は `--force` 無しでも ignored ファイルの削除を許すので、この見落としは実際に
secrets を消す方向に倒れる。`--ignored=matching` はディレクトリ全体が ignore パターンにマッチする場合、
中身を 1 つずつ列挙せず親ディレクトリ 1 行にまとめる（`--untracked-files=all` の未追跡ディレクトリ展開とは
挙動が違う）。

## 2026-09-27 lsof は一部のプロセスで失敗しても部分出力を出しつつ非 0 を返す

`subprocess.run(['lsof', ...])` の `stdout` が空でないことだけを「完全なスキャンができた」根拠にすると、
一部のプロセスの検査に失敗した部分出力を「見つからなかった (= 使われていない)」と誤読しうる
(`worktree_gc.py` t057 / PR#239 F3)。`returncode != 0` を先にチェックして拒否すること。

## 2026-09-28 Ren発見: `/proc/<pid>/exe` は comm/cmdline と違い偽装できない構造的な同定材料になる

`comm` (15文字打ち切り・`process.title` で書き換え可能) や `cmdline` (`exec` で消える・
`process.title` で書き換わる) は代理指標にしかならない (t065/t074/t091 で繰り返し破れた)。
`os.readlink(f"/proc/{pid}/exe")` は execve 時点の実体 inode への絶対パスをカーネルが
解決したものなので、プロセス自身がどう argv/環境を偽装しても変わらない — 「起動元」
(祖先関係) と組み合わせると、名前の部分一致に頼らない同定ができる
(`scripts/lib_pane_process.py` t097: pane root の直接の子 + exe が
`/share/claude/versions/` を含む、で Claude Code のセッション本体だけを同定した)。
symlink 経由で実行するとこのパスは symlink の**先**(実体ファイル)を指す —
fixture で「本番と同じパスに実体が要る」場合は `shutil.copy2` で実体ファイルを置く
(symlink だとさらに先の実体を指してしまい一致しない)。

## 2026-09-28 Ren発見: `Path.read_text()` は既定で strict decode — `/proc/<pid>/stat` のようなカーネル由来のバイト列には `errors="replace"` か `read_bytes()` を使う

Linux のプロセス名 (comm) は NUL 以外の制約が無い任意バイト列 (`exec -a` や、
ファイル名自体に不正な UTF-8 バイトを含む実行ファイルを exec するだけで作れる)。
`Path(...).read_text()` は既定で UTF-8 strict decode するため、**無関係な1
プロセスの名前が壊れているだけで**、その `/proc` エントリを読んだ瞬間に
`UnicodeDecodeError` を投げる。これは `ValueError` の派生であって `OSError`
ではないので、`except OSError` だけの呼び出し元では拾えない
(`scripts/lib_pane_process.py` t097→t101 の回帰: `/proc` を丸ごと走査する
関数がこれで丸ごと落ち、無関係なペインの watchdog 判定まで巻き添えにした)。

対策は 2 通り、影響範囲で選ぶ:
- 生の値 (comm 等) をそのまま使う/表示するだけなら `read_text(errors="replace")`
  で十分 (不正バイトが U+FFFD に化けるだけで、後続のパース対象 — ppid や
  state のような ASCII 数字フィールド — には影響しない)
- 既に bytes で読んでいる箇所がある関数なら `read_bytes().decode("utf-8",
  errors="replace")` に揃える (このコードベースでは `_proc_cmdline` /
  `_proc_environ` が先行してこのパターンを使っていた — 一つの関数だけ
  `read_text()` のまま取り残されると同じ族の欠陥が再発する)

赤の実証: `shutil.copy2` で `/bin/sleep` を `os.fsdecode(b"\xffbad")` という
ファイル名にコピーして exec すると、そのプロセスの comm (ファイル名から取られる
— `exec -a` の argv[0] 書き換えは comm には効かない) が壊れた名前になり、
`/proc/<pid>/stat` の `read_text()` が確実に `UnicodeDecodeError` を再現する。

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## 2026-09-28 Ren発見: red proof のテストが「別のガード」で偶然緑になり、狙った欠陥を見逃す

`tests/leaked_descendants._open_pidfd_verified(pid, expected_start)` の
`signal.pidfd_send_signal` 不在ゲート漏れ (P2-2) を確かめるテストで、`expected_start=0`
(ダミー値) を渡していた。ゲートを外す欠陥を注入して `red_proof_t047.sh` を走らせたら、
このテストだけ赤にならなかった —— `expected_start=0` は実際の starttime とまず一致しないので、
関数内の**別の**再確認 (`recheck[2] != expected_start` → pid 再利用の疑いとして `None` を返す)
が、確かめたかった signal 側のゲートより**先に**同じ `None` を返し、欠陥があってもテストが
偶然パスしてしまった。

教訓: 複数の独立したガードが同じ戻り値 (`None` / `False` 等) に収束する関数を単体テストする
ときは、**そのガード以外の分岐が絶対に発火しない入力**を選ぶこと。ダミー値・境界値を安易に
使うと、狙ったガードを経由せずに「たまたま」正しい戻り値になり、そのガードを外しても
テストが赤くならない (memory: `red-proof-defeated-by-backstops` と同族 — バックストップが
別ファイルではなく**同じ関数内の別の if 分岐**でも起こる)。防ぎ方: 欠陥を注入して
`red_proof_*.sh` を実際に走らせ、狙ったテストが赤くなることを確認する。「書いた・通った」
だけで済ませない。

## 2026-09-28 Ren発見: fake 関数の引数の型 (str/bytes) を実装の変更に追従させないと mock が静かに無効化する

`tests/leaked_descendants.py` の `_belongs` の cwd 判定を `os.readlink(str_path).encode()` から
`os.readlink(os.fsencode(path))` (bytes 直渡し) に変えたところ、既存テスト
`test_a_readable_environ_and_cmdline_do_not_mask_a_failed_cwd_read` が壊れた。このテストは
`monkeypatch.setattr(os, "readlink", _flaky_readlink)` で `os.readlink` を差し替え、
`_flaky_readlink(path)` の中で `str(path).endswith("/cwd")` を見て意図的に `OSError` を出す
作りだった。呼び出し側を bytes に変えたら、`path` は bytes オブジェクトになり
`str(path)` は `"b'/proc/1234/cwd'"` のような repr になって `.endswith("/cwd")` が常に
`False` になった —— fake は「一致しない」側の分岐 (`return "/"`、文字列) に落ち、意図した
`OSError` 注入が起きないまま無条件に成功するようになった。実際には `TypeError` (str と
bytes の比較) で気づけたが、fake の分岐が単に無効化されるだけだと**エラーにすらならず
黙ってテストの意図が消える**こともありうる。

教訓: 呼び出し側の引数の型 (str か bytes か、Path か文字列か) を変えたら、その関数を
monkeypatch している fake の**分岐条件の型も必ず一緒に見直す**こと。fake は「呼ばれれば
動く」のではなく「呼び出し元が渡す形と一致して初めて意図通りに分岐する」。型を変える
リファクタでは `grep` で monkeypatch している箇所を全部洗い出し、fake 側も実引数の型に
合わせて更新すること。

## 2026-09-28 Ren発見: exec 直後の一瞬を検証するテストは `_wait_observable` で待たないと環境依存で揺れる

`tests/leaked_descendants.py` の `_belongs` は `environ` が exec 直後の一瞬 (実測 2.7%、
1ms 未満で解消) 空で読めることをドキュメント化しており、判定そのものを見るテストは
「観測できる状態になってから走査する」設計になっている (既存テストは `_wait_observable(pid)`
を使う)。新しく書いたテスト (`test_belongs_survives_a_non_utf8_cwd`) がこの待ちを省いて
`Popen` 直後に `_belongs` を呼んだところ、ホスト環境では毎回パスしたが、`red_proof_t047.sh`
の PID 名前空間 (`unshare --user --pid --fork --mount-proc`) の中 (プロセス起動が重く遅い)
では `observed=False` になって赤くなった —— 製品コードのバグではなく、テスト自身がこの
既知のレースを踏んだだけだった。

教訓: `/proc/<pid>/...` を Popen 直後に読むテストは、コメントに `_wait_observable` の
既存パターンがあれば必ず使う。ホストで緑でも、より遅い/重い実行環境 (PID 名前空間・CI 等)
では顕在化しうる。

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->
