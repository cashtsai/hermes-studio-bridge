from __future__ import annotations
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
import time
import uuid

# ── bridge 注入面(照 cc_sdk.configure 同款)──────────────────────────────
# acp_client 不 import bridge(避免循環),觀測掛鉤由 bridge 開機時注入。
# 預設 no-op:單測可以不接 bridge 直接驅動本模組。
LOG = None                 # bridge._log_event 同形:LOG(event, **fields)


def configure(log=None) -> None:
    global LOG
    if log is not None:
        LOG = log


def _log(event: str, **fields) -> None:
    """觀測掛鉤 fail-safe:log 本身壞掉也不准打斷 persona 流程。"""
    try:
        if LOG is not None:
            LOG(event, **fields)
    except Exception:  # noqa: BLE001
        pass


# ── A1/A2/A3 韌性旗標與參數(全部每次呼叫讀 env:部署開關在 plist,
#    測試也能逐測調整,不用 reload 模組)──────────────────────────────────
def resilience_on() -> bool:
    """ACP_RESILIENCE=1 才啟用回合看門狗/健康巡檢(預設關 = 零行為差異)。"""
    return os.environ.get("ACP_RESILIENCE", "") == "1"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ACP_STALL_DEGRADE_WINDOW_SECS = 1800.0   # 連續 stall 計數窗(30 分鐘)
ACP_CRASH_LOOP_WINDOW_SECS = 600.0       # 巡檢 reset 計數窗(10 分鐘)
ACP_CRASH_LOOP_N = 3                     # 窗內 >= N 次巡檢 reset → crash loop


def _first_existing(paths, fallback):
    for p in paths:
        full = os.path.expanduser(p)
        if os.path.exists(full):
            return full
    return os.path.expanduser(fallback)


HERMES_BIN = os.path.expanduser(os.environ.get("HERMES_BIN", "")) or _first_existing(
    ["~/apps/hermes-agent/runtime/venv/bin/hermes",
     "~/apps/hermes-agent/venv/bin/hermes",
     "~/.local/bin/hermes"],
    "~/apps/hermes-agent/runtime/venv/bin/hermes")
ACP_STREAM_LIMIT = 128 * 1024 * 1024
LOBSTER_ROOT = os.path.expanduser(os.environ.get("LOBSTER_ROOT", "~/apps/lobster-tg"))


def workspace_cwd_for(key: str, home: str) -> str:
    """Return the ACP tool cwd for a persona.

    HERMES_HOME owns durable state/memory. The ACP cwd should point at the
    persona's workspace so relative startup files from AGENTS.md resolve.
    """
    candidates = {
        "yuanfang": os.path.join(LOBSTER_ROOT, "workspace"),
        "pantianqing": os.path.join(LOBSTER_ROOT, "fliper-workspace"),
        "xcash": os.path.join(LOBSTER_ROOT, "xcash-workspace"),
        # shuijing-workspace is not fully populated yet; the shared workspace
        # contains the current absolute routing to ShuiJing's Hermes profile.
        "shuijing": os.path.join(LOBSTER_ROOT, "workspace"),
    }
    cwd = candidates.get(key) or home
    return cwd if os.path.isdir(cwd) else home


