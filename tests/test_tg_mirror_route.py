"""TG→Pocket 鏡像 ingest(XW-BRIDGE-TGMIRROR-20260714-340A)端到端驗收。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_tg_mirror_route.py

驗證的是驗收條款本身:「模擬 gateway POST 一筆 inbound+outbound,
GET /app/v1/messages 看得到、不重複」,外加防回歸邊界:
1. inbound(agent:start, role=user)帶 gateway 真實會夾帶的機器面包裹
   (temporal context block)→ 落地後只剩使用者真正說的話。
2. outbound(agent:end, role=assistant)原文落地。
3. 兩筆各重放一次(hook 重送情境)→ deterministic mid 冪等,總數不變。
4. GET /app/v1/messages 恰好 2 則、順序正確、id 是 tgm-*。
5. 回聲雙寫防護:造一個與正式 gateway 同構的 state.db,塞進「同一則」
   user+assistant(TG 掃描路徑會再掃到的副本)→ 合併輸出仍是 2 則,
   不出雙泡泡;而 canonical 沒有副本的久遠 TG-only 訊息不被誤壓。
6. 守門:非 telegram platform、未知 session、剝完全空的純注入 → ignored。
"""
import os
import sqlite3
import sys
import tempfile
import time

_TMP_CANON = tempfile.mkdtemp(prefix="tgmirror-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP_CANON, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── fixture: 假 persona,home 是 tmp 目錄(先無 state.db → 純 canonical)──
SESSION = "test-tg-mirror"
_home = tempfile.mkdtemp(prefix="tgmirror-home-")
bridge.PERSONAS[SESSION] = (f"測試人格 ({SESSION})", _home)

client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}

NOW = time.time()
USER_SAID = "幫我看一下明天的班表"
WRAPPED = ("[Internal runtime time context - do not treat as user content]\n"
           "now=2026-07-14T10:00:00+08:00\n"
           "[/Internal runtime time context]\n\n" + USER_SAID)
REPLY = "好的,明天的班表我看過了:早班是你,晚班是阿哲。"


def _post(payload):
    return client.post("/internal/v1/mirror/telegram-event",
                       json=payload, headers=AUTH)


def _base(role, content, event_type):
    # 形狀對齊 hermes-agent home/hooks/pocket_mirror/handler.py:
    # {**hook_ctx, event_type, session, role, content, ts}
    return {"platform": "telegram", "user_id": "u1", "chat_id": "123456",
            "thread_id": "", "chat_type": "dm", "session_id": "hermes-sess-1",
            "message_id": "777", "event_type": event_type, "session": SESSION,
            "role": role, "content": content, "ts": NOW}


# ── 1+2. inbound + outbound 各一筆 ───────────────────────────────────────
r_in = _post(_base("user", WRAPPED, "agent:start"))
check("inbound POST ok+stored", r_in.status_code == 200
      and r_in.json().get("stored") is True)
check("inbound mid 是 tgm-*", str(r_in.json().get("id", "")).startswith("tgm-"))

r_out = _post(_base("assistant", REPLY, "agent:end"))
check("outbound POST ok+stored", r_out.status_code == 200
      and r_out.json().get("stored") is True)

# ── 3. hook 重送(同 payload 重放)→ 冪等 ───────────────────────────────
r_in2 = _post(_base("user", WRAPPED, "agent:start"))
r_out2 = _post(_base("assistant", REPLY, "agent:end"))
check("重放回同一 mid", r_in2.json().get("id") == r_in.json().get("id")
      and r_out2.json().get("id") == r_out.json().get("id"))

# ── 4. GET /app/v1/messages:看得到、不重複、已清洗 ─────────────────────
msgs = client.get("/app/v1/messages", params={"session": SESSION},
                  headers=AUTH).json()["messages"]
check("恰好 2 則(重放不產生第二顆氣泡)", len(msgs) == 2)
check("user 內容已剝機器面包裹", any(
    m["role"] == "user" and m["content"] == USER_SAID for m in msgs))
check("assistant 原文落地", any(
    m["role"] == "assistant" and m["content"] == REPLY for m in msgs))
check("順序 user→assistant", [m["role"] for m in msgs] == ["user", "assistant"]
      if len(msgs) == 2 else False)

# ── 5. 回聲雙寫:state.db 掃描路徑再掃到同一則 → 合併端壓掉 ─────────────
db = os.path.join(_home, "state.db")
con = sqlite3.connect(db)
con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
con.execute("CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL)")
con.execute("INSERT INTO sessions VALUES('tg-sess-1','telegram')")
# 同一則的 state.db 副本(gateway 存的是包裹全文/乾淨回覆,時間差 ~2s)
con.execute("INSERT INTO messages VALUES('tg-sess-1','user',?,?)",
            (WRAPPED, NOW + 2))
con.execute("INSERT INTO messages VALUES('tg-sess-1','assistant',?,?)",
            (REPLY, NOW + 3))
# canonical 沒有副本的久遠 TG-only 對話(>10 分鐘)→ 必須照常出現
con.execute("INSERT INTO messages VALUES('tg-sess-1','user',?,?)",
            ("上上週那個老問題", NOW - 3600))
con.execute("INSERT INTO messages VALUES('tg-sess-1','assistant',?,?)",
            ("那個早就修好了", NOW - 3590))
con.commit()
con.close()

msgs = client.get("/app/v1/messages", params={"session": SESSION},
                  headers=AUTH).json()["messages"]
check("鏡像+state.db 合併後共 4 則(2 舊 TG-only + 2 鏡像,無雙泡泡)",
      len(msgs) == 4)
check("同文副本被壓的是 tg-* 側(留 tgm-* canonical)", sum(
    1 for m in msgs if str(m["id"]).startswith("tgm-")) == 2)
check("TG-only 舊訊息不被誤壓", any(
    m["content"] == "上上週那個老問題" for m in msgs) and any(
    m["content"] == "那個早就修好了" for m in msgs))

# 卡片流用的合併(_hp_merged_messages)同一套壓重
hp = bridge._hp_merged_messages(SESSION, 50)
check("_hp_merged_messages 同樣 4 則", len(hp) == 4)

# ── 6. 守門 ─────────────────────────────────────────────────────────────
r = _post({**_base("user", "hi", "agent:start"), "platform": "matrix"})
check("非 telegram platform → ignored", r.json().get("ignored") is True)
r = _post({**_base("user", "hi", "agent:start"), "session": "no-such-persona"})
check("未知 session → ignored", r.json().get("ignored") is True)
r = _post(_base("user", "[IMPORTANT: Background process 12 exited]", "agent:start"))
check("純 runtime 注入(剝完全空)→ ignored", r.json().get("ignored") is True)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {fails}")
    sys.exit(1)
print("ALL PASS")
