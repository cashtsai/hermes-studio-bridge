"""S3:小任務文字生成 —— 把雜務從 coding agent 的訂閱額度上搬下來。

## 為什麼有這個檔

bridge 裡有一批「小文字工作」:session 標題、回合摘要(roster
`last_turn_summary`)、commit 訊息、語音轉錄清理……它們的共同點是
**幾十個 token 的輸入、十幾個字的輸出**,但只要交給 CC/CX 這類 coding
agent 去做,燒的就是**訂閱額度**(CC/CX 的額度是命 —— 撞到限額整條線停擺,
而摘要這種雜務一點都不值得佔用它)。

這支提供第二條路:**便宜 model API**。設定一個 OpenAI 相容端點
(`base_url` + `key` + `model`)就能用 —— OpenAI / Anthropic 的 OpenAI 相容
層 / OpenRouter / Ollama / LM Studio 都講同一套 `POST /chat/completions`,
所以只要一個客戶端就全部涵蓋,不必為每家寫 adapter。

**review/coding 類絕不走這裡** —— 那本來就該走 coding agent。這支只接
「寫一行字」等級的雜務。

## 靜默失敗紅線

沒設定時 `generate()` 回 `ok=False, reason="not_configured"`,呼叫端
**退回原本的行為**(規則式截斷、agent 路徑……)。鐵則:

    功能不准因為「沒設定 textgen」而消失。

同理,端點掛了 / 超時 / 回了垃圾,一律是 `ok=False` + reason,**從不 raise**
—— 這些都掛在既有熱路徑上(回合結束、roster 重建),一顆 500 不能拖垮它們。
但也**絕不無聲**:每次失敗都走 `_sink` 留痕,查得到。

## 成本留痕

每次呼叫(含失敗)都把 token 用量餵給 usage sink,方便日後對帳。sink 是
**可插拔的**:預設只進 log,bridge 啟動時會換成「同時落 event_log」的版本
(見 bridge.py 的 `textgen.usage`)。這樣這支檔可以**獨立測試**,不必
`import bridge`(那是 2MB 的模組,還會在 import 時建表)。

## 設定來源優先序

    env(POCKET_TEXTGEN_BASE_URL / _KEY / _MODEL)> 檔(~/.pocket/textgen.json)

與 grok_sdk / gemini 同款語意:env 優先、來源各自獨立判定(只設 env model
不該讓 file 的 key 消失)。寫入走 `/app/v1/textgen/config` PUT,0600 落檔。
"""
from __future__ import annotations

import json
import os
import re
import time

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("POCKET_TEXTGEN_CONFIG_FILE", "~/.pocket/textgen.json"))

#: 單次呼叫的輸入上限(字元)。雜務的輸入本來就該很短 —— 這道閘是防「某個
#: 呼叫端不小心把整包 transcript 塞進來」,那會把便宜檔的成本優勢吃光。
#: 超過就從**尾端**取(最後一段才是「最近發生什麼」,摘要要的是那個)。
MAX_INPUT_CHARS = int(os.environ.get("POCKET_TEXTGEN_MAX_INPUT_CHARS", "6000"))

#: 預設超時(秒)。雜務是掛在熱路徑上的,寧可放棄也不要拖住回合結束。
DEFAULT_TIMEOUT_SECS = float(os.environ.get("POCKET_TEXTGEN_TIMEOUT", "12"))

#: 便宜檔建議模型。**只是建議清單**(給 app 設定面板的 picker 用),不是
#: 預設值 —— 猜一個端點不認得的 model id 會換來 404,那是靜默失敗的親戚。
#: 選型依據見 ~/apps/pid-subsync/docs/LLM_PRICING_REFERENCE_20261117.md
#: (便宜檔:Haiku / gpt-6-luna 級);價格會變,那份文件才是真相來源。
SUGGESTED_MODELS = [
    {"model": "claude-haiku-4-5", "label": "Claude Haiku 4.5",
     "hint": "Anthropic;需指向 OpenAI 相容層(官方 compat 端點或 OpenRouter)"},
    {"model": "gpt-6-luna", "label": "GPT-6 Luna",
     "hint": "OpenAI 最便宜檔;base_url = https://api.openai.com/v1"},
    {"model": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite",
     "hint": "有免費層;走 Google 的 OpenAI 相容端點"},
    {"model": "", "label": "本機(Ollama / LM Studio)",
     "hint": "base_url = http://127.0.0.1:11434/v1(或 1234/v1);key 可留空"},
]


