"""feat/report-actions-api 驗收(repo 慣例:python3 tests/test_report_actions.py)。

報告快速行動鈕的 bridge 資料面:
1. `POST /app/v1/persona-report` 增收選填 `actions:[{label,text,target_session}]`
   — 上限 6 顆、label ≤20 字、text ≤500 字(超限**截斷不擋件**)、壞元素略過。
2. `GET /app/v1/reports/{id}` 回應帶 `actions`(正典形)。
3. 舊列相容:無 actions 的報告(cron 線 / 舊 bridge 寫入的 NULL 欄)→
   `actions: []`,不炸不缺欄。
4. 更新語意:同一報告重發帶新 actions → 整組替換;重發不帶 → 清空。
5. #174 報告閱讀器讀取面不回歸(單筆四形歸一照舊、列表端點照舊)。
"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_report_actions.py):測試邏輯直接寫在
# 模組層、用 sys.exit() 回報結果。被 `unittest discover` 匯入時,那些程式碼會在
# import 期間執行 —— 一來 SystemExit 會被 loader 記成 `_FailedTest` ERROR(就算
# 腳本自己是全過的也一樣紅),二來它在模組層設的 os.environ / monkeypatch /
# bridge 全域會照順序潑到同一批的其他測試上(bridge 早就被別人 import 過,
# `os.environ.setdefault` 這時已經不算數)。
#
# 正式行為不動,只在測試側宣告「這支要自己的行程」:被匯入就明確 skip,
# 直接執行照舊完整跑。
if __name__ != "__main__":
    import unittest as _unittest

    raise _unittest.SkipTest(
        "腳本式驗收,module 層即執行 + sys.exit,需獨立行程:"
        "python3 tests/test_report_actions.py"
    )

import json
import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="report-actions-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表+actions 欄遷移)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}
NOW = time.time()

# ── 0. 遷移:report_events 有 actions 欄(舊庫 ALTER 補欄) ──────────────
con = sqlite3.connect(bridge.CANON_DB)
cols = {r[1] for r in con.execute("PRAGMA table_info(report_events)")}
con.close()
check("schema 有 actions 欄", "actions" in cols)

# ── 1. _report_actions_normalize 收斂規則 ───────────────────────────────
norm = bridge._report_actions_normalize
check("非 list → 空", norm("not-a-list") == [] and norm(None) == []
      and norm({"label": "x"}) == [])
check("元素非 dict → 略過", norm(["str", 42, None]) == [])
check("缺 label/text → 略過",
      norm([{"label": "只有面"}, {"text": "只有字"}]) == [])
long_label = "超" * 30
long_text = "長" * 600
got = norm([{"label": long_label, "text": long_text}])
check("label 截 20", got[0]["label"] == "超" * 20)
check("text 截 500", got[0]["text"] == "長" * 500)
check("target_session 預設空字串", got[0]["target_session"] == "")
got = norm([{"label": f"a{i}", "text": f"t{i}"} for i in range(9)])
check("上限 6 顆", len(got) == 6 and got[-1]["label"] == "a5")
got = norm([{"label": "  修   ", "text": "  去改 bug  ",
             "target_session": " claude_code:dev "}])
check("字段修剪空白", got == [{"label": "修", "text": "去改 bug",
                               "target_session": "claude_code:dev"}])

# ── 2. POST persona-report 帶 actions → GET 單筆帶回正典形 ─────────────
ACTIONS = [
    {"label": "叫水鏡再算一卦", "text": "再起一卦,問今天的財運",
     "target_session": ""},
    {"label": "交辦 CC 修這個", "text": "去修晨報裡提到的那個 bug",
     "target_session": "claude_code:dev-main"},
    {"label": long_label, "text": long_text},          # 截斷後仍收
    {"label": "壞顆:沒有 text"},                       # 略過
]
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "yuanfang", "label": "晨報(帶行動)", "name": "morning-act",
    "content": "# 晨報\n\n今天有三件事。", "ts": NOW - 300,
    "external_source": "test-actions", "external_id": "act:morning:1",
    "actions": ACTIONS})
check("POST 帶 actions 200", r.status_code == 200 and r.json().get("ok"))
rid = r.json()["id"]
check("POST 回 id", bool(rid))

r = client.get(f"/app/v1/reports/{rid}", headers=AUTH)
check("GET 單筆 200", r.status_code == 200)
rep = r.json().get("report") or {}
acts = rep.get("actions")
check("GET 帶 actions 欄", isinstance(acts, list))
check("壞顆被略過(4 進 3 出)", len(acts) == 3)
check("正典形三鍵齊", all(set(a) == {"label", "text", "target_session"}
                          for a in acts))
check("label/text 原樣保留", acts[0]["label"] == "叫水鏡再算一卦"
      and acts[1]["target_session"] == "claude_code:dev-main")
check("超限顆已截斷", acts[2]["label"] == "超" * 20
      and acts[2]["text"] == "長" * 500)

# 四形歸一不回歸:rep-/card-hp-rep-/external_id 取到同一筆(含 actions)
for form in (f"rep-{rid}", f"card-hp-rep-{rid}", "act:morning:1"):
    rr = client.get(f"/app/v1/reports/{form}", headers=AUTH)
    check(f"四形歸一 {form[:16]}… 帶 actions",
          rr.status_code == 200
          and rr.json()["report"]["actions"] == acts)

# ── 3. 舊列相容:無 actions 報告 → actions == [] ────────────────────────
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "yuanfang", "label": "無行動報告", "name": "plain",
    "content": "純內容", "ts": NOW - 200, "external_id": "act:plain:1"})
rid_plain = r.json()["id"]
r = client.get(f"/app/v1/reports/{rid_plain}", headers=AUTH)
check("無 actions 報告 → []", r.json()["report"]["actions"] == [])

# 直接塞一列模擬「actions 欄遷移前寫入的舊列」(欄值 NULL)
con = sqlite3.connect(bridge.CANON_DB)
con.execute(
    "INSERT INTO report_events(id,session,label,name,content,ts,"
    "external_source,external_id,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
    ("legacyrow0001", "yuanfang", "舊列", "legacy", "遷移前的舊報告",
     NOW - 100, "hermes-cron", "act:legacy:1", NOW))
con.commit()
con.close()
r = client.get("/app/v1/reports/legacyrow0001", headers=AUTH)
check("遷移前舊列(NULL 欄)→ []",
      r.status_code == 200 and r.json()["report"]["actions"] == [])

# actions 送非 list(舊發送端手滑)→ 當空,不 500
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "yuanfang", "label": "手滑", "name": "oops",
    "content": "actions 給了字串", "ts": NOW - 90,
    "external_id": "act:oops:1", "actions": "not-a-list"})
check("actions 非 list 不擋件", r.status_code == 200)
r = client.get("/app/v1/reports/act:oops:1", headers=AUTH)
check("actions 非 list → []", r.json()["report"]["actions"] == [])

# ── 4. 更新語意:同 external_id 重發 → actions 整組替換/清空 ────────────
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "yuanfang", "label": "晨報(帶行動)", "name": "morning-act",
    "content": "# 晨報 v2\n\n改稿。", "ts": NOW - 60,
    "external_source": "test-actions", "external_id": "act:morning:1",
    "actions": [{"label": "只剩一顆", "text": "回報進度"}]})
check("重發 200", r.status_code == 200)
r = client.get("/app/v1/reports/act:morning:1", headers=AUTH)
rep2 = r.json()["report"]
check("重發 → actions 整組替換",
      [a["label"] for a in rep2["actions"]] == ["只剩一顆"])
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "yuanfang", "label": "晨報(帶行動)", "name": "morning-act",
    "content": "# 晨報 v3\n\n再改。", "ts": NOW - 30,
    "external_source": "test-actions", "external_id": "act:morning:1"})
r = client.get("/app/v1/reports/act:morning:1", headers=AUTH)
check("重發不帶 actions → 清空", r.json()["report"]["actions"] == [])

# ── 5. 冪等短路仍含 actions:同 payload(含 actions)重發 → upsert 短路 ──
payload = {"session": "yuanfang", "label": "冪等", "name": "idem",
           "content": "同包重發", "ts": NOW - 10, "external_id": "act:idem:1",
           "actions": [{"label": "A", "text": "a"}]}
client.post("/app/v1/persona-report", headers=AUTH, json=payload)
r = client.post("/app/v1/persona-report", headers=AUTH, json=payload)
check("同包重發 → 短路(id 空)", r.status_code == 200 and r.json()["id"] == "")
r = client.get("/app/v1/reports/act:idem:1", headers=AUTH)
check("短路後 actions 仍在",
      [a["label"] for a in r.json()["report"]["actions"]] == ["A"])

# ── 6. 列表端點不回歸(不揹 actions,欄位形狀照舊) ─────────────────────
r = client.get("/app/v1/reports", headers=AUTH,
               params={"session": "yuanfang", "limit": 10})
check("列表 200", r.status_code == 200)
items = r.json()["reports"]
check("列表照舊無 actions 欄(全文/行動走單筆端點)",
      items and all("actions" not in it for it in items))

# ── 7. 無 auth → 401 ────────────────────────────────────────────────────
check("POST 無 auth 401",
      client.post("/app/v1/persona-report", json=payload).status_code == 401)
check("GET 無 auth 401",
      client.get("/app/v1/reports/act:idem:1").status_code == 401)

print()
if fails:
    print(f"FAILED: {len(fails)} → " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
