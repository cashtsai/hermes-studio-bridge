# Console QR 登入規格（任意電腦開桌面控制台）

> 機主 2026-10-29 提案。本文件是實作依據 —— **改實作先改這裡**。
> 狀態：**bridge 側第一階段已實作**（分支 `feat/console-qr-login`，
> worktree `~/apps/bridge-console-qr`）。入口分流與能力降級未完成，見 §16。

## 進度速查（2026-10-29）

| 項目 | 狀態 |
|---|---|
| 旗標 `POCKET_CONSOLE_QR_LOGIN`（預設 `0`） | ✅ |
| login request 狀態機（120s TTL／一次性／40 筆上限／**全域**節流） | ✅ |
| `_console_login_client_key()` 單一來源識別函式 | ✅ |
| rid／sid 分離（§6.2 的防搶設計） | ✅ |
| `GET /console/login/status`（含來源一致性比對） | ✅ |
| `GET /app/v1/console/login/requests/{rid}` | ✅ |
| `POST .../approve`、`POST .../deny` | ✅ |
| session cookie 關瀏覽器即死（`_console_set_cookie(session_only=)`） | ✅ |
| QR 登入頁（自包含、零外部資源） | ✅ |
| `_pair_local_ok()` 述詞版（判定邏輯零複製） | ✅ |
| **`console_page()` 入口分流** | ⬜ **未接上 → 功能目前無法觸發**（§16） |
| **token scope 與能力降級（擋 `/file`、`/filediff`）** | ⬜ 未開始 |
| iOS／Android 批准頁 | ⬜ 未開始 |

驗證到的程度：`py_compile` 通過；QR 頁字串格式化單獨跑過（payload 代入、
CSS `100%` escape、rid 進 payload）。**未啟動服務、未合併 main**（CLAUDE.md 鐵則）。

## 0. 一句話

瀏覽器顯示 QR、**已配對的手機掃碼並明示批准**，瀏覽器才拿到 console 權限；
門從「瀏覽器在哪個網路」換成「可信裝置的授權」。

## 1. 動機

今天 `/console` 只能從 **loopback 或 tailnet 直連** 開。機主的實際需求是
「**在一台完全沒有 tailnet 的電腦上也能開控制台**」（出門借電腦、客戶端機器）。

機主原始提案是「用那個六碼登入，關掉瀏覽器或 timeout 就重新六碼登入」。
session 短命這部分完全正確且比現狀更安全（見 §10），但**六碼不能當唯一的鎖**
（§3 有算術）。QR + 手機批准是同一個需求的正解。

## 2. 現狀：為什麼今天做不到

`_console_guard`（`bridge.py:10567`）的順序是**刻意**的 —— 先驗來源，才驗憑證：

```python
if not POCKET_CONSOLE_ENABLED: 404          # bridge.py:601,預設 "0"(本機實況=1)
_pair_local_only(request)                   # 第一關:必須直連 loopback/tailnet
_pair_check_boot(request, allow_cookie=True)  # 真閘:boot code 或簽過名的 cookie
```

`_pair_local_only`（`bridge.py:9573`）要「**直連**且來源是 loopback 或 tailnet」，
並且**只要帶任何轉送標頭（XFF / Forwarded / CF-Connecting-IP / X-Real-IP）就一律拒絕**
（因為 XFF 可偽造，不賭 uvicorn 怎麼取值）。實際結果：

| 瀏覽器怎麼連 | 結果 |
|---|---|
| 同機 `http://127.0.0.1:8081/console` | ✅ loopback |
| tailnet 直連 `http://<magicdns>:8081/console` | ✅ |
| LAN `http://192.168.x.x:8081` | ❌ 403 |
| 公網 cloudflared tunnel | ❌ 403（必帶 XFF） |
| `tailscale serve` 的 `https://<magicdns>`（443） | ❌ 403（serve 也補 XFF） |
| **`gateway.shan.house`（Vultr Caddy 反代，現行公網入口）** | ❌ **403** |

→ 所以在沒有 tailnet 的電腦上，**六碼根本輪不到被檢查**，403 發生在那之前。
機主描述的流程目前不存在，要做它是改設計、不是調設定。

### 一個關鍵的架構事實（影響整個設計）

console cookie（`pocket_console`）**只被 `GET /console` 這一關接受** ——
`allow_cookie=True` 全檔只有 `_console_guard` 傳（`bridge.py:10585`）。
cookie 只換得到「頁面殼」。真正的 API 存取靠**前端 claim 來的 device token**
（`_console_mint_claim_code`，`bridge.py:10310`；一次性、60 秒、只在成功 POST
驗碼的當下鑄造，存在前端 localStorage）。

