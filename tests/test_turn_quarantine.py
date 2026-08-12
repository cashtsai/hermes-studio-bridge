"""活 turn 檢疫(fix/active-turn-quarantine)判定測試。"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_turn_quarantine.py):測試邏輯直接寫在
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
        "python3 tests/test_turn_quarantine.py"
    )

import os, sys, types, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

class FakeTask:
    def __init__(self, running): self._r = running
    def done(self): return not self._r

bridge._APP_TURN_INFLIGHT.clear()
check("無 turn → False", not bridge._session_turn_in_flight("xcash"))
bridge._APP_TURN_INFLIGHT[("xcash", "cid1")] = {"task": FakeTask(running=True), "state": {}}
check("有活 turn → True", bridge._session_turn_in_flight("xcash"))
check("別的 session 不受影響", not bridge._session_turn_in_flight("yuanfang"))
bridge._APP_TURN_INFLIGHT[("xcash", "cid1")] = {"task": FakeTask(running=False), "state": {}}
check("turn 收尾 → False", not bridge._session_turn_in_flight("xcash"))
bridge._APP_TURN_INFLIGHT[("xcash", "cid2")] = {"task": None, "state": {}}
check("task None 不算活", not bridge._session_turn_in_flight("xcash"))
bridge._APP_TURN_INFLIGHT.clear()
print(); sys.exit(1 if fails else 0)
