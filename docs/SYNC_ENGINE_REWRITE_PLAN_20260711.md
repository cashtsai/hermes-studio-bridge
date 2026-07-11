# Pocket App 同步引擎重構 — 完整規劃（v1，待善彰拍板）

**狀態**：規劃草案，未派工、未動工
**發起背景**：善彰體感 Pocket App「聊天列表同步/未讀不準、進聊天窗慢、跟 TG 同步慢」，明確要求「整個人格體驗的部分設定也是可以重新評估，我也是不惜重構」
**產出日期**：2026-07-11

---

## 1. 現況架構圖（查證過的真實現況，非猜測）

```
┌─────────────────────────────────────────────────────────────┐
│  Telegram Bot                          Pocket iOS App        │
│       │                                      │                │
│       ▼                                      ▼                │
│  Hermes gateway 官方套件              StudioBridge.swift       │
│  （不可改內核）                        │                       │
│       │ 寫入                                 │ HTTP/SSE        │
│       ▼                                      ▼                │
│  <persona_home>/state.db  ◄──唯讀──   bridge.py (FastAPI)      │
│  (WAL, Hermes 官方 schema)   stat        │                    │
│                              watcher      ├─ canonical.db      │
│                            (剛修好)        │  (App 自己的回合)  │
│                                            ├─ _CANON_VER (int) │
│                                            │  純記憶體版本號     │
│                                            └─ SSE 事件推送      │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 三條資料來源，各自有不同的「新鮮度保證」

| 來源 | 寫入方 | 新鮮度機制 | 延遲 |
|---|---|---|---|
| App 發送/收到的訊息 | bridge.py 自己 | `_canon_add()` 寫 `canonical.db` 同時 `_canon_notify()` bump `_CANON_VER`（純記憶體 dict，**bridge 重啟就歸零**） | ~0.2s |
| Telegram 訊息 | Hermes 官方 gateway | 剛修好的 state.db-wal stat watcher（唯讀輪詢，疊加在 30s 保險絲上） | ~0.4s（新） |
| cron 晨報 | 各 cron job | 同上，state.db 或 report_events 表 | ~0.4s〜30s |

### 1.2 已讀狀態：完全沒有伺服器真相

`UnreadStore`（`StudioHomeCache.swift`）是純 App 端 `UserDefaults` 計數器：
```swift
static func markActivity(_ id: String, at t: TimeInterval)  // 本地 +1
static func markSeen(_ id: String, at t: TimeInterval)      // 本地歸零
```
**沒有任何欄位在 `canonical.db` 或 `state.db` 記錄「使用者在哪個裝置、什麼時間點已讀到哪一則」**。這代表：
- 兩台裝置（例如 iPhone + iPad 都裝 Pocket）未讀數必然不同步
- App 被殺掉重裝、`UserDefaults` 清空，未讀狀態全部消失/重算
- 沒有「已讀游標」這個資料模型，本質上無法做到 TG 等級的已讀同步

### 1.3 跨裝置對話持久化缺口（backlog B3，已知未解）

ACP session 的載入回合**不會回寫 `state.db`**，換裝置登入後，該裝置本地沒有的訊息永久看不到（要等下一次全量 `_persona_history` 拉取，但那條路徑也有自己的 200 則上限與清洗規則）。

---

## 2. 根因診斷：為什麼「疊加式修復」有天花板

目前每一次修復都是「在既有輪詢骨架上加一條加速通道」：
- SSE 只覆蓋「App 自己觸發的寫入」這一條路徑
- 剛修的 stat watcher 只加速「TG 寫入 state.db」這一條路徑
- 首頁列表刷新仍是純輪詢（4s/20s 自適應）
- 未讀完全没有伺服器概念，是本地猜測

**每加一條「來源」就要加一條對應的加速通道**，且沒有一個統一的「事件序號 + 訂閱」協議。這不是效能問題，是**資料模型缺少單一事實來源（single source of truth）**：現在是 canonical.db + state.db + UserDefaults 三處各自維護部分真相，App 端要拼湊出「這個人格現在的完整狀態」。

---

## 3. 目標架構（路線 B）

### 3.1 核心原則：單一事件日誌 + 游標訂閱（借鏡 TG/Signal 的做法）

```
┌───────────────────────────────────────────────────────────┐
│                     bridge.py（唯一事實來源）                 │
│                                                             │
│  event_log 表（新增，SQLite，append-only）：                 │
│    id INTEGER PRIMARY KEY AUTOINCREMENT (= 全域遞增 seq)      │
│    session TEXT, type TEXT, payload JSON, created_at REAL   │
│                                                             │
│  來源全部匯入這張表：                                          │
│    - App 自己的訊息（原 _canon_add 呼叫點順便寫 event_log）     │
│    - TG 訊息（state.db stat watcher 偵測到後，讀取新增列          │
│      並寫進 event_log —— 這步驟本來就在做，只是現在寫的是        │
│      canonical follower 的記憶體卡片，改成也落 event_log）      │
│    - cron 晨報（report_events 表已存在，改成也鏡射進 event_log）  │
│    - 已讀游標變更（新事件類型 read_cursor.update）              │
│                                                             │
│  GET /app/v2/events?session=X&since_seq=N  (SSE, 唯一端點)    │
│    → 從 event_log 撈 id > since_seq 的所有列，即時 + 補洞      │
└───────────────────────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────┐
│                   App 端（StudioStore 重構）                  │
│                                                             │
│  本地持久化改成「event log 副本 + 已處理到的 seq」：             │
│    - 開 App / 開某人格聊天窗 → 讀本地副本立即渲染（無網路等待）    │
│    - 背景訂閱 /app/v2/events?since_seq=<本地已知最大值>         │
│    - 每個 event 落地時更新本地副本 + seq 游標                    │
│    - 已讀：使用者看完某訊息 → POST 一個 read_cursor.update       │
│      事件回 bridge，其他裝置訂閱到就能顯示「已讀」                │
└───────────────────────────────────────────────────────────┘
```

### 3.2 這樣設計解決的三個症狀

| 症狀 | 解法 |
|---|---|
| TG↔App 同步慢 | TG 寫入照樣先進 event_log 再推送，跟 App 自己的訊息走同一條 SSE 通道，不再是「加速通道追著跑」 |
| 進聊天窗慢 | 本地 event log 副本本來就有歷史，直接渲染，跟現在剛修的 `ensureLoaded` 精神一致，但這次是「架構原生支援」不是「補一個快取層」 |
| 未讀不準 | `read_cursor` 是伺服器事件，天生跨裝置一致，不再靠本地計數器瞎猜 |

### 3.3 額外解決：backlog B3（跨裝置持久化）

因為所有訊息（不分來源）最終都落在 `event_log`，任何裝置登入後只要「從 seq=0 開始重放」就能重建完整歷史，天然解決 B3。

---

## 4. 工作分解與工作量估計

| 階段 | 內容 | 涉及檔案 | 估計工作量 |
|---|---|---|---|
| **P0：event_log 資料層** | 新增 `event_log` 表 + `_event_append()`/`_event_since()` 函式，先只做「寫入」不改任何現有讀取路徑（純加法，不影響現有功能） | `bridge.py` | 1〜2 天 |
| **P1：三個來源接入** | App 訊息 / TG watcher / cron 晨報三處寫入點都額外鏡射寫一份到 `event_log`（現有 canonical.db / state.db 讀取路徑先不動，雙寫過渡） | `bridge.py` | 2〜3 天 |
| **P2：新端點 + 已讀游標** | `GET /app/v2/events` SSE 端點；新增 `read_cursor` 表 + `POST /app/v2/read` | `bridge.py` | 2〜3 天 |
| **P3：App 端訂閱層重構** | `StudioStore` 改吃 `/app/v2/events`，本地持久化改成 event log 副本，UI 層（聊天窗/列表/未讀 badge）改吃新資料源 | `StudioBridge.swift`、`StudioChatUI.swift`、`StudioHomeCache.swift` | 5〜8 天（App 端改動範圍最大） |
| **P4：舊路徑淘汰** | 確認新路徑穩定運行一段時間後，拔掉 `_CANON_VER`/stat watcher/`UnreadStore` 等舊機制，避免雙軌長期並存增加維護成本 | 兩邊都要 | 1〜2 天 |
| **P5：多裝置 QA** | 兩台裝置同時測試已讀同步、離線重連補洞、卸載重裝後歷史重建 | 無新檔案，純測試 | 2〜3 天 |

**總計估算：約 3〜4 週**（單人全職節奏；用子程序平行的話可能壓縮，但 App 端 P3 這塊改動集中在少數幾個核心檔案，多個子程序同時改同一批檔案會互相衝突，實務上很難真正平行，這塊可能還是要串行）。

---

## 5. 風險與取捨

| 風險 | 說明 | 緩解方式 |
|---|---|---|
| **App 端改動範圍大，牽動核心聊天 UI** | `StudioChatUI.swift` 是 8000 行的核心檔案，任何时候都有其他功能在上面疊加開發（approval hub、用量卡片等剛做完的都在這裡） | 需要善彰決定：這段期間是否暫緩其他 App 新功能開發，避免兩條線同時改同一批檔案衝突 |
| **雙寫過渡期的一致性** | P1〜P3 之間，新舊兩套機制並存，理論上有機會出現「event_log 有但 canonical.db 沒有」之類的短暫不一致 | P4 之前不淘汰舊路徑，讀取仍以舊路徑為準，新路徑先只做「影子驗證」 |
| **已讀游標的多裝置定義** | 「已讀」在多裝置情境下語意要先定義清楚：是「任一裝置讀過就算已讀」還是「每裝置各自記」？這是產品決策不是技術決策 | 需要善彰先拍板語意，這會影響 P2 的 schema 設計 |
| **舊 App 版本相容性** | 如果有人還在用舊版 App（走 `/app/v1/messages` 舊端點），P4 淘汰舊路徑前必須確認沒有舊版本仍在使用 | 走 `/app/v2/` 前綴、v1 端點先保留，之後再談淘汰時程 |

---

## 6. 建議的決策點（善彰讀完這份文件後需要回答）

1. **要不要啟動路線 B？**（vs 繼續路線 A 疊加式修復，或先做 A 的剩餘項目、B 排在其後）
2. **已讀游標的語意**：多裝置各自記，還是「任一裝置讀過即全部已讀」？
3. **這段重構期間，其他 Pocket App 功能開發要不要暫緩**？（因為 P3 階段會大幅改動核心聊天 UI 檔案，其他人/子程序同時開工風險高）
4. **要不要先做一個範圍更小的 P0+P1（只做 bridge 端 event_log 資料層，不動 App）**，讓你先看到「資料層統一」的效果，再決定要不要繼續往 App 端推進？這樣可以把「先看小成果再決定要不要繼續砸大資源」的風險降到最低。

---

## 附錄：本次查證涉及的現有機制清單（給後續派工的子程序快速定位）

- `bridge.py:739` — `CANON_DB` 路徑定義
- `bridge.py:759` — `_canon_init()`，含 `messages`/`reactions`/`message_meta`/`personas`/`approvals`/`devices`/`report_events`/`delegations` 等既有表結構
- `bridge.py:1170` — `_CANON_VER` 記憶體版本號字典（App 自己訊息的喚醒源）
- `bridge.py:1186` — `_canon_add()`，App 訊息寫入點
- `bridge.py:6875〜6975`（約） — `_hp_canon_follower`/`_state_db_watcher_loop`（本次剛修好的 TG 加速通道）
- `StudioHomeCache.swift:47` — `UnreadStore`（本地未讀計數器，純 UserDefaults）
- `StudioChatUI.swift:6047` — `loadHistoryIfNeeded()`（本次剛修好的本地優先渲染）
- `StudioBridge.swift:688` — `appMessageEvents()`，現有 SSE 訂閱實作（P2/P3 可部分沿用其串流解析邏輯）
