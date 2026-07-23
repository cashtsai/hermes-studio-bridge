"""feat/apns-sender 驗收(repo 慣例:python3 tests/test_apns_push_register.py)。

驗證:
1. 金鑰缺席(APNS_KEY_PATH 指到不存在的檔)→ bridge import/啟動照常,
   push_notify 短路回 disabled=True,完全不碰 _apns_send。
2. POST /app/v1/push/register:
   - 註冊 token 落 devices 表 + 偏好落 push_prefs.json(canonical 旁)。
   - preview/personas 預設(true/null=全訂閱);缺 token → 400;
     personas 非清單 → 400;無 auth → 401。
   - 冪等:重打同 token 覆蓋偏好。
3. push_notify(mock _apns_send):
   - persona 過濾:沒訂閱該人格的裝置跳過(skipped)。
   - preview=False + no_preview_body → body 換占位,訊息內容不外送。
   - 410 → token 從 devices 表剪掉,偏好一併清掉。
4. 舊 /app/v1/devices 註冊(無偏好)→ 預設照推(向後相容)。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="apns-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
# 金鑰缺席情境:指到保證不存在的路徑 → apns_configured() 必為 False。
os.environ["APNS_KEY_PATH"] = os.path.join(_TMP, "no-such-key.p8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 即啟動路徑:_canon_init 等都在這裡跑)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}


# ── 1. 金鑰缺席:bridge 活著、模組靜默停用 ────────────────────────────────
check("import 成功(缺金鑰不影響啟動)", hasattr(bridge, "app"))
check("apns_configured() False", bridge.apns_configured() is False)

_sent_calls = []


async def _mock_send(token, title, body, data=None, category=None,
                     thread_id=None, content_available=False):
    _sent_calls.append({"token": token, "title": title, "body": body,
                        "data": data, "thread_id": thread_id})
    return 200, ""


_orig_send = bridge._apns_send
bridge._apns_send = _mock_send

res = asyncio.run(bridge.push_notify("t", "b"))
check("缺金鑰 push_notify 短路 disabled", res.get("disabled") is True)
check("缺金鑰不碰 _apns_send", _sent_calls == [])

r = client.post("/app/v1/push/test", headers=AUTH, json={})
check("/push/test 回報 apns_configured=False",
      r.status_code == 200 and r.json()["apns_configured"] is False
      and r.json()["disabled"] is True)

# ── 2. /app/v1/push/register ────────────────────────────────────────────────
r = client.post("/app/v1/push/register", json={"token": "tok-a"})
check("無 auth → 401", r.status_code == 401)

r = client.post("/app/v1/push/register", headers=AUTH, json={})
check("缺 token → 400", r.status_code == 400)

r = client.post("/app/v1/push/register", headers=AUTH,
                json={"token": "tok-a", "personas": "not-a-list"})
check("personas 非清單 → 400", r.status_code == 400)

r = client.post("/app/v1/push/register", headers=AUTH, json={"token": "tok-a"})
check("最簡註冊 200 + 預設偏好",
      r.status_code == 200 and r.json()["prefs"] == {"preview": True,
                                                     "personas": None})
check("register 回報 apns_configured", r.json()["apns_configured"] is False)

r = client.post("/app/v1/push/register", headers=AUTH,
                json={"token": "tok-b", "preview": False,
                      "personas": ["yuanfang"]})
check("帶偏好註冊 200",
      r.status_code == 200 and r.json()["prefs"] == {"preview": False,
                                                     "personas": ["yuanfang"]})

con = sqlite3.connect(bridge.CANON_DB)
toks = {row[0] for row in con.execute("SELECT token FROM devices")}
con.close()
check("devices 表落兩個 token", {"tok-a", "tok-b"} <= toks)
check("push_prefs.json 在 canonical 旁",
      os.path.dirname(bridge.PUSH_PREFS_PATH) == os.path.dirname(bridge.CANON_DB)
      and os.path.isfile(bridge.PUSH_PREFS_PATH))

# 冪等覆蓋
r = client.post("/app/v1/push/register", headers=AUTH,
                json={"token": "tok-b", "preview": True, "personas": None})
check("重打覆蓋偏好", r.json()["prefs"] == {"preview": True, "personas": None})
# 還原 tok-b 偏好供下面過濾測試
client.post("/app/v1/push/register", headers=AUTH,
            json={"token": "tok-b", "preview": False, "personas": ["yuanfang"]})

# ── 3. push_notify:persona 過濾 + preview 占位(假裝金鑰已配置)────────────
_key = os.path.join(_TMP, "fake.p8")
with open(_key, "w") as f:
    f.write("fake")
bridge.APNS_KEY_PATH = _key            # 只為讓 apns_configured() 過;送出走 mock
check("apns_configured() True(fake key)", bridge.apns_configured() is True)

# tok-a:預設(全訂閱、preview on);tok-b:只訂 yuanfang、preview off。
_sent_calls.clear()
res = asyncio.run(bridge.push_notify(
    "袁方", "今天的晨報內容", {"kind": "message"},
    persona="yuanfang", no_preview_body="傳了一則訊息"))
bodies = {c["token"]: c["body"] for c in _sent_calls}
check("兩台都收到 yuanfang 推播", res["sent"] == 2 and set(bodies) == {"tok-a", "tok-b"})
check("preview on → 原文", bodies.get("tok-a") == "今天的晨報內容")
check("preview off → 占位(內容不外送)", bodies.get("tok-b") == "傳了一則訊息")

_sent_calls.clear()
res = asyncio.run(bridge.push_notify(
    "水鏡", "卦象解讀", {"kind": "message"},
    persona="shuijing", no_preview_body="傳了一則訊息"))
check("未訂閱人格被過濾(tok-b skipped)",
      res["sent"] == 1 and res["skipped"] == 1
      and [c["token"] for c in _sent_calls] == ["tok-a"])

_sent_calls.clear()
res = asyncio.run(bridge.push_notify("✅ 任務完成", "build 52 出貨"))
check("非人格推播(任務完成)不受人格訂閱影響", res["sent"] == 2)

# ── 410 剪 token + 偏好一併清掉 ─────────────────────────────────────────────
async def _mock_send_410(token, title, body, data=None, category=None,
                         thread_id=None, content_available=False):
    if token == "tok-b":
        return 410, "Unregistered"
    return 200, ""

bridge._apns_send = _mock_send_410
res = asyncio.run(bridge.push_notify("t", "b"))
con = sqlite3.connect(bridge.CANON_DB)
toks = {row[0] for row in con.execute("SELECT token FROM devices")}
con.close()
check("410 → token 剪掉", "tok-b" not in toks and res["sent"] == 1)
check("410 → 偏好清掉", "tok-b" not in bridge._push_prefs_load())

# ── 4. 舊 /app/v1/devices 註冊(無偏好)→ 預設照推 ─────────────────────────
bridge._apns_send = _mock_send
r = client.post("/app/v1/devices", headers=AUTH, json={"token": "tok-legacy"})
check("舊端點註冊仍可用", r.status_code == 200)
_sent_calls.clear()
res = asyncio.run(bridge.push_notify(
    "袁方", "內容", {"kind": "message"},
    persona="yuanfang", no_preview_body="傳了一則訊息"))
legacy = [c for c in _sent_calls if c["token"] == "tok-legacy"]
check("無偏好紀錄 → 預設全訂閱+原文",
      len(legacy) == 1 and legacy[0]["body"] == "內容")

bridge._apns_send = _orig_send

print()
if fails:
    print(f"✗ {len(fails)} failed: {fails}")
    sys.exit(1)
print("✓ all passed")
