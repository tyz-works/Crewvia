# herdr server 再起動後の空 pane と自動復帰

## 問題の説明

herdr server が落ちて再起動すると、workspace `crewvia` の
**タブレイアウト（label 付き pane）は復元されるが、中のプロセスは復元されない**。

その結果 `Sora-director` / `dispatcher` / `watchdog` という **正しい label を持つ、
中身が空の bash shell** だけが残る。これを本レポジトリでは「husk pane」と呼ぶ。

修正前は `HerdrBackend.spawn()` が「同名 label の pane が在る = 生きている」と決めつけて
何も起動せず False を返し、`scripts/start.sh` はそれを

```
[crewvia] Window Sora-director already exists, skipping launch
```

と解釈して `exit 0` していた。
→ **`./crewvia` がエラーも出さず正常終了するのに、エージェントが 1 つも起動しない。**

dispatcher / watchdog の二重起動ガードも `mux_list | grep -qx` という
**名前だけの判定**だったため、同じく husk pane を「稼働中」と誤認して黙ってスキップしていた。

---

## 現在の挙動（自動復帰）

`spawn()` は同名 pane を見つけると `herdr pane process-info` で
`foreground_processes` を確認し、次のように分岐する。

| pane の foreground_processes | 判定 | spawn の挙動 |
|---|---|---|
| 全て「引数なしのシェル」 | husk | **その pane で `pane run` し直して True** |
| 空リスト | husk | 同上 |
| 1 つでもそれ以外が居る | 稼働中 | 何もせず False（従来通りの no-op） |
| process-info が読めない / argv が無い | **稼働中とみなす** | False（fail-safe: 生きた agent に上書きしない） |

### 判定はプロセス名だけでは足りない

`dispatcher` tab は `bash scripts/dispatcher.sh` で動くため、
**稼働中でもプロセス名は `bash`** である。名前だけで判定すると
生きている dispatcher を husk と誤認して二重起動してしまう。

herdr 0.9.0 の `pane process-info` で実測した差は `argv` の長さ:

```
アイドル      → {"name": "bash", "argv": ["/bin/bash"]}
スクリプト実行 → {"name": "bash", "argv": ["bash", "…/dispatcher.sh"]}
```

そのため判定は **「名前がシェル」かつ「argv が 1 要素」** の両方を要求する
(`_is_idle_shell_process()`)。`argv` が報告されない場合は idle と証明できないため
稼働中扱いにする。

husk を再利用する際は tab を作り直さず **同じ pane に流し直す**ので、
タブの位置が変わらず `registry/mux/<name>.json` のキャッシュも更新される。

Director の pane が本当に稼働中だった場合、`start.sh` は黙って exit せず
**その Director に attach する**（ユーザーが `./crewvia` で辿り着きたかった先はそこなので）。

---

## 手動での診断

自動復帰が効かない・様子がおかしい時の確認手順。

```bash
# 1. server が生きているか
herdr status                        # → server: running / not running

# 2. label 付き pane が残っていないか
herdr pane list --workspace <ws_id>

# 3. その pane が husk か（素の bash prompt だけなら husk）
CREWVIA_MUX=herdr python3 scripts/lib_mux.py capture Sora-director

# 4. foreground process を直接見る
herdr pane process-info --pane <pane_id>
```

## 手動での復旧

自動復帰があるので通常は不要だが、tab ごと作り直したい場合:

```bash
for n in Sora-director dispatcher watchdog; do
  CREWVIA_MUX=herdr python3 scripts/lib_mux.py kill "$n"
done
./crewvia   # 普通のターミナルから
```

---

## 関連

- `scripts/lib_mux.py` — `_is_idle_shell_process()` / `HerdrBackend._pane_has_live_process()` / `spawn()`
- `scripts/start.sh` — Director spawn 失敗時の attach、dispatcher / watchdog ガード
- `tests/lib-mux.bats` — "stale husk panes (herdr server restart)" セクション
- `knowledge/dispatcher-restart-after-merge.md` — fix を merge しても稼働中 tab は旧コードのままな件
