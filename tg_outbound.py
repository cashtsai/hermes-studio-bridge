"""app→TG 反向鏡射(#32 的最後一哩):Pocket 端的 persona 對話回投到同一條
Telegram 聊天室,讓「TG 那頭看得到」。

## 為什麼還缺這一層

hermes-studio-bridge#1 已經把 app 的 persona turn 釘到 gateway **當前**那條 TG
session(sessions.json 對映),所以 `hermes /history`、下一輪對話上下文、以及
`GET /app/v1/messages` 的 state.db 側掃描都看得到 app 說過的話 —— issue #32 原
驗收條件的那半邊已經成立。

但 state.db 只是 transcript 倉。Telegram **聊天室的畫面**只在 gateway 自己處理
一則 TG inbound 時才會被推送(gateway 內的 telegram adapter `send`),而 app 發起
的回合 gateway 從沒經手 —— 所以 TG 畫面上永遠是空的。補上的就是那一次 outbound
投遞:直接用該 profile 自己的 bot 身分,把 user 說的話與 persona 的回覆貼進同一
個 chat。

## 去重不變式(沿用 #37,不新增第二套)

Telegram **不會**把 bot 自己送出的訊息當成 Update 回送。因此這條 outbound:

  * gateway 不經手 → state.db **不留列** → `_persona_history` 掃不到
  * canonical 那份本來就只有一份(app 回合寫的)
  * ⇒ 不產生第三份副本,app 端不會多一顆泡泡

`bridge._tg_dup` 的壓重語意(同 role、剝 `<details>` 後正文相同、±600s)完全不
變、不需要放寬。正文一律先過 `bridge._steps_stripped` 再送:送進 TG 的與
state.db 側的乾淨正文同形,萬一將來 gateway 真的開始回收這些訊息,也仍然對得上
既有的壓重鍵,不會退化成雙氣泡。

模組自身另有一層冪等:同一個 canonical message id(mid)只投一次,回合重試/
`/app/v1/messages` 重放都不會重複貼。

## 預設關閉

`POCKET_TG_MIRROR_OUTBOUND`:

  * ``off``(**預設**)— 完全不動作,零 production 行為改變。
  * ``dry``   — 全程照跑(解析 chat、正規化、冪等、切段),只是不真的送出;
                payload 收進 `outbox()` 供測試與人工核對。可在不干擾正式 TG
                聊天室的前提下端到端驗證。
  * ``on``    — 真的呼叫 Bot API。

只讀:bot token 讀該 persona home 的 `.env`,chat 讀 `sessions/sessions.json`。
不寫 state.db,不碰 gateway 內核。
"""
import asyncio
import collections
import os
import re

from acp_client import canonical_telegram_entry

# Telegram sendMessage 單則上限 4096 字元(UTF-16 code unit,這裡保守用字元數)。
TG_TEXT_LIMIT = 3900

# user 側鏡射的前綴:讓 TG 那頭一眼看出「這句是從手機 app 說的」,不是在 TG 打的。
# persona 回覆刻意不加前綴 —— 它就是人格在說話,與 TG 原生回覆無從區分,這正是
# 「同一段對話」要的效果。
USER_PREFIX = "📱 "

# 已投遞過的 canonical mid(冪等)。bounded:回合量大也不會無限長。
_SENT: "collections.OrderedDict[str, bool]" = collections.OrderedDict()
_SENT_MAX = 4096

# dry-run 的 payload 紀錄(最近 N 筆),測試與人工核對用。
_OUTBOX: "collections.deque" = collections.deque(maxlen=200)


def mode() -> str:
    """``off`` | ``dry`` | ``on``。每次讀環境變數,測試可即時翻。"""
    v = (os.environ.get("POCKET_TG_MIRROR_OUTBOUND") or "").strip().lower()
    return v if v in ("dry", "on") else "off"


def outbox():
    """dry-run 收下的 payload(最舊在前)。"""
    return list(_OUTBOX)


def reset_state() -> None:
    """清冪等帳與 outbox（測試用）。"""
    _SENT.clear()
    _OUTBOX.clear()


def _seen(mid: str) -> bool:
    """記下並回報這個 mid 之前投過沒有(冪等閘門)。"""
    if not mid:
        return False               # 沒有 mid 就無從冪等,照送(呼叫端都有給)
    if mid in _SENT:
        return True
    _SENT[mid] = True
    while len(_SENT) > _SENT_MAX:
        _SENT.popitem(last=False)
    return False


