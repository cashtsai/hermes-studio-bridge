"""TG tool error → Pocket 錯誤報告同步驗收。

驗證:
1. role=tool 的 error-like 訊息會被轉成 report_events 錯誤報告。
2. 同一筆 tool error 重複同步不新增 report / notice。
3. status=success + exit_code=0 的正常 tool result 不進錯誤報告。

feat/hide-internal-reports 後,工具錯誤報告預設不進 app(隱藏閘),本檔驗的
是 POCKET_ENABLE_TOOL_ERROR_REPORTS=1 逃生門下的完整舊行為;預設(flag off)
的隱藏行為由 tests/test_hidden_reports.py 覆蓋。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tg-tool-error-report-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_ENABLE_TOOL_ERROR_REPORTS"] = "1"  # 逃生門全開驗舊契約
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


home = os.path.join(_TMP, "fliper-home")
os.makedirs(home, exist_ok=True)
os.makedirs(os.path.join(home, "cron"), exist_ok=True)
with open(os.path.join(home, "cron", "jobs.json"), "w", encoding="utf-8") as f:
    json.dump({"jobs": []}, f)
state_db = os.path.join(home, "state.db")
now = time.time()

con = sqlite3.connect(state_db)
con.executescript(
    """
    CREATE TABLE sessions(
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL
    );
    CREATE TABLE messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        tool_name TEXT,
        timestamp REAL NOT NULL
    );
    """
)
con.execute("INSERT INTO sessions(id,source) VALUES('tg-session','telegram')")
con.execute("INSERT INTO sessions(id,source) VALUES('cron-session','cron')")
con.execute(
    "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
    ("tg-session", "user", "今天的稿務為什麼失敗", now - 20),
)
con.execute(
    "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
    ("cron-session", "user",
     "[IMPORTANT: You are running as a scheduled cron job. Produce the morning brief.]",
     now - 18),
)
con.execute(
    "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
    ("tg-session", "tool",
     json.dumps({"status": "success", "output": "ok", "exit_code": 0, "error": None},
                ensure_ascii=False),
     "terminal", now - 15),
)
error_payload = {
    "status": "error",
    "output": "Traceback (most recent call last): HTTP Error 403: Forbidden",
    "exit_code": 1,
    "error": "HTTP Error 403: Forbidden",
}
con.execute(
    "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
    ("tg-session", "tool", json.dumps(error_payload, ensure_ascii=False),
     "fliper_wordpress_pending.py", now - 10),
)
cron_error_payload = {
    "status": "failed",
    "stderr": "remote did not return json",
    "exit_code": 2,
}
con.execute(
    "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
    ("cron-session", "tool", json.dumps(cron_error_payload, ensure_ascii=False),
     "story_publish.py", now - 8),
)
con.commit()
con.close()

bridge.PERSONAS["pantianqing"] = ("潘天晴", home)

reports = bridge._sync_persona_reports("pantianqing", 20)
tool_reports = [r for r in reports
                if r.get("external_source") == bridge.TOOL_ERROR_REPORT_SOURCE]
check("同步產生 TG 與 cron tool 錯誤報告", len(tool_reports) == 2)
rep = next(r for r in tool_reports if ":tg-session:" in r.get("external_id", ""))
cron_rep = next(r for r in tool_reports if ":cron-session:" in r.get("external_id", ""))
check("label/name 正確",
      rep["label"] == "錯誤報告" and rep["name"] == bridge.TOOL_ERROR_REPORT_NAME)
check("內容含摘要與前一則使用者訊息",
      "HTTP Error 403" in rep["content"] and "今天的稿務為什麼失敗" in rep["content"])
check("cron 錯誤也進報告且排除排程系統 prompt",
      "remote did not return json" in cron_rep["content"]
      and "scheduled cron job" not in cron_rep["content"])
check("內容含原始工具輸出 details",
      "<details><summary>原始工具輸出</summary>" in rep["content"]
      and "fliper_wordpress_pending.py" in rep["content"])

db = sqlite3.connect(bridge.CANON_DB)
count1 = db.execute(
    "SELECT COUNT(*) FROM report_events WHERE external_source=?",
    (bridge.TOOL_ERROR_REPORT_SOURCE,),
).fetchone()[0]
notice1 = db.execute(
    "SELECT COUNT(*) FROM approvals WHERE provider='hermes' AND kind='notice'"
).fetchone()[0]
db.close()
check("report_events 寫入兩筆", count1 == 2)
check("錯誤報告建立通知中心 notice", notice1 == 2)

bridge._sync_persona_reports("pantianqing", 20)
db = sqlite3.connect(bridge.CANON_DB)
count2 = db.execute(
    "SELECT COUNT(*) FROM report_events WHERE external_source=?",
    (bridge.TOOL_ERROR_REPORT_SOURCE,),
).fetchone()[0]
notice2 = db.execute(
    "SELECT COUNT(*) FROM approvals WHERE provider='hermes' AND kind='notice'"
).fetchone()[0]
db.close()
check("重複同步不新增 report", count2 == count1)
check("重複同步不新增 notice", notice2 == notice1)

print()
if fails:
    print(f"❌ {len(fails)} failed: {fails}")
    sys.exit(1)
print("✅ all passed")
