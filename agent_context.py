"""agent_context 護欄層 —— 跨 session 上下文互讀的政策、遮罩與封頂。

`agent_call` 解決的是「A 叫得動 B」;這裡解決的是「A 看得懂 B 在幹嘛」。
善彰 2026-08:cc/cx 接手/協作時的資訊落差,靠的是人肉貼上下文;把「讀對方
session 內文」變成一個受管的能力,接手成本才會塌下來。

與 `agent_call.py` 同樣是**純守門邏輯**(不 import bridge、不碰網路/DB,只讀
政策檔),bridge 端負責接線與資料撈取。

## 安全模型(全部 bridge 側強制)

1. **旗標** `AGENT_CONTEXT=1` 才開,預設 OFF(端點 404,merge = 零風險)。
2. **遮罩是結構性的**:每一段離開 bridge 的文字都先過 `redact_text()`,
   摘要模式連「餵給模型的素材」也先遮罩(見下)。
3. **default DENY**:沒命中任何放行來源 → 拒。
4. **模式分級**:`summary`(蒸餾過、再遮罩)可以放得比 `recent`/`search`
   (原文片段)寬 —— 三種放行來源各自可設允許的模式集合。
5. **每次讀取都留痕**:bridge 往**被讀的那個 session** 的卡片流落一張
   「👁 上下文讀取」卡 + `_log_event`,使用者看得到誰讀了什麼。
6. **封頂**:單次回應字元硬上限、search 命中數上限、每 caller 速率上限。

## 放行來源(三條,任一命中即放行該模式)

| 來源 | 來自哪裡 | 預設允許模式 | env 覆寫 |
|---|---|---|---|
| 家譜邊(parent↔child) | registry 的 `parent` 欄 | summary, recent, search | `AGENT_CONTEXT_FAMILY_MODES` |
| 明示 context 規則 | 政策檔 `context_targets` | summary | `AGENT_CONTEXT_RULE_MODES` |
| 既有 agent_call 放行 | 政策檔 `targets` | summary | `AGENT_CONTEXT_CALL_MODES` |

設計理由(給未來的自己):

- **家譜邊最寬**。母子邊是唯一「這段工作本來就是我派下去的」關係:parent
  讀 child 的原文,讀到的本來就是自己交辦的事;child 讀 parent 則是接手時
  最需要的背景。registry 的 `parent` 欄是 spawn 當下就落籍的事實,不是使用者
  另外維護的設定,所以拿它當預設放行邊,不會有「忘了設政策」的空窗。
  (註:`agent_call.py` 目前的**調用**政策是純 allowlist,並沒有母子邊自動
  放行;母子邊自動放行是本模組新增的**讀取**規則。要一致化成調用也吃母子邊
  是另一個決策,不在這裡偷偷做。)
- **明示規則預設只給 summary**。摘要是蒸餾+二次遮罩後的產物,資訊密度高、
  原文外洩面小;raw 片段(recent/search)可以直接把對方的貼文、路徑、
  半成品搬走。所以「寫一條規則」的預設語意是最小權限,要 raw 得在規則裡
  明寫 `"modes": ["summary", "recent", "search"]`。
- **調用權隱含 summary 讀取權**。你本來就叫得動 B(可以直接問它「你在幹嘛」
  並得到一個回合的回答),那讓你免費讀它的摘要不會擴大攻擊面,反而省掉
  對方一個真回合。這條可用 `AGENT_CONTEXT_CALL_MODES=` 關掉。

政策檔沿用 `agent_call` 那一份(`~/.pocket/agent-call-policy.json`,env
`AGENT_CALL_POLICY` 覆寫)——**不另立第二份政策檔**,免得兩份權限來源打架::

    {
      "version": 1,
      "rules": [
        {"caller": "hermes:yuanfang", "targets": ["claude_code:*"]},
        {"caller": "claude_code:*", "context_targets": ["claude_code:*"]},
        {"caller": "claude_code:tirith", "context_targets": ["codex:*"],
         "modes": ["summary", "recent", "search"]}
      ]
    }

同一個 rule 物件可以同時有 `targets`(調用)與 `context_targets`(讀取);
`modes` 只約束 `context_targets` 那一半。
"""
from __future__ import annotations

