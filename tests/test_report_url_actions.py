"""feat/report-url-actions 驗收(repo 慣例:python3 tests/test_report_url_actions.py)。

報告快速行動鈕新增**連結型** `{label,url}`(點了開連結,app 走 StudioLinkRouter):
1. `_report_actions_normalize` 收連結型:url 只收 http/https + 有 host、
   ≤1000 字;壞 url **略過該顆**(不截斷 — 截了就斷鏈),不落回指令型。
2. 指令型 `{label,text,target_session}` 行為不回歸(截斷/略過/上限同 #178)。
3. 兩型混發:順序照發送端、共用上限 6 顆。
4. `POST /app/v1/persona-report` 帶連結型 → `GET /app/v1/reports/{id}`
   原樣帶回(reports 單筆端點不再動 actions,存什麼回什麼)。
"""
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="report-url-actions-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}
NOW = time.time()
norm = bridge._report_actions_normalize

# ── 1. 連結型收斂規則 ───────────────────────────────────────────────────
got = norm([{"label": "影片", "url": "https://drive.google.com/file/d/abc/view"}])
check("https url 收下,正典形兩鍵",
      got == [{"label": "影片", "url": "https://drive.google.com/file/d/abc/view"}])
got = norm([{"label": "內網", "url": "http://127.0.0.1:8081/x"}])
check("http url 也收", got and got[0]["url"] == "http://127.0.0.1:8081/x")
check("非 http(s) scheme 略過",
      norm([{"label": "壞", "url": "javascript:alert(1)"},
            {"label": "壞2", "url": "file:///etc/passwd"},
            {"label": "壞3", "url": "ftp://x/y"}]) == [])
check("裸路徑(無 scheme/host)略過 — FB 裸路徑 bug 不准進正典",
      norm([{"label": "FB", "url": "/61550/posts/123"}]) == [])
check("無 host 略過", norm([{"label": "空殼", "url": "https://"}]) == [])
check("空 url 略過", norm([{"label": "空", "url": ""},
                           {"label": "空2", "url": "   "}]) == [])
check("超長 url(>1000)略過不截斷",
      norm([{"label": "長", "url": "https://x.tw/" + "a" * 1000}]) == [])
got = norm([{"label": "  IG  ", "url": "  https://www.instagram.com/p/xx/  "}])
check("label/url 修剪空白", got == [{"label": "IG",
                                     "url": "https://www.instagram.com/p/xx/"}])
got = norm([{"label": "超" * 30, "url": "https://x.tw/a"}])
check("連結型 label 照樣截 20", got[0]["label"] == "超" * 20)
check("帶 url 鍵但 url 壞 → 不落回指令型",
      norm([{"label": "混", "url": "notaurl", "text": "有 text 也不收"}]) == [])

# ── 2. 指令型不回歸 ─────────────────────────────────────────────────────
got = norm([{"label": "修", "text": "去修 bug", "target_session": "claude_code:dev"}])
check("指令型三鍵照舊", got == [{"label": "修", "text": "去修 bug",
                                 "target_session": "claude_code:dev"}])
check("指令型缺 text 照舊略過", norm([{"label": "只有面"}]) == [])
long_text = "長" * 600
got = norm([{"label": "截", "text": long_text}])
check("指令型 text 照舊截 500", got[0]["text"] == "長" * 500)

# ── 3. 兩型混發:順序照舊、共用上限 6 顆 ────────────────────────────────
mixed = ([{"label": f"鏈{i}", "url": f"https://x.tw/{i}"} for i in range(4)]
         + [{"label": f"令{i}", "text": f"t{i}"} for i in range(4)])
got = norm(mixed)
check("混發共用上限 6 顆", len(got) == 6)
check("順序照發送端(4 鏈 + 前 2 令)",
      [a["label"] for a in got] == ["鏈0", "鏈1", "鏈2", "鏈3", "令0", "令1"])
check("兩型鍵形各自正典",
      all(set(a) == {"label", "url"} for a in got[:4])
      and all(set(a) == {"label", "text", "target_session"} for a in got[4:]))

# ── 4. POST → GET 原樣帶回(fed-console Today Pick 實形) ────────────────
ACTIONS = [
    {"label": "影片", "url": "https://drive.google.com/file/d/vid123/view"},
    {"label": "IG", "url": "https://www.instagram.com/stories/fliper.mag/1/"},
    {"label": "FB", "url": "https://www.facebook.com/61550/posts/123"},
    {"label": "Console", "url": "https://console.tsai.cash/#story"},
    {"label": "叫天晴回報", "text": "限動成效如何?", "target_session": ""},
    {"label": "壞顆", "url": "javascript:alert(1)"},   # 略過
]
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "pantianqing", "label": "新文章發佈", "name": "fed-story",
    "content": "# Today Pick\n\n![封面](https://cdn.flipermag.com/c.jpg)\n\n- 文章:x",
    "ts": NOW - 60, "external_source": "fed", "external_id": "urlact:story:1",
    "actions": ACTIONS})
check("POST 混型 actions 200", r.status_code == 200 and r.json().get("ok"))
rid = r.json()["id"]

r = client.get(f"/app/v1/reports/{rid}", headers=AUTH)
check("GET 單筆 200", r.status_code == 200)
acts = (r.json().get("report") or {}).get("actions")
check("壞顆被略過(6 進 5 出)", isinstance(acts, list) and len(acts) == 5)
check("連結型原樣帶回", acts[0] == {"label": "影片",
      "url": "https://drive.google.com/file/d/vid123/view"})
check("指令型混存原樣帶回", acts[4] == {"label": "叫天晴回報",
      "text": "限動成效如何?", "target_session": ""})

# 四形歸一(#174)含連結型不回歸
for form in (f"rep-{rid}", f"card-hp-rep-{rid}", "urlact:story:1"):
    rr = client.get(f"/app/v1/reports/{form}", headers=AUTH)
    check(f"四形歸一 {form[:16]}… 帶連結型",
          rr.status_code == 200 and rr.json()["report"]["actions"] == acts)

# 重發只帶連結型 → 整組替換(#178 更新語意含新型別)
r = client.post("/app/v1/persona-report", headers=AUTH, json={
    "session": "pantianqing", "label": "新文章發佈", "name": "fed-story",
    "content": "# Today Pick v2", "ts": NOW - 30,
    "external_source": "fed", "external_id": "urlact:story:1",
    "actions": [{"label": "Console", "url": "https://console.tsai.cash/#story"}]})
r = client.get("/app/v1/reports/urlact:story:1", headers=AUTH)
check("重發 → 整組替換成 1 顆連結型",
      [a["label"] for a in r.json()["report"]["actions"]] == ["Console"])

print()
if fails:
    print(f"FAILED: {len(fails)} → " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
