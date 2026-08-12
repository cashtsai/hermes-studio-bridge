"""軌跡正規化 + 秘密遮罩(Continual Harness 第一段)。

**純函式,零 IO**(照 `agent_call.py` 的慣例:不 import bridge、不開檔、不連
網、不碰 DB)。呼叫端(bridge 或 `harness.distill`)負責把原始素材撈出來餵
進來,這裡只負責把三種來源攤平成同一個形狀:

    {session_id, turn_id, ts, provider, purpose, node_config,
     steps: [{kind, tool?, summary, outcome}],
     result: {ok, duration_s, turns, error?}}

三種來源(全部是**現成的**軌跡,不需要新的紀錄機制):
1. `from_card_events()` — per-session 卡片流 ring(`carddigest.SessionCardStore.events`)
2. `from_cc_jsonl()`    — Claude Code 的 `~/.claude/projects/<slug>/<uuid>.jsonl`
3. `from_canonical_rows()` — canonical.db 的 `messages`(**唯讀**撈出來的 row)

## 兩條鐵律

**(a) 秘密永不進 harness 庫。** 每一段進到 step/result 的文字都必須過
`redact_text()`;node_config 必須過 `redact_config()`。api key 一旦寫進蒸餾
庫,就會被夜批餵給模型、被寫進提案 preview、被晨報顯示 —— 三重外洩。
所以遮罩做在**最上游的正規化**,而不是下游各自小心。

**(b) 一律封頂。** 一個節點跑一整晚可以產出上萬張卡;蒸餾器吃的是提示詞,
不是資料庫。所以 steps 封頂(`MAX_STEPS`,取頭尾、中間摺疊)、每段文字封頂
(`MAX_SUMMARY`),避免單一失控 turn 撐爆提示詞或 sqlite。
"""
from __future__ import annotations

import hashlib
import re

# ── 封頂(可用參數覆寫;預設值以「餵得進一個提示詞」為準)────────────────
MAX_STEPS = 120          # 每條軌跡保留的 step 數(超過取頭尾 + 中間摺疊卡)
MAX_SUMMARY = 200        # 每個 step 的 summary 字元上限
MAX_ERROR = 500          # result.error 字元上限
MAX_TEXT_STEP = 400      # 助手發言(say)保留的字元上限

REDACTED = "***redacted***"      # 沿用 bridge `_spawn_config_redacted` 的字樣

# ── 秘密樣式 ────────────────────────────────────────────────────────────
# 順序有意義:先打「有前綴、可精準辨認」的具體 key,再打「key=value」通則。
# 通則放最後,免得它先把 `ANTHROPIC_API_KEY=sk-ant-...` 整段吃掉,讓具體樣式
# 的測試看起來過了、其實是被通則救的。
_SECRET_PATTERNS = (
    # Anthropic / OpenAI 系(sk-ant-…, sk-proj-…, sk-…)
    re.compile(r"sk-(?:ant|proj|or|live|test)?-?[A-Za-z0-9_\-]{16,}"),
    # GitHub(ghp_/gho_/ghu_/ghs_/ghr_ + fine-grained PAT)
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # AWS access key id / Google API key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    # Slack
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    # Telegram bot token(bridge 自己就有一把,絕不能進庫)
    re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_\-]{30,}"),
    # JWT(三段 base64url)
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # Authorization: Bearer <token>
    re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._\-]{16,}"),
    # 通則:任何 (api_key|token|secret|password|passwd|BRIDGE_TOKEN…) = value
    re.compile(
        r"(?i)\b([a-z0-9_\-]*(?:api[_\-]?key|apikey|token|secret|password|passwd)"
        r"[a-z0-9_\-]*)\s*[=:]\s*[\"']?([^\s\"',;&]{8,})"),
    # PEM 私鑰整塊
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

# node_config 裡一律不外流的欄位名(比對時小寫化;`_spawn_config_public` 的
# 同一套精神:api_key 連遮罩版都不給,只留「有沒有」的布林)。
_SECRET_CONFIG_KEYS = ("api_key", "apikey", "token", "secret", "password")


