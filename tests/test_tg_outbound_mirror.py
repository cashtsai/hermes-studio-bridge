"""app→TG 反向鏡射(#32 的最後一哩)驗收。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_tg_outbound_mirror.py

方向與 test_tg_mirror_route.py 相反:那支測 TG→app 的 ingest,這支測「使用者在
Pocket 對人格發話 / 人格回覆 → 貼進同一條 Telegram 聊天室」。全程 dry-run,
**不會真的呼叫 Bot API、不碰任何正式聊天室**。

驗的條款:
1. 預設關閉:沒設 `POCKET_TG_MIRROR_OUTBOUND` → 完全不動作(production 零改變)。
2. chat 解析:從 gateway session_key 取 chat_id/thread_id,含 dm/群組/畸形 key。
3. 目標解析:chat 與 ACP session-pinning 挑的是**同一筆** sessions.json entry
   (寫進哪條 session,就貼到哪個聊天室);缺 bot token / 缺對映 → 不投。
4. 去重不變式(#37):送進 TG 的正文已剝掉 `<details>` 附錄,與 state.db 側的乾淨
   正文同形 → `bridge._tg_dup` 的壓重鍵仍然對得上,app 端不會多一顆泡泡。
5. 冪等:同一個 canonical mid 重放不重複投遞(回合重試/hook 重送)。
6. 長訊息切段:> 4096 上限要切,且優先在換行處斷。
7. 韌性:鏡射失敗/沒有目標都不得拋例外(回合不能被鏡射拖垮)。
8. 角色語意:user 帶 📱 前綴(TG 那頭看得出是從手機說的)、assistant 不帶
   (它就是人格在說話)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

# 這支是「腳本式驗收」(repo 慣例:python3 tests/test_tg_outbound_mirror.py):測試邏輯直接寫在
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
        "python3 tests/test_tg_outbound_mirror.py"
    )

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

_TMP_CANON = tempfile.mkdtemp(prefix="tgout-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP_CANON, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
# 預設關閉的條款要先測,所以這裡刻意不預設開啟。
os.environ.pop("POCKET_TG_MIRROR_OUTBOUND", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init)
import tg_outbound  # noqa: E402
from acp_client import canonical_telegram_session  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── fixture: 假 persona home,含 sessions.json 與 .env ──────────────────
SESSION = "test-tg-outbound"
CHAT_ID = "7957062076"
SID_OLD = "20260701_000000_aaaaaaaa"
SID_NOW = "20260726_074229_c115e2d1"
_home = tempfile.mkdtemp(prefix="tgout-home-")
bridge.PERSONAS[SESSION] = (f"測試人格 ({SESSION})", _home)

os.makedirs(os.path.join(_home, "sessions"), exist_ok=True)
with open(os.path.join(_home, "sessions", "sessions.json"), "w") as f:
    json.dump({
        "_README": "legacy mirror of the gateway routing index",
        # 舊的那條(已輪替掉):updated_at 較早,不該被選中。
        "agent:main:telegram:dm:1111111111": {
            "session_id": SID_OLD, "platform": "telegram", "chat_type": "dm",
            "updated_at": "2026-07-01T00:00:00.000000",
        },
        # gateway 現在在用的那條。
        "agent:main:telegram:dm:" + CHAT_ID: {
            "session_id": SID_NOW, "platform": "telegram", "chat_type": "dm",
            "updated_at": "2026-07-26T07:16:51.546094",
        },
        # 別的平台不該干擾。
        "agent:main:discord:dm:999": {
            "session_id": "20260726_999999_dddddddd", "platform": "discord",
            "chat_type": "dm", "updated_at": "2026-07-26T23:00:00.000000",
        },
    }, f)
with open(os.path.join(_home, ".env"), "w") as f:
    f.write("# comment line\nFOO=bar\nTELEGRAM_BOT_TOKEN=\"123456:FAKE-TEST-TOKEN\"\n")

# 2026-08-11 起(2ec6cd9「防復活」)canonical_telegram_session 不只讀 mapping,
# 還會回 state.db 驗證該 session 真的存在、source=telegram、未封存、非 claude
# 模型 —— fixture 的 state.db 要跟上這個契約,否則「單一真相」檢查拿到 None
# (這支 8/15 前的紅就是 fixture 停在舊契約)。schema 帶齊 archived/model 欄。
_db = os.path.join(_home, "state.db")
con = sqlite3.connect(_db)
con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
            "archived INT DEFAULT 0, model TEXT, "
            "message_count INT DEFAULT 0, started_at REAL)")
con.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL)")
con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            (SID_NOW, "telegram", 0, "gpt-5", 2, time.time()))
con.commit()
con.close()

REPLY_BODY = "好的,明天的班表我看過了:早班是你,晚班是阿哲。"
REPLY_WITH_STEPS = (REPLY_BODY +
                    "\n\n<details><summary>🔧 執行步驟 (2)</summary>\n"
                    "讀取班表\n查詢日曆\n</details>")


# ── 1. 預設關閉 ─────────────────────────────────────────────────────────
check("mode() 預設 off", tg_outbound.mode() == "off")
tg_outbound.reset_state()
r = run(tg_outbound.mirror(_home, SESSION, "user", "在嗎", "mid-off"))
check("預設關閉:不投遞", r["sent"] == 0 and r["skipped"] == "disabled")
check("預設關閉:outbox 空", tg_outbound.outbox() == [])

# 之後全部在 dry-run 下跑 —— 不會有任何真的 Bot API 呼叫。
os.environ["POCKET_TG_MIRROR_OUTBOUND"] = "dry"
check("mode() 讀得到 dry", tg_outbound.mode() == "dry")


# ── 2. session_key → chat 解析 ──────────────────────────────────────────
check("dm key 取 chat_id",
      tg_outbound.chat_from_session_key("agent:main:telegram:dm:7957062076")
      == ("7957062076", ""))
check("dm key 第 6 段是 thread_id",
      tg_outbound.chat_from_session_key("agent:main:telegram:dm:7957062076:42")
      == ("7957062076", "42"))
check("具名 profile 不影響位置解析",
      tg_outbound.chat_from_session_key("agent:coder:telegram:dm:555")
      == ("555", ""))
check("群組負數 chat_id 可解析、不猜 thread",
      tg_outbound.chat_from_session_key("agent:main:telegram:group:-1001234:88888")
      == ("-1001234", ""))
check("非 telegram → None",
      tg_outbound.chat_from_session_key("agent:main:discord:dm:999") is None)
check("畸形 key → None",
      tg_outbound.chat_from_session_key("agent:main:telegram:dm") is None)
check("chat_id 非數字 → None",
      tg_outbound.chat_from_session_key("agent:main:telegram:dm:not-an-id") is None)
check("空 key → None", tg_outbound.chat_from_session_key("") is None)


# ── 3. 目標解析:與 session-pinning 同一筆 entry ─────────────────────────
check(".env 讀得到 bot token(去引號)",
      tg_outbound.bot_token(_home) == "123456:FAKE-TEST-TOKEN")
tgt = tg_outbound.resolve_target(_home)
check("resolve_target 齊全", tgt == ("123456:FAKE-TEST-TOKEN", CHAT_ID, ""))
check("投遞目標 = ACP 寫進去的那條 session 所屬 chat(單一真相)",
      canonical_telegram_session(_home) == SID_NOW and tgt[1] == CHAT_ID)

_no_env = tempfile.mkdtemp(prefix="tgout-notoken-")
os.makedirs(os.path.join(_no_env, "sessions"), exist_ok=True)
with open(os.path.join(_no_env, "sessions", "sessions.json"), "w") as f:
    json.dump({"agent:main:telegram:dm:5": {"session_id": "s", "platform": "telegram",
                                            "chat_type": "dm", "updated_at": "2026"}}, f)
check("缺 bot token → 不投", tg_outbound.resolve_target(_no_env) is None)
check("缺 sessions.json → 不投",
      tg_outbound.resolve_target(tempfile.mkdtemp(prefix="tgout-empty-")) is None)


# ── 4. 去重不變式(#37):送出的正文已剝附錄,與 state.db 側同形 ──────────
tg_outbound.reset_state()
r = run(tg_outbound.mirror(_home, SESSION, "assistant",
                           bridge._steps_stripped(REPLY_WITH_STEPS), "mid-a1"))
sent = tg_outbound.outbox()[-1]
check("assistant 投遞成功(dry)", r["sent"] == 1 and r["skipped"] == "dry_run")
check("送進 TG 的正文剝掉了 <details> 附錄", sent["parts"] == [REPLY_BODY])
check("送出正文不含執行步驟字樣", "執行步驟" not in sent["parts"][0])
check("chat 對", sent["chat_id"] == CHAT_ID)

# 壓重鍵真的仍然對得上:拿「送進 TG 的正文」與「canonical 帶附錄那份」比,
# 剝完附錄後必須相同 —— 這就是 _tg_dup 判定同一則的依據。
check("壓重鍵仍相等(canonical 帶附錄 vs TG 乾淨正文)",
      bridge._steps_stripped(REPLY_WITH_STEPS)
      == bridge._steps_stripped(sent["parts"][0]))

# 端到端:即使 state.db 真的長出一份鏡射回來的副本,合併輸出也不該變雙氣泡。
# (state.db 本體與 sessions 列已在檔頭 fixture 建好 —— 單一真相檢查要用。)
con = sqlite3.connect(_db)
NOW = time.time()
con.execute("INSERT INTO messages VALUES (?,?,?,?)",
            (SID_NOW, "assistant", REPLY_BODY, NOW))
con.commit()
con.close()
bridge._canon_add(SESSION, "assistant", REPLY_WITH_STEPS, None,
                  mid="canon-a1", created_at=NOW, push=False)
merged = bridge._hp_merged_messages(SESSION, 50)
bodies = [m for m in merged if m.get("role") == "assistant"]
check("鏡射回來的副本被壓掉,只剩一則 assistant(無雙氣泡)", len(bodies) == 1)
check("留下的是 canonical 那份", bodies[0]["id"] == "canon-a1")

# user 側:鏡射加的 📱 標記在壓重時要當它不存在。Telegram 不回送 bot 自己的
# 訊息,所以正常不會有這份副本 —— 但這條不變式不建立在別人的實作細節上。
USER_SAID = "幫我看一下明天的班表"
check("📱 標記在壓重正規化中被剝掉",
      bridge._steps_stripped(tg_outbound.format_text("user", USER_SAID))
      == bridge._steps_stripped(USER_SAID) == USER_SAID)

con = sqlite3.connect(_db)
con.execute("INSERT INTO messages VALUES (?,?,?,?)",
            (SID_NOW, "user", tg_outbound.format_text("user", USER_SAID), NOW))
con.commit()
con.close()
bridge._canon_add(SESSION, "user", USER_SAID, None,
                  mid="canon-u1", created_at=NOW, push=False)
merged = bridge._hp_merged_messages(SESSION, 50)
users = [m for m in merged if m.get("role") == "user"]
check("帶 📱 標記的回收副本也被壓掉(無雙氣泡)", len(users) == 1)
check("留下的是 canonical 的原文",
      users[0]["id"] == "canon-u1" and users[0]["content"] == USER_SAID)


# ── 5. 冪等:同一個 mid 只投一次 ────────────────────────────────────────
tg_outbound.reset_state()
r1 = run(tg_outbound.mirror(_home, SESSION, "user", "同一句", "mid-dup"))
r2 = run(tg_outbound.mirror(_home, SESSION, "user", "同一句", "mid-dup"))
check("首投成功", r1["sent"] == 1)
check("重放被冪等擋掉", r2["sent"] == 0 and r2["skipped"] == "duplicate")
check("outbox 只有一筆", len(tg_outbound.outbox()) == 1)
r3 = run(tg_outbound.mirror(_home, SESSION, "user", "同一句", "mid-other"))
check("不同 mid 照投(不是按內容擋)", r3["sent"] == 1)


# ── 6. 長訊息切段 ───────────────────────────────────────────────────────
check("空字串不切", tg_outbound.split_text("   ") == [])
check("短訊息一段", tg_outbound.split_text("嗨") == ["嗨"])
long_parts = tg_outbound.split_text(("行\n" * 4000).strip())
check("超長被切多段", len(long_parts) > 1)
check("每段都在上限內",
      all(len(p) <= tg_outbound.TG_TEXT_LIMIT for p in long_parts))
check("切段不吃字",
      "".join(p.replace("\n", "") for p in long_parts).count("行") == 4000)
nl = "A" * 3000 + "\n" + "B" * 1500
check("優先在換行處斷", tg_outbound.split_text(nl) == ["A" * 3000, "B" * 1500])

tg_outbound.reset_state()
r = run(tg_outbound.mirror(_home, SESSION, "assistant",
                           ("句\n" * 4000).strip(), "mid-long"))
check("長回覆投遞成段數", r["sent"] == len(tg_outbound.outbox()[-1]["parts"]) > 1)


# ── 7. 韌性:沒有目標 / 空正文 / 例外都不拋 ─────────────────────────────
tg_outbound.reset_state()
r = run(tg_outbound.mirror(_no_env, SESSION, "user", "有話要說", "mid-nt"))
check("沒有投遞目標:不拋、標 no_target",
      r["sent"] == 0 and r["skipped"] == "no_target")
r = run(tg_outbound.mirror(_home, SESSION, "user", "   ", "mid-empty"))
check("空正文:不拋、標 empty", r["sent"] == 0 and r["skipped"] == "empty")
r = run(tg_outbound.mirror(None, SESSION, "user", "x", "mid-badhome"))
check("home 是 None:不拋(視為無目標)", r["sent"] == 0 and r["skipped"])

# 呼叫端的包裝層:未知 session 不得拋(bridge._tg_mirror_out)
try:
    bridge._tg_mirror_out("no-such-persona", "user", "x", "m")
    bridge._tg_mirror_out(SESSION, "user", "x", "m")   # 無 running loop
    check("_tg_mirror_out 不拋例外", True)
except Exception as e:  # noqa: BLE001
    check("_tg_mirror_out 不拋例外 (%s)" % e, False)


# ── 8. 角色語意:前綴 ───────────────────────────────────────────────────
check("user 帶 📱 前綴", tg_outbound.format_text("user", "在嗎") == "📱 在嗎")
check("assistant 不帶前綴", tg_outbound.format_text("assistant", "在") == "在")
check("空正文格式化成空", tg_outbound.format_text("user", "  ") == "")


print()
if fails:
    print("FAILED (%d): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