import fnmatch
import os
import re

import agent_call

MODES = ("summary", "recent", "search")
#: 會把**原文片段**送出去的模式(相對於 summary 的蒸餾產物)。
RAW_MODES = ("recent", "search")

REDACTED = "***redacted***"      # 與 harness/trajectory.py、bridge 同字樣


class ContextDenied(Exception):
    """讀取被拒 —— reason 是給人看的 zh-TW 一句話;code 供 API 對映。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ── 秘密遮罩 ────────────────────────────────────────────────────────────
# 樣式表逐字對齊 `harness/trajectory.py`(feat/continual-harness,未合併)。
# 那支是純函式、零 IO,本來最該直接 import;但它還在未合併分支上,import 會
# 讓 main 上的 agent_context 依賴一棵不存在的樹。所以這裡放一份最小等價物,
# **待 feat/continual-harness 合併後合併成單一實作**(屆時本區塊整段刪掉,
# 改 `from harness.trajectory import redact_text`;兩邊的測試都要同時綠)。
#
# 順序有意義:先打「有前綴、可精準辨認」的具體 key,再打 key=value 通則。
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
    # Telegram bot token(bridge 自己就有一把,絕不能外流)
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


def redact_text(s) -> str:
    """把任何 api-key 形狀的字串換成 `***redacted***`。

    寧可過度遮罩也不可漏 —— 這條路的產物會離開 bridge 進到另一個 agent 的
    提示詞裡,等於秘密被複製到第二個上下文、第二份 transcript、第二台機器。
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


TRUNC_MARK = "\n…(上下文過長,已截斷)"


def clip(s: str, limit: int) -> tuple[str, bool]:
    """封頂並回報有沒有截斷(回應的 `truncated` 欄就是這個布林)。"""
    s = s or ""
    if len(s) <= limit:
        return (s, False)
    return (s[:limit] + TRUNC_MARK, True)


# ── env 旋鈕 ────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_modes(name: str, default: str) -> tuple:
    """`AGENT_CONTEXT_*_MODES` 逗號清單 → 合法模式 tuple。

    空字串(明示設空)= 這條放行來源整個關掉;未設 = 用 default。
    """
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    out = [m.strip() for m in str(raw).split(",")]
    return tuple(m for m in out if m in MODES)


def family_modes() -> tuple:
    """家譜邊(parent↔child)可讀的模式。預設全開(理由見模組 docstring)。"""
    return _env_modes("AGENT_CONTEXT_FAMILY_MODES", "summary,recent,search")


def rule_default_modes() -> tuple:
    """`context_targets` 規則沒寫 `modes` 時的預設。預設只給 summary。"""
    return _env_modes("AGENT_CONTEXT_RULE_MODES", "summary")


def call_implies_modes() -> tuple:
    """既有 agent_call `targets` 放行所隱含的讀取模式。預設只給 summary;
    設 `AGENT_CONTEXT_CALL_MODES=` 可以整條關掉。"""
    return _env_modes("AGENT_CONTEXT_CALL_MODES", "summary")


def recent_limit_default() -> int:
    return _env_int("AGENT_CONTEXT_RECENT_DEFAULT", 20)


def recent_limit_max() -> int:
    return _env_int("AGENT_CONTEXT_RECENT_MAX", 100)


def max_chars() -> int:
    """單次回應 content 的硬上限(字元)。"""
    return _env_int("AGENT_CONTEXT_MAX_CHARS", 8000)


def search_max_hits() -> int:
    return _env_int("AGENT_CONTEXT_SEARCH_MAX", 30)


def search_max_sessions() -> int:
    """search 一次最多掃幾個 session(避免全機掃描把回合拖死)。"""
    return _env_int("AGENT_CONTEXT_SEARCH_SESSIONS", 40)


def rate_per_min() -> int:
    """每個 caller 每分鐘的讀取次數上限。"""
    return _env_int("AGENT_CONTEXT_RATE", 30)


def summary_material_cards() -> int:
    """蒸餾摘要餵給模型的卡片張數(取最近的)。"""
    return _env_int("AGENT_CONTEXT_SUMMARY_CARDS", 40)


def cache_max_entries() -> int:
    return _env_int("AGENT_CONTEXT_CACHE_MAX", 200)


