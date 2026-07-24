"""feat/report-reader-api 驗收(repo 慣例:python3 tests/test_report_reader_api.py)。

Pocket 原生報告閱讀器的 bridge 資料面:
1. `_report_key_normalize`:id 原形 / external_id / `rep-<id>` / `card-hp-rep-<id>`
   四形一律歸一到 report_events.id / external_id。
2. `GET /app/v1/reports/{report_id}`:四形都能取回同一筆全文;不存在 → 404
   REPORT_NOT_FOUND;無 auth → 401。
3. `GET /app/v1/reports?session=&limit=`:newest-first、preview 200 字截斷、
   chars 是全文長度、limit 夾擠(1..100);非人格 session → 400;無 auth → 401。
4. 唯讀鐵律:兩條端點打完,report_events 列數/內容不變(smoke:除了本測試
   自己 upsert 的種子,不產生任何寫入)。
"""
import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="report-reader-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}

# ── 種子:三筆報告(兩筆 yuanfang cron、一筆 pantianqing fed)──────────────
NOW = time.time()
LONG = "# 晨報\n\n" + "第二段內容,夠長才能驗證 preview 截斷。" * 30  # >200 chars
rid_a = bridge._report_upsert("yuanfang", {
    "label": "晨報", "name": "morning", "content": LONG,
    "ts": NOW - 600, "external_source": "hermes-cron",
    "external_id": "cron:yuanfang:morning:sid-1"})
rid_b = bridge._report_upsert("yuanfang", {
    "label": "午報", "name": "noon", "content": "短內容,不足兩百字。",
    "ts": NOW - 60, "external_source": "hermes-cron",
    "external_id": "cron:yuanfang:noon:sid-2"})
rid_c = bridge._report_upsert("pantianqing", {
    "label": "今日精選", "name": "fed-today", "content": "fed 內容",
    "ts": NOW - 30, "external_source": "fed",
    "external_id": "fed:today:2026-07-24"})
check("種子 upsert 有 id", bool(rid_a) and bool(rid_b) and bool(rid_c))


def _rows_snapshot():
    con = sqlite3.connect(bridge.CANON_DB)
    rows = con.execute(
        "SELECT id,session,label,name,content,ts,external_source,external_id "
        "FROM report_events ORDER BY id").fetchall()
    con.close()
    return rows


_BEFORE = _rows_snapshot()

# ── 1. _report_key_normalize 四形歸一 ────────────────────────────────────
check("normalize 原形", bridge._report_key_normalize(rid_a) == rid_a)
check("normalize rep- 前綴", bridge._report_key_normalize(f"rep-{rid_a}") == rid_a)
check("normalize card-hp-rep- 前綴",
      bridge._report_key_normalize(f"card-hp-rep-{rid_a}") == rid_a)
check("normalize 空字串", bridge._report_key_normalize("") == "")
check("normalize 空白修剪", bridge._report_key_normalize(f"  {rid_a} ") == rid_a)

# ── 2. 單筆端點:四形取回同一筆 ─────────────────────────────────────────
forms = [rid_a, f"rep-{rid_a}", f"card-hp-rep-{rid_a}",
         "cron:yuanfang:morning:sid-1"]
bodies = []
for f in forms:
    r = client.get(f"/app/v1/reports/{f}", headers=AUTH)
    bodies.append(r.json().get("report") if r.status_code == 200 else None)
check("四形皆 200 且同一筆",
      all(b and b["id"] == rid_a for b in bodies))
rep = bodies[0]
check("單筆帶全文", rep["content"] == LONG)
check("單筆帶標題/時間/來源",
      rep["label"] == "晨報" and rep["name"] == "morning"
      and abs(rep["ts"] - (NOW - 600)) < 1
      and rep["external_source"] == "hermes-cron"
      and rep["session"] == "yuanfang")

r404 = client.get("/app/v1/reports/rep-no-such-id", headers=AUTH)
check("不存在 → 404 REPORT_NOT_FOUND",
      r404.status_code == 404 and "REPORT_NOT_FOUND" in r404.text)
check("單筆無 auth → 401",
      client.get(f"/app/v1/reports/{rid_a}").status_code == 401)

# ── 3. 列表端點 ─────────────────────────────────────────────────────────
rl = client.get("/app/v1/reports", params={"session": "yuanfang"}, headers=AUTH)
check("列表 200", rl.status_code == 200)
reports = rl.json().get("reports", [])
check("列表只含該人格、newest-first",
      [x["id"] for x in reports] == [rid_b, rid_a])
la = next(x for x in reports if x["id"] == rid_a)
check("preview 截斷 + chars 全文長度",
      len(la["preview"]) < len(LONG) and "截斷" in la["preview"]
      and la["chars"] == len(LONG))
check("列表不揹全文", all("content" not in x for x in reports))
rl1 = client.get("/app/v1/reports", params={"session": "yuanfang", "limit": 1},
                 headers=AUTH)
check("limit=1 只回最新一筆",
      [x["id"] for x in rl1.json()["reports"]] == [rid_b])
rl0 = client.get("/app/v1/reports", params={"session": "yuanfang", "limit": 0},
                 headers=AUTH)
check("limit=0 退回預設(回全部兩筆)", len(rl0.json()["reports"]) == 2)
rbad = client.get("/app/v1/reports", params={"session": "nobody"}, headers=AUTH)
check("非人格 session → 400", rbad.status_code == 400)
check("列表無 auth → 401",
      client.get("/app/v1/reports", params={"session": "yuanfang"}).status_code == 401)

# ── 4. 唯讀鐵律:讀端點不產生任何寫入 ───────────────────────────────────
check("report_events 不變(唯讀)", _rows_snapshot() == _BEFORE)

print()
if fails:
    print(f"❌ {len(fails)} failed: {fails}")
    sys.exit(1)
print("✅ all passed")
