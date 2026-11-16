from __future__ import annotations
"""OpenCode(sst/opencode)CLI 客戶端 — Pocket 第六家 code-agent provider。

協定:`opencode acp`(標準 ACP,JSON-RPC over stdio,protocolVersion 1)。
定位:Pocket **Build 主引擎** —— MIT、75+ provider(OpenRouter 即插)、
而且**零認證就能跑**(內建免費模型池)。

職責邊界(同 grok_sdk / gemini_provider 慣例):本模組只管「配置 + 子程序
生命週期 + session 名錄/逐字稿 + opencode 專屬的 ACP 語意」;JSON-RPC 泵在
`acp_rail.AcpProcessClient`(與 grok 共用),卡片 digest 在
carddigest.OpenCodeDigest,v2 路由/審批接線在 bridge.py(S7 區)。

## 2026-11-16 本機實測(1.19.5)—— 這一刀的所有判斷都基於這份錄音
錄音檔:tests/fixtures/opencode/recorded-stdio-20261116.txt(真 wire bytes)

| 面向 | 實測結果 |
|---|---|
| 入口 | `opencode acp`(`--help` 自述 "start ACP server") |
| initialize | protocolVersion 1;`loadSession: true`、`sessionCapabilities:{close,fork,list,resume}`、`promptCapabilities:{image,embeddedContext}` |
| 認證 | `authMethods:[opencode-login]`,但**不握手也能跑**(免費模型池);`auth.json` 0 credentials 下 session/prompt 一樣回 end_turn |
| session/new | 回 `{sessionId, configOptions:[model, mode]}` ← **per-session 設定是一級公民** |
| session/load | 回 `{configOptions}` **並 replay 歷史**(user/agent_message_chunk) |
| 設定寫入 | `session/set_config_option {sessionId, configId, value}`(欄位是 `configId`,寫 `configOptionId` 回 -32602) |
| 原生列表 | `session/list` → `[{sessionId, cwd, title, updatedAt}]`(title 是 agent 生成的) |
| 審批 | `session/request_permission`,options = once/always/reject(kind: allow_once/allow_always/reject_once) |
| 中斷 | `session/cancel` → prompt 0.7 秒內回 `stopReason:"cancelled"` |
| 壓縮 | availableCommands **沒有** compact(有 build/plan 模式與一堆 skill)→ 不接,不留死按鈕 |

與 grok 的三個關鍵差異(= 這一刀不是複製貼上的地方):
1. **沒有登入牆**:grok 未登入 → NOT_LOGGED_IN 不開行程;opencode 零認證可用,
   所以 `logged_in()` 不當閘門(當成閘門會把一個本來就能用的 provider 關掉)。
   OpenRouter key 是**加分項**(換得 75+ provider),不是先決條件。
2. **模型是 per-session**(grok 是 spawn 參數):`session/set_config_option`
   讓 `_V2_SESSION_OPS.config_read/config_write` 對這家是**真支援** ——
   第二家(codex 之後)真的有 per-session 控制面的 provider。
3. **歷史靠 load replay**:不需要像 grok 那樣探一個私有擴展
   (x.ai/session/updates);session/load 本來就會把逐字 replay 回來,
   我們在泵的旁聽掛鉤把它擷下來(零額外 RPC、零冷啟)。

初始化很慢(實測 5~26 秒,載 plugin/skill),所以 spawn timeout 預設 120 秒 ——
抄 grok 的 45 秒會在這家變成「隨機啟動失敗」。
"""

import json
import os
import shutil
import time
import uuid

import acp_rail

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("OPENCODE_CONFIG_FILE", "~/.pocket/opencode.json"))
_STATE_DIR = os.path.expanduser(
    os.environ.get("OPENCODE_STATE_DIR", "~/.pocket/opencode"))
