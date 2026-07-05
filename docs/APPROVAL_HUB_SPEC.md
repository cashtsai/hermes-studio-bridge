# 統一待審核中心(Approval Hub)規範

> 狀態:**設計稿(待拍板)**。拍板後依 APP_BRIDGE_CONTRACT.md §0 鐵律
> 「任何 app 要消費的形狀,先寫進契約再上線」——本文件內容併入契約新 §12,
> 實作切片進 §9 遷移表。
>
> 動機(2026-07-05,xcash):cc/cx 各種格式的規範跟規則要統一;Hermes 的
> 通知跟審核也要接到同一個待審核中心——一眼看到**所有 session 全部的待審清單**。

## 0. 現況診斷(為什麼要統一)

盤點結論:bridge 端**單一權威其實已經存在**——SQLite `approvals` 表
(三個 provider 都寫入)+ `_approval_decide_core`(以 `source` 前綴分派的
唯一決定路由;v1 decision 與 v2 approve 都匯入它)。推播也已統一
(`_approval_push`,category `SCARF_PENDING_PERMISSION`)。

**分裂在表面層**,目前同一件事有三套形狀:

| 面向 | cc | codex | hermes |
|---|---|---|---|
| 誕生 | tmux pane 輪詢(4s)抓 TUI prompt | app-server JSON-RPC requestApproval | 明確 `POST /app/v1/approvals` |
| v2 sessions 曝露 | `meta.prompt {kind,title,options}` | `meta.approval {id,method,…}` | **完全不見**(恆 idle) |
| v2 approve body | `{key}` | `{approve}` | `{approval_id}` |
| app 決定路徑 | `ccKey`(app 端用 label 猜 deny 的 heuristic) | 讀已廢止的 v2 events 撈 approval_id | v1 decision |
| 卡片流 approval 卡 | 無 | 有(carddigest §2) | 無 |

app 的 `ApprovalCenterView` 因此養了三條決定 closure、一個 CC 專屬
generic-menu 例外、以及決定時的額外 round-trip。推播 action 反而早就
統一走 v1 decision——**in-app 路徑比推播路徑更分裂**是本次要修的核心。

## 1. 統一 Approval 物件(wire shape)

所有 provider 的待審項一律以此形狀曝露(v1 list、v2 meta、推播 payload、
approval 卡片共用同一組欄位名):

```jsonc
{
  "id": "cc-…|codex-…|hp-…",            // provider 前綴 + 短雜湊
  "session_id": "claude_code:Ops",       // §4.1 全寫 session id;取代自由字串 source
  "provider": "claude_code|codex|hermes",
  "kind": "permission|question|notice",  // 見 §2
  "title": "Bash: rm -rf build/",
  "detail": "session:Ops\n…(人話上下文,多行)",
  "risk": "low|medium|high",
  "options": [                            // 與契約 §6 approval 卡 options 同形
    {"key": "approve", "label": "允許", "style": "primary"},
    {"key": "deny",    "label": "拒絕", "style": "danger"}
  ],
  "created_at": 1783200000.0,
  "expires_at": 1783203600.0,
  "status": "pending|approved|denied|answered|acknowledged|expired"
}
```

規則:

- `source` 自由字串**廢除**(相容期仍回填),權威欄位是
  `session_id` + `provider`。routing 只 split 第一個 `:`(同契約 §4.1)。
- `options` 是**決定的唯一詞彙**:app 端永不再用 label 猜語意
  (現行 `deny/拒絕/no` heuristic 廢除)。bridge 產 options 時負責語意標註
  (`style: danger` = 否決類)。
- cc 的 TUI 選單選項:`key` 即 TUI 按鍵(現況不變),但**由 bridge 標好
  style/語意**,app 只渲染。

## 2. kind:三種待審語意

| kind | 語意 | options | 決定後 status |
|---|---|---|---|
| `permission` | 允許/拒絕某動作(cc 權限、cx exec/patch、hermes 高風險工具) | 恰兩鍵 approve/deny(cc 可多鍵,但必標 style) | `approved` / `denied` |
| `question` | 多選一問答(cc AskUserQuestion 泛用選單) | 2–N 鍵,無 danger 語意 | `answered`(result=key) |
| `notice` | **通知型**:只需「知道了」,無分支(hermes 日報、跑完通知等) | 單鍵 `ack`/「知道了」 | `acknowledged` |

> `notice` 就是「Hermes 的通知接進同一中心」的載體:通知不再只是推播即逝,
> 而是中心裡一條可勾銷的列;勾銷即 decision(key=ack),同樣走
> `_approval_decide_core`,同樣可帶 callback。

## 3. 端點(v1 為權威,v2 為 facade)

### 3.1 清單(中心的資料來源,一條 API 看全部)

```
GET /app/v1/approvals?status=pending[&provider=][&kind=]
→ {"approvals": [統一物件…], "total": n, "next_offset": …}
```

