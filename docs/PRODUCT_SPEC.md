# Studio OS — Product Spec

> 善彰的個人 OS:一個像通訊軟體的 App,作為 Studio(cashcamp)上多個 agent 的統一窗口。
> 活文件 — 隨設計逐段更新。狀態:§1–§3 已鎖 + Roadmap;**M1 完成** + **M2 完成**(GET /sessions 控制通道 + 子會話註冊表,真實對話預覽)。**M3 完成**(已從早期 `/dispatch` 升級為 `/app/v1/delegations` durable work-order control plane；Pocket/官方 provider surface 可接手同一 CC/CX session)。**M4 完成**(studio-memory MCP,CC/Codex 讀寫 Hermes 記憶,實測通過)。**M5-a 完成**(persona 品牌化 build 58)。**改版路線 M6–M9 全部完成**(build 59/60)。剩 M5-b(附件)+ 未來項(多通道、APNs、附件)。

## 北極星(一句話)

App 是**我的 Hermes agent 窗口** —— 遠端遙控 Studio、靠 Hermes 的 skill 做事、介面像通訊軟體;
並能讓 Hermes **調度** Studio 上的 Claude Code / Codex,三者**共享 Hermes 長期記憶**。
App 本身只是窗口;未來在窗口內**統整其他通訊軟體 + 多媒體發送**。

---

## §1 架構(LOCKED 2026-06-26)

```
        ┌─────────── App(窗口,通訊軟體式 UI)───────────┐
        │  Hermes personas（可對話）│  被調度的 CC/Codex 子程序（可延續、可見）│
        └───────────┬───────────────────────────┬──────┘
              (Tailscale 遠端)                    │
        ┌───────────┴───────────────────────────┴──────┐
        │           Bridge 層(統一窗口)                  │
        │  OpenAI 介面 + 串流/工具轉錄 + 控制通道(列表/延續/調度) │
        └───────┬───────────────┬───────────────┬───────┘
   Studio  ┌────┴────┐     ┌────┴─────┐    ┌────┴────┐
 (cashcamp)│ Hermes  │──調度─▶│Claude Code│   │  Codex  │
           │ 4 persona│──調度──────────────▶ └────┬────┘
           └────┬────┘     └────┬─────┘         │
                └────── 共享長期記憶層 ──────────┘
                  Hermes `memories/` = 唯一真相 (canonical brain)
```

**鎖定決策:**

1. **記憶是共同地基**:Hermes 的 `memories/` 為**唯一真相 (canonical brain)**;Claude Code 與 Codex 都讀得到(必要時寫)。記憶讀寫介面於 §2 定義。
2. **CC/Codex 接法**:它們是**被 Hermes 調度出來的子程序**;App 不直接點 CC/Codex,而是經 Hermes 派工。
3. **誰是總指揮**:**Hermes 是總指揮**。App 跟 persona 對話 → persona 視需要**調度 CC/Codex 子程序**;該**子程序在 App 列表中可被看到、可延續**(像一條可續的會話)。(取代「App 直連 CC/Codex」)
4. **窗口統一**:Hermes、被調度的 CC/Codex,**全走同一條 bridge + 同一種串流/工具轉錄**(已驗證的 Claude-Code-grade 體感)。介面一致。

**§1 衍生待辦(到 §2 處理):**
- 「子程序變成可延續列表項」的會話模型(session model)。
- 共享記憶層的讀寫介面(CC/Codex 怎麼讀 Hermes memories)。
- bridge 的控制通道(列出會話、延續、觸發調度)。

---

## §2 藍圖(LOCKED 2026-06-26)

### 2.1 會話模型 — 通訊軟體式列表
- 列表 = 一個像 LINE/Telegram 的聊天列表(頭像 + 最後訊息預覽 + 時間 + 搜尋)。
- **Persona 置頂**(袁方/潘天晴/xcash/水鏡 固定在上)。每個 persona = **一條 canonical 現行會話**;App **延續它**(續 Telegram 在驅動的同一條,累積脈絡都在 — 正式解掉「全新 session 沒持倉」的坑)。
- **其下 = 其他 session**:Hermes 調度出的 **CC/Codex 子會話**(掛在對應 persona 概念下,顯示狀態:跑中/完成),依最近活動排序。可點開、串流、延續。
- 未來:統整進來的其他通訊軟體會話也進同一列表(統一收件匣)。
- 會話用穩定 key 識別;bridge 提供「會話列表」。

### 2.2 共享記憶介面 — MCP memory server
- Hermes `memories/` = 唯一真相。包一個 **MCP「memory」server**,讓 Claude Code 與 Codex 雙向讀寫 Hermes 記憶。CC/Codex 都原生支援 MCP → 最乾淨、future-proof。

