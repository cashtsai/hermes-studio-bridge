#!/usr/bin/env python3
"""Minimal stdio MCP server exposing Hermes long-term memory to dispatched
Claude Code / Codex sub-agents — so they share the canonical brain.

Tools: read_memory · search_memory · write_memory.
Home via env STUDIO_MEMORY_HOME (defaults to the main Hermes home).
Wired into a dispatch via `claude --mcp-config '{...}'`.
"""
import glob
import json
import os
import sys

HOME = os.environ.get("STUDIO_MEMORY_HOME", "/Users/xcash/apps/hermes-agent/home")
MEMDIR = os.path.join(HOME, "memories")

TOOLS = [
    {"name": "read_memory",
     "description": "讀取善彰的 Hermes 長期記憶(MEMORY.md + USER.md)。做任務前先讀,確保用對脈絡。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_memory",
     "description": "在長期記憶中搜尋關鍵字,回傳命中的行。",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "write_memory",
     "description": "把一則重要、持久的事實追加進長期記憶(MEMORY.md)。只寫值得長期記住的事。",
     "inputSchema": {"type": "object",
                     "properties": {"note": {"type": "string"}}, "required": ["note"]}},
]


def _read_all() -> str:
    out = []
    for f in ("MEMORY.md", "USER.md"):
        p = os.path.join(MEMDIR, f)
        if os.path.exists(p):
            out.append(f"# {f}\n" + open(p, encoding="utf-8", errors="replace").read())
    return "\n\n".join(out) or "(無記憶)"


def _search(q: str) -> str:
    hits = []
    for p in glob.glob(os.path.join(MEMDIR, "*.md")):
        try:
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace")):
                if q.lower() in line.lower():
                    hits.append(f"{os.path.basename(p)}:{i + 1}: {line.strip()}")
        except Exception:
            continue
    return "\n".join(hits[:50]) or "(無結果)"


def _write(note: str) -> str:
    p = os.path.join(MEMDIR, "MEMORY.md")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"\n\n## (子 agent 寫入)\n{note}\n")
    return "已寫入 MEMORY.md"


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "studio-memory", "version": "1.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            try:
                if name == "read_memory":
                    text = _read_all()
                elif name == "search_memory":
                    text = _search(args.get("query", ""))
                elif name == "write_memory":
                    text = _write(args.get("note", ""))
                else:
                    text = f"unknown tool {name}"
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as e:  # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                  "isError": True}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