_SESSIONS_FILE = os.path.join(_STATE_DIR, "sessions.json")
_CALL_TIMEOUT = float(os.environ.get("OPENCODE_CALL_TIMEOUT", "60"))
_TURN_TIMEOUT = float(os.environ.get("OPENCODE_TURN_TIMEOUT", "900"))
#: 實測 initialize 要 5~26 秒(plugin/skill 載入),120 給足餘裕 ——
#: 這個數字是實測來的,不是抄 grok 的 45(抄了會隨機啟動失敗)。
_SPAWN_TIMEOUT = float(os.environ.get("OPENCODE_SPAWN_TIMEOUT", "120"))
_DEFAULT_CWD = os.path.expanduser(
    os.environ.get("OPENCODE_WORKDIR", "~/.pocket/opencode/workspace"))
#: 預設模型(provider/model 形式,如 `openrouter/anthropic/claude-sonnet-4.5`
#: 或 `opencode/big-pickle`)。空 = 用 CLI/config 的預設,不強制覆寫。
_MODEL_ENV = (os.environ.get("OPENCODE_MODEL") or "").strip()

#: `opencode auth login` 落的憑證(CLI 自己管;檔案內容=真相,不自己驗)。
_AUTH_FILE = os.path.expanduser(
    os.environ.get("OPENCODE_AUTH_FILE",
                   "~/.local/share/opencode/auth.json"))

#: 這一家的設定 id(session/new 的 configOptions 實測就這兩個)。
#: **不硬編成白名單用**:寫入時以 initialize/session 回來的 options 為準,
#: 這裡只是給 config 端點排序/翻譯用的已知集合。
KNOWN_CONFIG_IDS = ("model", "mode")


class OpenCodeError(acp_rail.AcpError):
    """opencode ACP 回的結構化錯誤或傳輸層失敗(欄位同 AcpError)。"""


# ── 配置/偵測 ───────────────────────────────────────────────────────────────

def _read_file() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — 缺檔/壞檔一律視為未配置
        return {}


def load_config() -> dict:
    """env 優先(OPENROUTER_API_KEY / OPENCODE_MODEL),否則讀落檔。
    `model` 與 `api_key` 來源各自獨立判定(gemini/grok 的教訓)。"""
    d = _read_file()
    file_model = str(d.get("model") or "").strip()
    model = _MODEL_ENV or file_model
    out = {"model": model,
           "model_source": ("env" if _MODEL_ENV
                            else "file" if file_model else "none")}
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
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


def opencode_bin() -> str:
    """opencode 執行檔;找不到回空字串(= 未安裝,caps 不宣告)。

    官方 installer(`curl -fsSL https://opencode.ai/install | bash`)裝到
    `~/.opencode/bin/opencode`;brew/npm 走 PATH。launchd 服務的 PATH 常常
    沒有 ~/.opencode/bin,所以 which 之外要明確摸(grok/agy 同款教訓)。"""
    env = (os.environ.get("POCKET_OPENCODE_BIN") or "").strip()
    if env:
        p = os.path.expanduser(env)
        return p if os.path.exists(p) else ""
    found = shutil.which("opencode")
    if found:
        return found
    for cand in ("~/.opencode/bin/opencode", "~/.local/bin/opencode"):
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return ""


