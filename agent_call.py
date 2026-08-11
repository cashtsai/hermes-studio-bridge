"""agent_call 護欄層 — 政策(allowlist)與 call chain 守門(藍圖 AGENT_INTEROP §1)。

bridge 是 agent 互調的**唯一 hub**:persona/cc/cx/openclaw 之間不直連,
一律經 bridge 的 `POST /app/v2/agent_call` 打現成的 v2 統一輸入路徑。
這個模組是純守門邏輯(不 import bridge、不碰網路/DB),bridge 端負責接線。

安全模型(全部 bridge 側強制,caller 自報身分但由 bridge token 把關):

1. **旗標**:`AGENT_CALL=1` 才開,預設 OFF(merge = 零風險)。
2. **政策檔 default DENY**:沒有政策檔、或沒有規則命中 → 一律拒絕。
3. **深度 ≤ 2**:A→B→C 止步(藍圖 §3.3),C 再往外調直接拒。
4. **循環偵測**:A→B→A 拒絕(await_reply 的天然回覆不算呼叫,不受影響)。
5. **每 chain 預算**:同一 root call chain 至多 6 個 call(每 call = 對目標的
   一個 turn,故 call 預算即 turn 預算)。
6. **絕不代審**:agent_call 只送訊息;目標端的 CC/CX approval 流程照常走,
   本模組與 bridge 接線都沒有任何自動核准路徑。

政策檔(預設 `~/.pocket/agent-call-policy.json`,env `AGENT_CALL_POLICY` 可
覆寫路徑;fnmatch 萬用字元)::

    {
      "version": 1,
      "rules": [
        {"caller": "hermes:yuanfang", "targets": ["claude_code:*"]},
        {"caller": "claude_code:tirith", "targets": ["hermes:yuanfang", "codex:*"]}
      ]
    }

上例即「袁方 → 所有 CC lane」與「tirith → 袁方 + 所有 codex thread」。
沒被任何規則涵蓋的 (caller, target) 組合一律 DENY。
"""
from __future__ import annotations

import fnmatch
import json
import os

DEFAULT_POLICY_PATH = "~/.pocket/agent-call-policy.json"

MODES = ("fire_and_forget", "await_reply", "background")

# call 生命週期:denied(政策/護欄拒)、sent(fire_and_forget 已投遞)、
# running(await/background 進行中)、done(收到回覆)、timeout(等無回覆)、
# error(投遞失敗)。running 以外皆為終態。
TERMINAL_STATUSES = ("denied", "sent", "done", "timeout", "error")


def chain_window_secs() -> float:
    """chain 推斷回看窗:fire_and_forget 投遞後,目標在這段時間內發出的
    call 視為同一 chain(投遞 call 已終態,無法靠 running 推斷)。"""
    try:
        return float(os.environ.get("AGENT_CALL_CHAIN_WINDOW", "") or 600.0)
    except ValueError:
        return 600.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def max_depth() -> int:
    """呼叫深度上限(root call = 深度 1;藍圖 §3.3 預設 2 = A→B→C 止步)。"""
    return _env_int("AGENT_CALL_MAX_DEPTH", 2)


def chain_max_calls() -> int:
    """同一 root chain 的 call 總數上限(= turn 預算,每 call 恰一個 turn)。"""
    return _env_int("AGENT_CALL_CHAIN_MAX", 6)


