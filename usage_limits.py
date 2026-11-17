"""S4:額度耗盡時自動換 provider —— 把「接了八家」的紅利真的拿到手。

## 為什麼有這個檔

E3 已經會**認出**撞限額這件事:`carddigest.classify_error()` 回
`{"error_kind": "usage_limit", "reset_at": …}`,`apply_usage_limited()` 把它
蓋成 `status.phase == "usage_limited"` + `status.usage_limit_reset_at`,一路
報到 app。

問題是:**那個訊號到今天沒有任何人消費**。使用者看到「CC 額度用完,
X 點重置」,然後只能自己去開另一家。我們接了八個 provider,撞牆時卻還是
手動換 —— 紅利沒拿到。

這支是那顆缺掉的消費者:限額判定(輸入)→ **failover 政策**(輸出)。

## 刻意的邊界

1. **這支是純函式模組**(同 `carddigest` / `t3code_runtime` 的血統):
   不 import bridge、不碰 DB、不發 HTTP。provider 能力(caps)與
   「裝了沒 / 登入了沒」(availability)都是**參數注入**。
   理由:bridge.py 是 2MB、import 時會建表;政策邏輯要能獨立測。
2. **切換 = 新 session + 帶上下文交棒**,不是假裝同一條。復用既有
   ContextTransfer v1(`t3code_runtime.build_transfer_prompt` 的 milestone
   摘要管線 + `rebind_app_thread`)。house precedent:`orchestrator.py:84`
   寫著「failover requires explicit approval and checkpoint」—— 所以
   預設是 `suggest`,不是 `auto`。
3. **自動切不得靜默**。`auto` 模式一定要在卡片上明示「已經換成 X 了、
   為什麼」。安靜地換掉使用者的 provider 是這個 repo 最忌諱的那種病
   (靜默失敗)——使用者會以為還在 CC 上,然後困惑為什麼風格變了。
4. **誠實邊界**:不同 provider 能力不同。切過去之後不支援的 op
   (rename/archive/history/config、interrupt、approvals…)要在卡片講明,
   不是等使用者按下去才發現是死鈕(cc2 2026-10-29 稽核那一顆教訓)。
"""
from __future__ import annotations

import json
import os
import time

_CONFIG_FILE = os.path.expanduser(
    os.environ.get("POCKET_FAILOVER_CONFIG_FILE", "~/.pocket/failover.json"))

#: 模式。`suggest` = 出一張有按鈕的卡片問使用者;`auto` = 直接切,卡片明示。
MODES = ("suggest", "auto")
DEFAULT_MODE = "suggest"

#: 預設備援順序(當輪任務指定)。只是**預設**,使用者可以改 —— 順序的語意
#: 是「我比較想用誰」,不是「誰比較強」。
DEFAULT_ORDER = ("cc2", "codex", "gemini", "grok", "opencode")

#: 哪些 op 算「切過去會不見的東西」—— 用來生誠實邊界文案。鍵照
#: `_V2_SESSION_OPS` 的 op 名,值是給人看的中文。
OP_LABELS = {
    "rename": "改名",
    "archive": "封存",
    "history": "歷史",
    "config_read": "讀設定",
    "config_write": "改設定",
}

#: caps 旗標 → 人話。只挑「使用者感受得到不見了」的那幾個;
#: `streaming`/`native_id_strength` 這類是程度差異不是有無,另外處理。
CAP_LABELS = {
    "interrupt": "中斷回合",
    "queued_messages": "排隊送訊息",
    "approvals": "審批卡片",
}


# ── 設定讀寫(同 grok_sdk / text_gen 的 env 優先 + 0600 落檔慣例)─────────

def _read_file() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def parse_order(raw) -> list:
    """`"cc2,codex"` 或 `["cc2","codex"]` → 去重後的 list。

    **不驗證 provider 名**:這裡不知道有哪些 provider(那是注入的 caps 的
    事),亂打的名字會在 `choose_target` 被 caps 過濾掉 —— 比在這裡 raise
    好,因為設定端點不該因為某一家暫時沒裝就拒絕整份順序。
    """
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace(" ", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw]
    else:
        return []
    out = []
    for p in items:
        if p and p not in out:
            out.append(p)
    return out


