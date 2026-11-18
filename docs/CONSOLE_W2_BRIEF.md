# Console UIUX 升級:W2 交辦單(九條閹割清單)

> 2026-11-18。給接手 agent 的**完整**交辦單 —— 不預設你看過任何對話,
> 每條結論都帶 `file:line`,照著做即可。與現況不符時以程式碼為準並回報差異。

## 紅線(repo 根目錄 CLAUDE.md,務必先讀)

`~/apps/hermes-openwebui-bridge` 跑的是 production 服務 `ai.studio.hermes-bridge`。
**只能 commit+push 到 `feat/console-w2-ops`。不准 merge main、不准 launchctl
重啟任何服務、不准對 production DB 非唯讀寫入、不要跑 bridge 服務。**
工作樹 `~/apps/bridge-console-w2` 已開好,直接在裡面做。

## 這一波的前提:能力閘已經修好了,**用它,不要手刻 provider 分支**

這棵樹是從 `feat/console-p0-caps` 長出來的,`canUse()` 已經改成
fail-open + hide-only(`static/console/app.js:636-651`):

```js
const CAP_VOCAB = new Set(["input", "attachments", "replay", "follow", "approve"]);
function canUse(cap) {
  const caps = currentSession()?.capabilities;
  if (borrowed()) return !!(state.listHealth === "ready" && caps?.includes(cap));
  if (state.listHealth !== "ready" || !CAP_VOCAB.has(cap)) return true;   // 不知道 → 不藏
  return !Array.isArray(caps) || caps.includes(cap);
}
```

**這一波你會加新的 op,每個都要走這條閘**,把對應的詞加進 `CAP_VOCAB`
(只有「缺席真的代表不支援」的詞才可以加 —— 註解裡有 `changes` / `interrupt`
兩個反例,看懂再動)。bridge 的單一真相表是 `bridge.py:25790-25840`
(`_V2_SESSION_OPS`)與 `:25855-25945`(`_PROVIDER_CAPS`),能力查詢端點是
`GET /app/v2/providers/capabilities`(`bridge.py:27391`,payload 由
`_provider_caps_payload()` `:27964` 從 `_V2_SESSION_OPS` 推導 `ops`)。
iOS 的現成範式是 `ProviderCapsStore`(`~/apps/pa-ship` 的
`StudioBridgeV2.swift:690-740`)。

**不准為某個 provider 寫 `if (provider === "codex")`。** 那正是
`_V2_SESSION_OPS` 存在的理由。

## 背景:為什麼有這一波

稽核結論:`_V2_SESSION_OPS` 五個 op,console **一個都沒消費**;bridge 有、
iOS 有、console 沒有的共九條。console 因此只能當「手機的唯讀鏡子」,
不能當主力。以下每條都查證過。

---

## 九條,依痛感排序

### 1. `rename`(痛感最高:列表只能長不能整)
- bridge:`POST /app/v2/sessions/{id}/rename`,路由 `bridge.py:27992`,
  表在 `:25791-25799` —— **5 家支援**(claude_code / cc2 / gemini / grok /
  opencode;codex、openclaw 有架構理由缺席,在 `_V2_OPS_KNOWN_GAPS` 裡)。
- iOS:`StudioBridgeV2.swift:247` → `AgentCardSessionView.swift:173`;
  CC/CX 走 v1(`CCCardSessionView.swift:993`、`CodexCardSessionView.swift:823`)。
- console:**完全沒有**。`.sess` 列只綁 click→開啟(`app.js:335-336`),
  無右鍵、無長按、無溢出選單。
- 做法:`.sess` 加溢出選單(注意 W4 那一波會把 `.sess` 從 `<div>` 改成
  `<button>`,若已合併請沿用)。不支援的 provider 照 `canUse` 藏掉。

### 2. `archive`(做完的工作永遠留在側欄)
- bridge:`POST …/archive`,路由 `:27997`,表 `:25800-25807` —— **6 家全有**。
- iOS:`AgentNamedSessionListView.swift:466`,**含 undo** `:471`。
- console:完全沒有。
- 做法:跟 rename 同一個選單。**undo 一定要做** —— iOS 有,而且誤封存一條
  正在跑的工作沒有退路會很痛。

### 3. `config_read` / `config_write`(網頁上不能換模型)
- bridge:`GET/POST /app/v2/sessions/{id}/config`,`:23143`(codex inline)/
  `:25769`(opencode),寫入路由 `:23198`;表 `:25821-25832`
  (read:codex + opencode;write:codex / cc2 / opencode)。
  CC 另走 `/ccsessions/{name}/model`(`:21851`)與 `/mode`(`:21749`)。
- iOS:`StudioBridgeV2.swift:519`/`:530` → `SessionControlSheet.swift:189`/`:225`。
- console:完全沒有 —— 不能換模型、不能換 effort、不能換 approval_policy、
  不能換 sandbox。
- 做法:一個 sheet。**先做 codex**(出貨最久、formats 最穩),其餘照能力長出來。

### 4. `compact`(一條會話撐不久)
- bridge:`POST …/compact`,`:22966-23055` —— **5 家**(cx / gm / gk / cc2 / cc)。
  docstring 直接寫這是機主 2026-10-10 點的需求。
