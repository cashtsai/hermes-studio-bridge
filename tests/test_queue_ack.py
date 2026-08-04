"""排隊回執不落正典(fix/queue-ack-not-canonical)判定測試。"""
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
