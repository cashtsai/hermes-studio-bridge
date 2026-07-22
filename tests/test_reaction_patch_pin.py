"""G2/#39 canonical 化收尾(issue 合約端點)驗收。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_reaction_patch_pin.py

驗證:
1. schema:message_meta 冪等補 session 欄(_canon_init 跑兩次不炸)。
2. PATCH /app/v1/messages/{id}:
   - 未知 id → 404(MESSAGE_NOT_FOUND)。
   - 設定 → GET /app/v1/messages 帶 reaction(legacy 單值)+ reactions(清單)。
   - {"reaction": null} → 清除,兩欄一起消失。
   - 缺 reaction 鍵 → 400;無 token → 401。
3. PUT /app/v1/sessions/{id}/pin:
   - 全量替換:換清單時舊置頂解除;GET /app/v1/sessions/{id}/pin 讀回。
   - GET /app/v1/messages 每則 pinned 旗標對齊。
   - tg-<ts> id(不在 canonical messages)也可置頂,session 歸屬直接落列。
   - 跨 session 隔離:別的 session 置頂不受影響。
   - 未知 session → 404;body 非清單 → 400。
"""
import os
import sqlite3
import sys
import tempfile

_TMP_CANON = tempfile.mkdtemp(prefix="g2pin-canon-")
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


# ── fixtures:兩個假 persona,home 是 tmp 目錄(無 state.db → 純 canonical)──
S_A, S_B = "test-g2-a", "test-g2-b"
for s in (S_A, S_B):
    bridge.PERSONAS[s] = (f"測試人格 ({s})", tempfile.mkdtemp(prefix=f"g2pin-{s}-"))

client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}

# canonical 訊息:A 兩則、B 一則
mid_a1, _ = bridge._canon_add(S_A, "user", "早安")
mid_a2, _ = bridge._canon_add(S_A, "assistant", "早,老闆")
mid_b1, _ = bridge._canon_add(S_B, "user", "B 的訊息")

# ── 1. schema 冪等 ──────────────────────────────────────────────────────
bridge._canon_init()  # 第二次跑不炸 = 冪等
con = sqlite3.connect(bridge.CANON_DB)
meta_cols = {r[1] for r in con.execute("PRAGMA table_info(message_meta)").fetchall()}
con.close()
check("message_meta has session column", "session" in meta_cols)

# ── 2. PATCH reaction ──────────────────────────────────────────────────
r = client.patch(f"/app/v1/messages/{mid_a1}", json={"reaction": "👍"}, headers=AUTH)
check("PATCH set reaction 200", r.status_code == 200 and r.json()["reaction"] == "👍")

msgs = client.get("/app/v1/messages", params={"session": S_A}, headers=AUTH).json()["messages"]
m1 = next(m for m in msgs if m["id"] == mid_a1)
check("GET carries reaction + reactions",
      m1.get("reaction") == "👍" and m1.get("reactions") == ["👍"])

r = client.patch(f"/app/v1/messages/{mid_a1}", json={"reaction": None}, headers=AUTH)
check("PATCH null clears (200, reaction=None)",
      r.status_code == 200 and r.json()["reaction"] is None)
msgs = client.get("/app/v1/messages", params={"session": S_A}, headers=AUTH).json()["messages"]
m1 = next(m for m in msgs if m["id"] == mid_a1)
check("GET cleared", "reaction" not in m1 and "reactions" not in m1)

r = client.patch("/app/v1/messages/no-such-id", json={"reaction": "🔥"}, headers=AUTH)
check("PATCH unknown id 404", r.status_code == 404
      and r.json()["error"]["code"] == "MESSAGE_NOT_FOUND")
r = client.patch(f"/app/v1/messages/{mid_a1}", json={}, headers=AUTH)
check("PATCH missing key 400", r.status_code == 400)
r = client.patch(f"/app/v1/messages/{mid_a1}", json={"reaction": "🔥"},
                 headers={"Authorization": "Bearer wrong"})
check("PATCH bad token 401", r.status_code == 401)

# ── 3. PUT/GET session pins ────────────────────────────────────────────
r = client.put(f"/app/v1/sessions/{S_A}/pin",
               json={"pinned_message_ids": [mid_a1]}, headers=AUTH)
check("PUT pin a1 200", r.status_code == 200 and r.json()["pinned_message_ids"] == [mid_a1])
r = client.put(f"/app/v1/sessions/{S_B}/pin",
               json={"pinned_message_ids": [mid_b1]}, headers=AUTH)
check("PUT pin b1 200", r.status_code == 200)

msgs = client.get("/app/v1/messages", params={"session": S_A}, headers=AUTH).json()["messages"]
check("GET messages pinned flag",
      next(m for m in msgs if m["id"] == mid_a1).get("pinned") is True
      and next(m for m in msgs if m["id"] == mid_a2).get("pinned") is None)

# 全量替換:a1 → a2 + tg id,a1 要解除
tg_id = "tg-1752000000"
r = client.put(f"/app/v1/sessions/{S_A}/pin",
               json={"pinned_message_ids": [mid_a2, tg_id]}, headers=AUTH)
check("PUT replace 200", r.status_code == 200
      and set(r.json()["pinned_message_ids"]) == {mid_a2, tg_id})
r = client.get(f"/app/v1/sessions/{S_A}/pin", headers=AUTH)
check("GET readback replaced", set(r.json()["pinned_message_ids"]) == {mid_a2, tg_id})
msgs = client.get("/app/v1/messages", params={"session": S_A}, headers=AUTH).json()["messages"]
check("old pin a1 cleared in GET messages",
      next(m for m in msgs if m["id"] == mid_a1).get("pinned") is None)

# 跨 session 隔離:B 的置頂不受 A 的全量替換影響
r = client.get(f"/app/v1/sessions/{S_B}/pin", headers=AUTH)
check("session B pins untouched", r.json()["pinned_message_ids"] == [mid_b1])

# 清空
r = client.put(f"/app/v1/sessions/{S_A}/pin",
               json={"pinned_message_ids": []}, headers=AUTH)
check("PUT empty clears", r.status_code == 200 and r.json()["pinned_message_ids"] == [])

# 守門
r = client.put("/app/v1/sessions/no-such/pin",
               json={"pinned_message_ids": []}, headers=AUTH)
check("PUT unknown session 404", r.status_code == 404)
r = client.put(f"/app/v1/sessions/{S_A}/pin",
               json={"pinned_message_ids": "not-a-list"}, headers=AUTH)
check("PUT bad body 400", r.status_code == 400)
r = client.get(f"/app/v1/sessions/{S_A}/pin",
               headers={"Authorization": "Bearer wrong"})
check("GET pins bad token 401", r.status_code == 401)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
