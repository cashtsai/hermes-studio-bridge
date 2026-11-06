# Orchestrator MCP 工具面 v1(`POST /mcp`)

更新日期:2026-11-06

把 bridge 的編排能力做成 agent 可呼叫的 MCP 工具 —— **agent 編排 agent**。
一個已經在跑的 CC/CX session 掛上這個 server 之後,可以自己開子會話、
派工、盯進度、收割結果,全程走 bridge 既有的 delegation/input/event_log
路徑,不開任何新的執行面。

設計依據:

- Codex #106(`681db30`)的 MCP 契約:`/app/v3/orchestrator/*` facade +
  `scripts/agent-call-mcp.py` 的 typed T3Code tools(工具只轉送白名單欄位)。
- t3code `docs/orchestration-v2/orchestrator-mcp-server.md` 的 HTTP MCP 模式:
  MCP server 只是 **command ingress**,所有權限、配額、audit 都在既有
  編排路徑上強制;工具呼叫帶的 scope 不得超過呼叫者自己的(權限只縮不放)。

## 傳輸與認證

- 端點:`POST http://127.0.0.1:8081/mcp`(JSON-RPC over HTTP;無 SSE,
  每個 request 一問一答)。
- 支援方法:`initialize`、`tools/list`、`tools/call`、`ping`;
  `notifications/*` 回 202。協定版本收 `2024-11-05 / 2025-03-26 / 2025-06-18`,
  其他值回 server 預設 `2025-06-18`。
- **認證:只收 master `BRIDGE_TOKEN`**(launchd plist 裡那把)。
  手機配對出來的 device token 會被 `_require_master_auth` 擋成 403 ——
  這是機器對機器的編排面,v1 一刀切,不給裝置級憑證任何編排權。
- 工具執行失敗走 MCP 的 `isError: true`(result 層,content 內是
  `{"error":{code,status,message}}` JSON);只有協定層錯誤(未知 method)
  才回 JSON-RPC error(-32601)。

## 權限模型(v1,一刀切)

| 護欄 | 實作 |
|---|---|
| 呼叫者身分 | master `BRIDGE_TOKEN`(403 擋 device token) |
| 並行 delegation 上限 | **3**(`ORCH_MCP_MAX_PARALLEL` 可調);記帳基準 = delegation registry 裡 `created_via=orchestrator_mcp` 且 runtime 非終態者,超額回 429 `ORCH_MCP_DELEGATION_LIMIT` |
| audit | 每顆 `pocket_open_thread` 都落 `delegations` 表(work_order `*-ORCH-MCP-*`、`created_via=orchestrator_mcp`),並寫一筆 `thread.delegated` 事件進 event_log |
| cwd | 沿用 `_normalise_workdir`:必須在 home 底下、不准用 home 本身 |
| 深度/子額 | 沿用 `app_delegation_create` 既有護欄(chain depth ≤2、同父 running 子任務 ≤3、registry 配額) |

記帳看 **runtime 狀態**(與 `/app/v3/orchestrator/delegations/{id}/await`
同一組終態:`idle/done/failed/archived/cancelled/denied/expired`),不看
registry 靜態 status 欄位 —— 靜態欄位不保證回寫,拿它記帳會把做完的舊單
永久算成活的,配額就鎖死了。

## 工具(四顆,全部復用既有能力)

### `pocket_open_thread` → 開新會話

| 欄位 | 型別 | 說明 |
|---|---|---|
| `provider` | string,必填 | `codex` 或 `claude_code`(`cx`/`cc` 別名也收) |
| `prompt` | string,必填 | 開場任務指令(= 第一回合輸入) |
| `cwd` | string | 工作目錄(home 底下;省略用預設) |

走 `app_delegation_create`(codex = app-server `thread/start`、cc = ccsess
tmux session),task_code 固定 `ORCH`、subtask_code 固定 `MCP`、parent
persona 預設 `xcash`(`ORCH_MCP_PARENT` 可調)。

回:`{ok, session_id, delegation_id, work_order, provider, status, event_seq}`
— `session_id` 形如 `codex:<thread_id>` / `claude_code:<name>`,直接餵給
下面三顆工具。

### `pocket_send` → 往會話送訊息

| 欄位 | 型別 | 說明 |
|---|---|---|
| `session_id` | string,必填 | `codex:<thread>` / `claude_code:<name>` / `cc2:<name>` |
| `text` | string,必填 | 訊息內容 |
| `client_id` | string | 冪等鍵:重試帶同一個,避免重複執行 |

走統一 input 路由 `POST /app/v2/sessions/{id}/input` 的同一個 handler:
cc=tmux bracketed paste、cc2=SDK submit、cx=`turn/start`,對方忙碌會自動
排隊(回 `{delivery:"queued"}`),不回 4xx。

