from __future__ import annotations
"""Grok Build CLI(ACP)客戶端 — Pocket 第五家 code-agent provider 的傳輸層。

協定:`grok agent stdio`(標準 ACP,JSON-RPC over stdio,protocolVersion 1)。
研究依據:pa-ship docs/GROK_BUILD_RESEARCH_20261116.md「第五 provider 判決」——
grok 是五家裡協定最乾淨的一家(純 ACP,不用自家 adapter),流程:
initialize → session/new|load → session/prompt,回覆經 `session/update`
通知逐字送達(agent_message_chunk / agent_thought_chunk / tool_call / plan)。

職責邊界(照 gemini_provider / openclaw_provider 慣例):本模組只管
「配置 + 子程序生命週期 + JSON-RPC 呼叫/通知泵 + session 名錄持久化」;
卡片 digest 在 carddigest.GrokDigest,v2 路由/審批接線在 bridge.py(S6 區)。

## 泵在哪:acp_rail.AcpProcessClient(2026-11-16 抽出共用層)

JSON-RPC 泵原本是從 `gemini_provider.GeminiClient` 最小複製來的,當時在這裡
留了一句「等第三家純 ACP provider 出現再抽共用層(三點成線)」。opencode 就是
第三家 —— 泵已經搬進 `acp_rail.py`(grok/opencode 共用),本檔只留「grok 怎麼
起行程 + 名錄/逐字稿/x.ai 擴展」。`GrokClient` 的對外介面
(configured/logged_in/ensure_session/call/cancel/updates_tail/server_info)
一個位元都沒變 —— 這一刀的回歸網就是 S6 那 57 條測試。

刻意**不**回頭改 `gemini_provider.GeminiClient`:gemini 是 production 在跑的
rail,為了新 provider 去重構它,風險(回歸)大於收益。兩家的差異也真實存在:
gemini 要 authenticate 握手(oauth/api-key 計畫),grok 用 ambient 憑證
(~/.grok/auth.json 或 XAI_API_KEY env),沒有 authenticate 呼叫。

設計要點:
- 偵測照 agy 模式:binary 不在 → configured() False → bridge 全部靜默缺席
  (caps 不宣告;CI/測試機沒裝 grok 一樣全綠)。
- 認證:`XAI_API_KEY` env(或 ~/.pocket/grok.json 落檔)或既有 `grok login`
  憑證(~/.grok/auth.json,CLI 自己管 refresh)。未登入 → NOT_LOGGED_IN,
  bridge 翻成人話 + 登入指引(照 CC/CX 登入狀態模式),不在 spawn 期猜。
- 單一常駐 `grok agent stdio` 子程序,lazy spawn;死了下次呼叫重拉,
  既有 ACP session 由 session/load 續接(initialize 實測 loadSession: true)。
- `x.ai/*` 擴展**不硬編**(官方文件明言 non-exhaustive):能力以 initialize
  回應為準(server_info 存起來),x.ai/session/updates 用 -32601 探測退場。
- 名錄由本模組持久化(~/.pocket/grok/sessions.json),逐字稿落
  ~/.pocket/grok/<key>.jsonl(seed/重啟補卡用)—— 與 gemini 同一套形狀,
  bridge 的統一 history 路由對五家回一樣的東西。
"""

import asyncio
import json
import os
import shutil
import time
import uuid

import acp_rail
import cjk_text

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("GROK_CONFIG_FILE", "~/.pocket/grok.json"))
_STATE_DIR = os.path.expanduser(
    os.environ.get("GROK_STATE_DIR", "~/.pocket/grok"))
_SESSIONS_FILE = os.path.join(_STATE_DIR, "sessions.json")
_CALL_TIMEOUT = float(os.environ.get("GROK_CALL_TIMEOUT", "30"))
_TURN_TIMEOUT = float(os.environ.get("GROK_TURN_TIMEOUT", "600"))
_SPAWN_TIMEOUT = float(os.environ.get("GROK_SPAWN_TIMEOUT", "45"))
_DEFAULT_CWD = os.path.expanduser(
    os.environ.get("GROK_WORKDIR", "~/.pocket/grok/workspace"))
