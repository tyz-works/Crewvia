# code ナレッジベース

> このファイルは code（コーディング全般）を担当した Worker が自動更新する。
> 起動時にシステムプロンプトへ注入され、次の Worker に引き継がれる。

## ノウハウ

<!-- Worker が発見したノウハウをここに追記 -->

## 注意事項

<!-- 失敗パターン・ハマりやすい落とし穴 -->

## よく使うパターン

<!-- 再利用できるコード・コマンド・手順 -->

## 2026-09-08 「呼び出しのために絶対パスを打つ習慣」自体が事故の温床になりうる (plan wrapper, t002)

Worker が `$CREWVIA_REPO_ROOT/scripts/plan.sh ...` を毎回タイプする運用は、タスク管理コマンドの
「呼び出し」としては正しいが、その打鍵の習慣が git 操作にまで無意識に持ち込まれ
`cd $CREWVIA_REPO_ROOT && git checkout -b ...`(main checkout への直接操作)に至った事故があった。
対策として `scripts/bin/plan` という薄いラッパー(`exec "$CREWVIA_REPO_ROOT/scripts/plan.sh" "$@"`)
を作り、`scripts/start.sh` で PATH に追加することで、そもそも絶対パスを書く理由を無くした。
ポイント: mux (tmux/herdr) が spawn する新しいペインは起動元プロセスの env/PATH を継承しない
(`spawn()` は env を運べない。`env=` 引数は両 backend が黙って捨てていたので t017 で廃止した —
[[lib-mux-spawn-env-arg-ignored]] / `knowledge/daemon-authority.md` §7-17 参照)。
そのため PATH 拡張は (1) 現在プロセスの `export PATH=...`(inline モード用)と (2) mux が実行する
LAUNCH_CMD 文字列内に埋め込む `export PATH=...`(mux モード用)の **両方** が必要。片方だけだと
モードによって効いたり効かなかったりする。

## 2026-09-27 `plan` ラッパーは常に main checkout の plan.sh を呼ぶ — 自分の修正の動作確認には使えない (t059)

`scripts/bin/plan`(PATH 経由の `plan` コマンド)は `exec "$CREWVIA_REPO_ROOT/scripts/plan.sh" "$@"`
なので、worktree 内で `plan lint ...` を打っても **main checkout の (未修正の) plan.sh / lint_plan.py**
が動く。`plan.sh` 自身や `lint_plan.py` を改修する task で「動いた」と確認したつもりが、実は自分の
変更を一度も通していなかった、という事故になりうる。自分の修正を検証するときは worktree 内の相対パス
`./scripts/plan.sh ...`(`REPO_ROOT` はスクリプト自身の `BASH_SOURCE` から解決されるので worktree 側になる)
を明示的に呼ぶこと。`QUEUE_DIR` は `$CREWVIA_QUEUE`(main checkout の実 queue)にフォールバックするので、
`lint` のような読み取り専用 subcommand なら本番データに対して安全に動作確認できる(書き込み系の
subcommand は絶対に本番 queue に向けて実行しない)。worker.md §「作業スコープの制約」に既出の規約の
再確認 — 呼び出しと編集だけでなく、**動作確認の呼び出し先**も worktree 内を明示する必要がある。

## 2026-09-27 config を読む簡易 YAML 手書きパーサは 3 回同じ族の欠陥を作った (t013→t055→t059)

