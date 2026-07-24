"""N4 TG→app 媒體(pocketagent#36)—— `_tg_extract_attachments` /
`_persona_history` 對 hermes 0.19 實際落庫格式的媒體引用萃取。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_tg_media_inbound.py

驗證面(每一種格式都是從 hermes 原始碼/live state.db 對出來的真實產物,
不是猜的):
1. 舊 image hint `[Image attached at: <path>]`(agent/image_routing.py:749)
   → image 附件;檔案不在 → 人話占位「帶檔名」。
2. 0.19 vision 描述塊(gateway/run.py:16552)+ 兩種描述失敗變體
   → 整塊移除、image 附件掛回;描述文字(agent 面)不得外洩到正文。
3. assistant 列 `MEDIA:<path>` 遞送標記(tools/send_message_tool 語法,
   live db row 9591 實例)→ 附件 + 正文保留原話;引號包裹變體同收。
4. send_message 跨 session 鏡射佔位 `[Sent image attachment]` 等
   (tools/send_message_tool.py `_describe_media_for_mirror`)→ 人話文字,
   不再把工程占位字端給使用者(善彰實測痛點)。
5. `_persona_history` 端到端:假 state.db(schema 對齊 SELECT)裡帶附件的
   TG 訊息 → 歷史輸出含 {kind, filename, mime, path} 媒體引用。
6. 文件/音訊 saved-at 提示(gateway/run.py:2298/11681)不回歸。
"""
import os
import sqlite3
import sys
import tempfile
import time

_TMP_CANON = tempfile.mkdtemp(prefix="tgmedia-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP_CANON, "canonical.db")
os.environ["POCKET_MEDIA_DIR"] = os.path.join(_TMP_CANON, "media-artifacts")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

fails = []


def check(name, cond, note=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note and not cond else ""))
    if not cond:
        fails.append(name)


_home = tempfile.mkdtemp(prefix="tgmedia-home-")