#: 模型是 spawn 參數(`grok agent -m <model> stdio`)—— 全域,不是 per-session
#: (同 gemini 的取捨;per-session 模型其實有 session/set_config_option 可走,
#: 列在下一波)。空字串 = 沿用 CLI 預設(實測 1.0.77 預設 grok-4.6)。
_MODEL_ENV = (os.environ.get("GROK_MODEL") or "").strip()

#: `grok login` 落的憑證(CLI 自己 refresh;檔案在不在就是登入狀態的真相,
#: 搶著驗 token 有效性只會做出第二份會漂移的真相 —— 同 gemini oauth 判準)。
_AUTH_FILE = os.path.expanduser(
    os.environ.get("GROK_AUTH_FILE", "~/.grok/auth.json"))


class GrokError(acp_rail.AcpError):
    """grok ACP 回的結構化錯誤或傳輸層失敗(欄位同 AcpError)。"""


# ── 配置/偵測 ───────────────────────────────────────────────────────────────

def _read_file() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — 缺檔/壞檔一律視為未配置
        return {}


def load_config() -> dict:
    """env 優先(XAI_API_KEY / GROK_MODEL),否則讀 ~/.pocket/grok.json。
    `model` 與 `api_key` 來源各自獨立判定(同 gemini 的教訓)。"""
    d = _read_file()
    file_model = str(d.get("model") or "").strip()
    model = _MODEL_ENV or file_model
    out = {"model": model,
           "model_source": ("env" if _MODEL_ENV
                            else "file" if file_model else "none")}
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    if key:
        return {**out, "api_key": key, "source": "env"}
    return {**out,
            "api_key": str(d.get("api_key") or "").strip(),
            "source": "file" if d else "none"}


def save_config(api_key: str | None = None, model: str | None = None) -> dict:
    """設定面板落檔(0600);合併寫入 —— 只帶 model 不可洗掉 api_key。"""
    d = _read_file()
    if api_key is not None:
        d["api_key"] = api_key.strip()
    if model is not None:
        d["model"] = model.strip()
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _CONFIG_FILE)
    return load_config()


def grok_bin() -> str:
    """grok CLI 執行檔;找不到回空字串(= 未安裝,caps 不宣告)。

    官方 installer 連進 ~/.local/bin/grok 與 ~/.grok/bin/grok ——
    launchd/systemd 服務的 PATH 常沒這兩段,which 之外要明確摸(agy 同款)。"""
    env = (os.environ.get("POCKET_GROK_BIN") or "").strip()
    if env:
        p = os.path.expanduser(env)
        return p if os.path.exists(p) else ""
    found = shutil.which("grok")
    if found:
        return found
    for cand in ("~/.local/bin/grok", "~/.grok/bin/grok"):
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return ""


def login_available() -> bool:
    """機器上有沒有 `grok login` 憑證快取(檔案在=有;refresh 交給 CLI)。"""
    return os.path.isfile(_AUTH_FILE)


def auth_state() -> dict:
    """給 config 端點/登入指引用:{logged_in, key_set, method}。
    method = 這顆行程將採用的身分來源(CLI 的解析序:簽入 session > env key)。
    """
    cfg = load_config()
    oauth = login_available()
    key = bool(cfg["api_key"])
    return {"logged_in": oauth, "key_set": key,
            "method": ("login" if oauth else "api-key" if key else "none")}


#: 未登入時給使用者的話(bridge 的失敗規則與 ensure_session 共用一份)。
LOGIN_GUIDANCE = ("Grok 還沒登入。在主機終端機跑一次 `grok login`"
                  "(無瀏覽器環境用 `grok login --device-auth`),"
                  "或設定 XAI_API_KEY(console.x.ai)後再回來。")