def load_config() -> dict:
    """env 優先(`POCKET_FAILOVER_MODE` / `POCKET_FAILOVER_ORDER`),否則讀檔。

    `mode` 認不出來的值一律退 `suggest` —— 打錯字不該變成「自動切」
    (fail-safe 的方向:寧可多問一次,不要擅自換掉使用者的 provider)。
    """
    d = _read_file()
    env_mode = (os.environ.get("POCKET_FAILOVER_MODE") or "").strip().lower()
    file_mode = str(d.get("mode") or "").strip().lower()
    mode, mode_source = DEFAULT_MODE, "default"
    if env_mode in MODES:
        mode, mode_source = env_mode, "env"
    elif file_mode in MODES:
        mode, mode_source = file_mode, "file"

    env_order = parse_order(os.environ.get("POCKET_FAILOVER_ORDER") or "")
    file_order = parse_order(d.get("order"))
    if env_order:
        order, order_source = env_order, "env"
    elif file_order:
        order, order_source = file_order, "file"
    else:
        order, order_source = list(DEFAULT_ORDER), "default"
    return {"mode": mode, "mode_source": mode_source,
            "order": order, "order_source": order_source}


def save_config(mode: str | None = None, order=None) -> dict:
    """設定面板落檔(0600);合併寫入 —— 只帶 order 不可洗掉 mode。

    **會 raise ValueError**(mode 不合法)—— 這裡不同於 load_config 的寬鬆:
    寫入時打錯字一定要頂回去告訴使用者,不然他以為自己開了 auto。
    """
    d = _read_file()
    if mode is not None:
        m = str(mode).strip().lower()
        if m not in MODES:
            raise ValueError("mode 必須是 " + "/".join(MODES))
        d["mode"] = m
    if order is not None:
        d["order"] = parse_order(order)
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _CONFIG_FILE)
    return load_config()


# ── 限額判定(E3 的輸出是這裡的輸入)──────────────────────────────────

def is_usage_limited(status: dict | None) -> bool:
    """這條 session 是不是正卡在限額上?

    看 `phase`(`carddigest.apply_usage_limited` 蓋的),不看錯誤字串 ——
    字串比對是 E3 的工作,重做一遍只會多一份會走樣的規則。
    """
    if not isinstance(status, dict):
        return False
    return str(status.get("phase") or "") == "usage_limited"


def reset_at_of(status: dict | None) -> float | None:
    if not isinstance(status, dict):
        return None
    v = status.get("usage_limit_reset_at")
    return float(v) if isinstance(v, (int, float)) else None


def _fmt_reset(reset_at: float | None, now: float | None = None) -> str:
    """重置時間 → 人話。解不出來就誠實說不知道(不要編一個時間)。"""
    if not reset_at:
        return "重置時間不明"
    now = time.time() if now is None else now
    secs = reset_at - now
    if secs <= 0:
        return "應該已經重置"
    if secs < 3600:
        return f"約 {int(secs // 60) or 1} 分鐘後重置"
    if secs < 86400:
        return f"約 {int(secs // 3600)} 小時後重置"
    return f"約 {int(secs // 86400)} 天後重置"


# ── 備援選擇 ────────────────────────────────────────────────────────────────

def available_providers(availability: dict) -> list:
    """availability 表 → 「現在真的可以用」的 provider 名單。

    `availability[prov]` 形狀(由 bridge 組,見 `_failover_availability`):
        {"installed": bool, "logged_in": bool | None}

    `logged_in is None` = **還在探測**(`_agent_auth_status` 的冷啟動會回
    None)。這裡**當成可用**,理由:那三家 CLI 的探測要 10s+,把「不知道」
    當「沒登入」會讓剛開機的機器一律沒有備援 —— 而真的沒登入的話,
    切過去時 spawn 會失敗並回報,比「安靜地不給備援」好查。
    """
    out = []
    for prov, st in (availability or {}).items():
        if not isinstance(st, dict) or not st.get("installed"):
            continue
        if st.get("logged_in") is False:
            continue
        out.append(prov)
    return out


def caps_gap(from_provider: str, to_provider: str, caps: dict) -> list:
    """從 A 切到 B **會失去什麼**,給人看的字串清單。

    只報「有 → 沒有」的方向。B 多出來的能力不報 —— 卡片上要講的是誠實
    邊界(你按下去會發現不能做的事),不是推銷。
    """
    a = (caps or {}).get(from_provider) or {}
    b = (caps or {}).get(to_provider) or {}
    lost = []
    for flag, label in CAP_LABELS.items():
        if a.get(flag) and not b.get(flag):
            lost.append(label)
    a_ops = set(a.get("ops") or ())
    b_ops = set(b.get("ops") or ())
    for op in sorted(a_ops - b_ops):
        lost.append(OP_LABELS.get(op, op))
    if (a.get("streaming") == "live") and (b.get("streaming") == "poll"):
        lost.append("即時串流(改為輪詢)")
    return lost


