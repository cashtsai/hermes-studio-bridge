from __future__ import annotations
"""OpenClaw gateway WS 客戶端 — Pocket 第四 provider 的傳輸層。

契約:docs/OPENCLAW_PROVIDER_SPEC.md(靶機 2026.7.1-2 實測)。
職責邊界:本模組只管「連線設定 + WS 握手/呼叫/事件泵」;卡片 digest 在
carddigest.OpenClawDigest,v2 路由/推播接線在 bridge.py(S4 區)。

設計要點:
- 未配置(env 與設定檔皆缺 base_url)→ `configured()` False,bridge 全部
  靜默缺席(照 APNs 金鑰缺席模式),本模組不開任何連線。
- 單一常駐連線,lazy 建立(第一個 call/ensure_started 才連);斷線指數退避
  重連,重連成功後呼叫 on_reconnect(bridge 對有訂閱者的 session 重 seed,
  SPEC §6-4:gateway 事件無 since 補洞)。
- 握手:等 server 的 connect.challenge 事件 → 送 connect req(mode="cli",
  webchat 會踩 Control UI origin 檢查,SPEC §1)→ hello-ok。
- 事件(chat/agent)原樣轉交 on_event(event, payload);tick/health 濾掉。
"""

import asyncio
import json
import os
import time
import uuid

try:
    import websockets
except ImportError:                    # pragma: no cover — bridge venv 一定有
    websockets = None

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("OPENCLAW_CONFIG_FILE", "~/.pocket/openclaw.json"))
_CALL_TIMEOUT = float(os.environ.get("OPENCLAW_CALL_TIMEOUT", "30"))
_CONNECT_TIMEOUT = float(os.environ.get("OPENCLAW_CONNECT_TIMEOUT", "20"))
_MAX_FRAME = 32 * 1024 * 1024          # gateway maxPayload 26MB,留餘裕
_BACKOFF_MAX = 60.0


class OpenClawError(Exception):
    """gateway 回的結構化錯誤({code,message})或傳輸層失敗。"""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def load_config() -> dict:
    """env 優先(OPENCLAW_BASE_URL/OPENCLAW_TOKEN),否則讀設定檔
    (~/.pocket/openclaw.json,PUT /app/v1/openclaw/config 寫入)。"""
    base = (os.environ.get("OPENCLAW_BASE_URL") or "").strip()
    token = (os.environ.get("OPENCLAW_TOKEN") or "").strip()
    if base:
        return {"base_url": base, "token": token, "source": "env"}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return {"base_url": str(d.get("base_url") or "").strip(),
                "token": str(d.get("token") or "").strip(), "source": "file"}
    except Exception:  # noqa: BLE001 — 缺檔/壞檔一律視為未配置
        return {"base_url": "", "token": "", "source": "none"}


def save_config(base_url: str, token: str) -> dict:
    """App「進階」頁的手動配置落檔(0600)。env 有值時 env 仍優先——
    回傳的 source 讓呼叫端能提示這件事。"""
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"base_url": (base_url or "").strip(),
                   "token": (token or "").strip()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _CONFIG_FILE)
    return load_config()


def ws_url(base: str) -> str:
    """base_url 歸一成 ws(s):// 形狀;http→ws、https→wss、無 scheme 補 ws。"""
    b = (base or "").strip().rstrip("/")
    if b.startswith("http://"):
        return "ws://" + b[len("http://"):]
    if b.startswith("https://"):
        return "wss://" + b[len("https://"):]
    if b.startswith(("ws://", "wss://")):
        return b
    return "ws://" + b