def canonical_telegram_entry(home: str):
    """The sessions.json entry the TG gateway is CURRENTLY driving for this
    persona, as ``(session_key, entry)`` — or None when the map is missing/empty.

    sessions.json is the gateway's own session_key → session_id map, updated
    every time it rotates (auto-reset / new day). It beats any state.db
    heuristic: the richest session is often a rotated-OUT one — stale history —
    and writing the app's turns there means Telegram never sees them.

    The whole entry (not just the id) is returned because the reverse mirror
    (app→TG, `tg_outbound`) needs the session_key as well: the chat_id lives
    there (`agent:<profile>:telegram:<chat_type>:<chat_id>[…]`) and in no other
    field. Both directions therefore agree on ONE chat by construction — we
    post to exactly the chat whose session we write into.
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
            if not ent.get("session_id"):
                continue
            upd = ent.get("updated_at") or ""          # ISO strings sort lexically
            if best is None or upd > best[0]:
                best = (upd, key, ent)
        return (best[1], best[2]) if best else None
    except Exception:
        return None


def canonical_telegram_session(home: str):
    """The session id the TG gateway is CURRENTLY driving for this persona.

    Thin accessor over :func:`canonical_telegram_entry` (the session-pinning
    path only ever needs the id). A stale mapping is ignored when its target
    is archived or pinned to an Anthropic model; the caller then starts a
    fresh session from the persona's current configuration.
    """
    found = canonical_telegram_entry(home)
    sid = found[1].get("session_id") if found else None
    if not sid:
        return None
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT source, archived, model FROM sessions WHERE id = ? LIMIT 1",
            (sid,),
        ).fetchone()
        con.close()
        if not row or row[0] != "telegram" or row[1]:
            return None
        model = (row[2] or "").lower()
        return None if model.startswith("claude") else sid
    except Exception:
        return None


class ACPSession:
    def __init__(self, home: str, cwd: str | None = None):
        self.home = home
        self.cwd = cwd or home
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
        # ── A1/A2/A3 韌性狀態(旗標關時全部維持初始值,零行為差異)──────
        self._waiters = 0                 # A2:正在 _lock 上排隊的 turn 數(可見佇列深度)
        self._last_item_at = 0.0          # A1:本回合最後一次 provider 有動靜(monotonic)
        self._stall_fired = False         # A1:看門狗已對本回合開刀
        self._stall_resets: list[float] = []   # A1:30 分鐘窗內的 stall-reset 時刻
        self._sweep_resets: list[float] = []   # A3:10 分鐘窗內的巡檢 reset 時刻
        self._sweep_cooldown = False      # A3:crash-loop 冷卻中(下個使用者回合解除)
        self.degraded = False             # A1/A3:連續 stall / crash-loop → 看板標降級

    def is_busy(self) -> bool:
        """True while this persona is already running or queued inside a turn."""
        return self._lock.locked()

    def queue_depth(self) -> int:
        """A2:排在本 persona 鎖上等待的 turn 數(不含正在跑的那一輪)。

        設計取捨:沒有把 prompt_stream 換成 submit()+drainer 的顯式佇列 ——
        prompt_stream 是「呼叫端自帶 consumer 的 async generator」,bridge.py
        六個呼叫點(SSE 串流/v2 input/interrupt 驗證…)都靠這個契約各自
        消費 items;改成集中 drainer 得同時重寫全部消費端,風險遠大於收益。
        鎖競爭本來就把並發 turn 排成嚴格序列(訊息不會丟,只是隱形),
        真正缺的是「看得見」:_waiters 計數 + is_busy() 讓 v2 session 列表
        與 turn status 說真話,app/看板/編隊工具因此看得見人格忙碌。"""
        return self._waiters

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
                                        {"cwd": self.cwd, "sessionId": sid, "mcpServers": []},
                                        timeout=120)
                    self.session_id = sid
                    self._loaded_session = True
                    return
                except Exception:
                    pass
            r = await self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60)
            self.session_id = (r or {}).get("sessionId")
            self._loaded_session = False

    async def _force_new_session(self):
        """Drop the loaded session and start a blank one — recovery path for a
        loaded Telegram session that turns out inert (prompts yield nothing)."""
        r = await self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60)
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
            # Only reuse a live, non-Anthropic Telegram session. When the
            # gateway map is missing, reviving the richest historical session
            # can pin Pocket to an exhausted Claude credential. If no safe
            # session exists, the caller creates a fresh session using the
            # persona's current default model.
            cur = con.execute(
                "SELECT id FROM sessions "
                "WHERE message_count > 5 AND source = 'telegram' "
                "AND archived = 0 "
                "AND (model IS NULL OR lower(model) NOT LIKE 'claude%') "
                "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 1")
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
                                {"cwd": self.cwd, "sessionId": sid, "mcpServers": []},
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
            # A1:任何一行 provider 輸出都算「活著」。用 reader 側(而非
            # consumer 側)記時,下游 SSE 消費慢不會被看門狗誤判成 provider
            # 卡住 —— 對照 CX 的 last_event_at 也是事件落地就記。
            self._last_item_at = time.monotonic()
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

    async def _watch_turn(self):
        """A1 回合看門狗(形狀對照 bridge.CodexAppServerClient._watch_turn)。

        provider 停止產出但不完成 JSON-RPC 時,prompt_stream 與 per-persona
        lock 會被佔死 —— reset() 的 docstring 自己承認要「呼叫端先取消」,
        但過去沒有任何自動偵測(CX 8 月「恆忙碌」同族病)。這裡:無輸出超過
        ACP_TURN_STALL_SECS → cancel()(advisory)→ reset() 取消 pending
        future,把卡在 _attempt 裡的 consumer 踢醒,prompt_stream 收到後下發
        誠實終態並釋放鎖。

        切片睡眠 ≤5s + 喚醒縫隙原諒:蓋著睡的 Mac 醒來時,單一長 sleep 會把
        整段睡眠計入「無輸出」一醒來就誤開刀(PerfProbes suspendedUntil 同一
        個坑)。每片實測耗時 >3× 片長就視為 OS 睡眠縫隙,重記起點不開刀。
        """
        stall_secs = _env_float("ACP_TURN_STALL_SECS", 180.0)
        slice_secs = min(5.0, max(0.05, stall_secs / 10))
        try:
            while True:
                before = time.monotonic()
                await asyncio.sleep(slice_secs)
                if time.monotonic() - before > slice_secs * 3:
                    self._last_item_at = time.monotonic()   # 睡眠喚醒縫隙,原諒
                    continue
                idle = time.monotonic() - self._last_item_at
                if idle < stall_secs:
                    continue
                self._stall_fired = True
                _log("acp_turn_stalled", home=self.home,
                     idle_secs=int(idle), stall_secs=int(stall_secs))
                try:
                    await asyncio.wait_for(self.cancel(), timeout=2.0)
                except Exception:  # noqa: BLE001 — cancel 是 advisory,失敗照樣開刀
                    pass
                await self.reset()
                return
        except asyncio.CancelledError:
            return

    def _note_stall_reset(self):
        """A1:stall-reset 計數。30 分鐘窗內 >= ACP_STALL_DEGRADE_N 次 →
        標 degraded(v2 sessions / turn status 上看板),成功回合歸零。"""
        now = time.time()
        self._stall_resets = [t for t in self._stall_resets
                              if now - t < ACP_STALL_DEGRADE_WINDOW_SECS] + [now]
        n = _env_int("ACP_STALL_DEGRADE_N", 3)
        if len(self._stall_resets) >= n and not self.degraded:
            self.degraded = True
            _log("acp_session_degraded", home=self.home,
                 stalls=len(self._stall_resets), threshold=n)

    def _note_turn_ok(self):
        """A1/A3:任何一個成功回合把降級狀態整組洗白(stall 與 crash-loop
        計數都清)——降級的語意是「最近持續出事」,成功即證明已恢復。"""
        if self._stall_resets or self._sweep_resets or self.degraded:
            self._stall_resets = []
            self._sweep_resets = []
            self.degraded = False
            self._sweep_cooldown = False
            _log("acp_session_recovered", home=self.home)

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
        but no longer respond).

        A1(旗標 ACP_RESILIENCE=1):每回合掛看門狗,provider 停產出超時 →
        cancel+reset+誠實終態 item,鎖照常釋放,persona 不再被佔死。
        A2:鎖競爭排隊改為可見(_waiters / queue_depth()),契約不變。"""
        # A2:等待計數 —— 手動 acquire 等價於原本的 async with,只是把
        # 「正在排隊」這件事記下來。acquire 被取消時 finally 一樣會歸還計數。
        waiting = self._lock.locked()
        if waiting:
            self._waiters += 1
        try:
            await self._lock.acquire()
        finally:
            if waiting:
                self._waiters -= 1
        try:
            if resilience_on():
                self._sweep_cooldown = False   # A3:使用者回合 = crash-loop 冷卻解除
            await self.ensure_started()
            await self._sync_canonical_session()   # gateway rotated? follow it
            yield ("status", {"state": "running", "label": "Hermes 開始處理"})
            watchdog = None
            self._stall_fired = False
            if resilience_on():
                self._last_item_at = time.monotonic()
                watchdog = asyncio.create_task(self._watch_turn())
            produced = 0
            total = 0
            try:
                try:
                    async for item in self._attempt(text):
                        produced += 1
                        total += 1
                        yield item
                    if produced:
                        self._proved_alive = True
                    # Self-heal ONLY for an inert just-loaded session we've
                    # never seen respond. Once a loaded session has produced
                    # output, a later empty turn is treated as legitimate — we
                    # must NOT drop the session and lose the accumulated
                    # Telegram context.
                    elif self._loaded_session and not self._proved_alive:
                        await self._force_new_session()
                        async for item in self._attempt(text):
                            total += 1
                            yield item
                except (Exception, asyncio.CancelledError):
                    # 看門狗開的刀:reset() 取消 pending future,_attempt 以
                    # CancelledError/RuntimeError 收場 —— 這不是呼叫端取消,
                    # 吞掉改下發誠實終態。看門狗沒開刀的例外照舊往上拋
                    # (呼叫端取消/真錯誤,行為與旗標關閉時完全一致)。
                    if not self._stall_fired:
                        raise
            finally:
                if watchdog is not None:
                    watchdog.cancel()
            if self._stall_fired:
                # 誠實終態:不裝沒事,也不讓呼叫端永等。reset() 已把程序
                # 收掉,下一回合 ensure_started 會重載 canonical session。
                self._note_stall_reset()
                yield ("error", "Hermes 回合卡住,已重置")
            elif total:
                self._note_turn_ok()
        finally:
            self._lock.release()