### `pocket_status` → 讀狀態(唯讀,不打擾對方)

| 欄位 | 型別 | 說明 |
|---|---|---|
| `session_id` | string,必填 | 同上 |

從 event_log 撈最後一筆 `session.status` + 最後一則 assistant 里程碑卡。
回:

```json
{"ok": true, "session_id": "codex:…",
 "phase": "run|idle|queued|blocked|unknown", "busy": true,
 "label": "執行中…",
 "last_assistant_milestone": {"ts": 1767…, "text": "…(≤2000 字)"},
 "last_event_seq": 12345}
```

`phase=unknown` = 這條 session 還沒有任何 status 事件(例如剛開、或不是
卡片流管的 session)。

### `pocket_wait_idle` → 等回合結束

| 欄位 | 型別 | 說明 |
|---|---|---|
| `session_id` | string,必填 | 同上 |
| `timeout_secs` | number | 1–600,預設 120(超出會被夾回範圍) |

輪詢 event_log 直到 `phase=idle`(回合結束)**或 `phase=blocked`**(對方
在等輸入/核准 —— 不會自己前進,立刻把球交回編排方,`phase` 說明原因),
或逾時。回 `{ok, timed_out, phase, busy, label, last_assistant_milestone, …}`;
**逾時不是工具錯誤**(`isError=false`、`timed_out=true`),也**不會取消**
對方的工作 —— 要收割就之後再 `pocket_status`/`pocket_wait_idle` 一次。

## 怎麼掛給 CC / CX 當 MCP server

token 都從 launchd plist 的 `BRIDGE_TOKEN` 拿;**不要**把 token 寫進 repo
或聊天。以下註冊動作屬 production 設定,由善彰核准後手動執行。

### Claude Code(HTTP transport,原生支援)

```sh
claude mcp add --transport http pocket-orchestrator \
  http://127.0.0.1:8081/mcp \
  --header "Authorization: Bearer <BRIDGE_TOKEN>"
```

或 `.mcp.json`:

```json
{"mcpServers": {"pocket-orchestrator": {
    "type": "http",
    "url": "http://127.0.0.1:8081/mcp",
    "headers": {"Authorization": "Bearer <BRIDGE_TOKEN>"}}}}
```

### Codex(`~/.codex/config.toml`)

新版 Codex 支援 streamable HTTP MCP client:

```toml
[mcp_servers.pocket_orchestrator]
url = "http://127.0.0.1:8081/mcp"
bearer_token_env_var = "BRIDGE_TOKEN"
# 舊版 codex 需要:experimental_use_rmcp_client = true
```

舊版只吃 stdio 的話,用 `mcp-remote` 代理(stdio→HTTP,純轉發):

```toml
[mcp_servers.pocket_orchestrator]
command = "npx"
args = ["-y", "mcp-remote", "http://127.0.0.1:8081/mcp",
        "--header", "Authorization: Bearer ${BRIDGE_TOKEN}"]
```

### 手動冒煙(curl)

```sh
TOKEN=$(/usr/libexec/PlistBuddy -c \
  'Print :EnvironmentVariables:BRIDGE_TOKEN' \
  ~/Library/LaunchAgents/ai.studio.hermes-bridge.plist)
curl -s http://127.0.0.1:8081/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

## 典型編排迴圈(給掛上來的 agent 看)

1. `pocket_open_thread {provider:"codex", prompt:"<工單>", cwd:"~/apps/x"}`
   → 記下 `session_id`。
2. `pocket_wait_idle {session_id, timeout_secs: 600}` → 看
   `last_assistant_milestone.text`。
3. `timed_out=true` → 對方還在跑,過一陣子再 wait 一次(**不要**重複開單)。
   `phase=blocked` → 對方在等輸入:用 `pocket_status` 看 label,該補資訊就
   `pocket_send`,該人工核准就回報使用者。
4. 要追加指示/追問 → `pocket_send`(重試帶同一個 `client_id`)。
5. 開新單撞到 429 `ORCH_MCP_DELEGATION_LIMIT` → 先收割(wait/status)
   既有會話;上限 3 是故意的,別繞。

## 測試與驗證

```sh
~/apps/hermes-agent/venvs/hermes-0.22.2/bin/python -m pytest \
  tests/test_orchestrator_mcp.py -q
```

涵蓋:協定三式與 tools/list 形狀、四工具 happy path(monkeypatch
create/input,status/wait 走真 event_log)、並行上限 3(含「終態不佔額」)、
auth(401/裝置 token 403)、wait 逾時語意。
