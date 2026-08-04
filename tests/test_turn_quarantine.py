"""活 turn 檢疫(fix/active-turn-quarantine)判定測試。"""
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