class ACPPool:
    def __init__(self):
        self._sessions: dict[str, ACPSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None

    def peek(self, key: str) -> ACPSession | None:
        """只窺不生 —— 狀態面(v2 sessions 列表等)用,絕不為了看忙碌/降級
        而冷啟一條 ACP 程序(對照 bridge._registry_is_busy 同一條紅線)。"""
        return self._sessions.get(key)

    async def get(self, key: str, home: str) -> ACPSession:
        async with self._lock:
            s = self._sessions.get(key)
            if s is None:
                s = ACPSession(home, workspace_cwd_for(key, home))
                self._sessions[key] = s
            # A3:健康巡檢惰啟(首次 get 才開,一 pool 一條)。旗標關不開;
            # 開了之後旗標熱關 → 迴圈裡每輪再讀一次 env,睡著待命。
            if resilience_on() and (self._sweeper is None or self._sweeper.done()):
                self._sweeper = asyncio.create_task(self._health_sweep())
            return s

    async def _health_sweep(self):
        """A3 背景健康巡檢(herdr 偵測循環的精神,我們 30s 夠)。

        程序死了不等下一回合寫入失敗才發現:每 ACP_HEALTH_SWEEP_SECS 掃一次
        returncode,死了先行 reset(),下一回合直接走 ensure_started 冷啟,
        使用者感受從「先吃一次錯誤」變「只是慢一點」。"""
        try:
            while True:
                await asyncio.sleep(_env_float("ACP_HEALTH_SWEEP_SECS", 30.0))
                if not resilience_on():
                    continue                    # 旗標熱關:睡著待命,不退出
                for key, s in list(self._sessions.items()):
                    try:
                        await self._sweep_one(key, s)
                    except Exception as e:  # noqa: BLE001 — 巡檢自己不准把 pool 弄死
                        _log("acp_health_sweep_error", key=key,
                             error=type(e).__name__, error_message=str(e)[:200])
        except asyncio.CancelledError:
            return

    async def _sweep_one(self, key: str, s: ACPSession):
        proc = s.proc
        if proc is None or proc.returncode is None:
            return                              # 沒程序 / 還活著
        if s.is_busy():
            # 回合進行中死掉:_read_loop 結束時會 fail 掉全部 pending,
            # 由該回合自己收尾 —— 巡檢不跟進行中的 turn 搶收屍。
            return
        if s._sweep_cooldown:
            return                              # crash-loop 冷卻:等下個使用者回合
        now = time.time()
        s._sweep_resets = [t for t in s._sweep_resets
                           if now - t < ACP_CRASH_LOOP_WINDOW_SECS] + [now]
        _log("acp_proc_died_swept", key=key, returncode=proc.returncode,
             sweep_resets=len(s._sweep_resets))
        await s.reset()
        if len(s._sweep_resets) >= ACP_CRASH_LOOP_N:
            # crash-loop 護欄:10 分鐘內第 3 次巡檢收屍 —— 程序反覆秒死,
            # 再繼續「收屍→下回合重啟」只是空轉。標降級 + 冷卻,冷卻到
            # 下個使用者回合(prompt_stream 開頭)才解除。
            s.degraded = True
            s._sweep_cooldown = True
            _log("acp_crash_loop", key=key, resets=len(s._sweep_resets),
                 window_secs=int(ACP_CRASH_LOOP_WINDOW_SECS))