def login_available() -> bool:
    """`opencode auth login` 有沒有存下任何憑證。

    注意:這**不是**能不能用的判準 —— opencode 零認證就能跑免費模型池
    (2026-11-16 實測)。這只影響「能用哪些 provider」。
    """
    try:
        with open(_AUTH_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return bool(isinstance(d, dict) and d)
    except Exception:  # noqa: BLE001 — 缺檔/壞檔 = 沒有憑證
        return False


def auth_state() -> dict:
    """給 config 端點用:{logged_in, key_set, method, required}。

    `required: False` 是這家的特色(也是契約欄位)—— app 不該因為沒登入就
    把入口藏起來或畫出登入牆。
    """
    cfg = load_config()
    oauth = login_available()
    key = bool(cfg["api_key"])
    return {"logged_in": oauth, "key_set": key, "required": False,
            "method": ("login" if oauth else "openrouter-key" if key
                       else "builtin-free")}


#: 給 app 的提示(不是錯誤):接上 OpenRouter 才解鎖 75+ provider。
AUTH_GUIDANCE = ("OpenCode 開箱就能用(內建免費模型)。想用 75+ provider 的"
                 "模型,在主機跑 `opencode auth login` 選 provider,或把"
                 "OpenRouter API key 存進設定(之後模型填 "
                 "`openrouter/<model-id>`)。")


# ── session 名錄(形狀同 grok/gemini:bridge key ↔ ACP sessionId 解耦,
#    CLI 端 session 蒸發時換 acp_sid 續命,bridge 端歷史不動)──────────────

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
    if title_hint and not row.get("title"):
        row["title"] = title_hint[:60]
    _save_sessions(d)


def rename_session(key: str, title: str) -> dict | None:
    d = _load_sessions()
    row = d.get(key)
    if not row:
        return None
    row["title"] = (title or "").strip()[:60]
    _save_sessions(d)
    return row


def set_archived(key: str, archived: bool) -> dict | None:
    """封存=不列出來,逐字稿與卡照留(對齊 cc2/gemini/grok)。"""
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
    mid = f"oce-{uuid.uuid4().hex[:12]}"
    with open(transcript_path(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "role": role, "content": text,
                            "ts": time.time()}, ensure_ascii=False) + "\n")
    return mid


