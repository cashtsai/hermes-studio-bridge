#!/usr/bin/env python3
"""Claude Code hook → Pocket worker 可見層(設計書 §2.4)。

把 Claude Code 自己派出去的 subagent 回報給 bridge,讓善彰在 Pocket 上看得到
「這個 session 現在有幾隻手在跑、在跑什麼」。這是 §2.4 三家接法裡最重要的一家
—— CC 的 subagent 不在 tmux、沒有 pane、不進 ccsess 名單,**除了 hook 之外
沒有任何方法看得到它們**。

安裝(**手動貼**,這支腳本不會自己改 ~/.claude/settings.json):
見 `docs/APP_BRIDGE_CONTRACT.md` §14.4(settings.json 片段 + token 檔 + 旗標)。

═══ 鐵律:絕不擋住工具呼叫 ═══
這支腳本掛在 PreToolUse 上,Claude Code 會**等它跑完**才真的去派 subagent。
所以:
  - 逾時上限預設 1.5 秒(對 127.0.0.1 實測是毫秒級);
  - 任何例外一律吞掉,**永遠 exit 0**(exit 2 會讓 Claude Code 擋掉工具呼叫,
    那等於「可見層壞掉 → 善彰的 agent 不能派工」,本末倒置);
  - 不印任何東西到 stdout(PreToolUse 的 stdout 會被當成控制 JSON 解析)。

═══ 鐵律:token 絕不外洩 ═══
token 只從 env 或 chmod 600 的檔案讀,只放進 Authorization header,
**永不印出、永不寫進錯誤訊息**。也因此建議走 token 檔而不是把 token 寫進
settings.json 的指令字串裡(那會讓 token 落在一個常被 diff/分享的設定檔)。

═══ hook contract(2026-08-12 對 claude 2.1.207 實測驗證,非文件推測)═══
從安裝檔 binary 直接讀出來的 hook payload 組法:
  PreToolUse        {…base, hook_event_name, tool_name, tool_input, tool_use_id}
  PostToolUse       {…base, hook_event_name, tool_name, tool_input, tool_response,
                     tool_use_id, duration_ms}
  PostToolUseFailure{…base, hook_event_name, tool_name, tool_input, tool_use_id,
                     error, is_interrupt, duration_ms}
  base = {session_id, transcript_path, cwd, prompt_id, permission_mode,
          agent_id, agent_type, effort}
兩個關鍵事實:
  1. **工具叫 `Agent` 不叫 `Task`**。2.1.207 的實際 transcript 裡是
     `"name":"Agent"`(近三天 82 筆,`Task` 0 筆)。舊版叫 `Task`,所以下面
     兩個名字都收。
  2. **`tool_use_id` 兩端都有** → Pre 與 Post 能精確配對成同一隻工人,
     不必猜。worker_id 直接用它。
  3. 失敗**不是**靠解析 tool_response,而是另一個事件 `PostToolUseFailure`。
"""
import json
import os
import sys
import urllib.error
import urllib.request

# 只認這兩個工具名(新版 Agent / 舊版 Task)——其他工具不是「派工人」。
AGENT_TOOLS = {"Agent", "Task"}

DEFAULT_URL = "http://127.0.0.1:8081"
# 只認這一個檔。**刻意不收 `~/.config/studio/token`** —— repo CLAUDE.md 明寫
# 「Token 在該 plist 的 EnvironmentVariables.BRIDGE_TOKEN(不是
# ~/.config/studio/token,已過時)」。收一個過時的憑證來源只會讓「到底是哪把
# token 在用」變成查不出來的問題(而且 post() 把 401 也吞掉,不會有人發現)。
TOKEN_FILES = ("~/.pocket/worker-hook.token",)


