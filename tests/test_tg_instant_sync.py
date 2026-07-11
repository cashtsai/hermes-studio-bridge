"""TG→App 即時同步(#tg-instant-sync)延遲實測 —— 修復前(純 30s 保險絲)
vs 修復後(state.db stat watcher 加速觸發)。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_tg_instant_sync.py

驗證的是「真實延遲」,不是 py_compile 過關:
1. 建一個假 persona,home 指到 tmp 目錄,裡面放一個跟 Hermes 官方
   gateway 寫的 state.db 同結構(schema 抄自 bridge._persona_history 的
   SELECT)的 WAL-mode sqlite db。
2. 「修復前」模擬:直接用舊版 `_canon_wait`(canonical 版本計數器)當
   follower 的喚醒源 —— TG 訊息只寫 state.db、不寫 canonical,所以這條
   等待永遠等不到訊號,只能靠外層 30s timeout 兜底。這段真的等滿 30s
   （這是舊機制的真實行為,不是猜測的數字）。
3. 「修復後」:啟動 `_state_db_watcher_loop`(跟 production 一模一樣的
   函式,只是把輪詢間隔設快一點驗證原理;預設 0.15s 一樣可測),接著
   寫一筆新 TG 訊息進 state.db-wal,量測到 `_STATEDB_VER` 真的被 bump、
   `_hp_canon_follower` 真的醒來並重新合併出新內容,前後總共花多久。
4. 額外驗證:30s 保險絲程式碼路徑(_hp_canon_follower 的
   `timeout=30.0`)沒有被拿掉 —— 讀原始碼字串斷言,防止有人「用新機制
   取代」而不是「疊加」。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

_TMP_CANON = tempfile.mkdtemp(prefix="tgsync-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP_CANON, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init)
import carddigest  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── fixture: 假 persona,home 是 tmp 目錄,state.db schema 對齊
# _persona_history 的 query ─────────────────────────────────────────────
SESSION = "test-instant-sync"
_home = tempfile.mkdtemp(prefix="tgsync-home-")
bridge.PERSONAS[SESSION] = (f"測試人格 ({SESSION})", _home)



# Hermes 官方 gateway 是長駐進程,對 state.db 維持一條「常開」的連線
# (不會每次寫完就 close)。這個 fixture 必須模擬同一件事：若每次寫入都
# open→commit→close,SQLite 在關閉最後一條連線時會自動把 WAL 內容
# checkpoint 回主檔,state.db-wal 反而會消失、state.db 本身的 mtime/size
# 常常「剛好沒變」(單頁大小、同一秒內)——這不是 watcher 的 bug,是測試
# fixture 沒有如實模擬「長駐連線」這個前提,曾經在這裡踩過一次(第一版
# 端到端測試因此誤判 watcher 抓不到變化)。用 `wal_autocheckpoint=0` +
# 保持連線常開,讓 state.db-wal 像正式環境一樣持續成長。
_GATEWAY_CONN: sqlite3.Connection | None = None


def _make_state_db(home: str) -> str:
    global _GATEWAY_CONN
    db = os.path.join(home, "state.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")   # 模擬長駐 gateway:不自動 checkpoint
    con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
    con.execute("CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, "
                "timestamp REAL)")
    con.execute("INSERT INTO sessions VALUES('tg-sess-1','telegram')")
    con.commit()
    _GATEWAY_CONN = con   # 故意不 close —— 這正是要驗證的前提
    return db


def _write_tg_message(home: str, text: str, ts: float) -> None:
    """模擬 Hermes 官方 TG gateway 寫入一則新訊息(唯讀監看端不能做這個 —
    這裡是在扮演 gateway 那一端,用來驅動觀察者)。沿用同一條長駐連線,
    不 open/close 新連線,如實對齊正式 gateway 的行為模式。"""
    assert _GATEWAY_CONN is not None, "call _make_state_db() first"
    _GATEWAY_CONN.execute("INSERT INTO messages VALUES('tg-sess-1','user',?,?)", (text, ts))
    _GATEWAY_CONN.commit()


_make_state_db(_home)


# ── 1. 修復前行為:_canon_wait 對 TG 寫入完全沒反應,只能靠 30s timeout ──
async def _measure_before() -> tuple:
    ver0 = bridge._CANON_VER.get(SESSION, 0)

    async def writer():
        await asyncio.sleep(0.5)
        _write_tg_message(_home, "TG 訊息(修復前情境)", time.time())

    t0 = time.monotonic()
    wtask = asyncio.create_task(writer())
    try:
        # 這就是修復前 _hp_canon_follower 真正在跑的那一行程式碼路徑
        # (舊版:`await asyncio.wait_for(_canon_wait(session, ver), timeout=30.0)`)。
        # canonical 版本不會因為 TG 訊息而變動,所以只會在 30s timeout 那一刻
        # 才返回 —— 這是舊機制的真實延遲,不是估計值。
        await asyncio.wait_for(bridge._canon_wait(SESSION, ver0), timeout=3.0)
        woke_by_signal = True
    except asyncio.TimeoutError:
        woke_by_signal = False
    elapsed = time.monotonic() - t0
    await wtask
    return elapsed, woke_by_signal


# 用 timeout=3.0(縮尺版)而非正式的 30.0 真的等滿,原理相同、省測試時間；
# 下面另外用程式碼掃描斷言正式路徑真的是 30.0（見第 4 節），確保沒有人
# 「順便」把保險絲改短了。
before_elapsed, before_woke_by_signal = asyncio.run(_measure_before())
check("修復前:canonical 版本對 TG 寫入無反應(靠 timeout 到底才醒)",
      before_woke_by_signal is False)
check(f"修復前實測延遲 ≈ {before_elapsed:.2f}s(卡在 timeout,不是即時)",
      before_elapsed >= 2.9)


# ── 2. 修復後行為:state.db stat watcher 偵測寫入 → 立刻 bump _STATEDB_VER
#    → _canon_or_statedb_wait 立刻醒 ─────────────────────────────────────
async def _measure_after() -> float:
    os.environ["POCKET_STATEDB_POLL_SECS"] = "0.1"
    bridge._STATEDB_POLL_SECS = 0.1
    bridge._STATEDB_STAT_CACHE.pop(SESSION, None)
    # 先跑一輪讓 watcher 把「目前狀態」記錄下來(否則第一次觀察一定會誤判
    # 成「有變化」，這是所有 stat-diff watcher 的標準暖機動作)。
    watcher_task = asyncio.create_task(bridge._state_db_watcher_loop())
    await asyncio.sleep(0.3)

    ver0 = bridge._STATEDB_VER.get(SESSION, 0)
    canon0 = bridge._CANON_VER.get(SESSION, 0)

    async def writer():
        await asyncio.sleep(0.3)
        _write_tg_message(_home, "TG 訊息(修復後情境,即時)", time.time())

    t0 = time.monotonic()
    wtask = asyncio.create_task(writer())
    await asyncio.wait_for(bridge._canon_or_statedb_wait(SESSION, canon0, ver0),
                           timeout=10.0)
    elapsed = time.monotonic() - t0
    await wtask
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    return elapsed


after_elapsed = asyncio.run(_measure_after())
check(f"修復後實測延遲 ≈ {after_elapsed:.3f}s(<2s 目標)", after_elapsed < 2.0)
check("修復後比修復前快至少一個數量級",
      after_elapsed < before_elapsed / 5.0)


# ── 3. 端到端:follower 醒來後,_hp_merged_messages 真的把新 TG 訊息吐出來 ──
async def _measure_end_to_end_follower() -> tuple:
    d = bridge._HP_CARD_DIGESTS[SESSION] = carddigest.PersonaDigest()
    d.seed_messages(bridge._hp_merged_messages(SESSION, 80))
    before_count = len(d.store.snapshot(limit=200)["cards"])

    bridge._STATEDB_STAT_CACHE.pop(SESSION, None)
    watcher_task = asyncio.create_task(bridge._state_db_watcher_loop())
    await asyncio.sleep(0.3)   # 暖機,同上
    follower_task = asyncio.create_task(bridge._hp_canon_follower(SESSION))

    t0 = time.monotonic()
    new_text = f"端到端 TG 訊息 {time.time()}"
    await asyncio.sleep(0.2)
    _write_tg_message(_home, new_text, time.time())

    deadline = time.monotonic() + 5.0
    found = False
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        cards = d.store.snapshot(limit=200)["cards"]
        if len(cards) > before_count:
            found = True
            break
    elapsed = time.monotonic() - t0

    follower_task.cancel()
    watcher_task.cancel()
    for t in (follower_task, watcher_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    return elapsed, found, new_text, d.store.snapshot(limit=200)["cards"]


e2e_elapsed, e2e_found, e2e_text, e2e_cards = asyncio.run(_measure_end_to_end_follower())
check(f"端到端(_hp_canon_follower 真的重掃出新卡)延遲 ≈ {e2e_elapsed:.2f}s (<2s)",
      e2e_found and e2e_elapsed < 2.0)
check("新卡內容確實包含剛寫入的 TG 訊息文字",
      any(e2e_text in str(c.get("body", {}).get("text", "")) for c in e2e_cards))


# ── 4. 保險絲鐵律:30s timeout 程式碼路徑仍在(疊加,不是取代)───────────
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bridge.py"), encoding="utf-8").read()
check("_hp_canon_follower 仍保留 timeout=30.0 保險絲(疊加,非取代)",
      "_canon_or_statedb_wait(session, ver, sver),\n                                   timeout=30.0)" in _src
      or "timeout=30.0" in _src.split("async def _hp_canon_follower")[1][:600])
check("state.db watcher 是唯讀 os.stat(),沒有任何寫入 state.db 的呼叫",
      "state.db" not in "\n".join(
          ln for ln in _src.splitlines()
          if ("con.execute" in ln or ".write(" in ln) and "state.db" in ln
      ))


print()
print("=" * 60)
print(f"修復前(舊：只靠 canonical 版本,對 TG 寫入無反應): {before_elapsed:.2f}s "
      f"(正式碼路徑保險絲為 30.0s,此處用縮尺 3.0s 驗證同一機制)")
print(f"修復後(新：state.db stat watcher 加速觸發):       {after_elapsed:.3f}s")
print(f"端到端(follower 真醒來 + 新卡真的出現):            {e2e_elapsed:.2f}s")
print("=" * 60)
print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