`config/skill-permissions.yaml` の `can_produce_deliverable` 欄を読む `lint_plan.py`
`_load_deliverable_capabilities()` は、行ベースの正規表現パーサを 2 回直しても
(t055: 値の空白入り値/リスト値、t059: コメント付きヘッダで宣言が隠れる/前の skill に誤って付く)
別の入口から同じ族の欠陥が出た。3 回目で本物の YAML パーサ (PyYAML、`yaml.safe_load` 相当) に
切り替えて構造ごと直した — `hooks/lib_skill_perms.py` が同じファイルを既に PyYAML で読んでいた
(fallback 付き) のに、`lint_plan.py` 側は独自の簡易パーサを別に書いていたのが遠因。
**同じ config ファイルを読む場所が複数あるとき、それぞれが独自の簡易パーサを持たないか確認する。**
真偽値の厳密さ (`yes`/`no`/`True`/`FALSE` 等を暗黙に true/false へ丸め込む YAML 1.1 の既定 resolver)
を維持したまま本物のパーサに乗り換えたい場合は、`yaml.SafeLoader` を継承し
`yaml_implicit_resolvers` から `tag:yaml.org,2002:bool` を外して狭い正規表現で足し直す方法がある
(`yaml.load(text, Loader=CustomLoader)` は Loader が SafeLoader のサブクラスで済む限り
`yaml.safe_load` と同じ安全性 — コンストラクタを足さない限り任意型構築はできない)。
PyYAML が無い環境向けの簡易フォールバックは意図的に**書かなかった** — 簡易パーサを書くたびに
コメント・引用符・フロースタイルのどれかを見落としてきたため、この欄では
「PyYAML が無ければ読めない (Unreadable) として拒否する」方を選んだ (CI には既定で入っている)。

## 2026-09-13 PortAudio/sounddevice: `check_output_settings` はストリームが実際に開く形式を保証しない (Minerva t027)

`sounddevice.check_output_settings(samplerate=..., channels=...)` は「その形式が受理可能か」を
聞くだけで、`OutputStream(...)` が実際にその samplerate/channels で開く保証にはならない
(特に WASAPI 共有モードは host API が黙って別形式に丸める余地がある)。事前チェックを通した形式で
サンプルを準備して書き込むコードでは、これが起きると音程・速度がずれた音声を「エラー無く」再生
してしまう。対策: `OutputStream` を実際に開いた後、その `.samplerate` / `.channels` (`.dtype` /
`.blocksize` もログ用に) を読み戻し、要求値と突き合わせる。不一致なら例外にして黙って壊れた形式で
再生しない。実機での検証ポイント: 元の音を生成したサンプルレート (この場合 VOICEVOX 24kHz) → 再生
デバイスのレート/チャンネル数への変換 (`resample_poly` 等) → 実際に開いたストリームのパラメータ、の
3点を全部ログに出すと、どの段で不一致が起きているか切り分けやすい。

## 2026-09-13 訂正: sounddevice の `Stream.channels` は読み戻し値ではない (Minerva t028 review, t029)

上の 09-13 のエントリ (`check_output_settings` の話) のうち「`.channels` を読み戻し」の部分は不正確
だった。t028 のレビューで判明: sounddevice 0.5.6 の `Stream.channels` は、開いた時に要求した
`parameters.channelCount` をそのまま返すだけで、PortAudio から読み戻した値ではない
(`sounddevice.py` の `OutputStream.__init__` 相当箇所で代入、プロパティで返すだけ)。実際に
PortAudio から読み戻しているのは `samplerate` だけ (`Pa_GetStreamInfo(...).sampleRate`)。そのため
「開いたストリームの channels を検証している」という書き方はできない — host API が黙って別の
チャンネル数で開いても、この比較では検出できない。samplerate の読み戻し・比較は引き続き有効。
コード自体 (channels の比較・ログ出力) は害が無いので残してよいが、コメント・ドキュメントでは
「検証している」ではなく「要求値を記録しているだけ」と書くこと (Minerva: `src/minerva/voicegate/audio/playback.py`)。

## 2026-09-13 実機での音声再生確認は「小さく・短く・記録して」行う (Minerva t027)

ヘッドセット等をユーザーが装着している可能性がある状態で実機の音声出力を切り分けるとき、
テスト用の再生音 (合成トーン等) の音量・回数に配慮が要る。実際に、振幅約 0.24 (フルスケール比) ×
1 秒のテスト音を複数回・複数デバイスに鳴らして「何か鳴った？ノイズが聞こえた」とユーザーに
気づかれた事故があった。次から: (1) 音量は 0.2 以下、1 回あたり数秒以内にする (2) 再生前後に
「何を・どのデバイスに・何秒鳴らしたか」を Result に書く (3) 同じ再生を繰り返さず、必要な測定だけ
に絞る。切り分けに再生が必要なこと自体は問題ない — 配慮なく繰り返すのが問題。