bridge 自己的註解寫得很清楚：「cookie 外洩頂多能看頁面殼，換不到可以接管主機的碼」。

**所以 QR 批准後真正要發的是 device token，不是 session flag。**
能力降級（§9）必須做在 **token 層**，做在 cookie 上是假的。

## 3. 不採用：放寬六碼（附算術）

節流是 **per-IP 每 60 秒 6 次失敗**（`_CONSOLE_AUTH_FAIL_MAX=6`，`bridge.py:10469`；
`_AUTH_FAIL_WINDOW=60`，`bridge.py:305`），碼存活 300 秒（`_CONSOLE_CODE_TTL`，
`bridge.py:10478`）。單一 IP 在一個碼的生命週期內可試 30 次：

| 攻擊者 IP 數 | 命中單一活碼機率 |
|---|---|
| 1 | 0.003% |
| 100 | 0.3% |
| 1,000 | **3%** |
| 10,000 | **30%** |

節流是 per-IP，**殭屍網路並行就破**。六碼（10⁶）今天可以接受，正因為它躲在
tailnet 那道門後面當第二層；拆掉第一道門，它就從 defense-in-depth 變成唯一的鎖
—— 而命中的代價是那台機器的檔案瀏覽權限。這是質變，不做。

## 4. 威脅模型

| # | 威脅 | 處置 |
|---|---|---|
| T1 | 公網暴力猜 rid | rid = 128-bit（§7），空間 3.4×10³⁸，出局 |
| T2 | **QR 釣魚**：攻擊者在自己機器開登入頁、把 QR 截圖傳給機主騙他掃 | **主要威脅**。三道防線見 §8 |
| T3 | 旁觀者看到機主螢幕上的 QR，從自己瀏覽器搶走這個 pending session | 瀏覽器端另發 `sid`（httpOnly cookie，**不在 QR 裡**），輪詢必須帶 sid |
| T4 | rid 被轉傳到別的網路使用 | rid 綁定「取得 QR 的來源 IP」，不符即拒 |
| T5 | 公網暴露 `/console` 登入頁 → DoS / 掃描 | 旗標預設關（§14）；建立 login request 有全域節流（§8.3） |
| T6 | 批准後的 token 外洩（XSS / 電腦被別人用） | token 短命 + 關瀏覽器即失效（§10）+ 能力降級（§9） |
| T7 | 跨站表單 POST 偽造批准 | 沿用既有 `_console_login_cross_site`（Sec-Fetch-Site） |

**明確不在範圍內**：機主自己在惡意電腦上主動登入（那台機器可以記錄一切）。
能力降級（§9）把這種情況的損失壓到有界，但無法消除。

## 5. 設計總覽

```text
瀏覽器(無 tailnet)                bridge                        手機(已配對)
  │  GET /console                   │                              │
  ├────────────────────────────────►│  建 login request            │
  │                                 │  rid=128bit, ip, ua, 120s    │
  │◄── QR 頁 + Set-Cookie: sid ─────┤                              │
  │    QR: pocket://console-login   │                              │
  │         ?rid=…&fp=…             │                              │
  │                                 │         掃 QR                │
  │                                 │◄──── GET  .../requests/{rid} ┤ (device token)
  │                                 ├────► {ip,ua,geo,expires} ───►│ 顯示批准頁
  │                                 │◄──── POST .../approve ───────┤ 使用者明示批准
  │  GET /console/login/status      │                              │
  ├──(帶 sid)──────────────────────►│  驗 sid + ip + 未過期        │
  │◄── 302 /console + token ────────┤  鑄 scoped device token      │
```

## 6. 端點契約

### 6.1 `GET /console`（既有，行為擴充）

`_pair_local_only` 不通過時的**新分支**：

- `POCKET_CONSOLE_QR_LOGIN != "1"` → 維持現狀 403（**預設行為不變**）
- `== "1"` → 回 QR 登入頁（200），並：
  - 建 login request：`{rid, sid_hash, ip, ua, created, expires}`，TTL **120 秒**
  - `Set-Cookie: pocket_console_login=<sid>`；**httpOnly、SameSite=Strict、
    path=/console、無 max-age（session cookie）**
  - 頁面輪詢 `/console/login/status`（每 2s，最多 60 次）

