# Studio OS — Product Spec

> 善彰的個人 OS:一個像通訊軟體的 App,作為 Studio(cashcamp)上多個 agent 的統一窗口。
> 活文件 — 隨設計逐段更新。狀態:§1 已鎖,§2 討論中。

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
- persona 用 **dev-orchestrator skill** 決定派工 → 呼叫 **bridge 控制 API** → bridge **spawn CC/Codex(headless)** 成子會話 → 註冊進會話清單 → App 列表現身,可串流可續。
- bridge **統一管子程序生命週期 + 列表**(不是 Hermes 自己亂 spawn)。
- 控制通道(OpenAI `/chat` 之外):
  - `GET /sessions` — 列 persona + 子程序會話(含狀態)
  - 續聊(帶 session id)
  - `POST /dispatch` — persona 觸發 CC/Codex

### 2.x 衍生待辦(到 §3 / 實作處理)
- 子會話狀態/事件如何回流到列表(跑中→完成的即時更新)。
- 記憶寫入的權限/審核(CC/Codex 寫 Hermes 記憶要不要把關)。

## §3 UI（討論中）
（待 §3 定案後填入）