# ── session 名錄(形狀同 gemini_provider,bridge key ↔ ACP sessionId 解耦:
#    CLI 端 session 蒸發時換 acp_sid 續命,bridge 端歷史不動)───────────────

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
    """名錄 → [{id,acp_sid,title,cwd,created,last}],last 新→舊;封存不列。"""
    rows = [r for r in _load_sessions().values() if not r.get("archived")]
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
        row["title"] = cjk_text.clip(title, 60, suffix="")   # grapheme 邊界
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
    if title_hint and not row.get("title"):
        row["title"] = cjk_text.clip(title_hint, 60, suffix="")
    _save_sessions(d)


def rename_session(key: str, title: str) -> dict | None:
    d = _load_sessions()
    row = d.get(key)
    if not row:
        return None
    row["title"] = cjk_text.clip((title or "").strip(), 60, suffix="")
    _save_sessions(d)
    return row


def set_archived(key: str, archived: bool) -> dict | None:
    """封存=不列出來,逐字稿與卡照留(對齊 cc2/gemini;不可用 forget 冒充)。"""
    d = _load_sessions()
    row = d.get(key)
    if not row:
        return None
    row["archived"] = bool(archived)
    _save_sessions(d)
    return row


def forget_session(key: str) -> None:
    d = _load_sessions()
    if key in d:
        del d[key]
        _save_sessions(d)


# ── 逐字稿(seed 補卡 + 統一 history 路由)───────────────────────────────────

def transcript_path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
    return os.path.join(_STATE_DIR, f"{safe}.jsonl")


def transcript_append(sid: str, role: str, text: str) -> str:
    os.makedirs(_STATE_DIR, exist_ok=True)
    mid = f"gk-{uuid.uuid4().hex[:12]}"
    with open(transcript_path(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "role": role, "content": text,
                            "ts": time.time()}, ensure_ascii=False) + "\n")
    return mid


