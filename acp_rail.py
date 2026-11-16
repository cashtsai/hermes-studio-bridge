from __future__ import annotations
"""共用 ACP(Agent Client Protocol)子程序泵 —— code-agent provider 的傳輸底座。

## 為什麼現在抽(2026-11-16)

`grok_sdk.GrokClient` 的 docstring 當時寫得很明白:JSON-RPC 泵是從
`gemini_provider.GeminiClient` **最小複製**來的,而且「等第三家純 ACP
provider 出現再抽共用層(三點成線)」。opencode 就是第三家 —— 複製第三次
就是三份會各自漂移的真相(ACP 協定錯誤處理、request 回覆的 fail-safe、
斷線清理這些**不該**有三種寫法)。

所以:
- `AcpProcessClient` = 子程序生命週期 + JSON-RPC 呼叫/通知泵 + server→client
  請求的 fail-safe 回覆。grok 與 opencode 兩家共用。
- **gemini_provider.GeminiClient 一個位元都不動**:那是 production 在跑的
  rail,為了新 provider 去重構它,風險(回歸)大於收益。同理
  `acp_client.py`(hermes 人格軌)也不動 —— 它是另一套在跑的東西,
  名字相近但職責不同(人格回合,非 code-agent session 名錄)。

## 子類要提供什麼(全部是「這一家怎麼起行程」的知識)

必要:`_bin_path()`、`_spawn_argv(binp)`、`_no_binary_message()`
選用:`_spawn_env()`、`_spawn_cwd()`、`_stderr_log_path()`、
`_initialize_params()`、`_connected_fields(init)`、`_on_proc_reset()`、
`_on_update_frame(sid, update)`(load replay 擷取等)。

子類**不要**覆寫泵本體(_read_loop/_call_locked/_answer_server_request)——
那正是這個檔案存在的理由。協定層的教訓都釘在這裡:
- server→client 請求**無論如何都要回**(不回,agent 的 turn 永遠掛著)。
- 未掛 on_request / 回 None → `session/request_permission` 保守 cancelled。
- 單筆通知的 handler 丟例外不准斷流(毒丸不該讓整條 session 瞎掉)。
- 行程死了 → 所有在途 future 收到 PROCESS_LOST(不是無限等)。
"""

import asyncio
import json
import os


