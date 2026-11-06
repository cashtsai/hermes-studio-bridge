# T3Code / Pocket 操作手冊

這份手冊對應已合併版本：Bridge `35e9608`、Pocket app `759859b`。Bridge 的更新與 app 的發版是兩條獨立流程；不要因為 app 更新就重啟 bridge，也不要因為 bridge 更新就重裝手機 app。

## A. Bridge 更新與安全重啟

### 1. 先確認沒有未提交變更

在 Cashcamp 的整合工作區執行：

```bash
cd ~/apps/hermes-openwebui-bridge
git status --short
git fetch origin main
git pull --ff-only origin main
```

`git status --short` 有輸出時先停下來，不要用 `reset --hard` 覆蓋別人的工作。

### 2. 安裝/升級 bridge

```bash
cd ~/apps/hermes-openwebui-bridge
./deploy/install-local-bridge.sh
```

安裝器會保留既有 bridge token，寫入目前使用者的 LaunchAgent，預設 label 是 `com.pocketconnect.bridge`。若這台仍是舊版 Studio 安裝，label 可能是 `ai.studio.hermes-bridge`；不要兩個服務同時跑在 8081。

### 3. 確認服務與安全重啟

```bash
launchctl print gui/$(id -u)/com.pocketconnect.bridge
```

若該 label 不存在，再查：

```bash
launchctl print gui/$(id -u)/ai.studio.hermes-bridge
```

有活躍回合時使用安全重啟腳本，它會等待人格、CC、CX、cc2 都歸零：

```bash
cd ~/apps/hermes-openwebui-bridge
scripts/bridge-safe-restart.sh 600
```

如果要明確指定 label：

```bash
POCKET_BRIDGE_LABEL=com.pocketconnect.bridge scripts/bridge-safe-restart.sh 600
```

超過等待時間腳本會中止，不會硬殺正在跑的工作。只有 bridge 掛掉、health 完全讀不到時，才會進入直接重啟分支。

### 4. 部署後煙測

```bash
cd ~/apps/hermes-openwebui-bridge
scripts/post-deploy-smoke.py --json
```

煙測是唯讀，會驗 bridge health、能力宣告、CX 清單不變式與卡片流。token 會從目前 LaunchAgent 讀取；若使用自訂 plist，可指定：

```bash
POCKET_BRIDGE_PLIST="$HOME/Library/LaunchAgents/<label>.plist" \
  scripts/post-deploy-smoke.py --json
```

看到 `bridge 可達`、`capabilities 有回`、`turns_in_flight 有回` 通過後，才在手機確認一條 CX 或 CC session 的狀態、歷史與送出功能。

### 5. 出問題時

```bash
tail -n 160 "$HOME/Library/Application Support/PocketConnect/bridge/current/bridge.err.log"
tail -n 160 "$HOME/Library/Application Support/PocketConnect/bridge/current/bridge.out.log"
launchctl print gui/$(id -u)/com.pocketconnect.bridge
```

不要把 bridge token 貼進聊天或 log。若是新版本造成問題，先保留 log 與 commit，再用上一個已知可用 commit 的獨立 worktree 執行同一套 installer 做回退；不要在整合工作區 reset。

若 bridge 因啟動崩潰進入 crash-loop hold，先清除 hold 再重試：

```bash
POCKET_BRIDGE_LABEL=pocket-bridge scripts/bridge-reset-crash-loop.sh
```

Vultr 的 `lobster-update.sh` 會在切版前保留 canonical/accounts SQLite snapshot，
候選版探活失敗會自動切回舊 release。資料庫 snapshot 不會自動覆蓋目前資料；只有
確認要捨棄 snapshot 之後的新寫入時，才使用：

```bash
CONFIRM_RESTORE=1 scripts/restore-sqlite-snapshot.sh <snapshot-prefix>
```

## B. Pocket macOS release workflow

Pocket app 的 PR #259 已合併，但 macOS archive/notarization 需要 release host 的 Apple secrets。GitHub 上依序操作：

1. 開 `cashtsai/pocketagent` 的 **Actions**。
2. 選 **Pocket macOS release**。
3. 按 **Run workflow**，branch 選 `main`，填 `archive_name`。
4. workflow 依序執行 macOS tests、archive、codesign、Gatekeeper、notarytool、staple。
5. 成功後從 workflow artifacts 下載 `Pocket-macOS-<archive_name>`。

需要事先設定的 secrets：

```text
MACOS_SIGNING_IDENTITY
MACOS_TEAM_ID
MACOS_CERTIFICATE_P12_BASE64
MACOS_CERTIFICATE_PASSWORD
MACOS_KEYCHAIN_PASSWORD
MACOS_NOTARY_KEY_P8_BASE64
MACOS_NOTARY_KEY_ID
MACOS_NOTARY_ISSUER_ID
```

這些只放 GitHub Actions secrets 或 release host keychain，不放 `.env`、repo、PR 或聊天。這個 workflow 目前只產生已簽名/已公證 macOS artifact，不會自動建立 GitHub release 或替你上傳 iOS TestFlight。

## C. 驗收順序

1. Bridge health / smoke 綠。
2. Pocket 重新連線，不要先清除 session。
3. 驗證一條 persona、CC、CX 各一則訊息與狀態更新。
4. 確認 approval card、重連後歷史、附件下載。
5. 最後才執行 macOS release workflow。
