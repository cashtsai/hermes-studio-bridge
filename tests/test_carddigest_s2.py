"""S2 CodexThreadDigest 行為驗證 — 契約 §1/§2/§3 關鍵不變量。"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_carddigest_s2.py):測試邏輯直接寫在
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
        "python3 tests/test_carddigest_s2.py"
    )

import sys
import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import carddigest as cd

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


d = cd.CodexThreadDigest()

# 1. seed:turns/list 舊→新
turns = [
    {"id": "t1", "items": [
        {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "跑個測試"}]},
        {"id": "r1", "type": "reasoning", "summary": ["想一下"]},
        {"id": "c1", "type": "commandExecution", "command": "pytest -q\nxx", "status": "completed",
         "aggregatedOutput": "3 passed"},
        {"id": "a1", "type": "agentMessage", "text": "都綠了"},
    ]},
]
d.seed_turns(turns)
snap = d.store.snapshot()
kinds = [c["kind"] for c in snap["cards"]]
check("seed 產出 5 卡(user/思考/tool_call/tool_result/markdown)",
      kinds == ["text", "text", "tool_call", "tool_result", "markdown"])
check("seed 卡皆 final", all(c["final"] for c in snap["cards"]))
check("tool_call summary 取首行", snap["cards"][2]["body"]["summary"] == "pytest -q")
check("tool_result 附 fallback", snap["cards"][3]["body"]["fallback_text"].startswith("↳ 結果"))
check("卡 id 由 item id 衍生", snap["cards"][0]["id"] == "card-cx-u1")

# 2. seed 重放同批 item → 同 id、rev 遞增、卡數不變
d.seed_turns(turns)
snap2 = d.store.snapshot()
check("重放 seed 卡數不變(dedup by id)", len(snap2["cards"]) == 5)
check("重放 seed rev 遞增", snap2["cards"][0]["rev"] == 2)
seq_before_catchup = d.store.seq
d.seed_turns(turns, emit_unchanged=False)
check("catch-up 同批 turns 不推新事件", d.store.seq == seq_before_catchup)
d.seed_turns([{"id": "t1b", "items": [
    {"id": "u1b", "type": "userMessage", "content": [{"type": "text", "text": "補新進度"}]},
]}], emit_unchanged=False)
check("catch-up 會補新 turn", "card-cx-u1b" in d.store.cards)

# 3. 真串流:delta 累積、rev 遞增、final 收尾
d.handle("turn/started", {"threadId": "T", "turn": {"id": "t2"}})
check("turn begin 事件(後跟 status)", d.store.events[-2]["type"] == "turn"
      and d.store.events[-2]["data"]["state"] == "begin"
      and d.store.events[-1]["type"] == "session.status")
check("status busy=思考中", d.store.status["label"] == "思考中")
d.handle("item/started", {"threadId": "T", "item": {"id": "c2", "type": "commandExecution",
                                                    "command": "ls"}})
check("started 卡 final=False", d.store.cards["card-cx-c2"]["final"] is False)
check("status 執行工具:command", d.store.status["label"] == "執行工具:command")
d.handle("item/agentMessage/delta", {"threadId": "T", "itemId": "a2", "delta": "你好"})
d.handle("item/agentMessage/delta", {"threadId": "T", "itemId": "a2", "delta": "世界"})
card = d.store.cards["card-cx-a2"]
check("delta 累積", card["body"]["text"] == "你好世界")
check("delta rev 遞增", card["rev"] == 2)
check("delta final=False", card["final"] is False)
check("status 回覆中", d.store.status["label"] == "回覆中")
d.handle("item/completed", {"threadId": "T", "item": {"id": "a2", "type": "agentMessage",
                                                      "text": "你好世界!"}})
card = d.store.cards["card-cx-a2"]
check("completed 全文覆蓋+final", card["final"] is True and card["body"]["text"] == "你好世界!")
check("delta 暫存清掉", "a2" not in d.agent_text)

# 4. approval 卡:pending → 等待核准;resolve → options 清空
rec = {"id": "codex-abc", "title": "Codex command approval", "detail": "command:\nrm x",
       "thread_id": "T"}
d.handle_approval(rec)
check("approval 卡 pending", d.store.cards["card-cx-appr-codex-abc"]["body"]["options"] != [])
check("status 等待核准", d.store.status["label"] == "等待核准")
d.resolve_approval(rec, "approved")
ap = d.store.cards["card-cx-appr-codex-abc"]
check("approval 卡收尾", ap["body"]["options"] == [] and ap["body"]["resolved"] == "approved"
      and ap["final"] is True)
d.handle("turn/completed", {"threadId": "T", "turn": {"id": "t2"}})
check("turn end + 待命", d.store.status["label"] == "待命")

# 5. turn error → 系統卡
d.handle("turn/started", {"threadId": "T", "turn": {"id": "t3"}})
d.handle("turn/completed", {"threadId": "T", "turn": {"id": "t3",
                                                      "error": {"message": "boom"}}})
errs = [c for c in d.store.cards.values() if c["body"].get("text", "").startswith("⚠️")]
check("turn error 出系統卡", len(errs) == 1 and errs[0]["role"] == "system")

# 6. since()/補洞與 410 邊界(共用 SessionCardStore,抽查)
evs = d.store.since(0)
check("since(0) 全量重放", evs is not None and evs[0]["seq"] == 1)
check("since(seq) 空增量", d.store.since(d.store.seq) == [])
check("since(超前) → None(410)", d.store.since(d.store.seq + 5) is None)

# 7. fallback 鐵律:每張卡都有 fallback_text
check("所有卡都有 fallback_text",
      all(c["body"].get("fallback_text") for c in d.store.cards.values()))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