def _bridge_url() -> str:
    for key in ("POCKET_BRIDGE_URL", "BRIDGE_URL"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val.rstrip("/")
    return DEFAULT_URL


def _token() -> str:
    """env 優先;沒有就讀 token 檔。

    為什麼要有檔案這條路:hook 是 Claude Code 生的子行程,它到底有沒有繼承
    使用者 shell/tmux 的自訂 env,**沒有實測驗證過**(官方文件只保證
    `CLAUDE_PROJECT_DIR` 這類 CLAUDE_* 變數)。token 檔不依賴繼承、也不必把
    token 寫進 settings.json,是比較穩的預設路。
    """
    for key in ("POCKET_WORKER_TOKEN", "BRIDGE_TOKEN"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    for path in TOKEN_FILES:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val
        except Exception:      # noqa: BLE001 —— 讀不到就算了,絕不吵
            continue
    return ""


def _timeout() -> float:
    try:
        return max(0.2, float(os.environ.get("WORKER_HOOK_TIMEOUT", "1.5")))
    except (TypeError, ValueError):
        return 1.5


def _label(tool_input: dict) -> str:
    """人話標籤。Agent 的 tool_input 實測是 {description, subagent_type, prompt}
    —— `description` 就是模型自己寫的一句人話(「審 PR #92 的 blocker」),
    正是 §2.4 要的東西。prompt 動輒數千字,**絕不帶**。"""
    desc = str(tool_input.get("description") or "").strip()
    kind = str(tool_input.get("subagent_type") or tool_input.get("name") or "").strip()
    if desc and kind:
        return f"{desc}（{kind}）"[:200]
    return (desc or kind or "subagent")[:200]


def build_payload(hook: dict) -> dict | None:
    """hook stdin JSON → /app/v2/workers/report 的 body。不該回報就回 None。

    抽成純函式是為了能用 fixture 直接測,不必真的起一個 Claude Code。
    """
    if not isinstance(hook, dict):
        return None
    if str(hook.get("tool_name") or "") not in AGENT_TOOLS:
        return None
    worker_id = str(hook.get("tool_use_id") or "").strip()
    if not worker_id:
        return None
    event = str(hook.get("hook_event_name") or "")
    if event == "PreToolUse":
        state = "running"
    elif event == "PostToolUse":
        state = "done"
    elif event == "PostToolUseFailure":
        state = "failed"
    else:
        return None
    tool_input = hook.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    meta = {"provider": "claude_code", "kind": "subagent"}
    kind = str(tool_input.get("subagent_type") or "").strip()
    if kind:
        meta["subagent_type"] = kind[:80]
    if hook.get("duration_ms") is not None:
        try:
            meta["duration_ms"] = int(hook["duration_ms"])
        except (TypeError, ValueError):
            pass
    if event == "PostToolUseFailure" and hook.get("is_interrupt"):
        meta["interrupted"] = True
    body = {
        "worker_id": worker_id,
        "label": _label(tool_input),
        "state": state,
        "meta": meta,
    }
    # session 身分:明給的優先(使用者自己知道 ccsess 名字時);否則交 CC 的
    # session_id 給 bridge,由 bridge 用它既有的 name↔sid 對應表反查 ——
    # 腳本這端不去猜 tmux 名字,猜錯會把工人掛到別人的 session 上。
    explicit = (os.environ.get("POCKET_WORKER_SESSION") or "").strip()
    if explicit:
        body["session"] = explicit
    cc_sid = str(hook.get("session_id") or "").strip()
    if cc_sid:
        body["cc_session_id"] = cc_sid
    if not explicit and not cc_sid:
        return None
    # hook 在 subagent 內部觸發時 base payload 會帶 agent_id,代表「這隻手又派了
    # 一隻手」。**但 agent_id 和 worker_id 不是同一個號碼空間** —— worker_id 是
    # tool_use_id(`toolu_…`),而派出這隻 subagent 的那次 Agent 呼叫的 tool_use_id
    # 並不在這份 payload 裡,兩者接不起來。所以不塞進 `parent_worker`(那個欄位
    # 承諾「指得到清單裡另一筆 worker」,給個接不上的值等於騙 app 去畫斷掉的樹),
    # 只放進 meta 當診斷線索。哪天 hook payload 給得出父層的 tool_use_id 再接。
    agent_id = str(hook.get("agent_id") or "").strip()
    if agent_id:
        meta["agent_id"] = agent_id[:80]
    agent_type = str(hook.get("agent_type") or "").strip()
    if agent_type:
        meta["running_inside"] = agent_type[:80]
    return body


def post(body: dict) -> None:
    url = _bridge_url() + "/app/v2/workers/report"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    token = _token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    # 這裡刻意不 raise、不 log:旗標沒開時 bridge 回 404,那是預期狀態,
    # 不是錯誤,更不該在善彰每次派工時噴訊息。
    try:
        urllib.request.urlopen(req, timeout=_timeout()).close()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        body = build_payload(json.loads(raw or "{}"))
        if body:
            post(body)
    except Exception:      # noqa: BLE001 —— 見檔頭鐵律:永遠 exit 0
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
