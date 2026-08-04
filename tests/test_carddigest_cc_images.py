"""CC lane 圖片附件(feat/cc-card-image-attachments)測試。

1. tool_use 帶圖片路徑 → tool_call 卡之外多一張 attachment 卡(path/filename/mime)。
2. 非圖片路徑 → 不多卡(行為不變)。
3. user 文字裡的 harness 圖片座標說明行被濾掉;整則只有說明 → 不出卡。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import carddigest as cd  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 1. Read 圖片 → tool_call + attachment 兩張卡
ev = {"type": "assistant", "timestamp": "2026-08-04T05:00:00Z",
      "message": {"content": [
          {"type": "tool_use", "name": "Read",
           "input": {"file_path": "/tmp/scratch/reader_new.png"}}]}}
cards = cd.cc_event_to_cards(ev, uid="i1", turn_id="t1")
check("Read 圖片 → 2 張卡(tool_call + attachment)",
      len(cards) == 2 and cards[0]["kind"] == "tool_call"
      and cards[1]["kind"] == "attachment")
check("attachment 卡帶 path/filename/mime",
      cards[1]["body"]["path"] == "/tmp/scratch/reader_new.png"
      and cards[1]["body"]["filename"] == "reader_new.png"
      and cards[1]["body"]["mime"] == "image/png")
check("attachment 卡 id 錨在 tool_use 卡上(replay 穩定)",
      cards[1]["id"] == "card-cc-i1-0-img")

# 2. 大小寫副檔名也認得
ev2 = {"type": "assistant", "timestamp": "2026-08-04T05:00:00Z",
       "message": {"content": [
           {"type": "tool_use", "name": "Read",
            "input": {"file_path": "/x/IMG_3757.PNG"}}]}}
cards2 = cd.cc_event_to_cards(ev2, uid="i2")
check("大寫 .PNG 也認得", len(cards2) == 2 and cards2[1]["body"]["filename"] == "IMG_3757.PNG")

# 3. 非圖片路徑 → 只有 tool_call(行為不變)
ev3 = {"type": "assistant", "timestamp": "2026-08-04T05:00:00Z",
       "message": {"content": [
           {"type": "tool_use", "name": "Read",
            "input": {"file_path": "/x/bridge.py"}}]}}
cards3 = cd.cc_event_to_cards(ev3, uid="i3")
check("非圖片 Read 不多卡", len(cards3) == 1 and cards3[0]["kind"] == "tool_call")

# 4. user 文字含座標說明行 → 濾掉,正文保留
ev4 = {"type": "user", "timestamp": "2026-08-04T05:00:00Z",
       "message": {"content":
           "[Image: original 1206x2622, displayed at 920x2000. Multiply coordinates by 1.31 to map to original image.]\n看一下這張截圖"}}
cards4 = cd.cc_event_to_cards(ev4, uid="i4")
check("座標說明行被濾掉、正文保留",
      len(cards4) == 1 and cards4[0]["body"]["text"] == "看一下這張截圖")

# 5. 整則只有座標說明 → 不出卡(醜泡泡根治)
ev5 = {"type": "user", "timestamp": "2026-08-04T05:00:00Z",
       "message": {"content":
           "[Image: original 1206x2622, displayed at 920x2000. Multiply coordinates by 1.31 to map to original image.]"}}
cards5 = cd.cc_event_to_cards(ev5, uid="i5")
check("純座標說明 → 0 張卡", cards5 == [])

# 6. 一般 user 文字完全不受影響
ev6 = {"type": "user", "timestamp": "2026-08-04T05:00:00Z",
       "message": {"content": "早,今天先修卡頓"}}
cards6 = cd.cc_event_to_cards(ev6, uid="i6")
check("一般文字不受影響", len(cards6) == 1 and cards6[0]["body"]["text"] == "早,今天先修卡頓")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