def redact_text(s) -> str:
    """把任何 api-key 形狀的字串換成 `***redacted***`。

    寧可過度遮罩也不可漏 —— 蒸餾庫是會被模型讀、被晨報顯示的地方。
    非字串輸入一律先 str()(step summary 可能拿到 int/None)。
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    if not s:
        return ""
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            # key=value 通則:留住 key 名(有診斷價值),只殺 value。
            s = pat.sub(lambda m: f"{m.group(1)}={REDACTED}", s)
        else:
            s = pat.sub(REDACTED, s)
    return s


def redact_config(cfg) -> dict:
    """node_config 的安全版:秘密欄位整個拿掉,只留 `has_api_key` 布林。

    與 bridge 的 `_spawn_config_public()` 同一個契約(bridge 端會先過那個
    函式再傳進來);這裡再擋一次,是因為 harness 也吃 registry meta 之類
    沒經過那條路的 dict —— 縱深防禦,不靠呼叫端記得。
    """
    if not isinstance(cfg, dict):
        return {}
    out: dict = {}
    for k, v in cfg.items():
        if str(k).lower() in _SECRET_CONFIG_KEYS:
            if v:
                out["has_api_key"] = True
            continue
        out[k] = redact_text(v) if isinstance(v, str) else v
    return out


def trajectory_id(session_id: str, turn_id: str) -> str:
    """穩定 id:同一個 (session, turn) 重放幾次都是同一個 id。

    提案的 evidence 欄存的就是這個 id;蒸餾跑兩次不會產生兩份看似不同的
    證據。沿用 bridge `_report_id` 的 sha1-截斷慣例。
    """
    raw = f"{session_id}\x00{turn_id}".encode("utf-8")
    return "traj-" + hashlib.sha1(raw).hexdigest()[:16]


def _clip(s, limit: int) -> str:
    s = redact_text(s)
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def make_step(kind: str, summary="", *, tool: str = "", outcome: str = "") -> dict:
    """一個正規化 step。kind ∈ say|tool|result|approval|attach|note。

    `tool`/`outcome` 只在有值時出現(契約允許缺欄),讓提示詞不被一堆空字串
    稀釋。summary 一律過遮罩 + 封頂。
    """
    step: dict = {"kind": kind, "summary": _clip(summary, MAX_SUMMARY)}
    if tool:
        step["tool"] = _clip(tool, 60)
    if outcome:
        step["outcome"] = outcome
    return step


# 判定 tool_result 是成功還失敗。刻意保守:**只有明確像錯誤才算 error**,
# 因為蒸餾器會把 error 當「這條路走不通」的訊號,誤判比漏判傷害大。
_ERROR_RE = re.compile(
    r"^\s*(?:error|exception|traceback|fatal|command not found|"
    r"permission denied|no such file|failed|cannot |could not )"
    r"|\berror:\s|\bexit code [1-9]|\bnon-zero exit\b",
    re.IGNORECASE)


def classify_outcome(text) -> str:
    """tool_result 文字 → "ok" | "error"。"""
    if not text:
        return "ok"
    return "error" if _ERROR_RE.search(str(text)[:600]) else "ok"


def _cap_steps(steps: list, max_steps: int = MAX_STEPS) -> list:
    """封頂:超過就取頭尾,中間插一張「摺疊了 N 步」的 note。

    取頭尾而不是只取頭:一條軌跡的價值在「開場怎麼下手」+「結尾怎麼收」,
    中段的重複試錯正是要被蒸餾掉的東西。
    """
    if len(steps) <= max_steps:
        return steps
    head = max_steps // 2
    tail = max_steps - head - 1
    dropped = len(steps) - head - tail
    return (steps[:head]
            + [make_step("note", f"(中段 {dropped} 步已摺疊)")]
            + steps[len(steps) - tail:])


def _finish(session_id, turn_id, ts, provider, purpose, node_config,
            steps, started, ended, error, max_steps) -> dict:
    """把累積的 steps 收成一條軌跡記錄(共用給三個 from_* 來源)。"""
    tool_rounds = sum(1 for s in steps if s.get("kind") == "tool")
    has_err = bool(error) or any(s.get("outcome") == "error" for s in steps)
    duration = max(0.0, float(ended or 0) - float(started or 0))
    result = {"ok": not has_err,
              "duration_s": round(duration, 3),
              "turns": tool_rounds}
    if error:
        result["error"] = _clip(error, MAX_ERROR)
    elif has_err:
        first = next((s for s in steps if s.get("outcome") == "error"), None)
        if first:
            result["error"] = _clip(first.get("summary") or "工具回報錯誤", MAX_ERROR)
    return {"id": trajectory_id(session_id, turn_id),
            "session_id": session_id,
            "turn_id": turn_id,
            "ts": float(ts or started or 0.0),
            "provider": provider or "",
            "purpose": _clip(purpose or "", 200),
            "node_config": redact_config(node_config),
            "steps": _cap_steps(steps, max_steps),
            "result": result}


# ── 來源 1:卡片流 ring(SessionCardStore.events)────────────────────────

_CARD_KIND_MAP = {
    "markdown": "say", "text": "say",
    "tool_call": "tool", "tool_result": "result",
    "attachment": "attach", "approval": "approval",
}


def from_card_events(events, *, session_id: str, provider: str = "",
                     purpose: str = "", node_config=None,
                     max_steps: int = MAX_STEPS) -> list[dict]:
    """卡片流事件 ring → 軌跡列(一個 turn_id 一條)。

    events 形狀 = `SessionCardStore.events`:`{seq, ts, type, data}`,
    type ∈ card.upsert | turn | session.status。turn 事件的
    `{state: start|end, turn_id}` 是天然的分界線;沒有 turn 事件的來源
    (舊 ring / 冷載入)就退回用卡片自己的 turn_id 分組,兩者都吃得下。

    同一張卡的 rev 修訂(串流來源會 upsert 多次)取**最後一版**:重複的
    card id 直接覆蓋原位,不會讓同一句話在軌跡裡出現三遍。
    """
    by_turn: dict = {}
    order: list = []

    def bucket(tid: str) -> dict:
        if tid not in by_turn:
            by_turn[tid] = {"cards": {}, "card_order": [],
                            "started": None, "ended": None}
            order.append(tid)
        return by_turn[tid]

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        ts = float(ev.get("ts") or 0.0)
        data = ev.get("data") or {}
        if etype == "turn":
            tid = str(data.get("turn_id") or "")
            if not tid:
                continue
            b = bucket(tid)
            if data.get("state") == "start":
                b["started"] = ts if b["started"] is None else b["started"]
            else:
                b["ended"] = ts
            continue
        if etype != "card.upsert":
            continue
        card = data.get("card") or {}
        tid = str(card.get("turn_id") or "")
        b = bucket(tid)
        cid = str(card.get("id") or f"_{len(b['card_order'])}")
        if cid not in b["cards"]:
            b["card_order"].append(cid)
        b["cards"][cid] = card
        cts = float(card.get("ts") or ts or 0.0)
        if b["started"] is None or cts < b["started"]:
            b["started"] = cts
        if b["ended"] is None or cts > b["ended"]:
            b["ended"] = cts

    out = []
    for tid in order:
        b = by_turn[tid]
        steps = []
        for cid in b["card_order"]:
            card = b["cards"][cid]
            kind = _CARD_KIND_MAP.get(str(card.get("kind") or ""))
            if kind is None:
                continue
            body = card.get("body") or {}
            if kind == "tool":
                steps.append(make_step("tool", body.get("summary") or "",
                                       tool=str(body.get("tool") or "tool")))
            elif kind == "result":
                txt = body.get("text") or body.get("fallback_text") or ""
                steps.append(make_step("result", txt,
                                       outcome=classify_outcome(txt)))
            elif kind == "say":
                txt = str(body.get("text") or "")
                if txt.startswith("💭"):     # thinking 卡:軌跡不需要內心戲
                    continue
                steps.append(make_step("say", _clip(txt, MAX_TEXT_STEP)))
            elif kind == "attach":
                steps.append(make_step("attach", body.get("filename") or ""))
            elif kind == "approval":
                steps.append(make_step("approval", body.get("title") or ""))
        if not steps:
            continue
        out.append(_finish(session_id, tid, b["ended"] or b["started"],
                           provider, purpose, node_config, steps,
                           b["started"], b["ended"], None, max_steps))
    return out


# ── 來源 2:Claude Code jsonl ─────────────────────────────────────────────

# CC 會把工具回填、系統提醒等「管路訊息」也寫成 user 行。碰到這些前綴就
# 不當成新 turn 的起點(沿用 carddigest.PLUMBING_TAGS 的判定精神)。
_PLUMBING_PREFIXES = ("<command-name>", "<local-command", "<system-reminder>",
                      "Caveat: The messages below", "<bash-input>",
                      "<bash-stdout>", "<user-prompt-submit-hook>")


def from_cc_jsonl(lines, *, session_id: str, provider: str = "claude_code",
                  purpose: str = "", node_config=None,
                  max_steps: int = MAX_STEPS) -> list[dict]:
    """CC transcript jsonl(已 parse 的 dict 列)→ 軌跡列。

    turn 分界 = **一則真正的使用者輸入**(type=user 且 content 是字串且不是
    管路訊息)。同一個 turn 內的 assistant text / tool_use、以及回填的
    tool_result(CC 把它寫成 type=user 的 block 列)全部歸進去。

    turn_id 用該 turn 首行的 `uuid`(CC 自己就有,穩定且可回溯);沒有就
    退回序號。壞行/缺欄一律跳過 —— 蒸餾寧可少一條軌跡,不能因為一行髒
    資料就整晚不出提案。
    """
    turns: list = []
    cur = None

    def open_turn(tid: str, ts: float, prompt: str):
        nonlocal cur
        cur = {"tid": tid, "started": ts, "ended": ts,
               "steps": [make_step("say", _clip(prompt, MAX_TEXT_STEP))]}
        turns.append(cur)

    for idx, d in enumerate(lines or []):
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        msg = d.get("message") or {}
        ts = _epoch(d.get("timestamp"))
        if t == "user":
            content = msg.get("content")
            if isinstance(content, str):
                head = content.lstrip()
                if any(head.startswith(p) for p in _PLUMBING_PREFIXES):
                    continue
                if not head.strip():
                    continue
                open_turn(str(d.get("uuid") or f"cc-{idx}"), ts, content)
                continue
            if isinstance(content, list) and cur is not None:
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    txt = _blocks_text(b.get("content"))
                    is_err = bool(b.get("is_error")) or classify_outcome(txt) == "error"
                    cur["steps"].append(make_step(
                        "result", txt, outcome="error" if is_err else "ok"))
                    cur["ended"] = max(cur["ended"], ts)
            continue
        if t == "assistant" and cur is not None:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    cur["steps"].append(make_step("say", _clip(b["text"], MAX_TEXT_STEP)))
                elif bt == "tool_use":
                    inp = b.get("input") or {}
                    summary = (inp.get("command") or inp.get("file_path")
                               or inp.get("path") or inp.get("pattern")
                               or inp.get("description") or "")
                    if not summary and isinstance(inp, dict):
                        summary = next((str(v) for v in inp.values()
                                        if isinstance(v, (str, int))), "")
                    cur["steps"].append(make_step(
                        "tool", str(summary).splitlines()[0] if summary else "",
                        tool=str(b.get("name") or "tool")))
            cur["ended"] = max(cur["ended"], ts)

    return [_finish(session_id, tr["tid"], tr["ended"], provider, purpose,
                    node_config, tr["steps"], tr["started"], tr["ended"],
                    None, max_steps)
            for tr in turns if len(tr["steps"]) > 1]


def _epoch(v) -> float:
    """CC 的 ISO8601 timestamp → epoch 秒(壞值回 0.0,不拋)。"""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return 0.0
    try:
        import datetime
        s = v.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _blocks_text(content) -> str:
    """tool_result 的 content(str 或 block 列)→ 純文字。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
    return "\n".join(parts)