# ── 設定讀寫 ────────────────────────────────────────────────────────────────

def _read_file() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        # 檔不在 / 壞 JSON 都當「沒設定」—— 退回現狀行為,不是錯誤。
        return {}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def load_config() -> dict:
    """env 優先,否則讀檔;三個欄位的來源各自獨立判定。

    回傳 `base_url` 已正規化(去尾斜線),`model`/`api_key` 原樣。
    """
    d = _read_file()
    out = {}
    for key, env_name in (("base_url", "POCKET_TEXTGEN_BASE_URL"),
                          ("api_key", "POCKET_TEXTGEN_KEY"),
                          ("model", "POCKET_TEXTGEN_MODEL")):
        ev = _env(env_name)
        fv = str(d.get(key) or "").strip()
        out[key] = ev or fv
        out[key + "_source"] = "env" if ev else "file" if fv else "none"
    out["base_url"] = out["base_url"].rstrip("/")
    return out


def save_config(base_url: str | None = None, api_key: str | None = None,
                model: str | None = None) -> dict:
    """設定面板落檔(0600);**合併寫入** —— 只帶 model 不可洗掉 api_key。

    傳空字串 = 明確清掉該欄(讓使用者有辦法「關掉 textgen」);傳 None =
    不動(同 grok_sdk.save_config 的語意)。
    """
    d = _read_file()
    for key, val in (("base_url", base_url), ("api_key", api_key),
                     ("model", model)):
        if val is not None:
            d[key] = val.strip()
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _CONFIG_FILE)
    return load_config()


def configured() -> bool:
    """能不能用?**base_url + model 兩個都有才算**。

    `api_key` 刻意不列入 —— Ollama / LM Studio 不需要 key,把它設成必填
    就是把一個本來能用的路徑關掉(同 opencode「沒有登入牆」那條教訓)。
    """
    cfg = load_config()
    return bool(cfg["base_url"] and cfg["model"])


def config_public() -> dict:
    """給 `/app/v1/textgen/config` GET 的形狀:**不回 key 本體**,只回有沒有。

    (AGENTS.md:不准 log/回傳 bearer token;設定面板只需要知道「設了沒」。)
    """
    cfg = load_config()
    return {
        "configured": configured(),
        "base_url": cfg["base_url"],
        "base_url_source": cfg["base_url_source"],
        "model": cfg["model"],
        "model_source": cfg["model_source"],
        "key_set": bool(cfg["api_key"]),
        "key_source": cfg["api_key_source"],
        "suggested_models": SUGGESTED_MODELS,
        "fallback_note": "未設定時各呼叫點退回原本行為(規則式/agent),"
                         "功能不會消失。",
    }


# ── 成本留痕(可插拔 sink)────────────────────────────────────────────────

def _default_sink(record: dict) -> None:
    """預設 sink:印一行結構化 log。bridge 啟動時會換成「同時落 event_log」。"""
    print("[bridge-event] " + json.dumps(
        {"event": "textgen_usage", **record},
        ensure_ascii=False, sort_keys=True), flush=True)


_sink = _default_sink


def set_usage_sink(fn) -> None:
    """換掉 usage sink(bridge.py 用來把用量也落進 event_log)。

    傳 None 回到預設(只 log)。sink 自己不准 raise —— 這裡還是會包一層,
    對帳管線壞掉不該讓摘要生成跟著死。
    """
    global _sink
    _sink = fn or _default_sink


def _emit_usage(record: dict) -> None:
    try:
        _sink(record)
    except Exception as exc:  # noqa: BLE001 — sink 壞了不准拖垮呼叫端
        print("[bridge-event] " + json.dumps(
            {"event": "textgen_usage_sink_failed", "error": repr(exc)},
            ensure_ascii=False, sort_keys=True), flush=True)


# ── 結果形狀 ────────────────────────────────────────────────────────────────