def choose_target(current: str, *, availability: dict, caps: dict,
                  order=None, targets=None) -> dict:
    """選下一家。回傳 `{"provider": str|None, "reason": str, ...}`,**不 raise**。

    過濾順序(每一層都留痕在 `rejected`,因為「為什麼沒給我備援」是這個
    功能最容易被問的問題):
      1. 不選自己
      2. caps 表裡要有(沒宣告 caps 的 provider 不是備援候選 —— 宣告是
         加入這張表的門票,見 tests/test_provider_caps.py)
      3. `targets` 白名單(交棒**落地端**的實作限制:今天只有
         `_TRANSFER_TARGETS` 那兩家有 spawn helper。宣告了卻不能落地
         就是死鈕)
      4. availability(裝了 + 不是明確沒登入)

    回傳的 `order` 是實際考慮過的順序,`caps_gap` 是選中那家的誠實邊界。
    """
    order = parse_order(order) if order is not None else list(DEFAULT_ORDER)
    usable = set(available_providers(availability))
    allowed = set(targets) if targets is not None else None
    rejected = []
    for prov in order:
        if prov == current:
            rejected.append({"provider": prov, "why": "same_provider"})
            continue
        if prov not in (caps or {}):
            rejected.append({"provider": prov, "why": "no_caps_declared"})
            continue
        if allowed is not None and prov not in allowed:
            rejected.append({"provider": prov, "why": "no_handoff_target"})
            continue
        if prov not in usable:
            rejected.append({"provider": prov, "why": "unavailable"})
            continue
        return {"provider": prov, "reason": "ok", "order": order,
                "rejected": rejected,
                "caps_gap": caps_gap(current, prov, caps)}
    return {"provider": None,
            "reason": "no_candidate" if order else "empty_order",
            "order": order, "rejected": rejected, "caps_gap": []}


# ── 卡片 ────────────────────────────────────────────────────────────────────
# 走既有的 `kind: "choices"`(carddigest.extract_studio_cards 的形狀):
# `options[].send` 的文字會被當成使用者訊息送回來,Telegram 上自動退化成
# 純文字 —— 不必為這個功能開新端點、也不必改 app 才看得到。

def _plan_lines(current: str, target: str, reset_at: float | None,
                caps_lost: list, now: float | None = None) -> list:
    lines = [f"{current} 的額度用完了({_fmt_reset(reset_at, now)})。"]
    if caps_lost:
        # 誠實邊界:先講會少什麼,再讓他決定。
        lines.append("切到 " + target + " 之後會少:" + "、".join(caps_lost))
    lines.append("交棒會開一條新的 " + target
                 + " session,並帶上這條的里程碑摘要(不是同一條對話)。")
    return lines


#: 切換的控制面在這支端點上(卡片只負責「告知」)。
SWITCH_ENDPOINT = "POST /app/v2/threads/{thread_id}/failover"


def suggest_card_body(current: str, plan: dict, *, thread_id: str = "",
                      reset_at: float | None = None,
                      now: float | None = None) -> dict:
    """`suggest` 模式的卡片 —— 告知 + 給出該怎麼切,但**不自己切**。

    ## 為什麼 `options` 是空的

    第一版把兩顆按鈕做成 `choices` 的 `send`(把 `/failover codex` 當成
    使用者訊息送回來)。查下去發現 **bridge 沒有任何地方在解析斜線指令**
    —— 那兩顆按鈕按下去只會把 `/failover codex` 原封不動送進一條**正卡在
    限額上**的 session,然後什麼都不會發生。那就是這個 repo 反覆踩到的
    「死鈕」:宣告了卻不能落地。

    所以這張卡片誠實地只做告知:要切的人(app / CLI)照 `plan.provider`
    打 `SWITCH_ENDPOINT`。結構化的 `plan` 在預覽端點的回應裡,app 畫自己
    的按鈕時直接用 —— 按鈕的 wiring 歸 app 那一波(契約 §19.2/§19.4)。
    等哪天真的有指令解析層,再把 `options` 補回來,**連同它的端到端測試**。
    """
    target = plan.get("provider") or ""
    lines = _plan_lines(current, target, reset_at,
                        plan.get("caps_gap") or [], now)
    lines.append("要切的話:" + SWITCH_ENDPOINT.format(thread_id=thread_id
                                                   or "{thread_id}")
                 + f'  body {{"to_provider": "{target}"}}')
    lines.append("不切就等額度重置,這條 session 會自己恢復。")
    return {
        "kind": "choices",
        "title": f"額度用完 —— 可以切到 {target} 繼續",
        "detail": "\n".join(lines),
        "ref": thread_id,
        # 刻意為空:沒有能落地的按鈕,就不要畫按鈕(見上面的 docstring)。
        "options": [],
        "fallback_text": (f"⚠️ {current} 額度用完"
                          f"({_fmt_reset(reset_at, now)})。"
                          f"可以交棒給 {target} 繼續,或等重置。"),
    }