# ── 來源 3:canonical.db messages(唯讀撈出來的 row)──────────────────────

def from_canonical_rows(rows, *, session_id: str, provider: str = "hermes",
                        purpose: str = "", node_config=None,
                        max_steps: int = MAX_STEPS) -> list[dict]:
    """canonical.db `messages` 的 row → 軌跡列(人格線用)。

    rows:`{role, content, created_at, turn_id}` 的 dict 或 sqlite3.Row
    (時間**遞增**排序)。人格線沒有 tool 卡,軌跡就是「使用者說 → 人格答」
    的對話節奏;turn_id 缺失時以每則 user 訊息當分界(人格早期資料沒有
    turn_id,不能因此整批棄用)。

    ⚠️ 呼叫端**必須**用 `mode=ro` URI 開 canonical.db。這個函式只吃 row,
    本身不開任何檔 —— 唯讀保證由呼叫端 + 測試共同把關。
    """
    turns: list = []
    cur = None
    seen_turn: dict = {}
    for r in rows or []:
        try:
            role = str(r["role"] or "")
            content = r["content"] or ""
            ts = float(r["created_at"] or 0.0)
            tid = str((r["turn_id"] if "turn_id" in r.keys() else "") or "") \
                if hasattr(r, "keys") else str(r.get("turn_id") or "")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if role == "user" or (tid and tid not in seen_turn):
            cur = {"tid": tid or f"canon-{len(turns)}", "started": ts,
                   "ended": ts, "steps": []}
            turns.append(cur)
            if tid:
                seen_turn[tid] = cur
        if cur is None:
            continue
        cur["steps"].append(make_step("say", _clip(content, MAX_TEXT_STEP)))
        cur["ended"] = max(cur["ended"], ts)
    return [_finish(session_id, tr["tid"], tr["ended"], provider, purpose,
                    node_config, tr["steps"], tr["started"], tr["ended"],
                    None, max_steps)
            for tr in turns if tr["steps"]]