class TextGenResult:
    """一次呼叫的結果。`ok=False` 時 `text` 必為空 —— 呼叫端照 reason 退路。

    刻意不是 NamedTuple:呼叫端常寫 `res.text or <fallback>`,用屬性比
    unpack 不容易漏接欄位(日後加欄位不會炸掉既有解構)。
    """

    __slots__ = ("text", "ok", "reason", "usage", "model", "elapsed_ms")

    def __init__(self, text: str = "", ok: bool = False, reason: str = "",
                 usage: dict | None = None, model: str = "",
                 elapsed_ms: int = 0):
        self.text = text
        self.ok = ok
        self.reason = reason
        self.usage = usage or {}
        self.model = model
        self.elapsed_ms = elapsed_ms

    def __repr__(self) -> str:   # 測試失敗時看得懂
        return (f"TextGenResult(ok={self.ok!r}, reason={self.reason!r}, "
                f"text={self.text!r}, usage={self.usage!r})")


def _clip(raw: str, limit: int | None = None) -> str:
    """輸入正規化 + 上限。超長取**尾段**(最近發生的事才是摘要要的)。

    `limit` 可以逐呼叫覆寫:摘要/標題類幾百字就夠(預設 `MAX_INPUT_CHARS`
    把成本夾住),但逐字稿清稿的輸入本來就是整場會議 —— 套同一個 6000 字
    上限會**安靜地把前面的內容吃掉**,而使用者只會發現「清稿漏了開頭」。
    """
    t = str(raw or "")
    cap = MAX_INPUT_CHARS if limit is None else max(1, int(limit))
    if len(t) > cap:
        t = t[-cap:]
    return t


def _parse_usage(body: dict) -> dict:
    """OpenAI 形狀的 usage → 我們的對帳欄位。沒回報就誠實標 reported=False。

    (有些本機 server 根本不回 usage —— 寫 0 當成「花了 0」會讓對帳說謊。)
    """
    u = body.get("usage")
    if not isinstance(u, dict):
        return {"reported": False}
    def _i(k):
        v = u.get(k)
        return int(v) if isinstance(v, (int, float)) else None
    prompt = _i("prompt_tokens")
    completion = _i("completion_tokens")
    total = _i("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    return {"reported": True, "prompt_tokens": prompt,
            "completion_tokens": completion, "total_tokens": total}


def _extract_text(body: dict) -> str:
    """OpenAI 形狀 → 第一則訊息的文字。取不到回空字串(交給呼叫端退路)。"""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    # 有些端點回 content 陣列(多模態形狀),把 text 片段接起來
    if isinstance(content, list):
        parts = [p.get("text") or "" for p in content
                 if isinstance(p, dict) and p.get("type") in (None, "text")]
        return "".join(parts).strip()
    return ""


def strip_wrapping(text: str) -> str:
    """把小模型愛加的裝飾拆掉:圍欄、引號、`摘要:` 這類前綴、尾端句點。

    小模型就算叫它「只回摘要」也常回 ```摘要:「部署好了」```。這層是
    **便宜的保險**,不是信任問題 —— 沒有它,wire 上就會出現一串反引號。
    """
    t = str(text or "").strip()
    t = re.sub(r"^```[a-zA-Z-]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    t = re.sub(r"^(摘要|標題|Summary|Title)\s*[:：]\s*", "", t, flags=re.I)
    t = t.strip().strip("「」\"'“”`").strip()
    return re.sub(r"\s+", " ", t)


def _reason_for(exc: BaseException) -> str:
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "JSON" in name or "Decode" in name:
        return "bad_response"
    return "transport"


async def _lane_api(cfg: dict, prompt: str, system: str, max_tokens: int,
                    timeout: float) -> TextGenResult:
    """便宜 API lane:OpenAI 相容 `POST {base_url}/chat/completions`。"""
    try:
        import httpx
    except Exception:   # pragma: no cover — requirements.txt 釘著 httpx
        return TextGenResult(reason="no_httpx", model=cfg["model"])

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": cfg["model"], "messages": messages,
               "max_tokens": int(max_tokens), "temperature": 0,
               "stream": False}
    headers = {"content-type": "application/json"}
    if cfg["api_key"]:
        headers["authorization"] = "Bearer " + cfg["api_key"]

    try:
        # 每次開新 client:這些呼叫很零星(回合結束才一次),省下的連線重用
        # 遠不如「client 綁死在已關掉的 event loop」那類 bug 貴。
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(cfg["base_url"] + "/chat/completions",
                                     json=payload, headers=headers)
        if resp.status_code >= 400:
            return TextGenResult(reason=f"http_{resp.status_code}",
                                 model=cfg["model"])
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — 熱路徑:一律降級不中斷
        return TextGenResult(reason=_reason_for(exc), model=cfg["model"])

    if not isinstance(data, dict):
        return TextGenResult(reason="bad_response", model=cfg["model"])
    usage = _parse_usage(data)
    text = strip_wrapping(_extract_text(data))
    if not text:
        # 連上了、也花了 token,但沒拿到字 —— 用量照記(錢真的花了)。
        return TextGenResult(reason="empty_text", usage=usage,
                             model=cfg["model"])
    return TextGenResult(text=text, ok=True, usage=usage, model=cfg["model"])