# ── 政策(沿用 agent_call 那一份檔案)──────────────────────────────────

def policy_path() -> str:
    return agent_call.policy_path()


def _norm_modes(raw, default: tuple) -> tuple:
    if raw is None:
        return default
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return default
    out = tuple(m for m in (str(x).strip() for x in raw) if m in MODES)
    return out or ()      # 明示寫了空清單 = 這條規則不放行任何模式


def load_policy(path: str | None = None) -> dict:
    """讀政策檔 → `{"context_rules": [...], "call_rules": [...]}`。

    檔案不存在/壞 JSON/形狀不對 → 兩邊都空(= DENY all)。每次呼叫都重讀
    (檔案小;善彰改政策即時生效,不用重啟 bridge),與 agent_call 同慣例。
    `call_rules` 直接借用 `agent_call.load_policy()`,確保「調用權」的解讀
    只有一份實作。
    """
    call_pol = agent_call.load_policy(path)
    ctx: list = []
    data = _raw_rules(path)
    default_modes = rule_default_modes()
    for r in data:
        if not isinstance(r, dict):
            continue
        caller = str(r.get("caller") or "").strip()
        targets = r.get("context_targets")
        if not caller or not isinstance(targets, list):
            continue
        pats = [str(t).strip() for t in targets if str(t).strip()]
        if not pats:
            continue
        ctx.append({"caller": caller, "targets": pats,
                    "modes": _norm_modes(r.get("modes"), default_modes)})
    return {"context_rules": ctx, "call_rules": call_pol.get("rules") or []}


def _raw_rules(path: str | None) -> list:
    import json
    p = os.path.expanduser(path) if path else policy_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001  (不存在、壞 JSON —— 一律當空政策)
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return rules if isinstance(rules, list) else []


def decide(policy: dict, caller: str, target: str, mode: str, *,
           family: bool = False) -> tuple[bool, str]:
    """(caller, target, mode) 能不能讀。回 `(allowed, basis_or_reason)`。

    `family`:bridge 依 registry 的 `parent` 欄算出的「是不是母子邊」。
    default DENY;自讀一律拒(要看自己的上下文不用經這條路)。
    """
    if not caller or not target or mode not in MODES:
        return (False, "參數不合法(caller/target/mode)")
    if caller == target:
        return (False, "不能讀自己的上下文")
    if family and mode in family_modes():
        return (True, "family")
    for r in policy.get("context_rules") or []:
        if not fnmatch.fnmatchcase(caller, r["caller"]):
            continue
        if not any(fnmatch.fnmatchcase(target, pat) for pat in r["targets"]):
            continue
        if mode in r["modes"]:
            return (True, "context_rule")
    implied = call_implies_modes()
    if mode in implied:
        for r in policy.get("call_rules") or []:
            if not fnmatch.fnmatchcase(caller, r["caller"]):
                continue
            if any(fnmatch.fnmatchcase(target, pat) for pat in r["targets"]):
                return (True, "agent_call")
    return (False,
            f"政策未放行 {caller} 讀取 {target} 的 {mode}(default DENY;"
            f"母子邊自動放行,其餘請在 {policy_path()} 加 "
            f"context_targets 規則)")


def permitted(policy: dict, caller: str, targets, mode: str,
              family_of=None) -> list:
    """把一串候選 target 過濾成「這個 caller 在這個 mode 讀得到」的子集。

    `family_of`:`callable(target) -> bool`,由 bridge 提供家譜判定。
    search 模式用它決定掃描範圍 —— 搜不到的 session 連命中都不該看到。
    """
    out = []
    for t in targets:
        fam = bool(family_of(t)) if family_of else False
        ok, _ = decide(policy, caller, t, mode, family=fam)
        if ok:
            out.append(t)
    return out


# ── 家譜邊 ──────────────────────────────────────────────────────────────

def is_family_edge(caller_row: dict | None, target_row: dict | None,
                   caller: str, target: str) -> bool:
    """母子邊判定:任一方的 registry `parent` 指向對方即成立(雙向對稱)。

    只認**直接**母子邊,不認祖孫/兄弟:再往外一層就不是「我派的工作」了,
    那種要讀請寫明示規則。
    """
    if caller_row and str(caller_row.get("parent") or "") == target:
        return True
    if target_row and str(target_row.get("parent") or "") == caller:
        return True
    return False


