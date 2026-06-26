"""Minimal persistent ACP client for Hermes.

Keeps one warm `hermes acp` process per persona (HERMES_HOME), so each turn
skips the ~5s cold start of `hermes -z`, and streams the agent's output
token-chunks live. Auto-approves tool permission requests (own machine).

Protocol (newline-delimited JSON-RPC, learned from the Scarf ACPClient):
  → initialize {protocolVersion:1, clientCapabilities:{}, clientInfo:{…}}
  → session/new {cwd, mcpServers:[]}  ⇒ {sessionId}
  → session/prompt {sessionId, messageId, prompt:[{type:text,text}]}
       ⇐ notif session/update {update:{sessionUpdate:"agent_message_chunk",
                                        content:{text}}}   (streamed)
       ⇐ req   session/request_permission {options:[{optionId,name}]}  → allow
       ⇐ resp  {stopReason, usage}
"""
import asyncio
import json
import os
import uuid

HERMES_BIN = "/Users/xcash/apps/hermes-agent/runtime/venv/bin/hermes"


class ACPSession:
    def __init__(self, home: str):
        self.home = home
        self.proc = None
        self.session_id = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._active_q: asyncio.Queue | None = None
        self._reader = None
        self._lock = asyncio.Lock()       # one turn at a time per persona
        self._start_lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, obj: dict):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float | None = 60):
        rid = self._next_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if timeout:
            return await asyncio.wait_for(fut, timeout=timeout)
        return await fut

    async def ensure_started(self):
        async with self._start_lock:
            if self.proc and self.proc.returncode is None and self.session_id:
                return
            env = dict(os.environ)
            env["HERMES_HOME"] = self.home
            env["HERMES_ACCEPT_HOOKS"] = "1"
            self.proc = await asyncio.create_subprocess_exec(
                HERMES_BIN, "acp", "--accept-hooks",
                env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            self._pending.clear()
            self._reader = asyncio.create_task(self._read_loop())
            await self._request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "studio-bridge", "version": "1.0"},
            }, timeout=30)
            r = await self._request("session/new", {"cwd": self.home, "mcpServers": []}, timeout=60)
            self.session_id = (r or {}).get("sessionId")

    async def _read_loop(self):
        proc = self.proc
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and ("result" in msg or "error" in msg):
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
            elif msg.get("method") == "session/update":
                upd = (msg.get("params") or {}).get("update") or {}
                kind = upd.get("sessionUpdate")
                q = self._active_q
                if q is None:
                    continue

                def _content_text(u):
                    # tool_call / tool_call_update carry content:[{type:content,
                    # content:{type:text,text:…}}]; messages carry content:{text}
                    c = u.get("content")
                    if isinstance(c, dict):
                        return c.get("text", "")
                    if isinstance(c, list):
                        for item in c:
                            cc = (item or {}).get("content") or {}
                            if cc.get("type") == "text" and cc.get("text"):
                                return cc["text"]
                    return ""

                if kind == "agent_message_chunk":
                    t = _content_text(upd)
                    if t:
                        q.put_nowait(("text", t))
                elif kind == "agent_thought_chunk":
                    t = _content_text(upd)
                    if t:
                        q.put_nowait(("thought", t))
                elif kind == "tool_call":
                    title = (upd.get("title") or "").strip()
                    name = (title.split(":", 1)[0].strip() or "tool")
                    q.put_nowait(("tool_start", {"name": name, "cmd": _content_text(upd)}))
                elif kind == "tool_call_update":
                    q.put_nowait(("tool_result", {"text": _content_text(upd),
                                                  "status": upd.get("status", "")}))
                elif kind == "usage_update":
                    q.put_nowait(("usage", {"used": upd.get("used"), "size": upd.get("size")}))
            elif msg.get("method") == "session/request_permission" and mid is not None:
                opts = (msg.get("params") or {}).get("options") or []
                allow = None
                for o in opts:
                    name = (o.get("name") or "").lower()
                    if any(k in name for k in ("allow", "always", "yes", "approve", "accept")):
                        allow = o.get("optionId")
                        break
                if allow is None and opts:
                    allow = opts[0].get("optionId")
                await self._send({"jsonrpc": "2.0", "id": mid,
                                  "result": {"outcome": {"outcome": "selected", "optionId": allow}}})
        # process died — fail any waiters so callers don't hang
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("acp process ended"))
        self._pending.clear()
        self.session_id = None

    async def prompt_stream(self, text: str):
        """Async generator yielding text chunks for one turn."""
        async with self._lock:
            await self.ensure_started()
            rid = self._next_id()
            done = asyncio.get_event_loop().create_future()
            self._pending[rid] = done
            q: asyncio.Queue = asyncio.Queue()
            self._active_q = q
            await self._send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
                              "params": {"sessionId": self.session_id,
                                         "messageId": uuid.uuid4().hex,
                                         "prompt": [{"type": "text", "text": text}]}})
            try:
                while True:
                    getter = asyncio.ensure_future(q.get())
                    d, _ = await asyncio.wait({getter, done}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in d:
                        yield getter.result()
                        continue
                    getter.cancel()
                    while not q.empty():
                        yield q.get_nowait()
                    break
            finally:
                self._active_q = None
                self._pending.pop(rid, None)
            if done.done() and done.exception():
                raise done.exception()


class ACPPool:
    def __init__(self):
        self._sessions: dict[str, ACPSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, home: str) -> ACPSession:
        async with self._lock:
            s = self._sessions.get(key)
            if s is None:
                s = ACPSession(home)
                self._sessions[key] = s
            return s