def _transcript_rows(sid: str) -> list[dict]:
    try:
        with open(transcript_path(sid), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001 — 新 session 沒逐字稿
        return []
    rows = []
    for line in lines:
        try:
            m = json.loads(line)
            if isinstance(m, dict):
                rows.append(m)
        except Exception:  # noqa: BLE001 — 壞行跳過
            continue
    return rows


def transcript_page(sid: str, limit: int = 40, offset: int = 0) -> dict:
    """歷史分頁 —— 形狀對齊 cc2/gemini/grok 的 `{messages, more, total}`
    (offset 從最新往回數,messages 舊→新;app 只有一個解析器)。"""
    rows = _transcript_rows(sid)
    total = len(rows)
    end = max(0, total - max(0, offset))
    start = max(0, end - max(1, limit))
    return {"messages": rows[start:end], "more": start > 0, "total": total}


def transcript_read(sid: str, limit: int = 200) -> list[dict]:
    return _transcript_rows(sid)[-limit:]


def project_updates_to_messages(updates: list) -> list[dict]:
    """ACP update 幀 → `{id, role, content, ts}` 訊息列(舊→新)。
    投影邏輯與 grok 共用(acp_rail);這裡只決定 id 前綴。"""
    return acp_rail.project_updates_to_messages(updates, "oce-")


# ── configOptions(per-session 設定)的形狀轉換 ─────────────────────────────

def config_to_contract(options: list) -> dict:
    """ACP 的 `configOptions` → 契約的扁平 snake_case 設定物件。

    回 `{model?, mode?, model_options?, mode_options?}`:
    - `model` / `mode` = 現值(app 的 SpawnConfigDTO 讀得懂的扁平字串);
    - `*_options` = 可選值列 `[{value,name}]` —— 這家的賣點就是模型多,
      app 要畫得出選單就需要這份(其他家沒有這層,所以是新增欄位而非改形狀)。

    認不出的 configId 一律原樣帶過(未來 opencode 加新設定,app 不必等 bridge
    改碼就看得到;但我們不猜它的語意)。
    """
    out: dict = {}
    for o in options or []:
        if not isinstance(o, dict):
            continue
        cid = str(o.get("id") or "").strip()
        if not cid:
            continue
        cur = o.get("currentValue")
        if cur is not None:
            out[cid] = cur
        opts = []
        for c in (o.get("options") or []):
            if isinstance(c, dict) and c.get("value") is not None:
                opts.append({"value": c["value"],
                             "name": str(c.get("name") or c["value"])})
        if opts:
            out[f"{cid}_options"] = opts
    return out


def config_allowed_values(options: list, config_id: str) -> list:
    """某個設定的合法值列(寫入前驗證用;沒有 options 回空 = 不驗)。"""
    for o in options or []:
        if isinstance(o, dict) and str(o.get("id") or "") == config_id:
            return [c.get("value") for c in (o.get("options") or [])
                    if isinstance(c, dict) and c.get("value") is not None]
    return []


# ── ACP 客戶端 ─────────────────────────────────────────────────────────────

class OpenCodeClient(acp_rail.AcpProcessClient):
    """單一常駐 `opencode acp` 子程序的 ACP 客戶端。

    泵在 acp_rail;本類描述「opencode 怎麼起行程」+ 這家的 session 語意:
    per-session configOptions 快取、session/load 的歷史 replay 擷取。
    """

    _log_prefix = "opencode"
    _debug_env = "OPENCODE_DEBUG"
    _error_cls = OpenCodeError

    def __init__(self, log=None):
        super().__init__(log=log)
        self._loaded: set[str] = set()   # 本程序已 new/load 過的 ACP session
        #: acp_sid → 最近一次 new/load/set_config_option 回來的 configOptions。
        #: per-session 設定的真相在 CLI,這裡只是快取(控制面讀取用)。
        self._config: dict[str, list] = {}
        #: session/load 期間擷下來的 replay 幀(acp_sid → [update, …])。
        #: 用途:逐字稿是空的時候補歷史(零額外 RPC —— load 本來就會 replay)。
        self._replay: dict[str, list] = {}
        #: 正在 load 的 acp_sid(只有這段期間才擷取 replay,否則會把
        #: 進行中回合的逐字也當成歷史收進來)。
        self._loading: str = ""

    # ── acp_rail 掛鉤(「opencode 怎麼起行程」)──

    def _bin_path(self) -> str:
        return opencode_bin()

    def _no_binary_message(self) -> str:
        return ("opencode 不在這台主機(curl -fsSL https://opencode.ai/install "
                "| bash)")

    def _spawn_argv(self, binp: str) -> list:
        # 模型**不是** spawn 參數(與 grok 的差別):per-session 走
        # session/set_config_option,所以 argv 永遠就這兩個字。
        return [binp, "acp"]

    def _spawn_env(self) -> dict:
        env = dict(os.environ)
        cfg = load_config()
        if cfg["api_key"]:
            # 官方文件:把 OPENROUTER_API_KEY 給 server 就能用
            # `openrouter/<model-id>`(不必寫進使用者的 opencode.json ——
            # 我們不動使用者自己的設定檔,那是他的東西)。
            env["OPENROUTER_API_KEY"] = cfg["api_key"]
        return env

    def _spawn_cwd(self) -> str:
        return _DEFAULT_CWD

    def _stderr_log_path(self) -> str:
        return os.environ.get("OPENCODE_STDERR_LOG",
                              os.path.join(_STATE_DIR, "cli-stderr.log"))

    def _spawn_timeout(self) -> float:
        return _SPAWN_TIMEOUT

    def _default_call_timeout(self) -> float:
        return _CALL_TIMEOUT

    def _on_proc_reset(self) -> None:
        """只清**與這顆子程序綁定**的狀態。

        `_config` / `_replay` 刻意保留:它們的鍵是 ACP sessionId,而 ACP
        session 活在 opencode 自己的 DB(~/.local/share/opencode/opencode.db)
        裡 —— 它比我們的子程序長命。連帶清掉的後果是控制面剛讀到的設定在
        子程序重拉後憑空消失(這條是測試抓出來的:asyncio.run 收尾時 read
        loop 被取消 → _drop_proc → 快取被清光)。

        新鮮度由 `_loaded` 把關:行程重拉後那條 session 不在 `_loaded`,
        config_read 會重新 session/load 一次(回應就帶最新 configOptions)。
        """
        self._loaded = set()
        self._loading = ""

    def _connected_fields(self, init: dict) -> dict:
        info = (init or {}).get("agentInfo") or {}
        st = auth_state()
        return {"agent_version": str(info.get("version") or ""),
                "auth": st["method"],
                "model_pref": load_config().get("model") or "(cli-default)"}

    def _on_update_frame(self, session_id: str, update: dict) -> None:
        """session/load 的 replay 擷取(只在 load 進行中、且 sid 對得上)。"""
        if self._loading and session_id == self._loading:
            self._replay.setdefault(session_id, []).append(update)

    # ── 能力 feature-detect(不硬編)──

    def _agent_caps(self) -> dict:
        return (self.server_info or {}).get("agentCapabilities") or {}

    def supports_load(self) -> bool:
        return bool(self._agent_caps().get("loadSession"))

    def supports_image(self) -> bool:
        return bool((self._agent_caps().get("promptCapabilities")
                     or {}).get("image"))

    def logged_in(self) -> bool:
        """有沒有額外憑證。**注意:不是能不能用的閘門**(見模組 docstring)。"""
        return login_available() or bool(load_config()["api_key"])

    # ── session 生命週期 ──

    async def new_session(self, key: str, cwd: str = "",
                          title: str = "") -> dict:
        """建 ACP session 並以 bridge key 記名錄;回名錄列。

        建完**順手套用偏好模型**:這家的模型是 per-session 的,新 session
        不套就等於使用者在設定面板選的模型對新對話無效(= 設了沒用的設定,
        比沒有設定更糟)。套用失敗只記 log,不讓它擋掉開 session。
        """
        cwd = os.path.expanduser(cwd or _DEFAULT_CWD)
        os.makedirs(cwd, exist_ok=True)
        res = await self.call("session/new", {"cwd": cwd, "mcpServers": []},
                              timeout=_SPAWN_TIMEOUT)
        acp_sid = str((res or {}).get("sessionId") or "")
        if not acp_sid:
            raise self._err("session/new 沒回 sessionId", code="BAD_RESPONSE")
        self._loaded.add(acp_sid)
        self._note_config(acp_sid, res)
        want = (load_config().get("model") or "").strip()
        if want and want != self.current_config(acp_sid).get("model"):
            try:
                await self.set_config(acp_sid, "model", want)
            except Exception as e:  # noqa: BLE001 — 偏好套不上不該擋開場
                self._log("opencode_model_apply_failed", model=want[:60],
                          error=str(e)[:160])
        return remember_session(key, acp_sid, title=title, cwd=cwd)

    async def ensure_session(self, key: str) -> str:
        """key → 可用的 acp_sid(= resume:子程序重啟後先 session/load 續接;
        載不回就開新 ACP session 換 acp_sid 續命,bridge 端歷史不動)。"""
        await self._ensure_proc()
        row = get_session(key)
        if row is None:
            row = await self.new_session(key)
            return str(row["acp_sid"])
        acp_sid = str(row.get("acp_sid") or "")
        cwd = os.path.expanduser(row.get("cwd") or _DEFAULT_CWD)
        if acp_sid and acp_sid in self._loaded:
            return acp_sid
        if acp_sid and self.supports_load():
            try:
                await self._load_session(acp_sid, cwd)
                return acp_sid
            except OpenCodeError as e:
                self._log("opencode_session_reopen", key=key,
                          error=str(e)[:120])
        row = await self.new_session(key, cwd=cwd)
        return str(row["acp_sid"])

    async def _load_session(self, acp_sid: str, cwd: str) -> None:
        """session/load + 擷取它 replay 回來的歷史幀。

        `_loading` 窗口要在 await 之前開、在 finally 關 —— replay 幀是在
        load 的回應**之前**就流進來的(實測),窗口開晚了就什麼都收不到。
        """
        self._replay.pop(acp_sid, None)
        self._loading = acp_sid
        try:
            res = await self.call("session/load",
                                  {"sessionId": acp_sid, "cwd": cwd,
                                   "mcpServers": []},
                                  timeout=_SPAWN_TIMEOUT)
        finally:
            self._loading = ""
        self._loaded.add(acp_sid)
        self._note_config(acp_sid, res)

    async def cancel(self, key: str):
        """中斷進行中的 turn(標準 ACP `session/cancel` notification)。
        實測:prompt 會在 0.7 秒內回 `stopReason:"cancelled"`。"""
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

    # ── per-session 設定(model / mode)──

    def _note_config(self, acp_sid: str, res) -> None:
        opts = (res or {}).get("configOptions") if isinstance(res, dict) else None
        if isinstance(opts, list):
            self._config[acp_sid] = opts

    def session_loaded(self, acp_sid: str) -> bool:
        """這顆行程有沒有 new/load 過這條 ACP session(= 快取是否新鮮)。"""
        return bool(acp_sid) and acp_sid in self._loaded

    def config_options(self, acp_sid: str) -> list:
        """快取的 configOptions(沒有回空 list —— 呼叫端不准拿它當「沒設定」)。"""
        return self._config.get(acp_sid) or []

    def current_config(self, acp_sid: str) -> dict:
        return config_to_contract(self.config_options(acp_sid))

    async def set_config(self, acp_sid: str, config_id: str, value) -> dict:
        """`session/set_config_option`。欄位名是 **configId**(實測:寫
        `configOptionId` 回 -32602 Invalid params —— 這個坑錄在 fixture 裡)。"""
        res = await self.call("session/set_config_option",
                              {"sessionId": acp_sid, "configId": config_id,
                               "value": value})
        self._note_config(acp_sid, res)
        return self.current_config(acp_sid)

    # ── 原生面(session/list、load replay 歷史)──

    async def list_native(self, limit: int = 40) -> list[dict] | None:
        """`session/list` → [{sessionId,cwd,title,updatedAt}]。

        回 None = 用不了(行程沒活著/沒這方法/失敗)。**絕不為了列表冷啟
        行程**(列表熱路徑紅線:冷啟要 5~26 秒)。
        """
        if not self.alive():
            return None
        if not ((self._agent_caps().get("sessionCapabilities")
                 or {}).get("list") is not None):
            return None
        try:
            res = await self._call_locked("session/list", {})
        except OpenCodeError as e:
            self._log("opencode_list_native_failed", error=str(e)[:120])
            return None
        rows = (res or {}).get("sessions")
        return rows[:limit] if isinstance(rows, list) else None

    def replay_messages(self, key: str) -> list[dict] | None:
        """這條 session 上次 session/load 時 replay 回來的歷史 → 訊息列。

        回 None = 沒有(沒 load 過/這條 session 本來就空)。呼叫端用它補
        「bridge 逐字稿是空的」那一格 —— 與 grok 的原生歷史同一條判準,
        但這家不需要額外 RPC(load 的副產品)。
        """
        row = get_session(key)
        acp_sid = str((row or {}).get("acp_sid") or "")
        if not acp_sid:
            return None
        frames = self._replay.get(acp_sid)
        if not frames:
            return None
        msgs = project_updates_to_messages(frames)
        return msgs or None


def make_client(log=None):
    return OpenCodeClient(log=log)


# ── v2 session 形狀助手(bridge 對映用,不碰網路)─────────────────────────────

def session_v2_id(sid: str) -> str:
    return f"opencode:{sid}"


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
    return (prefix + text)[:120]


def session_v2_row(row: dict, busy: bool = False) -> dict:
    sid = str(row.get("id") or "")
    title = " ".join(str(row.get("title") or "OpenCode").split())[:48] \
        or "OpenCode"
    return {"id": session_v2_id(sid), "provider": "opencode",
            "title": title,
            "subtitle": _last_preview(sid),
            "status": "running" if busy else "idle",
            "last_event_at": float(row.get("last") or 0) or None,
            # config:這家 per-session 設定是真的(session/set_config_option),
            # 所以敢宣告 —— 其他五家沒有這一項。
            "capabilities": ["input", "interrupt", "attachments",
                             "replay", "follow", "approve", "config"],
            "meta": {}}