### 2.3 調度 + 控制通道 — Hermes 經 bridge 派工
- persona 用 **dev-orchestrator skill** 決定派工 → 呼叫 **bridge delegation API** → bridge 建立 Codex app-server 原生 thread 或 Claude Code ccsess session → 寫入 durable work-order registry → App 列表現身,可串流可續。
- bridge **統一管子程序生命週期 + 列表**(不是 Hermes 自己亂 spawn)。
- 控制通道(OpenAI `/chat` 之外):
  - `GET /sessions` — 列 persona + 子程序會話(含狀態)
  - `GET /app/v1/delegations` / `GET /app/v2/sessions` — 列 durable CC/CX work orders
  - 續聊(帶 session id)
  - `POST /app/v1/delegations` — persona 觸發 CC/Codex；`/dispatch` 僅保留 legacy/debug 用途

### 2.x 衍生待辦(到 §3 / 實作處理)
- 子會話狀態/事件如何回流到列表(跑中→完成的即時更新)。
- 記憶寫入的權限/審核(CC/Codex 寫 Hermes 記憶要不要把關)。

## §3 UI(LOCKED 2026-06-26)

- **3.1 導覽**:底部 2 分頁 `聊天 | 設定`(未來再加聯絡人/通道)。主畫面=聊天列表。
- **3.2 聊天列表(home)**:搜尋列 + **置頂 persona 區**(袁方/潘天晴/xcash/水鏡,頭像+預覽+時間)+ 其下 **session 區**(CC/Codex 子會話,副標「父 persona › 工具:任務」,狀態徽章 跑中/完成,依最近活動排序)。扁平列表。
- **3.3 對話畫面**:沿用已驗的 **Claude-Code-grade 轉錄**(🔧工具/↳結果/💭思考/串流答案 + context% + 中斷)。persona 與 CC/Codex 子會話**共用同一畫面**。
- **3.4 輸入區/多媒體**:文字 + 附件(圖/檔進對話)+ 送出/中斷。「從 agent 發多媒體出去」歸未來「多通道」階段。
- **3.5 品牌**:每 persona 頭像(ScarfDesign 素材)+ 配色,實作時做。

---

## Roadmap(實作里程碑)

| M | 內容 | 解決/交付 | 依賴 |
|---|---|---|---|
| **M1** | **會話延續 + 聊天列表 home** | persona 續 canonical 會話(持倉/脈絡回來)+ 通訊軟體式列表(置頂 persona) | — |
| **M2** | **bridge 控制通道 + 會話列表 API** | `GET /sessions`、帶 id 續聊 | M1 |
| **M3** | **CC/Codex 調度 + 子會話** | persona 派工→bridge spawn→子會話進列表、可串流可續 | M2 |
| **M4** | **MCP memory server** | CC/Codex 讀寫 Hermes 記憶 | — |
| **M5-a** | **persona 頭像/品牌**(每人格獨立主色+emoji 頭像,每條對話 tint 吃人格色,子任務按父人格上色) | 質感/識別 build 58 | M1 |
| **M5-b** | 附件(圖/檔進對話) | 多媒體輸入 | M1 |

### 改版路線(M6+,2026-06-27 評估現有 App 後排定 — 全部完成)
| M | 內容 | 狀態 |
|---|---|---|
| **M6** | 即時狀態 + 推播:連線健康膠囊、任務進行中自適應輪詢(4s/20s)、任務完成本地通知 | ✅ build 59 |
| **M7** | 安全硬化:token 移到 Keychain、可在設定頁輪換、bridge 位址可改。HTTPS-via-Serve 暫緩(流量已走 Tailscale WireGuard 加密;Serve 需 GUI/tailnet 後台) | ✅ build 59 |
| **M8** | 子任務可續(bridge 抓 CC session_id、追問 `--resume` 續上下文,實測 X=2 記得)+ app 子任務追問框 + 直接派新任務 composer | ✅ build 60 |
| **M9** | 新 app icon(Hermes 中樞+四人格衛星)+ 觸覺回饋;設定頁於 M7 實做 | ✅ build 60 |
| **F**(未來) | 多通道統整 + 多媒體發送 + 真·遠端 APNs 推播(需 APNs 金鑰)+ M5-b 附件 + persona 多線程(若要) | 待辦 |

註:persona 維持單一連續 thread(像跟一個人持續對話、保住持倉脈絡);「多線程工作台」改由可續的子任務體現。每個 M 完成即 build 上 TestFlight + 更新 spec。
