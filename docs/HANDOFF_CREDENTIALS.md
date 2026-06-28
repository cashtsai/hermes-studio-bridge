# 憑證交接手續 (Credentials Handoff)

> 本文件只記錄「金鑰放在哪、誰在用、怎麼換」。**絕不**把金鑰本體 commit 進 repo。
> 最後更新：2026-06-28

## 1. 憑證清單 (Inventory)

| 用途 | 檔名 / 識別碼 | 實際位置 (本機，未進 git) | 權限 | 使用者 |
|------|--------------|--------------------------|------|--------|
| **APNs 推播金鑰** (M23) | `AuthKey_86FF9D976T.p8`<br>Key ID `86FF9D976T` | `~/apps/hermes-agent/home/credentials/` | `600` (目錄 `700`) | `bridge.py` 的 `_apns_jwt()` |
| **App Store Connect API 金鑰** (上架/release) | `AuthKey_7YB9Q2WYSA.p8`<br>Key ID `7YB9Q2WYSA` | `~/.appstoreconnect/private_keys/` | `600` | `asc.py` (出版流程) |
| **Bridge Bearer Token** | `BRIDGE_TOKEN` | LaunchAgent `ai.studio.hermes-bridge` 的環境變數 | — | 所有 app→bridge 請求 |

### 共用識別碼
- **Apple Team ID**：`4F8B93R3SH`
- **App Bundle ID / APNs topic**：`com.scarfgo.app.4F8B93R3SH`
- **App Store App ID**：`6784576279`

> ⚠️ **兩把 .p8 是不同東西，別搞混**：
> - `86FF9D976T` = **推播** (APNs，Key Services 勾 Apple Push Notifications)，歸 Hermes 管理。
> - `7YB9Q2WYSA` = **上架 API** (App Store Connect)，留在 `~/.appstoreconnect/`。

## 2. APNs 金鑰歸位原則 (為什麼放這裡)

推播金鑰是 **Hermes runtime 在用**（bridge 是 Hermes 的對外橋接層），所以歸到
`~/apps/hermes-agent/home/credentials/` 這個「Hermes 管理底下」的目錄，與其他
Hermes 機密同層、同備份策略。**不要**留在 `~/Downloads`，也**不要**跟 ASC 的上架
金鑰混在 `~/.appstoreconnect/`。

bridge.py 以絕對路徑引用，不複製、不內嵌：
```python
APNS_KEY_PATH = "~/apps/hermes-agent/home/credentials/AuthKey_86FF9D976T.p8"
APNS_KEY_ID   = "86FF9D976T"
APNS_TEAM_ID  = "4F8B93R3SH"
APNS_BUNDLE_ID= "com.scarfgo.app.4F8B93R3SH"
APNS_HOST     = "https://api.push.apple.com"   # production
```

## 3. M23 推播鏈路 (這把金鑰被誰、怎麼用)

```
事件 (e.g. 建立 approval)
  → push_notify(title, body, data)
      → _devices()        取出所有已註冊的 device token (canonical.db: devices 表)
      → _apns_jwt()       用 .p8 簽 ES256 JWT (kid=86FF9D976T, iss=Team, cache ~50min)
      → _apns_send()      httpx HTTP/2 POST api.push.apple.com/3/device/<token>
      → 回 410/BadDeviceToken 的死 token 自動從 devices 表清掉
```

相關 endpoint（contract = `app/v1`）：
- `POST /app/v1/devices`     — app 啟動/換 token 時註冊 `{token, platform}`
- `GET  /app/v1/devices`     — 看目前註冊數
- `POST /app/v1/push/test`   — 對所有裝置送測試推播（驗證整條鏈路）
- 觸發點：`POST /app/v1/approvals` 建立核准時自動推播 🔐

## 4. 驗證金鑰有效 (交接後自我檢查)

不需要真實裝置就能確認「金鑰簽章有沒有被 Apple 接受」：
```bash
cd ~/apps/hermes-openwebui-bridge
~/apps/hermes-agent/runtime/venv/bin/python - <<'PY'
import asyncio, bridge
print(asyncio.run(bridge._apns_send("a"*64, "probe", "probe")))
PY
```
- 回 **`400 BadDeviceToken`** → ✅ 金鑰/Team/Key ID 正確，只是 token 是假的（預期）。
- 回 **`403 InvalidProviderToken`** → ❌ 金鑰、Key ID 或 Team ID 有錯，去查。

## 5. 輪替 / 撤銷 (Rotation)

1. Apple Developer → Certificates, IDs & Profiles → **Keys** → 新增一把勾
   *Apple Push Notifications service (APNs)* 的 Key，下載 `.p8`（**只能下載一次**）。
2. 放到 `~/apps/hermes-agent/home/credentials/`，`chmod 600`。
3. 改 bridge.py 的 `APNS_KEY_PATH` / `APNS_KEY_ID`。
4. `py_compile` → `launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge`。
5. 跑第 4 節的驗證。
6. 回 Apple 後台 **Revoke** 舊 Key。

## 6. 依賴 (bridge venv)
`~/apps/hermes-agent/runtime/venv` 需有：`PyJWT`(ES256)、`cryptography`、`httpx[http2]`(含 `h2`)。
