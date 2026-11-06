# AppThread ≠ ProviderThread 雙層 ID(疊加層設計)

> 分支:`feat/appthread-dual-id`(2026-11-06)。
> 思路來源:t3code `docs/orchestration-v2/core-graph-and-data-model.md`
> (AppThread / ProviderThread 分離);解剖報告見
> `pa-ship/docs/T3CODE_RESEARCH_20261103.md`「殺手級設計」節。

## 要治的病(真實病史)

1. **CX thread 鎖事件**:桌面 app 搶走 Codex 的 writer lock,provider-native
   thread 整條報廢 —— 在單層 ID 世界裡,app 的「對話」就是那條原生 thread,
   原生 thread 死了對話陪葬,使用者眼裡是「我的對話壞了」。
2. **cc2 session 重啟換 ID**:`provider_session_id` 重啟後換新,同一個對話
   在 app 端斷成兩截,歷史接不起來。

共同根因:**app 對話身分和 provider 原生會話身分是同一個 ID**。t3code 的
答案是拆成兩層 —— app 對話身分(AppThread)穩定不變,底下的 provider
原生會話(ProviderThread)是**可替換的綁定**。原生會話死了只是換一個
ProviderThread;對話不陪葬。

## 資料模型(t3code_runtime.py,純函式 + SQLite)

既有 phase2 已落的 `app_threads`(id/owner/title/status/created_at/
updated_at/version)直接沿用為 AppThread 本體;本輪新增的是**綁定帳本**:

```
app_thread_bindings(
  id                  TEXT PK            -- bind_<hex>
  app_thread_id       TEXT → app_threads(id)
  provider            TEXT               -- codex / claude_code / …
  provider_thread_id  TEXT               -- bridge 既有 session id(見相容節)
  state               TEXT               -- active | dead | detached
  reason              TEXT               -- 換綁原因(audit)
  bound_at            REAL
  unbound_at          REAL               -- 離開 active 的時刻
)
```

### 不變量(DB 層 partial unique index 直接守,不是只靠 Python)

- `idx_binding_active_thread`:一個 app thread 同時**至多一個** active 綁定。
- `idx_binding_active_provider`:同一個 provider thread **不准**同時 active
  綁兩個 app thread(防「一條原生會話被兩個對話搶」)。

### 狀態機

```
(無綁定) --bind--> active --rebind--> dead      (原生會話死了/鎖死)
                        \--rebind--> detached  (刻意換手,原生會話還活著)
```

- **換綁 = 舊列標 dead/detached + 新列 active**,同一交易落地;歷史列永不
  改寫、永不刪除,`list_bindings` 就是完整 audit 鏈。
- rebind 到同一目標是冪等 no-op(`changed: false`);沒有 active 綁定時
  rebind 等同 bind。
- 純函式面:`create_app_thread` / `bind_app_thread` / `current_binding` /
  `list_bindings` / `rebind_app_thread` / `active_binding_owner`。

與 phase2 既有 `provider_threads` 表的關係:`provider_threads` 是 round2
留下的「provider 原生 thread 目錄」(native_id + capabilities,context
transfer 的掛點);`app_thread_bindings` 回答的是另一個問題 ——「**此刻**
這個對話由哪條原生會話撐著、歷史上換過幾手、為什麼換」。兩表並存,
不互相遷移。

## 端點(/app/v2/threads,新增不動舊)

| 端點 | 作用 |
| --- | --- |
| `POST /app/v2/threads` | 建 app thread;可同時給 `provider` + `provider_thread_id` 一步綁上既有 session。綁定目標被佔時整個請求 400,不留孤兒 thread。 |
| `GET /app/v2/threads/{id}` | thread 本體 + `binding`(當前)+ `bindings`(完整鏈)。 |
| `POST /app/v2/threads/{id}/rebind` | `{provider, provider_thread_id, reason?, prior_state?=dead}`;真換綁時落 event_log 事件 **`thread.rebound`**(session=app thread id),/app/v2/events 訂閱者即時看到。 |

全部走 `_check_auth`(master token / device token 同舊規)。

## 相容紅線(疊加層,不是取代層)

- 現有 session id(`codex:xxx` / `claude_code:name` / cc2 名字)**照舊全通**;
  binding 的 `provider_thread_id` 存的就是這些 bridge 既有 session id,
  app 消費端拿它去走所有舊路徑(卡片流/輸入/核准…)。
- `/app/v2/sessions` 列表**一個位元不動**;沒有任何舊端點讀這兩張表。
- 不建 app_thread 一樣什麼都能用 —— 這層只對想要「對話不陪葬」語意的
  消費端(app 下一波)生效。

## 「CX thread 鎖死」在這層怎麼續命

1. app 對話 = `app_thread`(穩定 ID),active binding 指向 `codex:abc`。
2. 桌面 app 搶走 writer lock,`codex:abc` 整條死透。
3. 消費端(或守護邏輯)在 bridge 開一條新 Codex session(`codex:fresh`),
   然後 `POST /app/v2/threads/{id}/rebind
   {provider: codex, provider_thread_id: codex:fresh, reason: "writer lock stolen", prior_state: dead}`。
4. 對話 ID 不變、標題不變、audit 鏈多一節(`codex:abc` 標 dead + reason);
   `thread.rebound` 事件推到訂閱端,UI 原地續命 —— 使用者看到的是
   「同一個對話換了引擎」,不是「對話壞了」。

cc2 重啟換 `provider_session_id` 同理:rebind 一下,舊綁定標 `detached`。

## 下一步:CC↔CX 接力(ContextTransfer)

換綁只保住**身分**;要把**上下文**也搬過去(CC 做到一半換 CX 接手),走
phase2 已落地的 checkpoint / context transfer 契約:

- `context_transfers` 狀態機(dry_run → approved → target_created → applied,
  secret 拒收、envelope hash 冪等):`docs/T3CODE_PHASE2_PROVIDER_CONTRACT_20261105.md`、
  `docs/T3CODE_ROUND2_IMPLEMENTATION_20261105.md`。
- checkpoint(唯讀 manifest → 核准後 hidden git ref → 乾淨 worktree 還原):
  同上契約文件;「provider 會話回不去就不准動檔案系統」的那道閘。

接力的完整形狀 = **rebind(身分)+ context transfer(上下文)**:
transfer `applied` 之後對新目標 rebind(prior_state=detached),audit 鏈上
兩個原語各留一節。app 消費端設計屬下一波,t3code 參考:
`docs/orchestration-v2/thread-lineage-and-context-transfer.md`。