def auto_card_body(current: str, plan: dict, *, thread_id: str = "",
                   new_session_id: str = "", reset_at: float | None = None,
                   now: float | None = None) -> dict:
    """`auto` 模式的卡片 —— **已經切了,明示**。

    沒有按鈕(事情已經發生),但一定要說清楚換成誰、為什麼、會少什麼。
    這張卡片的存在就是「自動切不得靜默」那條紅線的實作。
    """
    target = plan.get("provider") or ""
    lines = _plan_lines(current, target, reset_at,
                        plan.get("caps_gap") or [], now)
    lines.insert(0, f"已自動切換:{current} → {target}。")
    if new_session_id:
        lines.append("新 session:" + new_session_id)
    lines.append("(自動切換可在設定裡改回「先問我」。)")
    detail = "\n".join(lines)
    return {
        "kind": "choices",
        "title": f"已自動切到 {target}(原因:{current} 額度用完)",
        "detail": detail,
        "ref": thread_id,
        "options": [],      # 已成事實,不給假按鈕
        "fallback_text": (f"🔀 {current} 額度用完,已自動切到 {target} 繼續。"
                          + (" 新 session:" + new_session_id
                             if new_session_id else "")),
    }


def blocked_card_body(current: str, plan: dict, *, thread_id: str = "",
                      reset_at: float | None = None,
                      now: float | None = None) -> dict:
    """撞限額但**沒有備援可用** —— 也要說話。

    這張卡片是最容易被忽略、卻最該存在的一張:沒有它,使用者會以為
    failover 壞了(或根本沒裝)。講清楚「考慮過誰、為什麼都不行」。
    """
    why = {"same_provider": "就是目前這家", "no_caps_declared": "未宣告能力",
           "no_handoff_target": "尚不支援交棒落地", "unavailable": "未安裝或未登入"}
    bits = [f"{r['provider']}({why.get(r['why'], r['why'])})"
            for r in (plan.get("rejected") or [])]
    detail = [f"{current} 的額度用完了({_fmt_reset(reset_at, now)}),"
              "但目前沒有可用的備援 provider。"]
    if bits:
        detail.append("考慮過:" + "、".join(bits))
    detail.append("裝好/登入其中一家,或等額度重置。")
    return {
        "kind": "choices",
        "title": "額度用完,且沒有可用備援",
        "detail": "\n".join(detail),
        "ref": thread_id,
        "options": [],
        "fallback_text": (f"⚠️ {current} 額度用完,且沒有可用的備援 provider"
                          f"({_fmt_reset(reset_at, now)})。"),
    }


def plan_for(current: str, status: dict, *, availability: dict, caps: dict,
             order=None, targets=None, mode: str | None = None) -> dict:
    """一站式:限額了嗎 → 選誰 → 該出哪張卡。bridge 只要呼這一支。

    回傳 `{"limited": bool, "mode": str, "action": str, "plan": {...},
            "reset_at": float|None}`;`action ∈ none|suggest|auto|blocked`。
    """
    cfg = load_config()
    mode = mode if mode in MODES else cfg["mode"]
    order = cfg["order"] if order is None else order
    if not is_usage_limited(status):
        return {"limited": False, "mode": mode, "action": "none",
                "plan": {}, "reset_at": None}
    plan = choose_target(current, availability=availability, caps=caps,
                         order=order, targets=targets)
    action = "blocked" if not plan.get("provider") else mode
    return {"limited": True, "mode": mode, "action": action, "plan": plan,
            "reset_at": reset_at_of(status)}
