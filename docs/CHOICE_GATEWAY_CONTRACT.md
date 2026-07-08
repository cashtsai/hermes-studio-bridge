# 選擇閘道公版契約(Choice Gateway Contract)

> 一套按鈕,三種來源。persona 選擇卡、CC/CX 核准、二元審核——使用者眼裡就一種可點卡。
> 相關實作:`carddigest.py`(圍欄抽取 + fallback)、`bridge.py`(approval 卡 options / decision 路由)、
> Pocket App `CardStreamView.swift`(渲染)。persona 端動手要點見 hermes skill `choice-gateway`。

## §1 兩條產生路徑,同一種卡

### A. persona 主動吐選項 —— `studio-card {kind:"choices"}` 圍欄
persona/工具在回覆內嵌一個 fenced 區塊,App 渲染成原生可點按鈕:

````
```studio-card
{
  "kind": "choices",
  "title": "FLiPER 複審卡 | #386274 《…》",
  "detail": "作者:Cecelia\n排程建議:2026-07-25 07:45",
  "ref": "386274",
  "options": [
    { "label": "核准排程", "send": "APPROVE 386274", "style": "primary" },
    { "label": "預覽",     "url": "https://…" },
    { "label": "待檢討",   "send": "HOLD 386274",    "style": "deny" }
  ]
}
```
````

- `kind` ✓ 固定 `"choices"`;`title` ✓;`detail` 內文(可多行 `\n`)。
- `options` ✓ **1–6 個**(超過只渲染前 6,`carddigest`/App 皆截斷)。每個選項二擇一:
  - 動作鈕 `{label, send, style}`:點了把 `send` 文字當一則訊息送回給發起 agent(= Telegram inline keyboard callback 的等價語意)。
  - 連結鈕 `{label, url}`:點了開網址,**不送訊息**。
- `style` ∈ `primary`(核准/發佈)/`secondary`(次要,如預覽/較軟的允許)/`deny`(否決/拒絕/取消)。
- `rows`(選填):按鈕分列佈局,如 `[[0,1],[2]]`。
- `ref`(選填):關聯 id,純標記,App 不解讀。
- **fallback 鐵律**:`send`/`label` 本身即文字備援。不認得 `choices` 的舊 client / Telegram 退成純文字清單照樣看得懂。卡片外不用再貼一份文字選項。

### B. 系統核准卡 —— `kind:"approval"`(Codex / Claude Code / Hermes)
bridge 收到後端的核准請求時,發一張 `approval` 卡,`options` 由**發起方宣告**(去二元),不再寫死:

```json
{
  "kind": "approval", "approval_id": "codex-…", "title": "允許執行 `rm -rf build`?",
  "options": [
    {"key": "approve", "label": "允許執行", "style": "primary"},
    {"key": "approve_for_session", "label": "本次全允許", "style": "secondary"},
    {"key": "deny", "label": "拒絕", "style": "deny"}
  ]
}
```

沒宣告 `options` → `carddigest` 退回二元預設 `[允許, 拒絕]`。

## §2 三態:approve / for_session / deny

第三顆 `approve_for_session`(「本次全允許」)= 允許本次**並**在此 session 不再問同類。

- **判準**:`style=="deny"` 是唯一「拒絕」判準;其餘皆視為允許。App 另以 option key 含 `session` 判定 for_session。
- **bridge 映射**(`_approval_response_result`):
  - `item/commandExecution/requestApproval`、`item/fileChange/requestApproval` → `acceptForSession`(Codex 原生,session 記憶由 Codex 自己維護)。
  - 其他 method → `approved_for_session`。
- **只有支援 acceptForSession 的 method 才給第三顆**;其餘卡維持 approve/deny 二態。
- **向後相容**:key `approve_for_session` 非 deny-ish,舊 App(只認 approve/deny)遇到會安全退成「一般允許」,不會誤送拒絕。

## §3 App → bridge decision 線形

- v2:`POST /app/v2/sessions/{id}/approve` body `{approval_id, approve, for_session?}`
- v1:`POST /app/v1/approvals/{id}/decision` body `{approve, for_session?}`
- CC(Claude Code):走 TUI 鍵——App 送 `{key}`,bridge 轉 `ccKey`;for_session 由 CC prompt 自帶的選項 key 表達,不用 flag。

`for_session`/`approve_for_session`/`remember` 任一為真即視為 session 授權。

## §4 落 Pocket 的可見性(潘天晴案例)

非經 bridge/gateway 的自動卡(如 FLiPER 複審走原生 Telegram Bot API)不進 canonical/state.db → App 看不到。
補救:送 TG 的同時,把卡以 `studio-card {kind:"choices"}` 形態鏡像進 canonical `report_events`
(`fliper_telegram_notify.py`,best-effort、冪等 per stage+post_id),`GET /app/v1/messages` 即讀出。