def bot_token(home: str) -> str:
    """該 profile 的 `TELEGRAM_BOT_TOKEN`(讀 home 的 `.env`,唯讀)。

    刻意讀 persona 自己的 home:多 persona 各有各的 bot,拿錯就會貼到別人的
    聊天室。找不到回空字串(呼叫端視為「這個 persona 沒接 TG」)。
    """
    try:
        with open(os.path.join(home, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != "TELEGRAM_BOT_TOKEN":
                    continue
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def chat_from_session_key(key: str):
    """從 gateway 的 session_key 取 ``(chat_id, thread_id)``,取不到回 None。

    格式(gateway `build_session_key`,positional 佈局是它自己的相容承諾):
    ``agent:<profile>:<platform>:<chat_type>:<chat_id>[:<more>]``

      * dm  → 第 6 段(有的話)就是 thread_id。
      * 群組/頻道 → 第 6 段之後可能是 user_id 或 thread_id,**無法**從 key 分辨,
        所以群組只取 chat_id、不帶 message_thread_id(貼到 General 主題)。
        現行三個 persona 都是 dm,這個保守處理不影響實際路徑。
    """
    parts = (key or "").split(":")
    if len(parts) < 5 or parts[0] != "agent" or parts[2] != "telegram":
        return None
    chat_id = parts[4].strip()
    if not chat_id or not re.fullmatch(r"-?\d+", chat_id):
        return None
    thread_id = ""
    if parts[3] == "dm" and len(parts) >= 6 and re.fullmatch(r"\d+", parts[5].strip()):
        thread_id = parts[5].strip()
    return (chat_id, thread_id)


def resolve_target(home: str):
    """該 persona 目前的 TG 投遞目標 ``(token, chat_id, thread_id)``,不齊回 None。

    chat 來自 `canonical_telegram_entry` 挑出的**同一筆** entry —— 也就是 ACP
    session-pinning 寫進去的那條 session 所屬的 chat。兩個方向共用一個真相,
    不會出現「寫進 A 條 session、貼到 B 個聊天室」。
    """
    found = canonical_telegram_entry(home)
    if not found:
        return None
    chat = chat_from_session_key(found[0])
    if not chat:
        return None
    token = bot_token(home)
    if not token:
        return None
    return (token, chat[0], chat[1])


def split_text(text: str):
    """切成 ≤ `TG_TEXT_LIMIT` 的段落,優先在換行處斷開(不硬切句子)。"""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    while len(text) > TG_TEXT_LIMIT:
        cut = text.rfind("\n", 0, TG_TEXT_LIMIT)
        if cut <= 0:
            cut = TG_TEXT_LIMIT
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return [p for p in out if p]


def format_text(role: str, body: str) -> str:
    """送進 TG 的最終正文。`body` 必須是**已剝掉 `<details>` 附錄**的乾淨正文。"""
    body = (body or "").strip()
    if not body:
        return ""
    return f"{USER_PREFIX}{body}" if role == "user" else body


async def _post(token: str, chat_id: str, thread_id: str, text: str) -> bool:
    import httpx

    payload = {
        "chat_id": chat_id,
        "text": text,
        # parse_mode 刻意不設:人格回覆常含 markdown 片段,MarkdownV2 規則嚴格,
        # 一個沒轉義的 `_`/`*` 就整則 400。純文字最穩。
        "disable_notification": True,
        # 為什麼靜音:同一個人剛在 Pocket 打完這句,app 已經有自己的 APNs 推播。
        # TG 再響一次是同一件事的重複噪音。訊息本身照樣留在聊天室裡。
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        return r.status_code == 200


async def mirror(home: str, session: str, role: str, body: str, mid: str,
                 log=None) -> dict:
    """把一則 app 端訊息鏡射進 TG。永不拋例外 —— 鏡射失敗不得影響回合。

    `body` 要傳**已過 `bridge._steps_stripped` 的乾淨正文**(見模組 docstring
    的去重不變式)。回傳一律是 dict,`sent`/`skipped` 供呼叫端記 log。
    """
    result = {"session": session, "role": role, "mode": mode(),
              "sent": 0, "skipped": ""}
    try:
        if result["mode"] == "off":
            result["skipped"] = "disabled"
            return result
        text = format_text(role, body)
        if not text:
            result["skipped"] = "empty"
            return result
        if _seen(mid):
            result["skipped"] = "duplicate"
            return result
        target = resolve_target(home)
        if not target:
            result["skipped"] = "no_target"
            return result
        token, chat_id, thread_id = target
        parts = split_text(text)
        if result["mode"] == "dry":
            _OUTBOX.append({"session": session, "role": role, "chat_id": chat_id,
                            "thread_id": thread_id, "mid": mid, "parts": parts})
            result["sent"] = len(parts)
            result["skipped"] = "dry_run"
            return result
        for p in parts:
            if await _post(token, chat_id, thread_id, p):
                result["sent"] += 1
    except Exception as e:  # noqa: BLE001
        result["skipped"] = f"error:{type(e).__name__}"
        result["error"] = str(e)[:180]
    if log is not None:
        try:
            log("tg_mirror_outbound", **{k: v for k, v in result.items()
                                        if k != "error"})
        except Exception:  # noqa: BLE001
            pass
    return result


def mirror_soon(home: str, session: str, role: str, body: str, mid: str,
                log=None) -> None:
    """射後不理版:掛成背景 task,回合不等 TG 的網路來回。"""
    if mode() == "off":
        return
    try:
        asyncio.get_running_loop().create_task(
            mirror(home, session, role, body, mid, log=log))
    except RuntimeError:
        pass          # 沒有 running loop(同步情境)→ 放棄鏡射,不影響回合
