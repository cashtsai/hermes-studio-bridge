"""B3 studio-card 圍欄協定驗證。"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_carddigest_b3.py):測試邏輯直接寫在
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
        "python3 tests/test_carddigest_b3.py"
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


TABLE = ('```studio-card\n{"kind": "table", "title": "持倉", '
         '"columns": ["代號", "股數"], "rows": [["2330", 100], ["VT", 50]]}\n```')
KV = ('```studio-card\n{"kind": "kv", "title": "今日", '
      '"items": {"天氣": "晴", "行程": "3 件"}, "fallback_text": "今日摘要"}\n```')

# 1. 抽取:兩個圍欄 → 兩個 body,原文換 fallback
text = f"早安!\n\n{TABLE}\n\n盤後再看。\n{KV}\n"
clean, bodies = cd.extract_studio_cards(text)
check("抽出兩張", len(bodies) == 2)
check("kind 正確", [b["kind"] for b in bodies] == ["table", "kv"])
check("自帶 fallback 保留", bodies[1]["fallback_text"] == "今日摘要")
check("缺 fallback 代生(title+表頭+列)",
      bodies[0]["fallback_text"] == "持倉\n代號 | 股數\n2330 | 100\nVT | 50")
check("原文圍欄消失、換成 fallback",
      "```" not in clean and "持倉\n代號 | 股數" in clean and "今日摘要" in clean
      and "早安!" in clean and "盤後再看。" in clean)

# 2. 壞 JSON / 不認得 kind → 原文保留、不吞內容
bad = '```studio-card\n{oops}\n```'
clean2, b2 = cd.extract_studio_cards(bad)
check("壞 JSON 原文保留", clean2 == bad and b2 == [])
weird = '```studio-card\n{"kind": "hologram", "text": "x"}\n```'
clean3, b3 = cd.extract_studio_cards(weird)
check("不認得 kind 原文保留", clean3 == weird and b3 == [])
plain = "沒有圍欄的普通回覆"
check("無圍欄零成本原樣回", cd.extract_studio_cards(plain) == (plain, []))

# 3. PersonaDigest 整合:canonical assistant 訊息 → 母卡(乾淨文字)+結構化卡
d = cd.PersonaDigest()
d.seed_messages([{"id": "m1", "role": "assistant",
                  "content": f"晨報來了\n{TABLE}", "ts": 100.0}])
snap = d.store.snapshot()
check("母卡+table 卡", [c["kind"] for c in snap["cards"]] == ["markdown", "table"])
check("母卡文字已清理", "```" not in snap["cards"][0]["body"]["text"])
check("table 卡 id 錨在母卡", snap["cards"][1]["id"] == "card-hp-m1-sc0")
check("table 卡 body 有 rows", snap["cards"][1]["body"]["rows"] == [["2330", 100], ["VT", 50]])
check("table 卡 body 不再帶 kind 鍵", "kind" not in snap["cards"][1]["body"])

# 4. user 訊息不抽(僅 assistant)
d.seed_messages([{"id": "m2", "role": "user", "content": TABLE, "ts": 101.0}])
check("user 圍欄原樣", "```studio-card" in d.store.cards["card-hp-m2"]["body"]["text"])

# 5. live turn 收尾:同樣抽取
d.turn_begin("c1")
d.turn_delta("c1", "算一下…")
d.turn_end("c1", f"結果:\n{KV}", reply_mid="m3")
main = d.store.cards["card-hp-turn-c1"]
sc = d.store.cards.get("card-hp-turn-c1-sc0")
check("turn 收尾母卡清理+結構化卡",
      "```" not in main["body"]["text"] and sc is not None and sc["kind"] == "kv")

# 6. fallback 鐵律
check("所有卡都有 fallback_text",
      all(c["body"].get("fallback_text") for c in d.store.cards.values()))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
