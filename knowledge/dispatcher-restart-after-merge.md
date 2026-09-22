# dispatcher.sh fix 反映には稼働 tab の restart が必要

## 問題の説明

`scripts/dispatcher.sh`（または依存ファイル）の fix を PR で main に merge しても、
**稼働中の `dispatcher` tab は旧コードで動き続ける**。
dispatcher tab を kill → respawn しないと fix が反映されない。

**理由**: dispatcher tab は `bash scripts/dispatcher.sh` を起動時に一度読み込んで実行し続けるプロセス。
ファイルを再読み込みする仕組みはない。`git pull` で working tree の `dispatcher.sh` が更新されても、
既に起動している bash プロセスの embedded Python (`PYEOF` ブロック) はロードされない。

**過去の誤診断事例**: mission 20260905-dispatcher-idle-worker-fix で PR #154 merge 後、
dispatcher tab を restart せずに再検証すると **OBS-1 バグが再現し続ける**ように見えた。
「fix が効いていない」と誤診断するリスクがあった。

---

## 影響を受けるプロセス

| プロセス | 再起動が必要なケース |
|----------|-------------------|
| `dispatcher` tab | `scripts/dispatcher.sh` / `scripts/lib_mux.py` の変更 |
| `watchdog` tab | `scripts/watchdog.py` / `scripts/watchdog.sh` の変更 |
| Worker tab | `scripts/start.sh` の変更（スキル割り当て等） |
| `Sora-director` | `agents/director.md` / `hooks/*.sh` の変更（プロンプト・hook 反映） |

---

## restart 手順

> **⚠️ 素の `lib_mux.py kill` → `spawn` は使わないこと (t005 以降)。**
> dispatcher と watchdog は互いの生存を見張るようになった
> (`knowledge/daemon-authority.md` §7)。kill と spawn の**隙間でデーモンは本当に
> 死んでいる**ので、そこを見た相手が正しく respawn し、その直後に手で打った spawn が
> 上に乗って**二重起動**になる。dispatcher が 2 つになると同じ task が 2 人の Worker に
> 渡るので、これは restart で起こしうる一番重い事故である。
>
> 停止マーカーを挟む `restart` サブコマンドを使えば、この隙間は開かない。

### dispatcher の restart 例

```bash
# 1. main に fix を pull (worktree 外の crewvia repo root で実行)
cd /path/to/crewvia
git fetch origin main && git pull --ff-only origin main

# 2. restart (pause → kill → spawn → resume を 1 コマンドで)
python3 scripts/lib_daemon_watch.py restart dispatcher

# 3. 起動確認 (ログの最終行が "Starting dispatcher (PID ...)" になっていること)
tail -3 logs/dispatcher/dispatcher-$(date +%Y%m%d).log

# 4. 相互監視から見た状態確認
#    双方が running=[<pid>] で、PAUSED が付いていないこと。
#    running は heartbeat ではなく /proc の実走査なので、heartbeat がまだ
#    書かれていない起動直後でも正しく答える。
python3 scripts/lib_daemon_watch.py status
```

### watchdog の restart 例

```bash
python3 scripts/lib_daemon_watch.py restart watchdog
```

### 両方を同時に restart する場合

`CREWVIA_KILL_AUTHORITY` の切り替えなど、**両デーモンを同時に入れ替える**必要が
あるとき (`daemon-authority.md` §5-3) は、**先に両方 pause してから** kill する。
片方だけ pause して kill すると、生きている側が死んだ側を起こしてしまう。

```bash
TOK_D=$(python3 scripts/lib_daemon_watch.py pause dispatcher --reason "同時 restart")
TOK_W=$(python3 scripts/lib_daemon_watch.py pause watchdog   --reason "同時 restart")

python3 scripts/lib_mux.py kill dispatcher
python3 scripts/lib_mux.py kill watchdog

python3 scripts/lib_mux.py spawn dispatcher \
  "$(python3 scripts/lib_daemon_watch.py spawn-cmd dispatcher)" "$PWD"
python3 scripts/lib_mux.py spawn watchdog \
  "$(python3 scripts/lib_daemon_watch.py spawn-cmd watchdog)" "$PWD"

python3 scripts/lib_daemon_watch.py resume dispatcher --token "$TOK_D"
python3 scripts/lib_daemon_watch.py resume watchdog   --token "$TOK_W"
```

起動コマンドを手で書かず `spawn-cmd` から取るのは、`Mux.spawn()` の `env=` 引数が
**両 backend とも無視される**ため、`CREWVIA_MUX` をコマンド文字列に埋め込む必要が
あるからである。手書きすると、起こし直したデーモンだけ別の backend を向く。

> pause したまま resume を忘れると、そのデーモンは相互監視の対象外になる
> (相手が死んでも誰も起こさない)。30 分でその旨が Director に 1 度通知されるが、
> `status` に `PAUSED` が出ていないことを確認しておくとよい。

---

## 教訓

> **「fix が効いていない」と誤診断する前に restart を確認すること。**

dispatcher / watchdog の挙動がおかしいと感じたとき、まず以下を確認する:

1. 直近で関連する PR を merge したか？
2. merge 後に該当 tab を restart したか？
3. restart していなければ restart してから再検証する

restart は **fix 効果検証の前提** であるため、mission 完了後のクリーンアップ手順にも組み込むこと。

---

## 関連

- MEMORY: `dispatcher-restart-after-merge` — この運用上の落とし穴の背景
- `scripts/lib_mux.py` — kill / spawn の実装
- `scripts/lib_daemon_watch.py` — `restart` / `pause` / `resume` / `status` / `spawn-cmd`
- `knowledge/daemon-authority.md` §7 — 相互監視の設計 (なぜ pause を挟むのか)
- `knowledge/ops.md` — その他の運用手順
