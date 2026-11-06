# T3Code Phase 2 發版保護

更新日期：2026-11-05

這份文件定義測試機、Vultr 龍蝦與本地 bridge 的共同故障邊界。它不改變
`/app/v1`、`/app/v2` wire contract，也不把資料庫復原當成一般自動修復。

## 已落地

- `deploy/bridge-supervisor.sh`：同一個 5 分鐘窗口內連續 5 次子程序失敗後
  進入 hold，不再無限重啟。hold 期間服務管理器仍看得到 supervisor 活著，
  但不會反覆啟動壞版本。
- Ubuntu systemd bridge unit：使用 supervisor，並設
  `StartLimitIntervalSec=300`、`StartLimitBurst=5`，雙重限制 supervisor
  自身異常退出時的重啟次數。
- macOS LaunchAgent：使用 supervisor 並設定 `ThrottleInterval=10`；
  `install-local-bridge.sh` 會在明確重跑安裝時清掉 hold。
- `deploy/lobster-update.sh`：先用 SQLite online backup 建立 canonical/accounts
  snapshot；只有新版已成功安裝後才停止 user service，切換 release symlink 後做帶授權的
  `/app/v2/discovery` 行為探活。失敗時切回舊 release、清掉 hold、重啟；
  壞版本目錄與 snapshot 都保留。
- `scripts/restore-sqlite-snapshot.sh`：資料庫復原是明確人工操作，必須提供
  `CONFIRM_RESTORE=1`。這避免自動回滾把更新期間已寫入的新訊息覆蓋掉。
- Guest link 撤銷：已驗證的 owner/device 可呼叫 `POST /app/v1/guest-links/revoke-all`，只輪替獨立 guest
  signing secret，所有既有分享連結立即失效；不保存 token denylist。
- APNs 金鑰來源：預設仍相容既有 `.p8` 檔案；可用 `APNS_KEY_SOURCE=keychain` 從
  macOS Keychain 讀取，或以 `APNS_KEY_REQUIRE_PRIVATE=1` 強制檔案 owner-only。
  provider 只回傳是否可讀與 key material 給 JWT signer，不把內容寫入 log、DB 或事件。

## 操作

先清除 crash-loop hold，再讓目前版本重試：

```bash
POCKET_BRIDGE_LABEL=pocket-bridge \
  scripts/bridge-reset-crash-loop.sh
```

列出可用資料庫 snapshot：

```bash
scripts/restore-sqlite-snapshot.sh
```

只有確認要捨棄 snapshot 之後的新寫入，才執行：

```bash
CONFIRM_RESTORE=1 scripts/restore-sqlite-snapshot.sh <snapshot-prefix>
```

復原後必須跑 `scripts/post-deploy-smoke.py --json`，再驗證 Pocket 的 persona、
CC、CX 各一條 session。不要直接刪除 `releases/` 或 `snapshots/`，它們是
回滾證據。

## 尚未宣稱完成

- 這套保護尚未在 Vultr 實機觸發壞版演練；目前以腳本測試與靜態 unit contract
  驗證為主。
- APNs Keychain provider 已有單元測試；TestFlight 背景/鎖屏矩陣仍需真機與實際 key。
- Guest link 目前是「全數撤銷」語意，不是單條撤銷；若未來要 per-link revoke，
  必須另設只存 token hash/expiry 的 denylist 與清理策略，不能把原始 URL 存回資料庫。
- 自動回滾只回退程式碼。資料庫 snapshot 的復原保留給明確人工決策。
- macOS 的舊版 standalone watchdog 仍可存在；它只處理 health timeout，不能
  取代 supervisor 的 crash-loop hold。