class CallDenied(Exception):
    """護欄拒絕 —— reason 是給人看的 zh-TW 一句話;code 供 API 對映。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ── 政策(allowlist,default DENY)────────────────────────────────────────

def policy_path() -> str:
    return os.path.expanduser(
        os.environ.get("AGENT_CALL_POLICY", "") or DEFAULT_POLICY_PATH)


def load_policy(path: str | None = None) -> dict:
    """讀政策檔。檔案不存在/壞 JSON/形狀不對 → 空政策(= DENY all)。
    每次呼叫都重讀(檔案小;善彰改政策即時生效,不用重啟 bridge)。"""
    p = os.path.expanduser(path) if path else policy_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001  (不存在、壞 JSON —— 一律當空政策)
        return {"rules": []}
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return {"rules": []}
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        caller = str(r.get("caller") or "").strip()
        targets = r.get("targets")
        if not caller or not isinstance(targets, list):
            continue
        pats = [str(t).strip() for t in targets if str(t).strip()]
        if pats:
            out.append({"caller": caller, "targets": pats})
    return {"rules": out}


def is_family_edge(registry, caller: str, target: str) -> bool:
    """caller 與 target 是否為 registry 家譜上的**直接**母子(任一方向)。

    善彰 2026-08-11 定的鐵律:「cc/cx 互相調閱就是只有母子」—— 一個節點只能
    叫自己的直接 parent 或自己生的直接 child,不得呼叫兄弟或樹上任意節點。
    這比 fnmatch 白名單更嚴也更好推理:呼叫圖被綁死在家譜的樹邊上。parent 是
    spawn 當下就落籍的事實,不需要人另外維護政策,沒有忘設政策的空窗。

    registry 不可用/查不到 → False(這條路不放行),交給白名單那條自行判斷。
    """
    if not caller or not target or caller == target or registry is None:
        return False
    try:
        crow = registry.get(caller)
        if crow and (crow.get("parent") or "") == target:
            return True          # target 是 caller 的直接 parent
        trow = registry.get(target)
        if trow and (trow.get("parent") or "") == caller:
            return True          # target 是 caller 生的直接 child
    except Exception:            # noqa: BLE001 — registry 掛了不該擋死整個調用面
        return False
    return False


def allowed(policy: dict, caller: str, target: str, registry=None) -> bool:
    """(caller, target) 是否放行。default DENY;自呼永遠拒。

    兩條放行來源(任一命中即放行):
      ① **家譜直接母子邊**(善彰鐵律;registry 事實,免政策維護)
      ② 政策檔白名單 rules(fnmatch)

    母子邊是**新增**的放行來源,不是限縮 —— 既有白名單行為一字不變。
    若要把調用嚴格收成「只有母子」,把政策檔的 rules 清空即可。
    """
    if not caller or not target or caller == target:
        return False
    if is_family_edge(registry, caller, target):
        return True
    for r in policy.get("rules") or []:
        if not fnmatch.fnmatchcase(caller, r["caller"]):
            continue
        if any(fnmatch.fnmatchcase(target, pat) for pat in r["targets"]):
            return True
    return False


def allowed_target_patterns(policy: dict, caller: str) -> list[str]:
    """該 caller 命中規則的 target patterns(agent_targets 端點用)。"""
    pats: list[str] = []
    for r in policy.get("rules") or []:
        if fnmatch.fnmatchcase(caller, r["caller"]):
            pats.extend(r["targets"])
    return pats


# ── call chain 守門(深度/循環/預算)──────────────────────────────────────

def check_chain(parent: dict | None, ancestors: list[dict], chain_size: int,
                caller: str, target: str) -> tuple[str | None, str | None, int]:
    """新 call 的 chain 判定。

    parent:呼叫者當下正在服務的 call(bridge 依「有 call 正打在 caller 身上」
    推斷,或 API body 明給 parent_call_id);None = root call。
    ancestors:parent 往上追的整條 call 列(含 parent 自己,新→舊)。
    chain_size:該 root chain 現有 call 數。

    回傳 (root_call_id, parent_call_id, depth);root call 回 (None, None, 1),
    由呼叫端把 root_call_id 落成新 call 自己的 id。
    """
    if parent is None:
        return (None, None, 1)
    depth = int(parent.get("depth") or 0) + 1
    if depth > max_depth():
        raise CallDenied(
            "AGENT_CALL_DEPTH",
            f"超過呼叫深度上限 {max_depth()}(A→B→C 止步):"
            f"{caller} 已在第 {parent.get('depth')} 層,不可再往外調")
    for row in ancestors:
        if str(row.get("caller") or "") == target:
            raise CallDenied(
                "AGENT_CALL_CYCLE",
                f"偵測到循環呼叫:{target} 是本 call chain 的上游呼叫者"
                f"(A→B→A 拒絕;要回話請直接回覆,不要再發 call)")
    if chain_size >= chain_max_calls():
        raise CallDenied(
            "AGENT_CALL_BUDGET",
            f"本 call chain 已用掉 {chain_size} 個 call"
            f"(上限 {chain_max_calls()},每 call 即一個 turn 的預算);"
            f"chain 到此為止,請收斂結果回報")
    return (str(parent.get("root_call_id") or parent.get("id") or ""),
            str(parent.get("id") or ""), depth)
