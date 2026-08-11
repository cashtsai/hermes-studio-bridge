#!/usr/bin/env python3
"""agent-call MCP stdio server — 讓 CC/codex 以工具形式互調其他 agent(1c)。

這是 bridge `POST /app/v2/agent_call` 家族端點的極薄 MCP 包裝(純 stdlib,
無第三方依賴):所有權限控制(AGENT_CALL 旗標、allowlist 政策、深度/循環/
預算護欄、audit 卡)都在 bridge 側強制,本腳本只是傳聲筒。

暴露工具:
  agent_call(target, message, mode?, timeout_secs?)  # 調用另一個 agent
  agent_list()                                       # 政策放行的可調用對象
  agent_result(call_id)                              # background 模式收割

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
]


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
    else:
        return {"isError": True, "text": f"unknown tool: {name}"}
    is_err = bool(res.get("_http_error"))
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