- iOS:`AgentCardSessionView.swift:110`、`CodexCardSessionView.swift:1040`。
- console:沒有。做法:`index.html:71-76` 的 `.sh-actions` 加一顆。

### 5. `history` 深度(翻到底拿到 409,但 provider 還有逐字稿)
- 現況:`loadOlderCards`(`app.js:595-617`)走 `/cards?before_id=`,只能翻到
  bridge 的 ring(`POCKET_CARD_STORE_MAX=2000`,見 `carddigest.py` 的
  `_CARDS_MAX_DEFAULT`)→ 翻到底 409「這段歷史已超出主機保留範圍」(`:613`)。
- bridge **其實還有**:`GET /app/v2/sessions/{id}/history`,路由 `:28002`,
  表 `:25808-25815` —— **6 家全有**,grok/opencode 還有原生 replay 回退
  (`:25620-25700`)。
- 做法:`older_cursor` 耗盡時接 `/history`,不要讓使用者撞到 409。

### 6. 已讀游標(在網頁上看完,手機紅點不會掉)
- bridge:`POST /app/v2/read`,`:34760`(GET `:34748`);
  `read_cursor.update` 已登記為 persisted 事件(`:3092`,「已讀游標伺服器真相
  — 多裝置未讀一致性」)。
- iOS:`StudioBridgeV2.swift:423`。
- console:**沒有未讀概念**。`#awaiting`(`app.js:740-746`)只數待審核。
- 做法:`trackCardScroll` 貼底時回報。注意 W3 那一波會接
  `read_cursor.update` 的**接收**端,你負責**送出**端 —— 兩邊要對得上。

### 7. 審批 hub 清單(五條 session 各有待審,只看得到第一條)
- bridge:`GET /app/v1/approvals?status=pending`,`:36460`;規範
  `docs/APPROVAL_HUB_SPEC.md:9` 第一句就是「一眼看到**所有 session 全部的
  待審清單**」,§3.1 明定「app 的待審中心從此只打這一條」。
- console:審批區只對**當前打開的那一條** session 渲染(`app.js:666`
  `currentSession()`);`#awaiting` 只有一個數字,點它跳到**第一條**
  (`jumpToAwaiting` `:749-752`);首頁「待我處理」(`:427-428`)用的是 session 列
  不是 approval 物件 → **一條 session 有兩筆待審時只看得到一列**。
- `actionable()`(`:654-663`)的註解說「不必另外打 /app/v1/approvals」——
  那個決定在「每條 session 最多一筆 pending」的世界成立,在 hub 契約下不成立。
- 做法:`#awaiting` 改成可展開的面板,資料來源換成 hub API。
  **注意**:審批卡的渲染剛被另一刀改過(`feat/console-p0-caps`,已在這棵樹裡),
  含 multiselect / danger class / detail+risk。**沿用它的渲染函式,不要重寫。**

### 8. `roster`(狀態只有 3 態)
- 這條 **W3 那一波在做**(`feat/console-w3-state`),你不要碰,列在這裡只是
  讓你知道完整版圖。

### 9. 其餘表外 op(行有餘力)
`/sessions/{id}/key`(原生按鍵,`:23255`)、`/sessions/{id}/media`(`:31390`)、
`/ccsessions/{name}/model|mode`(`:21851`/`:21749`)。

---

## 驗收:**不准只看程式碼**

這個 repo 的教訓是「靜默失敗是最大病原」「驗行為不驗存活」。
前一刀建了離線算圖的 harness,照它的做法走:
`static/console/__fixtures__/approval-preview.html` 是範例(它**抓出貨的
`app.js` 區塊 + 出貨的 `index.html` 標記**,不抄第二份,重跑指令寫在檔頭)。

```
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --headless \
  --disable-gpu --hide-scrollbars --screenshot=out.png --window-size=1200,900 <file-uri>
```

算圖放 `docs/console-w2-preview/`,至少涵蓋:session 溢出選單(支援/不支援
兩種 provider)、config sheet(codex)、hub 展開後的多筆待審、history 接續
後的「更早訊息」。**我要看圖。**

## 守門

- 測試:`PYTHON=~/apps/hermes-agent/runtime/venv/bin/python scripts/run-tests.sh`
  (逐檔跑;這個 repo **不能**直接 `pytest tests/`,見該腳本檔頭說明)。
- 已知無關的紅:`test_persona_description` 在乾淨 main 上同樣紅,**不要去修**。
  逐檔 runner 另有跨檔共用狀態互踩,單跑會過 —— 判斷時以單跑為準。
- 新增測試釘住:每個新 op 都走 `canUse`(不支援的 provider 不畫鈕)、
  archive 的 undo、history 接續不再撞 409、已讀回報不重送。
- 改了 `static/console/` 的檔案要同步 `sw.js` 的 cache 版本(repo 守門會要求)。
- 棘輪(`scripts/ratchet/`)只准往前轉:**不要用 `--update` 把新債變舊債**。

## 回報

commit hash、改檔清單、算圖路徑、測試結果(逐檔 runner 的綠/紅數字,
紅的逐條說明是不是你造成的)。做完就 push `feat/console-w2-ops`。
