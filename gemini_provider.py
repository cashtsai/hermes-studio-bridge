from __future__ import annotations
"""Gemini CLI(ACP)客戶端 — Pocket 第五 provider 的傳輸層。

協定:gemini-cli `--acp`(Zed Agent Client Protocol,JSON-RPC over stdio)。
與 hermes 的 ACP 同族:initialize → authenticate → session/new →
session/prompt,回覆經 `session/update` 通知逐字送達
(`agent_message_chunk` / `agent_thought_chunk` / `tool_call` …)。

職責邊界(照 openclaw_provider 慣例):本模組只管「配置 + 子程序生命週期 +
JSON-RPC 呼叫/通知泵 + session 名錄持久化」;卡片 digest 在
carddigest.GeminiDigest,v2 路由/送訊接線在 bridge.py(S5 區)。

設計要點:
- 認證走 **API key**(env `GEMINI_API_KEY` 或 ~/.pocket/gemini.json)。
  OAuth 個人帳號 2026-10 已被 Google 停用(導去 Antigravity),不做。
- 未配置 → configured() False,bridge 全部靜默缺席(APNs 金鑰缺席模式)。
- 單一常駐 `gemini --acp` 子程序,lazy spawn;死了下次呼叫重拉,
  既有 ACP session id 由 session/load 續接(loadSession capability)。
- ACP 沒有「列出 sessions」:名錄由本模組持久化(~/.pocket/gemini-sessions.json),
  逐字稿落 ~/.pocket/gemini/<sid>.jsonl(seed/重啟補卡用)。
"""

import asyncio
import json
import os
import time
import uuid

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("GEMINI_CONFIG_FILE", "~/.pocket/gemini.json"))
_STATE_DIR = os.path.expanduser(
    os.environ.get("GEMINI_STATE_DIR", "~/.pocket/gemini"))
_SESSIONS_FILE = os.path.join(_STATE_DIR, "sessions.json")
_CALL_TIMEOUT = float(os.environ.get("GEMINI_CALL_TIMEOUT", "30"))
_TURN_TIMEOUT = float(os.environ.get("GEMINI_TURN_TIMEOUT", "600"))
_SPAWN_TIMEOUT = float(os.environ.get("GEMINI_SPAWN_TIMEOUT", "45"))
_DEFAULT_CWD = os.path.expanduser(
    os.environ.get("GEMINI_WORKDIR", "~/.pocket/gemini/workspace"))


class GeminiError(Exception):
    """gemini ACP 回的結構化錯誤或傳輸層失敗。"""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ── 配置 ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """env 優先(GEMINI_API_KEY),否則讀 ~/.pocket/gemini.json。"""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return {"api_key": key, "source": "env"}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return {"api_key": str(d.get("api_key") or "").strip(), "source": "file"}
    except Exception:  # noqa: BLE001 — 缺檔/壞檔一律視為未配置
        return {"api_key": "", "source": "none"}


def save_config(api_key: str) -> dict:
    """App「進階」頁的手動配置落檔(0600)。env 有值時 env 仍優先。"""
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"api_key": (api_key or "").strip()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _CONFIG_FILE)
    return load_config()


def gemini_bin() -> str:
    return os.environ.get("GEMINI_BIN", "gemini")


# ── session 名錄(ACP 沒有 list,自己記)──────────────────────────────────

def _load_sessions() -> dict:
    try:
        with open(_SESSIONS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — 缺檔=空名錄
        return {}


def _save_sessions(d: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = _SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _SESSIONS_FILE)


def list_sessions() -> list[dict]:
    """名錄 → [{id,acp_sid,title,cwd,created,last}],last 新→舊。
    `id` 是 bridge 自己的 key(v2 id 的 rest 段);`acp_sid` 是 gemini 端
    session id —— 解耦的原因:CLI 更新/狀態清掉時 ACP session 會蒸發,
    bridge 這邊換一顆 acp_sid 續命,歷史(逐字稿/卡)照舊。"""
    rows = list(_load_sessions().values())
    rows.sort(key=lambda r: float(r.get("last") or 0), reverse=True)
    return rows


def get_session(key: str) -> dict | None:
    return _load_sessions().get(key)


def remember_session(key: str, acp_sid: str, title: str = "",
                     cwd: str = "") -> dict:
    d = _load_sessions()
    row = d.get(key) or {"id": key, "created": time.time()}
    row["acp_sid"] = acp_sid
    if title and not row.get("title"):
        row["title"] = title[:60]
    if cwd:
        row["cwd"] = cwd
    row["last"] = time.time()
    d[key] = row
    _save_sessions(d)
    return row


def touch_session(key: str, title_hint: str = "") -> None:
    d = _load_sessions()
    row = d.get(key)
    if not row:
        return
    row["last"] = time.time()
    # 第一句使用者訊息當標題(名錄顯示用;已有標題不覆蓋)。
    if title_hint and not row.get("title"):
        row["title"] = title_hint[:60]
    _save_sessions(d)


def forget_session(key: str) -> None:
    d = _load_sessions()
    if key in d:
        del d[key]
        _save_sessions(d)


# ── 逐字稿(seed 補卡 + 歷史)────────────────────────────────────────────────

def transcript_path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
    return os.path.join(_STATE_DIR, f"{safe}.jsonl")


def transcript_append(sid: str, role: str, text: str) -> str:
    """一則訊息落檔;回 mid(卡 id 錨)。"""
    os.makedirs(_STATE_DIR, exist_ok=True)
    mid = f"gm-{uuid.uuid4().hex[:12]}"
    with open(transcript_path(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "role": role, "content": text,
                            "ts": time.time()}, ensure_ascii=False) + "\n")
    return mid


