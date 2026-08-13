#!/usr/bin/env python3
"""agent-call MCP stdio server — 讓 CC/codex 以工具形式互調其他 agent(1c)。

這是 bridge `POST /app/v2/agent_call` 家族端點的極薄 MCP 包裝(純 stdlib,
無第三方依賴):所有權限控制(AGENT_CALL 旗標、allowlist 政策、深度/循環/
預算護欄、audit 卡)都在 bridge 側強制,本腳本只是傳聲筒。

暴露工具:
  agent_call(target, message, mode?, timeout_secs?)  # 調用另一個 agent
  agent_list()                                       # 政策放行的可調用對象
  agent_result(call_id)                              # background 模式收割
  agent_context(target, mode?, limit?)               # 讀另一個 session 的上下文
  agent_context_search(query, target?, limit?)       # 在可讀範圍內找關鍵字
  agent_context_list()                               # 讀得到誰、能讀到什麼程度
  propose_calendar_event(title, start, tz, …)        # 提議行事曆事件(要使用者確認)
  propose_reminder(title, tz, due?, …)               # 提議提醒事項(要使用者確認)

── 行事曆/提醒:你只能「提議」,不能寫 ─────────────────────────────────────
  `propose_*` **不會**動到使用者的行事曆,只會在他手機上出現一張確認卡;
  他按下去才由 Pocket 用 EventKit 寫入,結果會回填成同一張卡的狀態
  (✅ 已加入 / ✖️ 已略過)。所以:
  - **時間你自己算好**:傳 epoch 秒 + IANA 時區名(`tz` 必填,bridge 不猜)。
    今天幾號、使用者說「下週三下午三點」是幾號 —— 那是你的工作,不是 bridge 的。
  - 送出後**不要**假設已經加成功;要確認就過一陣子讀自己的卡片流看狀態。
  - 前提 = bridge 端 `CALENDAR_PROPOSALS=1`(獨立旗標),沒開就是 404。

── 上下文互讀怎麼用(接手/協作的正確順序)────────────────────────────────
  1. `agent_context_list()` 看自己讀得到哪些 session、各自允許哪些 mode。
  2. 先 `agent_context(target)`(預設 mode=summary):蒸餾過的交接簡報,
     有快取,對方沒新動靜時重讀不花錢也不吵到對方。
  3. 摘要不夠再 `agent_context(target, mode="recent", limit=40)` 拿原文卡片,
     或 `agent_context_search(query="檔名或關鍵字")` 定位。
  **每次讀取都會在對方的卡片流留下一張「👁 已被讀取」的卡**,使用者看得到;
  原文模式(recent/search)預設權限比 summary 嚴,拿到 403 是正常的,
  請退回 summary,不要反覆重試(有速率上限)。
  前提 = bridge 端 `AGENT_CONTEXT=1`(與 AGENT_CALL 各自獨立的旗標)。

環境變數:
  BRIDGE_URL        bridge 位址(預設 http://127.0.0.1:8081)
  BRIDGE_TOKEN      bridge bearer token(必填;launchd plist 裡那把)
  AGENT_CALL_SELF   本 session 的 v2 id,作為 caller 記名(必填,
                    例 claude_code:tirith / codex:<thread_id> / hermes:yuanfang)
  AGENT_CALL_PARENT 選填:明示 parent_call_id(通常不用,bridge 會自動推斷 chain)

── 註冊到 Claude Code(手動,絕不自動改善彰的 live 設定)──────────────────
  claude mcp add agent-call \
    -e BRIDGE_URL=http://127.0.0.1:8081 \
    -e BRIDGE_TOKEN=<token> \
    -e AGENT_CALL_SELF=claude_code:<本 lane 名> \
    -- python3 /Users/xcash/apps/hermes-openwebui-bridge/scripts/agent-call-mcp.py

  或 .mcp.json:
  {"mcpServers": {"agent-call": {
      "command": "python3",
      "args": ["/Users/xcash/apps/hermes-openwebui-bridge/scripts/agent-call-mcp.py"],
      "env": {"BRIDGE_URL": "http://127.0.0.1:8081",
              "BRIDGE_TOKEN": "<token>",
              "AGENT_CALL_SELF": "claude_code:<本 lane 名>"}}}}

── 註冊到 Codex(~/.codex/config.toml)────────────────────────────────────
  [mcp_servers.agent_call]
  command = "python3"
  args = ["/Users/xcash/apps/hermes-openwebui-bridge/scripts/agent-call-mcp.py"]
  env = { BRIDGE_URL = "http://127.0.0.1:8081", BRIDGE_TOKEN = "<token>",
          AGENT_CALL_SELF = "codex:<thread_id>" }

── 註冊到 Hermes persona(config 層,不碰 hermes_cli 內核)────────────────
  hermes_cli 有 `hermes mcp add`(設定落 $HERMES_HOME/config.yaml 的
  mcp_servers 鍵):在該 persona 的 HERMES_HOME 下執行
  `hermes mcp add agent-call --command python3 --args <本腳本路徑> \
     --env BRIDGE_TOKEN=<token> --env AGENT_CALL_SELF=hermes:<persona>`
  (實際旗標以 `hermes mcp add --help` 為準)。這屬於 persona production
  設定,由善彰核准後手動執行,本腳本/bridge 不代寫。

註:啟用前提 = bridge 端 AGENT_CALL=1 + 政策檔放行,缺一即 404/403。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BRIDGE_URL = (os.environ.get("BRIDGE_URL") or "http://127.0.0.1:8081").rstrip("/")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN") or ""
SELF_ID = os.environ.get("AGENT_CALL_SELF") or ""
PARENT_CALL = os.environ.get("AGENT_CALL_PARENT") or ""

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "agent_call",
     "description": ("調用另一個 agent(persona/cc/codex/openclaw),經 bridge "
                     "統一路由與權限控制。mode=await_reply 會等回覆(預設 120s,"
                     "逾時轉 background);fire_and_forget 只投遞;background 回 "
                     "call_id,之後用 agent_result 收割。"),
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string",
                    "description": "目標 session id(如 hermes:yuanfang、"
                                   "claude_code:tirith、codex:<thread>)"},
         "message": {"type": "string", "description": "要傳給對方的訊息"},
         "mode": {"type": "string",
                  "enum": ["fire_and_forget", "await_reply", "background"],
                  "description": "預設 await_reply"},
         "timeout_secs": {"type": "number",
                          "description": "await_reply 等待秒數(5–600)"}},
         "required": ["target", "message"]}},
    {"name": "agent_list",
     "description": "列出政策放行、本 agent 可調用的對象(含 busy 狀態與用途)。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "agent_result",
     "description": "以 call_id 收割 background / 逾時轉背景的調用結果。",
     "inputSchema": {"type": "object", "properties": {
         "call_id": {"type": "string"}}, "required": ["call_id"]}},
    {"name": "agent_context",
     "description": ("讀另一個 agent session 的上下文,用來接手或協作前先補齊"
                     "資訊落差(不會打擾對方、不佔對方回合)。"
                     "mode=summary(預設)= 蒸餾過的交接簡報(在做什麼/目前狀態/"
                     "關鍵決策與檔案/未解問題),有快取;mode=recent = 最近 N 張"
                     "卡的原文。原文權限比摘要嚴,403 就退回 summary。"
                     "秘密會被 bridge 遮罩;每次讀取都會在對方卡片流留痕。"),
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string",
                    "description": "要讀的 session id(如 claude_code:tirith)"},
         "mode": {"type": "string", "enum": ["summary", "recent"],
                  "description": "預設 summary"},
         "limit": {"type": "number",
                   "description": "recent 模式回幾張卡(預設 20,上限 100)"}},
         "required": ["target"]}},
    {"name": "agent_context_search",
     "description": ("在自己讀得到的 session 範圍內做關鍵字/子字串搜尋,回命中"
                     "片段 + 是哪個 session + 時間。省略 target 就掃全部可讀"
                     "範圍(只掃記憶體裡熱的 session)。"),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "關鍵字/子字串"},
         "target": {"type": "string",
                    "description": "選填:只搜某一個 session"},
         "limit": {"type": "number", "description": "命中數上限(預設 30)"}},
         "required": ["query"]}},
    {"name": "agent_context_list",
     "description": "列出本 agent 讀得到的 session,以及各自允許的讀取模式。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_calendar_event",
     "description": ("提議一個行事曆事件 —— **不會寫入**,只會在使用者手機上"
                     "出現一張確認卡,他按下去才由 app 寫進行事曆。"
                     "時間一律由你算好:`start`/`end` 是 epoch 秒,`tz` 是 "
                     "IANA 時區名(如 Asia/Taipei,必填,bridge 絕不猜)。"
                     "沒給 end 預設 +1 小時。"),
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "事件標題(≤200 字)"},
         "start": {"type": "number", "description": "開始時間(epoch 秒)"},
         "tz": {"type": "string",
                "description": "IANA 時區名,如 Asia/Taipei(必填)"},
         "end": {"type": "number", "description": "結束時間(epoch 秒);"
                                                  "省略 = start + 1 小時"},
         "notes": {"type": "string", "description": "備註(≤2000 字)"},
         "location": {"type": "string", "description": "地點(≤200 字)"},
         "all_day": {"type": "boolean", "description": "整天事件"},
         "alarm_minutes_before": {
             "type": "array", "items": {"type": "number"},
             "description": "提前幾分鐘提醒(最多 5 項,如 [10])"},
         "recurrence": {"type": "string",
                        "enum": ["none", "daily", "weekly", "monthly"],
                        "description": "重複規則,預設 none"},
         "session": {"type": "string",
                     "description": "選填:提案卡要落在哪條 session"
                                    "(預設 = 自己)"}},
         "required": ["title", "start", "tz"]}},
    {"name": "propose_reminder",
     "description": ("提議一則提醒事項(Reminders)—— 同樣**不會寫入**,"
                     "使用者在手機上確認才生效。`due` 是 epoch 秒(可省略,"
                     "省略即無期限提醒),`tz` 必填。"),
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "提醒標題(≤200 字)"},
         "tz": {"type": "string",
                "description": "IANA 時區名,如 Asia/Taipei(必填)"},
         "due": {"type": "number", "description": "到期時間(epoch 秒)"},
         "notes": {"type": "string", "description": "備註(≤2000 字)"},
         "all_day": {"type": "boolean", "description": "只到日期、不含時刻"},
         "alarm_minutes_before": {
             "type": "array", "items": {"type": "number"},
             "description": "提前幾分鐘提醒(最多 5 項)"},
         "recurrence": {"type": "string",
                        "enum": ["none", "daily", "weekly", "monthly"],
                        "description": "重複規則,預設 none"},
         "session": {"type": "string",
                     "description": "選填:提案卡要落在哪條 session"
                                    "(預設 = 自己)"}},
         "required": ["title", "tz"]}},
]

_PROPOSAL_FIELDS = ("title", "start", "end", "due", "notes", "location",
                    "all_day", "alarm_minutes_before", "recurrence", "tz")


def _proposal_body(target: str, args: dict) -> dict:
    """只挑契約認得的欄位往上送(agent 亂加的 key 不外流),空值不送 ——
    讓 bridge 的預設值(end=+1h、recurrence=none)生效。"""
    body = {"target": target}
    for key in _PROPOSAL_FIELDS:
        val = args.get(key)
        if val is None or val == "" or val == []:
            continue
        body[key] = val
    return body


def _http(method: str, path: str, body: dict | None = None,
          timeout: float = 630.0) -> dict:
    req = urllib.request.Request(
        BRIDGE_URL + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            detail = {"error": str(e)}
        return {"_http_error": e.code, **(detail if isinstance(detail, dict)
                                          else {"detail": detail})}
    except Exception as e:  # noqa: BLE001
        return {"_http_error": 0, "error": f"bridge 連不上:{e}"}


def _tool_call(name: str, args: dict) -> dict:
    if not SELF_ID:
        return {"isError": True, "text": "AGENT_CALL_SELF 未設定(caller 記名必填)"}
    if name == "agent_call":
        body = {"caller": SELF_ID,
                "target": str(args.get("target") or ""),
                "message": str(args.get("message") or ""),
                "mode": str(args.get("mode") or "await_reply")}
        if args.get("timeout_secs") is not None:
            body["timeout_secs"] = args["timeout_secs"]
        if PARENT_CALL:
            body["parent_call_id"] = PARENT_CALL
        res = _http("POST", "/app/v2/agent_call", body)
    elif name == "agent_list":
        res = _http("GET", f"/app/v2/agent_targets?caller={SELF_ID}")
    elif name == "agent_result":
        cid = str(args.get("call_id") or "")
        res = _http("GET", f"/app/v2/agent_call/{cid}")
    elif name == "agent_context":
        body = {"caller": SELF_ID, "target": str(args.get("target") or ""),
                "mode": str(args.get("mode") or "summary")}
        if args.get("limit") is not None:
            body["limit"] = args["limit"]
        res = _http("POST", "/app/v2/agent_context", body, timeout=180.0)
    elif name == "agent_context_search":
        body = {"caller": SELF_ID, "mode": "search",
                "query": str(args.get("query") or ""),
                "target": str(args.get("target") or "")}
        if args.get("limit") is not None:
            body["limit"] = args["limit"]
        res = _http("POST", "/app/v2/agent_context", body, timeout=120.0)
    elif name == "agent_context_list":
        res = _http("GET", f"/app/v2/agent_context_targets?caller={SELF_ID}")
    elif name in ("propose_calendar_event", "propose_reminder"):
        target = "reminder" if name == "propose_reminder" else "calendar"
        sid = str(args.get("session") or "").strip() or SELF_ID
        res = _http("POST",
                    "/app/v2/sessions/"
                    f"{urllib.parse.quote(sid, safe=':')}/proposals/calendar",
                    _proposal_body(target, args), timeout=60.0)
    else:
        return {"isError": True, "text": f"unknown tool: {name}"}
    # `_http_error` 只要出現就是失敗 —— 連不上 bridge 時它是 0(falsy),
    # 舊寫法 bool() 會把「連不上」報成成功,agent 就會以為事情辦好了。
    is_err = "_http_error" in res
    return {"isError": is_err,
            "text": json.dumps(res, ensure_ascii=False, indent=1)}


def _handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method") or ""
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-call", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        out = _tool_call(str(params.get("name") or ""),
                         params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": out["text"]}],
            "isError": bool(out.get("isError"))}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            resp = _handle(msg)
        except Exception as e:  # noqa: BLE001
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            try:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return          # client 端已收線,安靜退場


if __name__ == "__main__":
    main()
