# Bridge log 輪替（issue #7 項目 6）

## 問題

launchd 把 bridge 的 stdout/stderr 導到兩個檔案，**從來不輪替**：

```
StandardOutPath  → /Users/xcash/apps/hermes-openwebui-bridge/bridge.out.log
StandardErrorPath → /Users/xcash/apps/hermes-openwebui-bridge/bridge.err.log
```

稽核當天 `bridge.out.log` 41MB，2026-07-26 已經 43MB。bridge 是 launchd 常駐、
每筆 `_log_event` 都寫一行，而這張 issue 的項目 1 又刻意**增加**了留痕量，
所以「log 塞爆磁碟」是 Pocket 商用化會自己壞掉的風險之一。

## 結論：用 in-process 輪替，不用 newsyslog

兩條路都評估過，**選 in-process**（已實作在 `bridge.py` 的
`_rotate_log_file` / `_housekeeping_loop`）。

### 為什麼不用 newsyslog

macOS 內建的 newsyslog 在這個拓撲下**會靜默停掉所有 log**：

1. launchd 在 spawn 行程時就把那兩個檔開好，fd 直接交給子行程，
   整個生命週期**不會重開**。
2. newsyslog 預設的輪替動作是 `rename`。rename 之後，launchd 交下來的 fd
   仍然綁在**被改名的那個 inode** 上——bridge 會繼續往
   `bridge.out.log.0` 裡寫，而新建的 `bridge.out.log` 永遠是 0 byte。
3. newsyslog **沒有 `copytruncate`**（那是 Linux logrotate 的功能）。
   要讓 rename 生效，得讓行程收訊號後自己重開 fd —— 那需要在 bridge 裡
   裝訊號處理器並改掉 launchd 的 stdout 導向，是比 in-process 更大的改動，
   而且失敗模式是「log 全沒了」，比「log 太大」更難察覺。

### 為什麼 in-process 的 copy-then-truncate 是安全的

launchd 用 `O_APPEND` 開這兩個檔（實測，不是推論）：

```console
$ lsof +fg -p $(pgrep -f "uvicorn bridge:app")
COMMAND   PID  USER  FD  TYPE  FILE-FLAG          ... NAME
Python  97884 xcash   1u  REG  R,W,AP,0x10000     ... bridge.out.log
Python  97884 xcash   2u  REG  R,W,AP,0x10000     ... bridge.err.log
                            ↑↑ AP = O_APPEND
```

`O_APPEND` 的語意是**每次 write 都先 seek 到當下的 EOF**。所以
`truncate(path, 0)` 之後，下一筆寫入落在 offset 0，不會像非 append 模式那樣
在 offset 43MB 處留一個 sparse 空洞（那會讓 `ls -l` 看起來完全沒縮小）。
`tests/test_robustness_pack.py::TestLogRotation` 用一個真的 `O_APPEND` fd
把這個性質測起來，防止以後有人改成 rename。

## 實作

`bridge.py`：

| 旋鈕 | 預設 | env |
|---|---|---|
| 單檔輪替門檻 | 32MB | `BRIDGE_LOG_MAX_BYTES` |
| 保留幾代舊檔 | 3（`.1` 最新） | `BRIDGE_LOG_KEEP` |
| 檢查間隔 | 900s | `BRIDGE_LOG_CHECK_SECS` |
| 額外要輪替的檔（`:` 分隔） | 無 | `BRIDGE_LOG_ROTATE_PATHS` |

硬上限 = `32MB × (1 + 3) ≈ 128MB` 每個檔，兩個檔共 ~256MB。

輪替由 `_housekeeping_loop()` 每 15 分鐘檢查一次（同一個 loop 也負責
項目 5 的孤兒 worktree 回收）。輪替時發 `log_rotated` 事件，失敗發
`log_rotate_failed`。

## 套用步驟

**不需要改 LaunchAgent plist**——這是選 in-process 的附帶好處。
plist 一個字都不用動，所以不需要善彰批准 plist 變更。

```bash
# 1. 合併分支（照慣例由 XCash/善彰 驗收後執行）
cd ~/apps/hermes-openwebui-bridge
git merge --no-ff feat/robustness-pack

# 2. 重啟服務讓新碼生效（唯一需要的部署動作）
launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge

# 3. 確認活著
curl -s localhost:8081/health
```

### 驗證輪替真的會動

門檻是 32MB，正常要等 log 長到那麼大。想立刻確認機制沒壞，把門檻調小重跑一次
即可（**不要**在 production 上調，用 /tmp 假環境）：

```bash
cd ~/apps/hermes-openwebui-bridge
BRIDGE_LOG_MAX_BYTES=1024 BRIDGE_LOG_CHECK_SECS=5 \
POCKET_CANON_DB=/tmp/smoke-canon.db \
BRIDGE_TOKEN=smoke \
~/apps/hermes-agent/runtime/venv/bin/python -m uvicorn bridge:app \
    --host 127.0.0.1 --port 8099
# 另一個 terminal：打幾下 /health 讓 log 長過 1KB，15s 內應該看到
#   [bridge-event] {"event": "log_rotated", ...}
# 並出現 bridge.out.log.1
```

### 現有的 43MB 怎麼辦

新碼上線後，`bridge.out.log` 已經超過 32MB 門檻，所以**第一次
housekeeping 巡邏（最多 15 分鐘內）就會把它輪替掉**，變成
`bridge.out.log.1` + 一個空的 `bridge.out.log`。不需要手動處理。

想立刻回收也可以（bridge 不用停，O_APPEND 保證安全）：

```bash
cd ~/apps/hermes-openwebui-bridge
cp bridge.out.log bridge.out.log.1 && : > bridge.out.log
```

## 備案：真的想上系統層輪替的話

不建議（見上面 newsyslog 的分析），但如果哪天要走系統層，正確做法**不是**
newsyslog，而是把 launchd 的 stdout 接到 `logger(1)` 送進統一日誌系統，
或改成 bridge 自己用 `logging.handlers.RotatingFileHandler` 寫到獨立檔案、
不再依賴 launchd 的 stdout 導向。後者要連 `print()` 一起換成 logger，
是獨立一張 issue 的規模，這次不做。