通過 `_pair_local_only` 時**完全不變**（tailnet/loopback 仍走既有六碼/cookie 路徑）。

### 6.2 `GET /console/login/status`

- 認證：`pocket_console_login` cookie（sid）。**不接受 query 帶 rid** —— rid 在 QR 裡，
  它是給手機的，不是給瀏覽器的憑證。
- 回 `{"state": "pending" | "approved" | "denied" | "expired"}`
- `approved` 時：鑄 scoped device token（§9）→ 回
  `{"state":"approved","token":"pdev-…","redirect":"/console"}`，並種
  `pocket_console` session cookie（§10）
- 未知/過期 sid 一律回 `{"state":"expired"}` —— **不區分**，避免當探測器
- 節流：per-sid 每秒 1 次；超額 429

### 6.3 `GET /app/v1/console/login/requests/{rid}`（手機，新）

- 認證：`_check_auth`（master 或 device token）
- 回批准頁要顯示的資訊：
  ```json
  {"rid":"…","ip":"203.0.113.9","ip_label":"公網 / 日本東京(推測)",
   "user_agent":"Chrome 141 on Windows","created_at":1761…,"expires_at":1761…,
   "state":"pending"}
  ```
- `ip_label` 是**人話推測**（公網/LAN/tailnet + 粗略地理），用途是讓機主判斷
  「這是不是我現在面前那台電腦」。查不到地理就只寫網路種類，**不要編**。
- rid 不存在/過期 → 404（手機端顯示「這個 QR 已失效，請在電腦上重新整理」）

### 6.4 `POST /app/v1/console/login/requests/{rid}/approve`（手機，新）

- 認證：`_check_auth`
- 前置：`state == pending` 且未過期；否則 409
- 效果：`state = approved`，記 `approved_by`（device token hash）、`approved_at`
- 回 `{"ok": true}`
- **一次性**：approve 過的 rid 不能再 approve

### 6.5 `POST /app/v1/console/login/requests/{rid}/deny`（手機，新）

同上，`state = denied`。手機端在「這不是我」時呼叫 —— 讓瀏覽器立刻看到拒絕，
而不是等 120 秒過期（使用者體感差很多，也讓釣魚被明確否決）。

### 6.6 契約文件

上線時 `docs/APP_BRIDGE_CONTRACT.md` 要補 §（console QR 登入），寫明
`/app/v1/console/login/*` 三支的 shape 與未啟用時的表現（旗標關 → 404）。

## 7. QR payload

```text
pocket://console-login?rid=<32 hex>
```

- `rid`：`secrets.token_hex(16)` = **128 bit**
- ~~`fp`：bridge 身分指紋~~ **已移除（2026-10-29 實作時發現多餘）**。
  原本想用它讓 app 擋掉「掃到別人 bridge 的 QR」，但 `rid` 是**本 bridge 本地**
  的隨機值 —— app 拿著 rid 去呼叫**自己配對的那台** bridge，掃到別台的 QR
  自然就是 404。404 已經提供同樣的保護，再加指紋只是多一個要維護的欄位。
  app 端據 404 顯示「這個 QR 不是這台桌面的」。
- **刻意不放 bridge URL** —— app 必須用它**自己既有的連線**去呼叫，絕不可以被
  QR 指去另一個主機（那正好是釣魚要的）。
- app 端解析沿用既有 `pocket://` 深連結管線（iOS build 126 那批、Android
  `ConnectionRouter.PairPayload`），新增一個 scheme path 分支即可。

## 8. 三道防線（T2 QR 釣魚）

### 8.1 批准頁必須顯示請求方資訊，且要明示點擊

手機端**不得掃到就自動批准**。批准頁至少顯示 §6.3 的 `ip_label` +
`user_agent` + 倒數，並要求使用者按「批准」。文案要直說：
「如果這不是你現在面前的電腦，請按『不是我』。」

### 8.2 rid 綁定取得 QR 的來源 IP

`approve` 只標記狀態；**真正發 token 是在 `/console/login/status`**，那時再驗
「這次輪詢的來源 IP == 建立 request 時的 IP」。QR 被轉傳到別處使用 → IP 不符 → 拒。

> ⚠️ 取 IP 要用與 `_pair_local_only` 同一套認知：經 Caddy/Cloudflare 時
> `request.client` 可能已被 XFF 覆寫。這裡**接受** XFF（這條路本來就走反代），
> 但要固定取同一個來源欄位，不能一次取 socket peer、一次取 XFF 最左。
> 實作時寫一個 `_console_login_client_key(request)` 單一函式，兩處共用。

