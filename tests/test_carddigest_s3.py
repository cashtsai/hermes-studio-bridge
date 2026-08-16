"""S3 PersonaDigest 行為驗證 — 契約 §5/§6 關鍵不變量。"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_carddigest_s3.py):測試邏輯直接寫在
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
        "python3 tests/test_carddigest_s3.py"
    )

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import carddigest as cd

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


d = cd.PersonaDigest()

# 1. seed:canonical messages(mid 穩定 → 卡 id;known 去重)
msgs = [
    {"id": "m1", "role": "user", "content": "早安", "ts": 100.0},
    {"id": "m2", "role": "assistant", "content": "早安!今天…", "ts": 101.0},
]
d.seed_messages(msgs)
snap = d.store.snapshot()
check("seed 兩卡(user text/assistant markdown)",
      [c["kind"] for c in snap["cards"]] == ["text", "markdown"]
      and [c["role"] for c in snap["cards"]] == ["user", "assistant"])
check("卡 id 由 mid 衍生", snap["cards"][0]["id"] == "card-hp-m1")
check("ts 保留 canonical 時間", snap["cards"][0]["ts"] == 100.0)
d.seed_messages(msgs)
check("重掃不重出(known_mids)", len(d.store.snapshot()["cards"]) == 2)

# 2. live turn:user 卡即時出 + begin/delta 累積/status 透傳/end 收尾
d.message_card({"id": "m3", "role": "user", "content": "跑報告",
                "attachments": [{"kind": "image", "filename": "x.jpg"}],
                "ts": 102.0})
check("user 卡帶附件註記",
      d.store.cards["card-hp-m3"]["body"]["attachments"][0]["filename"] == "x.jpg")
d.turn_begin("cid1")
check("begin 後 busy+預設 label", d.store.status["busy"] is True
      and d.store.status["label"] == "已送達 Hermes，等待回覆。")
d.turn_status("執行步驟 2:Bash")
check("status label 原樣透傳", d.store.status["label"] == "執行步驟 2:Bash")
d.turn_delta("cid1", "報告")
d.turn_delta("cid1", "生成中")
c = d.store.cards["card-hp-turn-cid1"]
check("delta 累積+rev 遞增+final=False",
      c["body"]["text"] == "報告生成中" and c["rev"] == 2 and c["final"] is False)
check("delta 後 label=回覆中", d.store.status["label"] == "回覆中")
d.turn_end("cid1", "報告生成中。完成!", reply_mid="m4")
c = d.store.cards["card-hp-turn-cid1"]
check("end 全文覆蓋+final", c["final"] is True and c["body"]["text"] == "報告生成中。完成!")
check("end 後待命", d.store.status["label"] == "待命" and d.store.status["busy"] is False)

# 3. reply mid 已註記 → follower 補掃不出第二張
before = len(d.store.snapshot(limit=100)["cards"])
d.seed_messages([{"id": "m4", "role": "assistant", "content": "報告生成中。完成!",
                  "ts": 103.0}])
check("reply 不重出(turn 卡已涵蓋)",
      len(d.store.snapshot(limit=100)["cards"]) == before)

# 4. turn error → 系統卡
d.turn_begin("cid2")
d.turn_end("cid2", "", error="ACPTimeout: boom")
errs = [c for c in d.store.cards.values()
        if c["body"].get("text", "").startswith("⚠️")]
check("error 出系統卡、無空 assistant 卡", len(errs) == 1
      and "card-hp-turn-cid2" not in d.store.cards)

# 5. turn 事件序:begin…end 成對
turns = [e["data"]["state"] for e in d.store.events if e["type"] == "turn"]
check("turn begin/end 成對", turns == ["begin", "end", "begin", "end"])

# 6. fallback 鐵律
check("所有卡都有 fallback_text",
      all(c["body"].get("fallback_text") for c in d.store.cards.values()))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