class AcpError(Exception):
    """ACP 回的結構化錯誤或傳輸層失敗。

    `code` 可能是 JSON-RPC 的數字碼(字串化,如 "-32601")或我們自己的
    語意碼(NO_BINARY / PROCESS_LOST / TIMEOUT / BAD_RESPONSE / …)——
    呼叫端兩種都要認得(bridge 的失敗翻譯表就靠這個)。
    """

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AcpProcessClient:
    """單一常駐 ACP agent 子程序(stdio JSON-RPC)的客戶端底座。

    on_update(session_id:str, update:dict) — `session/update` 通知。
    on_request(req:dict) → awaitable|None — server→client 請求
        (`session/request_permission` 等)。未掛或回 None → 保守拒絕。
    """

    #: log 事件前綴({prefix}_connected / _disconnected / _rx / _tx)。
    _log_prefix = "acp"
    #: 設了這個 env 就把每一幀收送都寫 log(查協定問題用)。
    _debug_env = ""
    #: 這一家的錯誤型別(子類覆寫成自己的,bridge 的 except 才對得上)。
    _error_cls = AcpError

    def __init__(self, log=None):
        self._log = log or (lambda *_a, **_k: None)
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._rid = 0
        self._reader: asyncio.Task | None = None
        self.on_update = None
        self.on_request = None
        #: initialize 的完整回應(capabilities / authMethods / _meta)。
        #: 擴展能力要從這裡 feature-detect,**不硬編**。
        self.server_info: dict = {}

    # ── 子類掛鉤 ────────────────────────────────────────────────────────

    def _bin_path(self) -> str:
        """CLI 執行檔絕對路徑;找不到回空字串(= 未安裝)。"""
        raise NotImplementedError

    def _spawn_argv(self, binp: str) -> list:
        """起 ACP 模式的 argv(如 [binp, "acp"] / [binp, "agent", "stdio"])。"""
        raise NotImplementedError

    def _no_binary_message(self) -> str:
        """未安裝時給人看的話(含安裝指令)。"""
        return "CLI not installed"

    def _spawn_env(self) -> dict:
        return dict(os.environ)

    def _spawn_cwd(self) -> str | None:
        return None

    def _stderr_log_path(self) -> str:
        """stderr 落檔路徑;回 "" / off / none / devnull = 丟掉。

        預設落檔不丟:gemini「很慢查不到原因」的教訓 —— CLI 的致命訊息
        幾乎都在 stderr,DEVNULL 掉就只剩「逾時」兩個字可查。
        """
        return ""

    def _initialize_params(self) -> dict:
        return {"protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False,
                                              "writeTextFile": False}},
                "clientInfo": {"name": "studio-bridge", "version": "1.0"}}

    def _spawn_timeout(self) -> float:
        return 45.0

    def _default_call_timeout(self) -> float:
        return 30.0

    def _connected_fields(self, init: dict) -> dict:
        """`{prefix}_connected` 事件要附的欄位(版本/模型/身分來源等)。"""
        return {}

    def _on_proc_reset(self) -> None:
        """每次 spawn / 斷線都會呼叫:清掉與這顆行程綁定的子類狀態
        (已載入的 session 集合、feature-detect 結果等)。"""

    def _on_update_frame(self, session_id: str, update: dict) -> None:
        """`session/update` 的旁聽掛鉤(在 on_update 之前跑)。

        用途:session/load 的歷史 replay 擷取。丟例外不會斷流(與
        on_update 同款毒丸隔離),但會留 log。
        """

    # ── 便利 ──

    def _err(self, message: str, code: str = "", retryable: bool = False):
        return self._error_cls(message, code=code, retryable=retryable)

    def configured(self) -> bool:
        """「存在」= binary 在。未安裝 → bridge 全面靜默缺席(caps 不宣告)。"""
        return bool(self._bin_path())

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # ── 子程序/握手 ──

    async def _ensure_proc(self):
        if self.alive():
            return
        async with self._lock:
            if self.alive():
                return
            binp = self._bin_path()
            if not binp:
                raise self._err(self._no_binary_message(), code="NO_BINARY")
            cwd = self._spawn_cwd()
            if cwd:
                os.makedirs(cwd, exist_ok=True)
            errlog = (self._stderr_log_path() or "").strip()
            if errlog.lower() in ("", "off", "none", "devnull"):
                stderr_to = asyncio.subprocess.DEVNULL
            else:
                try:
                    os.makedirs(os.path.dirname(errlog) or ".", exist_ok=True)
                    stderr_to = open(errlog, "ab")
                except OSError:
                    stderr_to = asyncio.subprocess.DEVNULL
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self._spawn_argv(binp),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_to,
                    env=self._spawn_env(), cwd=cwd)
            except FileNotFoundError:
                raise self._err(self._no_binary_message(), code="NO_BINARY")
            self._on_proc_reset()
            self._reader = asyncio.create_task(self._read_loop(self.proc))
            try:
                init = await self._call_locked(
                    "initialize", self._initialize_params(),
                    timeout=self._spawn_timeout())
                self.server_info = init or {}
            except Exception:
                await self._drop_proc()
                raise
            self._log(f"{self._log_prefix}_connected",
                      protocol=(self.server_info or {}).get("protocolVersion"),
                      **self._connected_fields(self.server_info))

    async def _drop_proc(self):
        proc, self.proc = self.proc, None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(self._err("agent process lost",
                                            code="PROCESS_LOST",
                                            retryable=True))
        self._pending.clear()
        self._on_proc_reset()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — 已經死了就算了
                pass
        self._log(f"{self._log_prefix}_disconnected")

    async def _read_loop(self, proc):
        debug = bool(self._debug_env and os.environ.get(self._debug_env))
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                if debug:
                    self._log(f"{self._log_prefix}_rx",
                              frame=raw.decode("utf-8", "replace")[:200])
                try:
                    f = json.loads(raw.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 — 非 JSON 行跳過
                    continue
                if not isinstance(f, dict):
                    continue
                if "id" in f and ("result" in f or "error" in f):
                    self._settle(f)
                    continue
                method = f.get("method") or ""
                if method == "session/update":
                    params = f.get("params") or {}
                    sid = str(params.get("sessionId") or "")
                    update = params.get("update") or {}
                    try:
                        self._on_update_frame(sid, update)
                    except Exception as e:  # noqa: BLE001 — 旁聽不准斷流
                        self._log(f"{self._log_prefix}_update_hook_error",
                                  error=str(e)[:160])
                    if self.on_update:
                        try:
                            self.on_update(sid, update)
                        except Exception:  # noqa: BLE001 — 單筆毒丸不斷流
                            pass
                elif "id" in f:
                    # server→client 請求(session/request_permission 等)。
                    asyncio.get_running_loop().create_task(
                        self._answer_server_request(proc, f))
                # 其餘通知刻意不接:它們是進度 metadata,各家擴展會長新型別,
                # 接了就有維護債(能力一律從 initialize feature-detect)。
        except Exception:  # noqa: BLE001 — 斷線走同一條清理路
            pass
        finally:
            if self.proc is proc:
                await self._drop_proc()

    def _settle(self, f: dict) -> None:
        """回應幀 → 叫醒在途 future。id 不是我們發的數字就丟掉。"""
        rid = f.get("id")
        if not str(rid if rid is not None else "").lstrip("-").isdigit():
            return
        fut = self._pending.pop(int(rid), None)
        if fut is None or fut.done():
            return
        if "error" in f:
            err = f["error"] or {}
            msg = str(err.get("message") or "acp error")
            data = err.get("data")
            if data:
                msg = f"{msg}: {str(data)[:200]}"
            fut.set_exception(self._err(msg, code=str(err.get("code") or "")))
        else:
            fut.set_result(f.get("result"))

    async def _answer_server_request(self, proc, f: dict):
        """server→client 請求:bridge 的 on_request 決定;沒人接=保守拒絕。
        無論如何都要回 —— 不回,agent 的 turn 會永遠掛著(blocking request)。
        """
        rid = f.get("id")
        result = None
        try:
            if self.on_request:
                result = await self.on_request(f)
        except Exception as e:  # noqa: BLE001
            self._log(f"{self._log_prefix}_on_request_error", error=str(e)[:160])
        if result is None:
            method = f.get("method") or ""
            if method == "session/request_permission":
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                result = {}
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid,
                                          "result": result}) + "\n").encode())
            await proc.stdin.drain()
        except Exception:  # noqa: BLE001 — 行程死了由 read_loop 收屍
            pass

    # ── 呼叫 ──

    async def _call_locked(self, method: str, params: dict,
                           timeout: float | None = None):
        """已確定行程活著時的呼叫(spawn 期間的 initialize 也走這條)。"""
        if timeout is None:
            timeout = self._default_call_timeout()
        self._rid += 1
        rid = self._rid
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params}) + "\n"
        if self._debug_env and os.environ.get(self._debug_env):
            self._log(f"{self._log_prefix}_tx", frame=line[:200])
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise self._err(f"{self._log_prefix} call timeout: {method}",
                            code="TIMEOUT", retryable=True)

    async def call(self, method: str, params: dict | None = None,
                   timeout: float | None = None):
        await self._ensure_proc()
        return await self._call_locked(method, params or {}, timeout=timeout)

    async def notify(self, method: str, params: dict | None = None) -> bool:
        """送 notification(無 id,不等回應)。行程不在 → False,不冷啟。

        為什麼不冷啟:notification 的唯一用途是對**進行中**的事情下指令
        (session/cancel)。行程不在就沒有進行中的事,為此開一顆行程只會
        製造「中斷了一個不存在的回合」的假象。
        """
        if not self.alive():
            return False
        line = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params or {}}) + "\n"
        if self._debug_env and os.environ.get(self._debug_env):
            self._log(f"{self._log_prefix}_tx", frame=line[:200])
        try:
            self.proc.stdin.write(line.encode())
            await self.proc.stdin.drain()
        except Exception:  # noqa: BLE001 — 行程死了由 read_loop 收屍
            return False
        return True