# ── 內容組裝輔助(純字串處理,bridge 負責撈資料)────────────────────────

def card_line(role: str, text: str, ts: float | None = None,
              *, max_len: int = 600) -> str:
    """一張卡 → 一行 `[hh:mm:ss role] text`(已遮罩、單卡封頂)。"""
    import time as _time
    text = redact_text(str(text or "")).strip().replace("\r", "")
    if len(text) > max_len:
        text = text[:max_len] + "…"
    stamp = ""
    if ts:
        try:
            stamp = _time.strftime("%H:%M:%S", _time.localtime(float(ts))) + " "
        except (TypeError, ValueError, OSError):
            stamp = ""
    return f"[{stamp}{role or '?'}] {text}"


def match_snippet(text: str, query: str, *, width: int = 160) -> str | None:
    """大小寫不敏感的子字串命中 → 前後文片段(已遮罩)。沒命中回 None。"""
    if not text or not query:
        return None
    hay = text.lower()
    i = hay.find(query.lower())
    if i < 0:
        return None
    start = max(0, i - width // 2)
    end = min(len(text), i + len(query) + width // 2)
    frag = text[start:end].replace("\n", " ").strip()
    if start > 0:
        frag = "…" + frag
    if end < len(text):
        frag = frag + "…"
    return redact_text(frag)


SUMMARY_PROMPT = (
    "你是一個 AI 工程團隊的交接員。下面是某個 agent session 的近期活動摘錄"
    "(已去識別化)。請寫一份給「即將接手或要與它協作的另一個 agent」看的"
    "交接簡報,繁體中文,不要客套話,只寫這四段:\n"
    "1. 在做什麼:一到兩句話講清楚這個 session 的任務主軸。\n"
    "2. 目前狀態:進行中還是閒置、最後一個回合做完了什麼/卡在哪。\n"
    "3. 關鍵決策與檔案:做過的技術決定、動過或提到的檔案/路徑(照抄原文寫法)。\n"
    "4. 未解問題:還沒決定、還沒驗證、或明顯待辦的事;沒有就寫「無」。\n"
    "全文 400 字以內。不要杜撰摘錄裡沒有的東西;摘錄不足就直說資訊不足。\n\n"
    "── session 摘錄 ──\n"
)


def build_summary_material(meta: dict, lines: list) -> str:
    """組蒸餾素材:戶口欄位 + 近期卡片。**呼叫端傳進來前就該遮罩過**,
    這裡再過一次(縱深防禦:素材本身也不該帶著秘密進到模型)。"""
    head = [
        f"session id: {meta.get('id') or ''}",
        f"provider: {meta.get('provider') or ''}",
        f"用途(registry purpose): {meta.get('purpose') or '(未登記)'}",
        f"目前狀態: {'忙碌中(有回合進行中)' if meta.get('busy') else '閒置'}",
    ]
    if meta.get("parent"):
        head.append(f"上游(parent): {meta['parent']}")
    if meta.get("model"):
        head.append(f"模型: {meta['model']}")
    if meta.get("worktree"):
        head.append(f"工作目錄: {meta['worktree']}")
    body = "\n".join(lines) if lines else "(這個 session 的卡片流目前是空的)"
    return redact_text("\n".join(head) + "\n\n── 近期卡片 ──\n" + body)


def fallback_summary(meta: dict, lines: list) -> str:
    """本機模型不可用時的退路:不蒸餾,直接給結構化的抽取式簡報。

    刻意標明「未經蒸餾」——讓讀的人知道這份沒有被模型整理過,別當成結論。
    """
    head = [
        f"【{meta.get('id') or ''}】{meta.get('purpose') or '(未登記用途)'}",
        f"provider={meta.get('provider') or '?'}"
        f"、狀態={'忙碌中' if meta.get('busy') else '閒置'}",
    ]
    tail = lines[-12:] if lines else []
    return redact_text(
        "(本機蒸餾模型不可用,以下為未經蒸餾的原始摘錄)\n"
        + "\n".join(head)
        + "\n\n── 最近動態 ──\n"
        + ("\n".join(tail) if tail else "(卡片流是空的)"))