### 8.3 短 TTL + 一次性 + 全域節流

- login request TTL **120 秒**，過期即刪
- rid 一次性：approved/denied 後不可再用
- **建立 login request 的全域節流**：全網每分鐘最多 20 筆 pending（不是 per-IP
  —— per-IP 擋不住殭屍網路，見 §3 的教訓）。超額回 503 + 人話說明

## 9. Token scope 與能力降級（**需要新 primitive**）

QR 批准發出的 token **不能**與手機的 device token 同權限。

插入點已經有了：`_pair_issue_device_token(..., extra: dict | None)`
會把 `extra` 併進 token entry。所以：

```python
extra={"scope": "console-remote",
       "origin_ip": …, "expires": time.time() + CONSOLE_REMOTE_TOKEN_TTL}
```

**要新寫的是執行面**（目前 `_check_auth` 沒有 scope 概念、device token 沒有有效期）：

1. `_check_auth` 讀到 `scope == "console-remote"` 時：
   - 檢查 `expires`，過期即 401（**device token 目前完全沒有有效期，這是新行為**）
   - 標記 request 狀態供下一步用
2. **擋掉檔案面**：`GET /file`（`bridge.py:9043`）與 `GET /filediff`
   （`bridge.py:9108`）對 `console-remote` scope 一律 403。
   理由：這兩支是「那台機器的鑰匙」的實體，而 QR 登入的前提就是「人不一定
   在可信的電腦前」。
3. **放行**：session 清單、卡片流、下指令、核准 —— 這些是機主真正需要的遠端能力。
4. 新增端點時的預設：**scope 檢查採白名單** —— 沒明確標記「console-remote 可用」
   的敏感端點一律拒。黑名單會在加新端點時安靜漏掉。

> 這一節是整份規格裡**唯一需要動既有認證核心**的部分，風險最高，要單獨一個
> commit + 單獨測試。

## 10. Session / Token 生命週期（機主要求的部分）

| 項目 | 現狀 | QR 登入這條路 |
|---|---|---|
| `pocket_console` cookie | max-age **14 天**滑動（`bridge.py:10226`） | **無 max-age = session cookie，關瀏覽器即死** |
| device token | **永不過期** | `console-remote` scope **有 `expires`**，預設 **30 分鐘** |
| 閒置 | 無 | 閒置 **15 分鐘** 無請求即失效（`last_seen` 判定） |
| 重登 | 再用一次 boot 連結 | **重新掃一次 QR**（正是機主要的） |

env 可調：`POCKET_CONSOLE_REMOTE_TOKEN_TTL`（秒，預設 1800）、
`POCKET_CONSOLE_REMOTE_IDLE`（秒，預設 900）。

## 11. 錯誤與邊界

| 情況 | 行為 |
|---|---|
| 旗標關 | `GET /console` 對非 tailnet 來源維持 403（**不是 404** —— 與既有 `POCKET_CONSOLE_ENABLED=0` 的 404 區分開，查的人才分得出是哪一道門） |
| 手機不在線 / 沒批准 | 120 秒後瀏覽器顯示「逾時，請重新整理再掃一次」 |
| 手機按「不是我」 | 瀏覽器**立刻**顯示「已被拒絕」，並建議檢查是否被釣魚 |
| 同一瀏覽器重新整理 | 舊 rid 作廢、發新的（避免畫面上有兩個 QR 都有效） |
| 手機掃到別的 bridge 的 QR | `fp` 不符 → app 端擋下並說明，**不送批准** |
| 全域節流滿 | 503 + 「現在有太多登入請求，請稍後再試」 |
| 時鐘歪斜 | 沿用既有 `_CONSOLE_COOKIE_CLOCK_SKEW`（300s）的寬容度 |

## 12. 實作清單

### bridge（`~/apps/hermes-openwebui-bridge`）

1. login request 狀態機（記憶體 dict + lock，**不落盤** —— 120 秒的東西不值得持久化）
2. `GET /console` 的非 tailnet 分支 + QR 頁（QR 產生沿用既有 `qrcode` 依賴）
3. `GET /console/login/status`
4. `/app/v1/console/login/requests/{rid}` × 3（get / approve / deny）
5. `_console_login_client_key()` 單一來源 IP 函式（§8.2 的警告）
6. **token scope 執行面**（§9）← 風險最高，單獨 commit
7. 全域節流桶（§8.3）
8. `docs/APP_BRIDGE_CONTRACT.md` 補章
9. 旗標 `POCKET_CONSOLE_QR_LOGIN`（預設 `0`）