- 回傳**所有 provider**(現況已是)+ 新欄位 `session_id/provider/kind/options`。
- app 的待審中心從此**只打這一條**;現行三路
  (`/ccsessions` filter awaiting + v2 waiting_approval + v1 approvals)退役。

### 3.2 決定(唯一寫入路徑)

```
POST /app/v1/approvals/{id}/decision   body {"key": "approve"}
```

- **以 option key 決定**。`{"approve": bool}` 保留為相容糖,bridge 端映射
  (approve→第一個 primary、deny→第一個 danger),app 新版不再送 bool。
- 409 語意不變:已決定回 `APPROVAL_ALREADY_DECIDED`、prompt 已消失/換頁回
  `APPROVAL_NOT_PENDING`。
- 推播 action、in-app 中心、session 內 approval 卡片**三個入口同一條路**。

### 3.3 v2 facade 收斂

- `GET /app/v2/sessions`:三 provider 的 pending 一律曝露成
  `meta.approval = 統一物件`(cc 的 `meta.prompt` 併入其中保留相容期;
  hermes persona **補上** waiting_approval 狀態與 meta.approval——現況恆
  idle 是缺口)。
- `POST /app/v2/sessions/{id}/approve`:body 統一
  `{"approval_id": "…", "key": "approve"}`;三種舊 body(`{key}`/`{approve}`/
  `{approval_id}`)相容期照收,內部全部轉呼 `_approval_decide_core`。

### 3.4 誕生(hermes / skill 接入)

```
POST /app/v1/approvals
body {title, session_id, kind, risk?, detail?, options?, ttl_seconds?, callback_url?}
```

- 現有端點升級:`source`→`session_id`,新增 `kind`(預設 permission)與
  `options`(預設 approve/deny;`kind=notice` 預設單鍵 ack)。
- Hermes 側約定:persona/skill 要求人工把關 → `kind=permission`;
  日報/完工通知/提醒 → `kind=notice`。決定經 `callback_url` 推回
  (現有 `_approval_fire_callback` 不變)。

## 4. 卡片流:approval 卡三 provider 補齊

- 契約 §6 的 `approval` 卡(`{approval_id, title, options, source,
  fallback_text}`)目前**只有 codex 會發**。統一後:三 provider 的 pending
  在其 session 卡片流都 emit approval 卡、決定後 upsert 為 resolved
  (carddigest 現成 `handle_approval/resolve_approval` 模式套用到 cc watcher
  與 hermes create)。
- 卡上的 `options` 與 §1 同物;卡片內一鍵決定與中心決定等價
  (都打 3.2)。

## 5. App 端收斂(pocketagent)

1. `ApprovalCenterView`:三 section 併成**單一清單**(排序:risk desc →
   created_at asc),每列 = provider 徽章 + title + session 跳轉 + inline
   options 鍵。資料源只剩 `GET /app/v1/approvals?status=pending`。
2. 三條決定 closure(`decideHermes/decideCC/decideV2`)併成一條:
   `POST decision {key}`。刪 `ccKey` label-heuristic、刪讀廢止 v2 events
   撈 id 的路徑。
3. CC generic 選單(AskUserQuestion)= `kind=question`,中心內直接選,
   不再特例跳轉(跳轉保留為 detail 動作)。
4. `notice` 列渲染為單鍵「知道了」;勾銷後從 pending 清單消失。
5. 待放行角標:各 session 列表的「待放行 · N」計數改讀同一條 v1 清單
   (per-session filter),數字與中心永遠一致。

## 6. 遷移切片

| 切片 | 內容 | 依賴 |
|---|---|---|
| A1 | bridge:approvals 表加 `session_id/provider/kind/options` 欄(migration+回填),v1 list/decision 曝露新欄位;`{key}` 決定;v2 meta.approval 統一(含 hermes 補 waiting_approval) | 無 |
| A2 | app:中心單清單+單決定路徑;角標改同源 | A1 |
| A3 | bridge:cc/hermes 補 approval 卡片流;hermes `kind=notice` 通知接入(挑 1–2 個 cron 報告先行) | A1 |
| A4 | 相容期收尾:刪三種舊 v2 approve body、刪 `meta.prompt`、刪 app 舊三路輪詢 | A2+A3 全上線一版後 |

## 7. 驗收基準

1. 同時掛起一條 cc 權限、一條 cx exec、一條 hermes `notice` →
   `GET /app/v1/approvals?status=pending` 一次回三筆統一物件;
   中心一個清單三列,各自一鍵決定成功,三筆都經 `_approval_decide_core`。
2. 決定的四個入口等價:中心 inline 鍵、session 內 approval 卡、推播 action、
   (hermes)callback——同一筆只能成功一次,其餘 409。
3. cc AskUserQuestion 泛用選單在中心直接作答,答案送達 TUI(對 pane 驗證)。
4. 舊 client(現行 build)在 A1 上線後不壞:v1 舊欄位仍在、三種舊 v2 body 仍收。
5. hermes persona 有 pending 時,v2 sessions 該列 `status=waiting_approval`
   且 capabilities 動態含 `approve`。
