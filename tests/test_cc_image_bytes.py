"""base64 圖 bytes 入口(closeout #6)測試。"""

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_cc_image_bytes.py):測試邏輯直接寫在
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
        "python3 tests/test_cc_image_bytes.py"
    )

import os, sys, tempfile, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import media_artifacts, carddigest as cd
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

store = media_artifacts.MediaArtifactStore(tempfile.mkdtemp())
png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
item = store.capture_bytes("cc:test", png, filename="shot.png", mime="image/png", kind="image")
check("capture_bytes 可用且 available", item.get("available") is True and item.get("media_id"))
item2 = store.capture_bytes("cc:test", png, filename="shot.png", mime="image/png", kind="image")
check("同內容冪等(同 media_id)", item2.get("media_id") == item.get("media_id"))
blob = store.open_media(item["media_id"]) if hasattr(store, "open_media") else None
check("blob 可回讀", blob is not None)

captured = []
def sink(mime, b64):
    captured.append(mime)
    return {"media_id": "m1", "download_url": "/app/v2/artifacts/m1",
            "filename": "shot.png", "mime": mime}
ev = {"type": "user", "timestamp": "2026-08-05T06:00:00Z",
      "message": {"content": [{"type": "tool_result", "content": [
          {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                       "data": base64.b64encode(png).decode()}},
          {"type": "text", "text": "screenshot taken"}]}]}}
cards = cd.cc_event_to_cards(ev, uid="b1", image_sink=sink)
kinds = [c["kind"] for c in cards]
check("出 attachment 卡 + tool_result 卡", "attachment" in kinds and "tool_result" in kinds)
att = next(c for c in cards if c["kind"] == "attachment")
check("attachment 卡帶 media_id/download_url",
      att["body"]["media_id"] == "m1" and att["body"]["download_url"])
cards_nosink = cd.cc_event_to_cards(ev, uid="b2")
check("無 sink 行為不變(只有 tool_result)", [c["kind"] for c in cards_nosink] == ["tool_result"])
print(); sys.exit(1 if fails else 0)
