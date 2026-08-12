"""TG tool error → Pocket 錯誤報告同步驗收。

驗證:
1. role=tool 的 error-like 訊息會被轉成 report_events 錯誤報告。
2. 同一筆 tool error 重複同步不新增 report。
3. status=success + exit_code=0 的正常 tool result 就算內文含 error/failed
   字樣也不進錯誤報告。

feat/diagnostic-report-center 後,工具錯誤報告進報告中心,但不進人格聊天,
也不再產生「知道了」notice。
"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_tg_tool_error_reports.py):測試邏輯直接寫在
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
        "python3 tests/test_tg_tool_error_reports.py"
    )

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
con.execute(
    "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
    ("tg-session", "tool",
     json.dumps({"status": "success",
                 "output": "source file mentions error handlers and failed states",
                 "exit_code": 0, "error": None},
                ensure_ascii=False),
     "terminal", now - 14),
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
check("錯誤報告不建立通知中心 notice", notice1 == 0)

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
check("重複同步仍不新增 notice", notice2 == notice1)

print()
if fails:
    print(f"❌ {len(fails)} failed: {fails}")
    sys.exit(1)
print("✅ all passed")