def transcript_read(sid: str, limit: int = 200) -> list[dict]:
    try:
        with open(transcript_path(sid), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001 — 新 session 沒逐字稿
        return []
    out = []
    for line in lines[-limit:]:
        try:
            m = json.loads(line)
            if isinstance(m, dict):
                out.append(m)
        except Exception:  # noqa: BLE001 — 壞行跳過
            continue
    return out


# ── ACP 客戶端 ─────────────────────────────────────────────────────────────

class GeminiClient:
    """單一常駐 `gemini --acp` 子程序的 JSON-RPC 客戶端。

    on_update(session_id:str, update:dict) — session/update 通知(bridge 掛,
        update = params["update"],含 sessionUpdate 種別)。
    on_request(req:dict) → awaitable|None — server→client 請求
        (session/request_permission 等)。bridge 掛;回傳值會被當 result 回給
        gemini。未掛或回 None → 以「拒絕」保守回應。
    """

    def __init__(self, log=None):
        self._log = log or (lambda *_a, **_k: None)
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._rid = 0
        self._reader: asyncio.Task | None = None
        self._loaded: set[str] = set()   # 本程序已 new/load 過的 ACP session
        self.on_update = None
        self.on_request = None
        self.server_info: dict = {}

    def configured(self) -> bool:
        return bool(load_config()["api_key"])

    # ── 子程序/握手 ──

    async def _ensure_proc(self):
        if self.proc is not None and self.proc.returncode is None:
            return
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                return
            cfg = load_config()
            if not cfg["api_key"]:
                raise GeminiError("gemini not configured", code="NOT_CONFIGURED")
            env = dict(os.environ)
            env["GEMINI_API_KEY"] = cfg["api_key"]
            env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
            os.makedirs(_DEFAULT_CWD, exist_ok=True)
            stderr_to = asyncio.subprocess.DEVNULL
            errlog = os.environ.get("GEMINI_STDERR_LOG")
            if errlog:
                stderr_to = open(errlog, "ab")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    gemini_bin(), "--acp",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_to,
                    env=env, cwd=_DEFAULT_CWD)
            except FileNotFoundError:
                raise GeminiError("gemini CLI 不在 PATH(npm i -g @google/gemini-cli)",
                                  code="NO_BINARY")
            self._loaded = set()
            self._reader = asyncio.create_task(self._read_loop(self.proc))
            try:
                init = await self._call_locked(
                    "initialize",
                    {"protocolVersion": 1,
                     "clientCapabilities": {"fs": {"readTextFile": False,
                                                   "writeTextFile": False}}},
                    timeout=_SPAWN_TIMEOUT)
                self.server_info = init or {}
                await self._call_locked("authenticate",
                                        {"methodId": "gemini-api-key"},
                                        timeout=_SPAWN_TIMEOUT)
            except Exception:
                await self._drop_proc()
                raise
            self._log("gemini_connected",
                      protocol=(self.server_info or {}).get("protocolVersion"))

    async def _drop_proc(self):
        proc, self.proc = self.proc, None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(GeminiError("gemini process lost",
                                              code="PROCESS_LOST", retryable=True))
        self._pending.clear()
        self._loaded = set()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._log("gemini_disconnected")

    async def _read_loop(self, proc):
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                if os.environ.get("GEMINI_DEBUG"):
                    self._log("gemini_rx", frame=raw.decode("utf-8", "replace")[:200])
                try:
                    f = json.loads(raw.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 — 非 JSON 行(banner 等)跳過
                    continue
                if "id" in f and ("result" in f or "error" in f):
                    fut = self._pending.pop(int(f["id"]), None) \
                        if str(f.get("id") or "").lstrip("-").isdigit() else None
                    if fut and not fut.done():
                        if "error" in f:
                            err = f["error"] or {}
                            fut.set_exception(GeminiError(
                                str(err.get("message") or "gemini error"),
                                code=str(err.get("code") or "")))
                        else:
                            fut.set_result(f.get("result"))
                    continue
                method = f.get("method") or ""
                if method == "session/update":
                    params = f.get("params") or {}
                    if self.on_update:
                        try:
                            self.on_update(str(params.get("sessionId") or ""),
                                           params.get("update") or {})
                        except Exception:  # noqa: BLE001 — 單筆毒丸不斷流
                            pass
                elif "id" in f:
                    # server→client 請求(request_permission / fs 等)。
                    asyncio.get_running_loop().create_task(
                        self._answer_server_request(proc, f))
        except Exception:  # noqa: BLE001 — 斷線走同一條清理路
            pass
        finally:
            if self.proc is proc:
                await self._drop_proc()

    async def _answer_server_request(self, proc, f: dict):
        """server→client 請求:bridge 的 on_request 決定;沒人接=保守拒絕。
        無論如何都要回 —— 不回 gemini 的 turn 會永遠掛著。"""
        rid = f.get("id")
        result = None
        try:
            if self.on_request:
                result = await self.on_request(f)
        except Exception as e:  # noqa: BLE001
            self._log("gemini_on_request_error", error=str(e)[:160])
        if result is None:
            method = f.get("method") or ""
            if method == "session/request_permission":
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                # 未知請求:回空物件讓協定前進(fs 類已在 capabilities 關掉)。
                result = {}
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid,
                                          "result": result}) + "\n").encode())
            await proc.stdin.drain()
        except Exception:  # noqa: BLE001 — 行程死了由 read_loop 收屍
            pass

    # ── 呼叫 ──

    async def _call_locked(self, method: str, params: dict,
                           timeout: float = _CALL_TIMEOUT):
        """假設 proc 已活;_ensure_proc 內的握手用。"""
        self._rid += 1
        rid = self._rid
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params}) + "\n"
        if os.environ.get("GEMINI_DEBUG"):
            self._log("gemini_tx", frame=line[:200])
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise GeminiError(f"gemini call timeout: {method}",
                              code="TIMEOUT", retryable=True)

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = _CALL_TIMEOUT):
        await self._ensure_proc()
        return await self._call_locked(method, params or {}, timeout=timeout)

    # ── session 生命週期 ──

    async def new_session(self, key: str, cwd: str = "", title: str = "") -> dict:
        """建 ACP session 並以 bridge key 記名錄;回名錄列。"""
        cwd = os.path.expanduser(cwd or _DEFAULT_CWD)
        os.makedirs(cwd, exist_ok=True)
        await self._ensure_proc()
        res = await self.call("session/new", {"cwd": cwd, "mcpServers": []})
        acp_sid = str((res or {}).get("sessionId") or "")
        if not acp_sid:
            raise GeminiError("session/new 沒回 sessionId", code="BAD_RESPONSE")
        self._loaded.add(acp_sid)
        return remember_session(key, acp_sid, title=title, cwd=cwd)

    async def ensure_session(self, key: str) -> str:
        """key → 可用的 acp_sid。子程序重啟後先 session/load 續接;載不回
        (CLI 更新/狀態清掉)就開新 ACP session 換 acp_sid 續命 —— bridge 端
        歷史(逐字稿/卡)不動,只有模型側的對話記憶重開。"""
        await self._ensure_proc()
        row = get_session(key)
        if row is None:
            row = await self.new_session(key)
            return str(row["acp_sid"])
        acp_sid = str(row.get("acp_sid") or "")
        cwd = os.path.expanduser(row.get("cwd") or _DEFAULT_CWD)
        if acp_sid and acp_sid in self._loaded:
            return acp_sid
        if acp_sid:
            try:
                await self.call("session/load",
                                {"sessionId": acp_sid, "cwd": cwd,
                                 "mcpServers": []})
                self._loaded.add(acp_sid)
                return acp_sid
            except GeminiError as e:
                self._log("gemini_session_reopen", key=key,
                          error=str(e)[:120])
        row = await self.new_session(key, cwd=cwd)
        return str(row["acp_sid"])

    async def prompt(self, key: str, text: str) -> dict:
        """送一回合;逐字經 on_update 外流,本呼叫等 turn 結束(stopReason)。"""
        acp_sid = await self.ensure_session(key)
        touch_session(key, title_hint=text)
        res = await self.call("session/prompt",
                              {"sessionId": acp_sid,
                               "prompt": [{"type": "text", "text": text}]},
                              timeout=_TURN_TIMEOUT)
        return res or {}

    async def cancel(self, key: str):
        """中斷進行中的 turn(ACP session/cancel 是 notification)。"""
        row = get_session(key)
        acp_sid = str((row or {}).get("acp_sid") or "")
        if not acp_sid or self.proc is None or self.proc.returncode is not None:
            return
        line = json.dumps({"jsonrpc": "2.0", "method": "session/cancel",
                           "params": {"sessionId": acp_sid}}) + "\n"
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()

    def acp_to_key(self, acp_sid: str) -> str | None:
        """on_update 的 sessionId(ACP)→ bridge key。"""
        for k, row in _load_sessions().items():
            if str(row.get("acp_sid") or "") == acp_sid:
                return k
        return None


# ── v2 session 形狀助手(bridge 對映用,不碰網路)─────────────────────────────

def session_v2_id(sid: str) -> str:
    return f"gemini:{sid}"


def session_v2_row(row: dict, busy: bool = False) -> dict:
    sid = str(row.get("id") or "")
    return {"id": session_v2_id(sid), "provider": "gemini",
            "title": row.get("title") or "Gemini",
            "subtitle": None,
            "status": "running" if busy else "idle",
            "last_event_at": float(row.get("last") or 0) or None,
            "capabilities": ["input", "interrupt", "replay", "follow", "approve"],
            "meta": {}}