class OpenClawClient:
    """單連線 JSON-RPC-over-WS 客戶端(protocol 4)。

    on_event(event:str, payload:dict) — chat/agent 事件(bridge 掛)。
    on_reconnect() — 重連握手成功後(bridge 重 seed 有訂閱者的 digest)。
    """

    def __init__(self, log=None):
        self._log = log or (lambda *_a, **_k: None)
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._rid = 0
        self._connected_once = False
        self._backoff = 1.0
        self.on_event = None
        self.on_reconnect = None
        self.server_info: dict = {}

    # ── 配置 ──

    def configured(self) -> bool:
        return bool(websockets) and bool(load_config()["base_url"])

    # ── 連線/握手 ──

    async def _handshake(self, ws, cfg: dict) -> dict:
        """等 connect.challenge → 送 connect → 回 hello-ok payload。"""
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while True:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=max(0.1, deadline - time.monotonic()))
            f = json.loads(raw)
            if f.get("type") == "event" and f.get("event") == "connect.challenge":
                break
            # challenge 前不該有別的訊框;有就忽略再等
        cid = "connect-" + uuid.uuid4().hex[:8]
        await ws.send(json.dumps({
            "type": "req", "id": cid, "method": "connect",
            "params": {
                "minProtocol": 1, "maxProtocol": 5,
                "client": {"id": "cli", "version": "pocket-bridge",
                           "platform": "macos", "mode": "cli"},
                "role": "operator",
                "scopes": ["operator.read", "operator.write",
                           "operator.approvals"],
                "auth": {"token": cfg.get("token") or ""},
            }}))
        while True:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=max(0.1, deadline - time.monotonic()))
            f = json.loads(raw)
            if f.get("type") == "res" and f.get("id") == cid:
                if not f.get("ok"):
                    err = f.get("error") or {}
                    raise OpenClawError(err.get("message") or "connect rejected",
                                        code=str(err.get("code") or ""))
                return f.get("payload") or {}
            # 握手期間的事件(health…)先吞,reader 起來後才轉交

    async def _ensure_ws(self):
        if self._ws is not None:
            return self._ws
        async with self._lock:
            if self._ws is not None:
                return self._ws
            cfg = load_config()
            if not cfg["base_url"]:
                raise OpenClawError("openclaw not configured", code="NOT_CONFIGURED")
            if websockets is None:
                raise OpenClawError("websockets library unavailable",
                                    code="NO_WEBSOCKETS")
            url = ws_url(cfg["base_url"])
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(url, max_size=_MAX_FRAME,
                                       open_timeout=_CONNECT_TIMEOUT),
                    timeout=_CONNECT_TIMEOUT + 5)
                hello = await self._handshake(ws, cfg)
            except OpenClawError:
                raise
            except Exception as e:  # noqa: BLE001
                raise OpenClawError(f"openclaw connect failed: {e}",
                                    code="CONNECT_FAILED", retryable=True) from e
            self.server_info = hello.get("server") or {}
            self._ws = ws
            self._backoff = 1.0
            was_reconnect = self._connected_once
            self._connected_once = True
            self._reader = asyncio.create_task(self._read_loop(ws))
            self._log("openclaw_connected", url=url,
                      server=str(self.server_info.get("version") or ""),
                      protocol=hello.get("protocol"))
            if was_reconnect and self.on_reconnect:
                try:
                    self.on_reconnect()
                except Exception:  # noqa: BLE001 — 重 seed 失敗不拖垮連線
                    pass
            return ws

    async def _read_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    f = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                t = f.get("type")
                if t == "res":
                    fut = self._pending.pop(str(f.get("id")), None)
                    if fut and not fut.done():
                        if f.get("ok"):
                            fut.set_result(f.get("payload"))
                        else:
                            err = f.get("error") or {}
                            fut.set_exception(OpenClawError(
                                err.get("message") or "openclaw error",
                                code=str(err.get("code") or ""),
                                retryable=bool(err.get("retryable"))))
                elif t == "event":
                    ev = f.get("event") or ""
                    if ev in ("tick", "health", "connect.challenge"):
                        continue
                    if self.on_event:
                        try:
                            self.on_event(ev, f.get("payload") or {})
                        except Exception:  # noqa: BLE001 — 單筆事件毒丸不斷流
                            pass
        except Exception:  # noqa: BLE001 — 斷線走同一條清理路
            pass
        finally:
            self._drop_ws(ws)

    def _drop_ws(self, ws):
        if self._ws is ws:
            self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(OpenClawError("openclaw connection lost",
                                                code="CONNECTION_LOST",
                                                retryable=True))
        self._pending.clear()
        self._log("openclaw_disconnected")

    async def close(self):
        ws, self._ws = self._ws, None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    def reset(self):
        """配置變更後強制下次呼叫重連(fire-and-forget)。"""
        ws, self._ws = self._ws, None
        self._connected_once = False
        if ws is not None:
            try:
                asyncio.get_running_loop().create_task(ws.close())
            except RuntimeError:
                pass

    # ── 呼叫 ──

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = _CALL_TIMEOUT):
        ws = await self._ensure_ws()
        self._rid += 1
        rid = f"b{self._rid}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send(json.dumps({"type": "req", "id": rid,
                                      "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise OpenClawError(f"openclaw call timeout: {method}",
                                code="TIMEOUT", retryable=True)
        except Exception:
            self._pending.pop(rid, None)
            raise

    # ── 常駐事件泵(bridge startup 啟用;沒配置就純 no-op)──

    async def run_forever(self):
        """保持連線活著(斷了退避重連),事件經 on_event 外流。"""
        while True:
            if not self.configured():
                await asyncio.sleep(30)
                continue
            try:
                await self._ensure_ws()
                # reader 活著時本 task 只要等它斷
                reader = self._reader
                if reader is not None:
                    await asyncio.shield(asyncio.wait({reader}))
            except OpenClawError as e:
                self._log("openclaw_reconnect_wait", error=e.code or str(e),
                          backoff=self._backoff)
            except Exception as e:  # noqa: BLE001
                self._log("openclaw_reconnect_wait", error=str(e)[:120],
                          backoff=self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX)


# ── session 形狀助手(bridge v2 對映用,不碰網路)──

def session_v2_id(key: str) -> str:
    return f"openclaw:{key}"


def session_short_name(key: str) -> str:
    """sessionKey `agent:main:main` → `main`;其他形狀取最後一段。"""
    parts = [p for p in (key or "").split(":") if p]
    if len(parts) >= 3 and parts[0] == "agent":
        agent, name = parts[1], ":".join(parts[2:])
        return name if agent in ("main", name) else f"{agent}/{name}"
    return parts[-1] if parts else "openclaw"


def session_status(row: dict) -> str:
    """SessionRow → v2 status(SPEC §2:hasActiveRun 是忙碌權威)。"""
    if row.get("hasActiveRun"):
        return "running"
    return "idle"


def session_v2_row(row: dict) -> dict:
    """sessions.list 的 SessionRow → /app/v2/sessions 的統一 Session 形狀。
    caps 依 SPEC §4:v1 = input/interrupt/replay/follow(attachments/approve
    明示缺席)。"""
    key = str(row.get("key") or "")
    updated = row.get("updatedAt")
    last = None
    if isinstance(updated, (int, float)) and updated > 0:
        last = updated / 1000.0 if updated > 1e11 else float(updated)
    model = str(row.get("model") or "")
    return {"id": session_v2_id(key), "provider": "openclaw",
            "title": session_short_name(key),
            "subtitle": model or None,
            "status": session_status(row),
            "last_event_at": last,
            "capabilities": ["input", "interrupt", "replay", "follow"],
            "meta": {}}