#: 本機 lane 的負向快取:連不上就先別再試。沒裝 Ollama 的機器每個回合結束
#: 都去撞一次 connection refused,log 會被洗版、熱路徑也白等 —— 但**絕不
#: 永久放棄**(使用者中途把 Ollama 開起來要能自己恢復),所以只冷卻一段。
_LOCAL_COOLDOWN_SECS = float(os.environ.get("POCKET_TEXTGEN_LOCAL_COOLDOWN",
                                            "600"))
_local_blocked_until = 0.0
#: 連續逾時幾次就冷卻(見 _lane_local 內 2026-11-28 事故註解)。
_LOCAL_TIMEOUT_STREAK_MAX = 3
_local_timeout_streak = 0


def local_enabled() -> bool:
    """本機 Ollama lane 開不開。`auto`(預設)= 試,連不上就冷卻。

    設 `POCKET_TEXTGEN_LOCAL=0/off/no` 可以關掉(例如機器上的 Ollama 被別的
    東西佔用、不想讓雜務去搶顯卡)。
    """
    v = _env("POCKET_TEXTGEN_LOCAL").lower()
    return v not in ("0", "off", "no", "false")


def reset_local_cooldown() -> None:
    """清掉本機 lane 的冷卻(設定一改就該重試;測試也用這支隔離狀態)。"""
    global _local_blocked_until, _local_timeout_streak
    _local_blocked_until = 0.0
    _local_timeout_streak = 0


