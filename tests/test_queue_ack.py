"""排隊回執不落正典(fix/queue-ack-not-canonical)判定測試。"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_queue_ack.py):測試邏輯直接寫在
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
        "python3 tests/test_queue_ack.py"
    )

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)
check("純回執", bridge._is_queue_ack("Queued for the next turn."))
check("帶件數", bridge._is_queue_ack("Queued for the next turn. (3 queued)"))
check("前後空白", bridge._is_queue_ack("  Queued for the next turn. (1 queued)\n"))
check("嵌在長文中不動", not bridge._is_queue_ack("好的。Queued for the next turn. 我先排隊"))
check("一般回覆不動", not bridge._is_queue_ack("善彰,晨報整理好了"))
check("空字串不動", not bridge._is_queue_ack(""))
print(); sys.exit(1 if fails else 0)