def _mkfile(name: str, data: bytes = b"x") -> str:
    p = os.path.join(_home, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


IMG = _mkfile("photo_1.jpg", b"\xff\xd8\xff\xe0jpeg")
PDF = _mkfile("Rakutai_Form.pdf", b"%PDF-1.4")
M4A = _mkfile("voice_note.m4a", b"m4a")
SPACED = _mkfile("hr form v2.pdf", b"%PDF-1.4")

X = bridge._tg_extract_attachments

# ── 1. 舊 image hint ────────────────────────────────────────────────────
text, atts = X(f"你看這張\n[Image attached at: {IMG}]")
check("image-hint: 附件萃取", len(atts) == 1 and atts[0]["kind"] == "image"
      and atts[0]["path"] == IMG and atts[0]["mime"] == "image/jpeg")
check("image-hint: 正文乾淨", text == "你看這張")

text, atts = X("[Image attached at: /nonexistent/gone_123.jpg]")
check("image-hint 檔案不在: 無附件+占位帶檔名",
      not atts and "gone_123.jpg" in text and "已失效" in text)

# ── 2. 0.19 vision 描述塊 ───────────────────────────────────────────────
_desc = ("[The user sent an image~ Here's what I can see:\n"
         "This image appears to be a reservation confirmation screen "
         "[with brackets] inside.]\n"
         f"[If you need a closer look, use vision_analyze with image_url: {IMG} ~]")
text, atts = X(_desc + "\n\n幫我確認訂位")
check("vision 塊: image 附件", len(atts) == 1 and atts[0]["kind"] == "image"
      and atts[0]["path"] == IMG)
check("vision 塊: 描述不外洩", "reservation" not in text and text == "幫我確認訂位")

for variant in (
    "[The user sent an image but I couldn't quite see it this time (>_<) "
    f"You can try looking at it yourself with vision_analyze using image_url: {IMG}]",
    "[The user sent an image but something went wrong when I tried to look at it~ "
    f"You can try examining it yourself with vision_analyze using image_url: {IMG}]",
):
    text, atts = X(variant)
    check("vision 失敗變體: 附件+正文空",
          len(atts) == 1 and atts[0]["path"] == IMG and text == "")

# vision 塊路徑失效 → 占位帶檔名
text, atts = X("[The user sent an image~ Here's what I can see:\nblah]\n"
               "[If you need a closer look, use vision_analyze with "
               "image_url: /nonexistent/lost.jpg ~]")
check("vision 塊檔案不在: 占位帶檔名", not atts and "lost.jpg" in text)

# ── 3. assistant MEDIA:<path> 遞送標記 ─────────────────────────────────
text, atts = X(f"善彰,表格已做好:A4 PDF,3 頁。\n\nMEDIA:{PDF}")
check("MEDIA 標記: file 附件", len(atts) == 1 and atts[0]["kind"] == "file"
      and atts[0]["path"] == PDF and atts[0]["mime"] == "application/pdf"
      and atts[0]["filename"] == "Rakutai_Form.pdf")
check("MEDIA 標記: 原話保留、標記剝除",
      "表格已做好" in text and "MEDIA:" not in text)

text, atts = X(f'圖表在此 MEDIA:"{SPACED}" 請查收')
check("MEDIA 標記引號+空白檔名", len(atts) == 1 and atts[0]["path"] == SPACED)

text, atts = X(f"語音 MEDIA:{M4A}")
check("MEDIA 標記 audio kind", len(atts) == 1 and atts[0]["kind"] == "audio")

text, atts = X("MEDIA:/nonexistent/chart_9.png")
check("MEDIA 檔案不在: 占位帶檔名", not atts and "chart_9.png" in text)

# ── 4. send_message 鏡射佔位 → 人話 ────────────────────────────────────
for raw, expect in (
    ("[Sent image attachment]", "圖片附件"),
    ("[Sent document attachment]", "文件附件"),
    ("[Sent video attachment]", "影片附件"),
    ("[Sent audio attachment]", "音訊附件"),
    ("[Sent voice message]", "語音訊息"),
    ("[Sent 3 media attachments]", "3 件媒體附件"),
):
    text, atts = X(raw)
    check(f"鏡射佔位人話: {raw}",
          not atts and expect in text and "[Sent" not in text)

# 非獨佔一行的佔位(正文行中引用同字串)不誤傷
text, atts = X("我剛看到 [Sent image attachment] 這個字串是什麼?")
check("鏡射佔位僅整行匹配", text == "我剛看到 [Sent image attachment] 這個字串是什麼?"
      and not atts)

# 0.19 compaction 會把多則訊息併成一列(live db row 12713 實例)—— 佔位
# 以「行」的形式出現在長文中段,逐行替換、其餘正文原樣保留。
text, atts = X("收到，Android 版驗收正常。\n🖼️ 圖片測試\n"
               "[Sent image attachment]\n[Sent document attachment]\n"
               "[Sent document attachment]")
check("compaction 合併列: 逐行人話",
      "[Sent" not in text and "驗收正常" in text and "圖片測試" in text
      and text.count("（已在 Telegram 傳送文件附件）") == 2
      and "（已在 Telegram 傳送圖片附件）" in text and not atts)

# ── 4.5 原檔被清、media artifacts 有封存 → 引用仍有效 ──────────────────
# hermes 的 image_cache 週期修剪(live 站 7/23 已清空)。bridge 只要在檔案
# 還在時掃過一次(卡片 follower / v2 media 端點都會),封存就留有快照,
# GET /file 對缺檔路徑走 resolve_original 供檔 —— 所以萃取端不能因原檔
# 消失就退占位,要先問封存。
PRUNED = _mkfile("pruned_photo.jpg", b"\xff\xd8\xff\xe0soon-gone")
bridge._media_store().capture_path("hermes:test", PRUNED)
os.unlink(PRUNED)
text, atts = X(f"[Image attached at: {PRUNED}]")
check("封存快照: 原檔已清仍掛附件",
      len(atts) == 1 and atts[0]["path"] == PRUNED and atts[0]["kind"] == "image")

# ── 5. 文件/音訊 saved-at 提示不回歸 ───────────────────────────────────
text, atts = X(f"[The user sent a document: 'Rakutai_Form.pdf'. It is saved at: {PDF}. "
               "Its text is not inlined here (it's a binary format such as PDF or DOCX). "
               "To read it, extract the document's text yourself.]\n\n這寫什麼?")
check("document note: file 附件", len(atts) == 1 and atts[0]["kind"] == "file"
      and atts[0]["filename"] == "Rakutai_Form.pdf")
check("document note: 正文乾淨", text == "這寫什麼?")

# ── 6. _persona_history 端到端(假 state.db,schema 對齊 SELECT)────────
db = os.path.join(_home, "state.db")
con = sqlite3.connect(db)
con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
con.execute("CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL)")
con.execute("INSERT INTO sessions VALUES('tg-1','telegram')")
now = time.time()
rows = [
    ("user", f"照片來了\n[Image attached at: {IMG}]", now - 40),
    ("assistant", f"收到,回你一份表格。\n\nMEDIA:{PDF}", now - 30),
    ("assistant", "[Sent image attachment]", now - 20),
    ("user", "[Image attached at: /nonexistent/expired.jpg]", now - 10),
]
for r, c, ts in rows:
    con.execute("INSERT INTO messages VALUES('tg-1',?,?,?)", (r, c, ts))
con.commit()
con.close()

hist = bridge._persona_history(_home, 50)
check("history: 四列都在", len(hist) == 4)
check("history: user 圖片附件引用",
      hist[0]["attachments"] and hist[0]["attachments"][0]["path"] == IMG
      and hist[0]["content"] == "照片來了")
check("history: assistant MEDIA 附件引用",
      hist[1]["attachments"] and hist[1]["attachments"][0]["path"] == PDF
      and "MEDIA:" not in hist[1]["content"])
check("history: 鏡射佔位人話", hist[2]["content"] == "（已在 Telegram 傳送圖片附件）"
      and not hist[2]["attachments"])
check("history: 失效檔案退占位帶檔名",
      not hist[3]["attachments"] and "expired.jpg" in hist[3]["content"])

# 附件 dict 形狀 = app 契約 {kind, filename, mime, path}(缺一不可)
a = hist[0]["attachments"][0]
check("附件形狀符合 app 契約", set(a) == {"kind", "filename", "mime", "path"})

print()
if fails:
    print(f"FAILED: {len(fails)} — " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
