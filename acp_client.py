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
ACP_STREAM_LIMIT = 128 * 1024 * 1024


def canonical_telegram_session(home: str):
    """The session id the TG gateway is CURRENTLY driving for this persona.

    sessions.json is the gateway's own session_key → session_id map, updated
    every time it rotates (auto-reset / new day). It beats any state.db
    heuristic: the richest session is often a rotated-OUT one — stale history —
    and writing the app's turns there means Telegram never sees them.
    Returns None when the map is missing/empty (caller falls back).
    """
    try:
        with open(os.path.join(home, "sessions", "sessions.json")) as f:
            data = json.load(f)
        best = None
        for key, ent in (data or {}).items():
            if not isinstance(ent, dict):
                continue
            if (ent.get("platform") or "") != "telegram" and ":telegram:" not in key:
                continue
            sid = ent.get("session_id")
            if not sid:
                continue
            upd = ent.get("updated_at") or ""          # ISO strings sort lexically
            if best is None or upd > best[0]:
                best = (upd, sid)
        return best[1] if best else None
    except Exception:
        return None


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
        self._loaded_session = False      # True if session came from session/load
        self._proved_alive = False        # True once any turn produced output
        self._last_canonical_sid = None   # last mapping sid we attempted to load (flap guard)

    def is_busy(self) -> bool:
        """True while this persona is already running or queued inside a turn."""
        return self._lock.locked()

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
            # Allow loading cross-source sessions (e.g. Telegram sessions in
            # acp context). Without this, acp_adapter/_restore silently skips
            # any session whose source != "acp", so the TG history is invisible.
            env["HERMES_ACP_ALLOW_CROSS_SOURCE"] = "1"
            self.proc = await asyncio.create_subprocess_exec(
                HERMES_BIN, "acp", "--accept-hooks",
                env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                # ACP is newline-delimited JSON. Tool results and restored
                # session payloads can exceed asyncio's 64 KiB line default;
                # that would kill the reader task and leave the persona offline.
                limit=ACP_STREAM_LIMIT,
            )
            self._pending.clear()
            self._reader = asyncio.create_task(self._read_loop())
            await self._request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "studio-bridge", "version": "1.0"},
            }, timeout=30)
            # M1: continue the persona's canonical conversation (the Telegram
            # session the gateway drives) so accumulated context — holdings,
            # projects, people — is present, instead of a blank session/new.
            sid = self._latest_telegram_session()
            if sid:
                self._last_canonical_sid = sid   # one attempt per sid (flap guard)
                try:
                    await self._request("session/load",
                                        {"cwd": self.home, "sessionId": sid, "mcpServers": []},
                                        timeout=120)
                    self.session_id = sid
                    self._loaded_session = True
                    return
                except Exception:
                    pass
            r = await self._request("session/new", {"cwd": self.home, "mcpServers": []}, timeout=60)
            self.session_id = (r or {}).get("sessionId")
            self._loaded_session = False

    async def _force_new_session(self):
        """Drop the loaded session and start a blank one — recovery path for a
        loaded Telegram session that turns out inert (prompts yield nothing)."""
        r = await self._request("session/new", {"cwd": self.home, "mcpServers": []}, timeout=60)
        self.session_id = (r or {}).get("sessionId")
        self._loaded_session = False

    def _latest_telegram_session(self):
        # The gateway's own mapping is authoritative — the state.db heuristics
        # below are reachable only when sessions.json is missing (fresh home).
        sid = canonical_telegram_session(self.home)
        if sid:
            return sid
        import sqlite3
        db = os.path.join(self.home, "state.db")
        if not os.path.exists(db):
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            # Richest TELEGRAM session. Restricted by source: the old any-source
            # variant could pick a fat acp/cli session, which is exactly the
            # wrong place to write the persona's canonical conversation.
            cur = con.execute(
                "SELECT id FROM sessions "
                "WHERE message_count > 5 AND source = 'telegram' "
                "ORDER BY message_count DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                # fallback: any non-cron session with messages
                cur = con.execute(
                    "SELECT id FROM sessions "
                    "WHERE message_count > 0 AND source != 'cron' "
                    "ORDER BY started_at DESC LIMIT 1")
                row = cur.fetchone()
            con.close()
            return row[0] if row else None
        except Exception:
            return None

    async def _sync_canonical_session(self):
        """Re-check the gateway's session mapping at each turn and reload when
        it moved. The TG gateway rotates its session (auto-reset / new day); a
        warm ACP process would otherwise keep writing to the rotated-out
        session forever — the app's turns land where Telegram never looks.
        One load attempt per new sid (`_last_canonical_sid`): a failed or inert
        load must not flap between reload and the self-heal below.
        """
        sid = canonical_telegram_session(self.home)
        if not sid or sid == self.session_id or sid == self._last_canonical_sid:
            return
        self._last_canonical_sid = sid
        try:
            await self._request("session/load",
                                {"cwd": self.home, "sessionId": sid, "mcpServers": []},
                                timeout=120)
            self.session_id = sid
            self._loaded_session = True
            self._proved_alive = False       # new load → unproven again
        except Exception:
            pass                             # keep the current working session

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
                # surface what was auto-approved so the client can show it
                if self._active_q is not None:
                    title = ((msg.get("params") or {}).get("toolCall") or {}).get("title") \
                        or (msg.get("params") or {}).get("title") or "工具"
                    self._active_q.put_nowait(("perm", str(title).split(":", 1)[0].strip()))
        # process died — fail any waiters so callers don't hang
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("acp process ended"))
        self._pending.clear()
        self.session_id = None

    async def cancel(self):
        """Interrupt the current turn (Esc-style)."""
        if self.proc and self.proc.returncode is None and self.session_id:
            try:
                await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                                  "params": {"sessionId": self.session_id}})
            except Exception:
                pass

    async def reset(self):
        """Retire a stuck ACP process; the next turn starts and reloads it.

        `session/cancel` is advisory. A provider can stop producing output
        without ever completing the JSON-RPC request, leaving `prompt_stream`
        and its per-persona lock occupied forever. Callers first cancel the
        task that owns that lock, then use this method to discard the inert
        process. `ensure_started()` restores the canonical Telegram session on
        the next turn, so recovery does not create a second conversation.
        """
        async with self._start_lock:
            proc = self.proc
            reader = self._reader

            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        pass

            if reader and reader is not asyncio.current_task():
                if not reader.done():
                    reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            self.proc = None
            self._reader = None
            self.session_id = None
            self._active_q = None
            self._loaded_session = False
            self._proved_alive = False
            self._last_canonical_sid = None

    async def _attempt(self, text: str):
        """One session/prompt turn — yields (kind, val) items."""
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

    async def prompt_stream(self, text: str):
        """Async generator yielding (kind, val) items for one turn. Self-heals:
        if a *loaded* Telegram session produces an inert, empty turn, it drops
        to a fresh session/new and retries once (fixes old sessions that load
        but no longer respond)."""
        async with self._lock:
            await self.ensure_started()
            await self._sync_canonical_session()   # gateway rotated? follow it
            yield ("status", {"state": "running", "label": "Hermes 開始處理"})
            produced = 0
            async for item in self._attempt(text):
                produced += 1
                yield item
            if produced:
                self._proved_alive = True
            # Self-heal ONLY for an inert just-loaded session we've never seen
            # respond. Once a loaded session has produced output, a later empty
            # turn is treated as legitimate — we must NOT drop the session and
            # lose the accumulated Telegram context.
            elif self._loaded_session and not self._proved_alive:
                await self._force_new_session()
                async for item in self._attempt(text):
                    yield item


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
