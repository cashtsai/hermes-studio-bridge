"""S1 CC jsonl digest 行為驗證 — #15 稽核補洞:cc_event_to_cards / cc_status_label
一直只有間接覆蓋(S2/S3/B3 走 codex/persona 路徑),本檔直測契約 §1/§2 的 CC 面。"""
import sys
import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import carddigest as cd

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 1. user 純文字 → 1 張 text 卡;id 由 uid 衍生(重放同 id)
ev = {"type": "user", "timestamp": "2026-07-26T10:00:00Z",
      "message": {"content": "跑個測試"}}
cards = cd.cc_event_to_cards(ev, uid="u1", turn_id="t1")
check("user 文字 → 1 張 text 卡", len(cards) == 1 and cards[0]["kind"] == "text")
check("卡 id 由 uid 衍生(replay 穩定)", cards[0]["id"] == "card-cc-u1-0")
check("CC 卡一律 rev=1/final(整行事件無部分修訂)",
      cards[0]["rev"] == 1 and cards[0]["final"] is True)
from datetime import datetime, timezone
_want = datetime(2026, 7, 26, 10, tzinfo=timezone.utc).timestamp()
check("timestamp ISO8601 → epoch", cards[0]["ts"] == _want)

# 2. 管線訊息(harness/系統)不出卡
for tag in cd.PLUMBING_TAGS:
    ev = {"type": "user", "message": {"content": f"{tag} 內部管線"}}
    if cd.cc_event_to_cards(ev, uid="p1"):
        check(f"管線訊息不出卡:{tag}", False)
        break
else:
    check("管線訊息不出卡(PLUMBING_TAGS 全數)", True)

# 3. assistant 三種 block → markdown / 💭text / tool_call
ev = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "都綠了"},
    {"type": "thinking", "thinking": "想一下"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q\nxx"}},
]}}
cards = cd.cc_event_to_cards(ev, uid="a1", turn_id="t1")
check("assistant 3 blocks → 3 卡", [c["kind"] for c in cards] ==
      ["markdown", "text", "tool_call"])
check("thinking 卡帶 💭 前綴", cards[1]["body"]["text"].startswith("💭"))
check("tool_call summary 取首行", cards[2]["body"]["summary"] == "pytest -q")
check("所有卡都有 fallback_text",
      all(c["body"].get("fallback_text") for c in cards))

# 4. Edit tool_use → tool_call.patch(契約 §2;#38 深路徑不截斷)
deep = "/very/deep/nested/path/" + "x" * 200 + "/file.py"
ev = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Edit",
     "input": {"file_path": deep, "old_string": "a=1", "new_string": "a=2"}},
]}}
cards = cd.cc_event_to_cards(ev, uid="e1")
patch = cards[0]["body"].get("patch")
check("Edit → patch 附卡", bool(patch) and patch.get("path") == deep)
check("patch 統計 +1/-1", patch.get("adds") == 1 and patch.get("dels") == 1)
check("summary 深路徑不被 140 攔腰砍(#38)",
      cards[0]["body"]["summary"] == deep)

# 5. tool_result → tool_result 卡 + 截斷
long_out = "y" * (cd._TOOL_RESULT_MAX + 500)
ev = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": long_out},
]}}
cards = cd.cc_event_to_cards(ev, uid="r1")
check("tool_result → 1 張 tool_result 卡",
      len(cards) == 1 and cards[0]["kind"] == "tool_result")
check("tool_result 超長截斷", cards[0]["body"]["text"].endswith("…(截斷)"))

# 6. 未知事件型別 → 0 卡(向前相容)
check("未知型別不出卡", cd.cc_event_to_cards({"type": "summary"}, uid="z") == [])

# 7. cc_status_label 人話全表(契約 §1 session.status)
check("等待核准 優先於一切", cd.cc_status_label(True, {"q": "?"}, "Bash") == "等待核准")
check("待命", cd.cc_status_label(False, None) == "待命")
check("執行工具:Bash", cd.cc_status_label(True, None, "Bash") == "執行工具:Bash")
check("思考中(無輸出)", cd.cc_status_label(True, None, "", False) == "思考中")
check("回覆中(已見文字)", cd.cc_status_label(True, None, "", True) == "回覆中")

print()
if fails:
    print("FAILED:", len(fails))
    sys.exit(1)
print("ALL PASS")