### iOS（`~/apps/pocketagent`）

1. `pocket://console-login` 深連結分支（既有管線加 case）
2. QR 掃描器認這個 payload（掃描器已存在）
3. **批准頁**（照既有配對確認頁 `obProposal` 的模式）：顯示 ip_label / UA / 倒數 +
   「批准」「不是我」兩顆
4. `fp` 比對不符時的警告頁

### Android（`~/apps/pocket-android`）

同 iOS 三項。沿用 `ConnectionRouter.PairPayload` 的確認頁模式。
**排在 iOS 之後**（iOS 是正典，照 `IOS_PARITY.md` 流程追）。

## 13. 驗收

1. **正常路徑**：沒有 tailnet 的電腦開 `/console` → 顯示 QR → 手機掃 → 批准 →
   進得去，且**檔案瀏覽被擋**（403）
2. **關瀏覽器即死**：關掉視窗重開 → 要重新掃
3. **逾時**：30 分鐘後自動失效；閒置 15 分鐘失效
4. **拒絕**：手機按「不是我」→ 瀏覽器立刻顯示被拒
5. **IP 綁定**：在 A 機器開 QR、把 rid 拿到 B 機器輪詢 → 失敗
6. **sid 防搶**：拍下螢幕 QR，從另一個瀏覽器輪詢 → 失敗（沒有 sid）
7. **旗標關**：維持 403，行為與今天完全一致
8. **tailnet 路徑零回歸**：loopback 與 tailnet 直連的既有六碼/cookie 流程不變
9. **scope 白名單**：新增一支未標記的敏感端點 → `console-remote` token 打過去被拒

> ⚠️ 1–6 需要**真的一台沒裝 Tailscale 的電腦**（或把網路路徑繞過 tailnet）。
> 不要只在 loopback 驗，那條路根本不會進新分支。

## 14. 旗標與預設

| env | 預設 | 說明 |
|---|---|---|
| `POCKET_CONSOLE_ENABLED` | `0`（本機實況 `1`） | 既有，控整個 console |
| `POCKET_CONSOLE_QR_LOGIN` | **`0`** | 新增。關 = 非 tailnet 來源維持 403 |
| `POCKET_CONSOLE_REMOTE_TOKEN_TTL` | `1800` | scoped token 壽命（秒） |
| `POCKET_CONSOLE_REMOTE_IDLE` | `900` | 閒置失效（秒） |

**預設全關**：上線後要明確開旗標才會改變任何行為，不會因為部署就擴大攻擊面。

## 15. 待決（開工前請機主拍板）

1. **能力降級的範圍**：§9 建議「擋檔案面、放 session/指令/核准」。
   如果機主實際需求就是遠端看檔案，那降級就得改設計（例如只開唯讀 + 限定目錄），
   這會影響 §9 的工作量。**這是最該先確認的一題。**
2. **`console-remote` token 要不要出現在 `/pair/devices` 清單**裡讓機主能手動撤銷？
   建議要（可撤銷比可觀察重要），但要標清楚是「遠端瀏覽器」而非一台裝置。
3. **地理推測**要不要做：`ip_label` 的地理部分需要 GeoIP 資料或外部查詢。
   外部查詢 = 把訪客 IP 送給第三方，與專案的隱私立場衝突。
   建議**第一版只寫網路種類**（公網/LAN/tailnet）＋ IP 本身，不做地理。

## 附：為什麼這個方案繞過了六碼的所有問題

| | 六碼手打 | QR + 手機批准 |
|---|---|---|
| 熵 | 10⁶ | 2¹²⁸ |
| 秘密往哪流 | 人把碼打進不可信的瀏覽器 | 瀏覽器不證明任何事；授權來自已認證的手機 |
| 認證強度 | 6 位數字 | device token（bearer） |
| 人的確認 | 打完就進 | 手機上明示同意，且看得到請求方資訊 |
| 業界先例 | — | WhatsApp Web / Discord 掃碼登入同款 |

這其實就是**既有配對流程倒著跑**（桌面顯示 QR → 手機掃 → 授權），
所以 QR 產生、`pocket://` 管線、掃描器、確認頁、`_check_auth` 全部已經存在。
真正新的只有 login request 狀態機與 token scope 執行面。