def transcript_page(sid: str, limit: int = 40, offset: int = 0) -> dict:
    """歷史分頁 —— 形狀對齊 cc2/gemini 的 `{messages, more, total}`
    (offset 從最新往回數,messages 舊→新;app 只有一個解析器)。"""
    try:
        with open(transcript_path(sid), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001 — 新 session 沒逐字稿
        return {"messages": [], "more": False, "total": 0}
    rows = []
    for line in lines:
        try:
            m = json.loads(line)
            if isinstance(m, dict):
                rows.append(m)
        except Exception:  # noqa: BLE001 — 壞行跳過
            continue
    total = len(rows)
    end = max(0, total - max(0, offset))
    start = max(0, end - max(1, limit))
    return {"messages": rows[start:end], "more": start > 0, "total": total}


def transcript_read(sid: str, limit: int = 200) -> list[dict]:
    try:
        with open(transcript_path(sid), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines[-limit:]:
        try:
            m = json.loads(line)
            if isinstance(m, dict):
                out.append(m)
        except Exception:  # noqa: BLE001
            continue
    return out


# ── x.ai/session/updates → 統一 history 形狀的投影 ──────────────────────────

def project_updates_to_messages(updates: list) -> list[dict]:
    """ACP update 幀(x.ai/session/updates 的 `updates` 陣列)→
    `{id, role, content, ts}` 訊息列(舊→新)。投影規則與 id 前綴以外的
    邏輯都在 `acp_rail.project_updates_to_messages`(opencode 同用一份)。"""
    return acp_rail.project_updates_to_messages(updates, "gk-")


# ── ACP 客戶端 ─────────────────────────────────────────────────────────────

class GrokClient(acp_rail.AcpProcessClient):
    """單一常駐 `grok agent stdio` 子程序的 ACP 客戶端。

    泵(spawn/read_loop/call/server→client 回覆)在 acp_rail;本類只描述
    「grok 怎麼起行程」與 grok 專屬的 session/擴展語意。

    on_update(session_id:str, update:dict) — session/update 通知(bridge 掛)。
    on_request(req:dict) → awaitable|None — server→client 請求
        (session/request_permission 等)。未掛或回 None → 保守拒絕。
    """

    _log_prefix = "grok"
    _debug_env = "GROK_DEBUG"
    _error_cls = GrokError

    def __init__(self, log=None):
        super().__init__(log=log)
        self._loaded: set[str] = set()   # 本程序已 new/load 過的 ACP session
        #: 跑著的行程實際用的身分("login" / "api-key";未連線 = "")。
        self.auth_used = ""
        #: x.ai/session/updates 探測結果(None=未探,True/False=探過)。
        self._updates_supported: bool | None = None

    # ── acp_rail 掛鉤(「grok 怎麼起行程」)──

    def _bin_path(self) -> str:
        return grok_bin()

    def _no_binary_message(self) -> str:
        return "grok CLI 不在(curl -fsSL https://x.ai/cli/install.sh | bash)"

    def _spawn_argv(self, binp: str) -> list:
        argv = [binp, "agent"]
        model = load_config().get("model") or ""
        if model:
            argv += ["-m", model]          # agent 選項放 mode 名之前(官方文件)
        return argv + ["stdio"]

    def _spawn_env(self) -> dict:
        env = dict(os.environ)
        cfg = load_config()
        if cfg["api_key"]:
            # 落檔的 key 也要進 env —— CLI 的憑證解析序認 XAI_API_KEY。
            env["XAI_API_KEY"] = cfg["api_key"]
        # 自動更新訊息走 stderr,但 bridge 管版本的原則是「裝了什麼跑什麼」
        # (研究報告:官方 SDK 對子 agent 也注入這個)。
        env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
        return env

    def _spawn_cwd(self) -> str:
        return _DEFAULT_CWD

    def _stderr_log_path(self) -> str:
        return os.environ.get("GROK_STDERR_LOG",
                              os.path.join(_STATE_DIR, "cli-stderr.log"))

    def _spawn_timeout(self) -> float:
        return _SPAWN_TIMEOUT

    def _default_call_timeout(self) -> float:
        return _CALL_TIMEOUT

    def _on_proc_reset(self) -> None:
        self._loaded = set()
        self._updates_supported = None

    def _connected_fields(self, init: dict) -> dict:
        # 沒有 authenticate 握手:grok 的憑證是 ambient 的
        # (~/.grok/auth.json 或 XAI_API_KEY),session/new 時才驗。
        cfg = load_config()
        self.auth_used = ("login" if login_available()
                          else "api-key" if cfg["api_key"] else "")
        return {"agent_version": str(((init or {}).get("_meta") or {})
                                     .get("agentVersion") or ""),
                "model": cfg.get("model") or "(cli-default)",
                "auth": self.auth_used or "(none)"}

    def logged_in(self) -> bool:
        return login_available() or bool(load_config()["api_key"])

    # ── session 生命週期 ──

    def _require_login(self):
        if not self.logged_in():
            raise GrokError(LOGIN_GUIDANCE, code="NOT_LOGGED_IN")

    async def new_session(self, key: str, cwd: str = "", title: str = "") -> dict:
        """建 ACP session 並以 bridge key 記名錄;回名錄列。"""
        self._require_login()
        cwd = os.path.expanduser(cwd or _DEFAULT_CWD)
        os.makedirs(cwd, exist_ok=True)
        res = await self.call("session/new", {"cwd": cwd, "mcpServers": []},
                              timeout=_SPAWN_TIMEOUT)
        acp_sid = str((res or {}).get("sessionId") or "")
        if not acp_sid:
            raise GrokError("session/new 沒回 sessionId", code="BAD_RESPONSE")
        self._loaded.add(acp_sid)
        return remember_session(key, acp_sid, title=title, cwd=cwd)

    async def ensure_session(self, key: str) -> str:
        """key → 可用的 acp_sid(= resume:子程序重啟後先 session/load 續接;
        載不回就開新 ACP session 換 acp_sid 續命,bridge 端歷史不動)。"""
        self._require_login()
        await self._ensure_proc()
        row = get_session(key)
        if row is None:
            row = await self.new_session(key)
            return str(row["acp_sid"])
        acp_sid = str(row.get("acp_sid") or "")
        cwd = os.path.expanduser(row.get("cwd") or _DEFAULT_CWD)
        if acp_sid and acp_sid in self._loaded:
            return acp_sid
        if acp_sid and bool(((self.server_info or {}).get("agentCapabilities")
                             or {}).get("loadSession")):
            try:
                await self.call("session/load",
                                {"sessionId": acp_sid, "cwd": cwd,
                                 "mcpServers": []}, timeout=_SPAWN_TIMEOUT)
                self._loaded.add(acp_sid)
                return acp_sid
            except GrokError as e:
                self._log("grok_session_reopen", key=key, error=str(e)[:120])
        row = await self.new_session(key, cwd=cwd)
        return str(row["acp_sid"])

    async def cancel(self, key: str):
        """中斷進行中的 turn(標準 ACP `session/cancel` notification)。"""
        row = get_session(key)
        acp_sid = str((row or {}).get("acp_sid") or "")
        if not acp_sid:
            return
        await self.notify("session/cancel", {"sessionId": acp_sid})

    def acp_to_key(self, acp_sid: str) -> str | None:
        for k, row in _load_sessions().items():
            if str(row.get("acp_sid") or "") == acp_sid:
                return k
        return None

    # ── 原生歷史(x.ai/session/updates;feature-detect,退逐字稿)──

    async def updates_tail(self, key: str, limit: int = 40) -> list[dict] | None:
        """`x.ai/session/updates` 負 offset 取尾(研究報告 §3 的契約)。

        回 None = 用不了(行程沒活著/沒這方法/任何失敗)—— 呼叫端退回
        bridge 自己的逐字稿。**絕不為了讀歷史冷啟行程**(列表熱路徑紅線)。
        """
        if not self.alive():
            return None
        if self._updates_supported is False:
            return None
        row = get_session(key)
        acp_sid = str((row or {}).get("acp_sid") or "")
        if not acp_sid:
            return None
        try:
            res = await self._call_locked(
                "x.ai/session/updates",
                {"sessionId": acp_sid,
                 "cwd": os.path.expanduser((row or {}).get("cwd") or _DEFAULT_CWD),
                 "offset": -max(1, int(limit))},
                timeout=_CALL_TIMEOUT)
        except GrokError as e:
            # -32601 method not found → 這版 CLI 沒這擴展,記住別再敲。
            if str(e.code) == "-32601":
                self._updates_supported = False
            return None
        self._updates_supported = True
        upd = (res or {}).get("updates")
        return upd if isinstance(upd, list) else None


def make_client(log=None):
    return GrokClient(log=log)


# ── v2 session 形狀助手(bridge 對映用,不碰網路)─────────────────────────────

def session_v2_id(sid: str) -> str:
    return f"grok:{sid}"


def _last_preview(sid: str) -> str | None:
    try:
        rows = transcript_read(sid, limit=1)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    last = rows[-1] or {}
    text = str(last.get("content") or last.get("text") or "").strip()
    if not text:
        return None
    text = " ".join(text.split())
    prefix = "你:" if last.get("role") == "user" else ""
    return cjk_text.clip(prefix + text, 120, suffix="")


def session_v2_row(row: dict, busy: bool = False) -> dict:
    sid = str(row.get("id") or "")
    title = cjk_text.clip(" ".join(str(row.get("title") or "Grok").split()),
                          48, suffix="") or "Grok"
    return {"id": session_v2_id(sid), "provider": "grok",
            "title": title,
            "subtitle": _last_preview(sid),
            "status": "running" if busy else "idle",
            "last_event_at": float(row.get("last") or 0) or None,
            "capabilities": ["input", "interrupt", "attachments",
                             "replay", "follow", "approve"],
            "meta": {}}