# ── ACP update 幀 → 統一訊息列的投影(兩家共用的純函式)────────────────

def project_updates_to_messages(updates: list, id_prefix: str) -> list[dict]:
    """ACP update 幀列 → `{id, role, content, ts}` 訊息列(舊→新)。

    規則(grok 刀訂的,opencode 實測同構):
    - 連續同角色的 *_message_chunk 併成一則(ACP 逐字就是這樣切的);
    - thought / tool_call / plan / usage / config 等非正文幀略過 ——
      這條投影餵的是「訊息歷史」分頁,工具過程由卡片流呈現;
    - 認不出的幀一律跳過(各家擴展會長新型別,歷史分頁壞掉不值得)。

    三種包法都吃:`{update:…}`、原始通知 `{params:{update:…}}`、裸 update。
    """
    out: list[dict] = []
    for fr in updates or []:
        if not isinstance(fr, dict):
            continue
        upd = fr.get("update") if isinstance(fr.get("update"), dict) else (
            (fr.get("params") or {}).get("update")
            if isinstance(fr.get("params"), dict) else None)
        if upd is None and fr.get("sessionUpdate"):
            upd = fr
        if not isinstance(upd, dict):
            continue
        kind = str(upd.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            role = "assistant"
        elif kind == "user_message_chunk":
            role = "user"
        else:
            continue
        content = upd.get("content")
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        elif isinstance(content, list):
            text = "\n".join(str((c or {}).get("text") or "") for c in content
                             if isinstance(c, dict))
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += text
        else:
            out.append({"id": f"{id_prefix}h{len(out)}", "role": role,
                        "content": text, "ts": None})
    return out