async def _lane_local(prompt: str, system: str, timeout: float) -> TextGenResult:
    """本機 Ollama lane —— **既有行為的保存者**。

    `_polish_transcript` / 蒸餾 / agent_context 摘要本來就跑這條;便宜 API
    沒設定時一定要還是走得到它,不然這次改動等於**把一個本來能用的功能
    關掉**(靜默失敗紅線)。實作沿用 `harness/model.py` 的唯一實作,不在
    這裡複製第四份 Ollama 呼叫。
    """
    global _local_blocked_until, _local_timeout_streak
    if not local_enabled():
        return TextGenResult(reason="local_disabled")
    if time.monotonic() < _local_blocked_until:
        return TextGenResult(reason="local_cooldown")
    try:
        from harness import model as harness_model
    except Exception:   # pragma: no cover
        return TextGenResult(reason="local_unavailable")

    model_name = harness_model.distill_model()
    # Ollama 的 /api/chat 只吃單一 prompt 串在這個包裝裡 —— system 當前綴,
    # 語意等價(harness_model 刻意只暴露 prompt,不替人決定 role 結構)。
    joined = (system + "\n\n" + prompt) if system else prompt
    try:
        text = await harness_model.ollama_text(
            joined, timeout=timeout,
            num_ctx=max(2048, min(16384, len(joined) // 2 + 1024)))
    except Exception as exc:  # noqa: BLE001
        reason = _reason_for(exc)
        if reason == "transport":
            # 連不上(沒裝/沒開)→ 冷卻。逾時不冷卻:那是「在跑但慢」,
            # 把它當成缺席會讓慢機器永遠拿不到本機 lane。
            _local_blocked_until = time.monotonic() + _LOCAL_COOLDOWN_SECS
        elif reason == "timeout":
            # ……但**連續**逾時另當別論(2026-11-28 事故):每次逾時 httpx 棄單,
            # Ollama 卻把生成跑完 —— 棄單堆起來把唯一 slot 永久佔滿,之後每發
            # 必逾時、每發又加深飽和(575% CPU 連燒 3 小時)。偶發慢(1~2 次)
            # 照舊不冷卻;連三次是病態,冷卻讓佇列有機會排空。
            _local_timeout_streak += 1
            if _local_timeout_streak >= _LOCAL_TIMEOUT_STREAK_MAX:
                _local_blocked_until = time.monotonic() + _LOCAL_COOLDOWN_SECS
                _local_timeout_streak = 0
        return TextGenResult(reason="local_" + reason, model=model_name)
    _local_timeout_streak = 0
    text = strip_wrapping(text)
    if not text:
        return TextGenResult(reason="local_empty_text", model=model_name)
    # Ollama 不回 token 用量(本機免費,對帳上也沒意義)—— 誠實標 reported。
    return TextGenResult(text=text, ok=True, model=model_name,
                         usage={"reported": False, "local": True})


async def generate(task: str, prompt: str, *, system: str = "",
                   max_tokens: int = 64, session: str | None = None,
                   timeout: float | None = None,
                   allow_local: bool = True,
                   max_input_chars: int | None = None) -> TextGenResult:
    """跑一次小任務。**從不 raise**;失敗一律 `ok=False` + reason。

    lane 順序:**便宜 API(設定了才有)→ 本機 Ollama → 放棄**。第一條
    能用的就用;它壞掉才往下掉 —— 這個 cascade 是刻意的,因為上游壞掉時
    「安靜地沒摘要」比「多花 200ms 用本機生一個」糟得多。全部失敗就回
    `ok=False`,呼叫端退回原本的規則式/原稿行為。

    `task` 是對帳用的分類鍵(例:`roster.summary`、`session.title`),請用
    穩定字串 —— 日後要按任務類別算錢就靠它。
    `session` 有值時,用量事件會掛在那條 session 上(bridge 的 sink 會落
    event_log);沒有就記在全域桶。
    `allow_local=False` 可以強制只走便宜 API(給「這個任務不值得佔顯卡」
    的呼叫點用)。

    reason 取值:`not_configured` / `empty_prompt` / `no_httpx` /
    `http_<code>` / `timeout` / `transport` / `bad_response` / `empty_text`,
    本機 lane 的失敗一律加 `local_` 前綴。
    """
    started = time.monotonic()
    body_text = _clip(prompt, max_input_chars)
    tmo = DEFAULT_TIMEOUT_SECS if timeout is None else float(timeout)
    lanes: list[str] = []

    def _done(res: TextGenResult) -> TextGenResult:
        res.elapsed_ms = int((time.monotonic() - started) * 1000)
        _emit_usage({"task": task, "session": session or "",
                     "ok": res.ok, "reason": res.reason,
                     "model": res.model, "elapsed_ms": res.elapsed_ms,
                     "lanes": ",".join(lanes),
                     **{("usage_" + k): v for k, v in res.usage.items()}})
        return res

    if not body_text.strip():
        return _done(TextGenResult(reason="empty_prompt"))

    last = TextGenResult(reason="not_configured")
    cfg = load_config()
    if cfg["base_url"] and cfg["model"]:
        lanes.append("api")
        last = await _lane_api(cfg, body_text, system, max_tokens, tmo)
        if last.ok:
            return _done(last)
    if allow_local:
        lanes.append("local")
        local = await _lane_local(body_text, system, tmo)
        if local.ok:
            return _done(local)
        # 兩條都掛 → 回報**最能讓使用者動作的**那一個 reason。
        #
        # 便宜 API 沒設定、本機 lane 也不在,這兩件事同一類(「這條路不存在」)
        # —— 這時該說的是 `not_configured`(去設定一個),不是
        # `local_disabled`(那是實作細節,使用者看了不知道要幹嘛)。
        # 反之,本機真的跑到模型才失敗(逾時/空回應)是**新情報**,
        # 蓋掉 `not_configured` 才對:它告訴你「本機有、但不行」。
        _ABSENT = ("local_disabled", "local_cooldown", "local_unavailable")
        if last.reason == "not_configured" and local.reason not in _ABSENT:
            last = local
    return _done(last)


# ── 任務包裝(呼叫端用這些,不要自己拼 prompt)──────────────────────────
# 把 prompt 集中在這裡,好處是:成本/品質要調的時候只改一個地方,而且
# `task` 鍵不會被各呼叫點拼成一堆變體(那會讓對帳表散掉)。

_SUMMARY_SYSTEM = (
    "你把開發 session 的最後動態壓成一句極短摘要。規則:"
    "只輸出摘要本身,不要前綴、引號、標點收尾;"
    "用繁體中文;最多 12 個字;描述『做了什麼』而非『正在想什麼』。")

_TITLE_SYSTEM = (
    "你替一條開發 session 取標題。規則:只輸出標題本身,不要引號或前綴;"
    "用繁體中文;最多 16 個字;具體到看得出在做哪件事。")

_COMMIT_SYSTEM = (
    "你寫 git commit 的標題行。規則:只輸出那一行;"
    "格式 `type(scope): 說明`(type 用 feat/fix/docs/refactor/test/chore);"
    "說明用繁體中文;整行不超過 60 字元;不要句點收尾。")

_TRANSCRIPT_SYSTEM = (
    "你清理語音轉錄的逐字稿。規則:只輸出清理後的文字;"
    "修正明顯的同音錯字與標點,刪掉『嗯』『那個』這類填充詞;"
    "**不要改寫語意、不要摘要、不要補上原文沒有的內容**;"
    "原文是什麼語言就用什麼語言輸出。")


async def summarize_turn(text: str, *, session: str | None = None,
                         max_chars: int = 12) -> TextGenResult:
    """回合摘要(roster `last_turn_summary` 的升級路徑)。

    `max_chars` 是**後置硬閘**:prompt 只是請求,小模型不一定聽,但 wire
    上的欄位契約(§17 的 ≤12 字)不能破。超了就截斷加省略號 —— 與既有
    規則式路徑同一個形狀,app 零改動。
    """
    res = await generate("roster.summary", text, system=_SUMMARY_SYSTEM,
                         max_tokens=48, session=session)
    if res.ok and len(res.text) > max_chars:
        res.text = res.text[:max_chars] + "…"
    return res


async def session_title(text: str, *, session: str | None = None,
                        max_chars: int = 16) -> TextGenResult:
    """session 標題。同上,長度硬閘在這層。"""
    res = await generate("session.title", text, system=_TITLE_SYSTEM,
                         max_tokens=48, session=session)
    if res.ok and len(res.text) > max_chars:
        res.text = res.text[:max_chars]
    return res


async def commit_message(diff_summary: str, *,
                         session: str | None = None) -> TextGenResult:
    """commit 標題行。**只生成訊息,不碰 git** —— 動 repo 的事不在這支。"""
    return await generate("commit.message", diff_summary,
                          system=_COMMIT_SYSTEM, max_tokens=96,
                          session=session)


#: 逐字稿的輸入上限 —— 一場會議本來就比摘要素材長一到兩個數量級,套
#: `MAX_INPUT_CHARS` 會安靜地吃掉開頭(見 `_clip` 的註解)。
TRANSCRIPT_MAX_INPUT_CHARS = int(os.environ.get(
    "POCKET_TEXTGEN_TRANSCRIPT_MAX_CHARS", "40000"))


async def clean_transcript(raw: str, *, session: str | None = None,
                           system: str = "",
                           allow_local: bool = True) -> TextGenResult:
    """語音轉錄清理。

    這是唯一「輸出可能長」的任務(其他都是一句話),所以 `max_tokens` 按
    輸入長度給 —— 給太少會被**截斷成半句**,而半句清稿比沒清稿更糟。

    `system` 可覆寫:bridge 的會議清稿有一份實測累積的專有名詞清單
    (`_MEETING_POLISH_PROMPT`),那是資產,不該為了搬到這裡重寫一份。
    """
    body = _clip(raw, TRANSCRIPT_MAX_INPUT_CHARS)
    # 輸出長度約等於輸入(只修標點錯字,不摘要);中文約 1 字 1.2~1.5 token。
    budget = max(256, min(8192, int(len(body) * 1.6) + 256))
    return await generate("transcript.cleanup", body,
                          system=system or _TRANSCRIPT_SYSTEM,
                          max_tokens=budget, session=session,
                          allow_local=allow_local,
                          max_input_chars=TRANSCRIPT_MAX_INPUT_CHARS)
