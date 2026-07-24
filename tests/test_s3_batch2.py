"""S3 批次 2 端到端驗收 — persona 對話走 /app/v2 卡片流的伺服器端供卡
+ 統一 input 路由(SYNC_ENGINE_REWRITE_PLAN §3.1;契約 §4.4/§5)。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_s3_batch2.py

驗證的是批次 2 的對外行為本身,不是 py_compile 過關:
1. `_v2_card_source` 路由:hermes:{persona} → hp;未知 persona 404、
   未知 provider 400(app 端 isRouteUnavailable 的回退判準就吃這兩個)。
2. 伺服器端供卡:GET /app/v2/sessions/hermes:{p}/cards 冷載 snapshot
   涵蓋 TG(state.db)來源 —— 人格卡片流不只 canonical,你在 TG 講的
   也要在 Pocket 出現(issue #32 的資料面前提)。
3. event_log 鏡射:卡片 seed 的三來源合併掃描順手把 TG 舊訊回填進
   event_log;per-session 與全域 /app/v2/events(follow=false)都撈得到,
   信封 {seq,ts,type,data}(全域多帶 session)。
4. 統一 input:POST /app/v2/sessions/hermes:{p}/input fire-and-forget
   → {accepted};user 卡即時進卡片流、回合收尾 assistant 進 canonical
   與 turn 卡;同 client_id 重送 → replayed(冪等)。
5. v1 相容:同一段對話 GET /app/v1/messages 照樣看得到(批次 2 不破壞
   老 app 的路)。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="s3batch2-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ["POCKET_MEDIA_DIR"] = os.path.join(_TMP, "media")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → event_log/read_cursors 建表)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── fixture:假 persona + 與正式 gateway 同構的 state.db(WAL、長駐連線)──
SESSION = "test-s3b2"
SID = f"hermes:{SESSION}"
_home = tempfile.mkdtemp(prefix="s3batch2-home-")
# 縮限成只有假 persona:全域訂閱(gen_all)按 PERSONAS 掃描,測試不該去讀
# 這台機器上真實人格的 home(hermetic;真實 builtins 行為由線上驗證)。
bridge.PERSONAS.clear()
bridge.PERSONAS[SESSION] = (f"測試人格 ({SESSION})", _home)

TG_TEXT = "TG 端先講的一句話(state.db 來源)"
TG_TS = time.time() - 120

_gw = sqlite3.connect(os.path.join(_home, "state.db"))
_gw.execute("PRAGMA journal_mode=WAL")
_gw.execute("PRAGMA wal_autocheckpoint=0")   # 模擬長駐 gateway:不自動 checkpoint
_gw.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
_gw.execute("CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL)")
_gw.execute("INSERT INTO sessions VALUES('tg-1','telegram')")
_gw.execute("INSERT INTO messages VALUES('tg-1','user',?,?)", (TG_TEXT, TG_TS))
_gw.commit()

client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}


def _sse_events(path: str) -> list[dict]:
    """follow=false 的 SSE 端點 → 收集 data: 信封直到 [DONE]。"""
    out = []
    with client.stream("GET", path, headers=AUTH) as r:
        check(f"SSE {path.split('?')[0]} 回 200", r.status_code == 200)
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            out.append(json.loads(body))
    return out


# ── 1. _v2_card_source 路由(統一路由的分流面)─────────────────────────
src = bridge._v2_card_source(SID)
check("hermes:{persona} → ('hp', persona)", src == ("hp", SESSION))
try:
    bridge._v2_card_source("hermes:no-such-persona")
    check("未知 persona → 404", False)
except Exception as e:  # noqa: BLE001
    check("未知 persona → 404", getattr(e, "status_code", 0) == 404)
# app 端 isRouteUnavailable 的判準之一:400 + body 帶 UNSUPPORTED
r = client.post("/app/v2/sessions/nosuchprovider:x/input",
                json={"content": "hi"}, headers=AUTH)
check("未知 provider → 400 + UNSUPPORTED(isRouteUnavailable 判準)",
      r.status_code == 400 and "UNSUPPORTED" in r.text)

# 老 app 相容:v1 messages 對未知 persona 的錯誤形狀不變(400)
r = client.get("/app/v1/messages", params={"session": "no-such"}, headers=AUTH)
check("v1 messages 未知 persona 仍 400(老 app 契約不變)", r.status_code == 400)

# ── 2. 伺服器端供卡:cards 冷載 snapshot 涵蓋 TG 來源 ────────────────────
r = client.get(f"/app/v2/sessions/{SID}/cards", headers=AUTH)
check("cards snapshot 200", r.status_code == 200)
snap = r.json()
check("snapshot 形狀 {cards, latest_seq}",
      isinstance(snap.get("cards"), list) and "latest_seq" in snap)
tg_cards = [c for c in snap["cards"] if TG_TEXT in json.dumps(c, ensure_ascii=False)]
check("TG(state.db)訊息進卡片流(伺服器端供卡,不只 canonical)",
      len(tg_cards) == 1 and tg_cards[0]["role"] == "user")

# ── 3. event_log 鏡射:per-session 與全域 /app/v2/events 都撈得到 ────────
evs = _sse_events(f"/app/v2/events?session={SESSION}&since_seq=0&follow=false")
tg_evs = [e for e in evs if e.get("type") == "message.upsert"
          and TG_TEXT in json.dumps(e.get("data") or {}, ensure_ascii=False)]
check("TG 舊訊回填 event_log(per-session 訂閱撈得到)", len(tg_evs) == 1)
check("信封形狀 {seq,ts,type,data}",
      tg_evs and set(tg_evs[0].keys()) == {"seq", "ts", "type", "data"})

evs_all = _sse_events("/app/v2/events?since_seq=0&follow=false")
tg_all = [e for e in evs_all if e.get("type") == "message.upsert"
          and TG_TEXT in json.dumps(e.get("data") or {}, ensure_ascii=False)]
check("全域訂閱同樣撈得到,信封多帶 session",
      len(tg_all) == 1 and tg_all[0].get("session") == SESSION)
max_seq = max(e["seq"] for e in evs_all)
check("since_seq 補洞語意:since=最大 seq → 無新事件",
      _sse_events(f"/app/v2/events?since_seq={max_seq}&follow=false") == [])

# ── 4. 統一 input:fire-and-forget → 卡片流收尾 + canonical 落地 ─────────
USER_SAID = "統一路由發話測試"
REPLY = "收到,統一路由回覆。"


class _FakeACP:
    def is_busy(self):
        return False


async def _fake_pool_get(session, home):
    return _FakeACP()


async def _fake_prepare(session, content, attachments, stt_lang=""):
    return content, [], f"PROMPT::{content}"


async def _fake_stream(session, prompt):
    yield ("status", {"label": "思考中"})
    yield ("content", REPLY[:3])
    yield ("content", REPLY[3:])


def _canon_texts():
    return [(m["role"], m["content"]) for m in bridge._canon_messages(SESSION, 20)]


with patch.object(bridge.POOL, "get", _fake_pool_get), \
     patch.object(bridge, "_persona_prepare_turn", _fake_prepare), \
     patch.object(bridge, "_persona_content_stream", _fake_stream):
    r = client.post(f"/app/v2/sessions/{SID}/input",
                    json={"content": USER_SAID, "client_id": "cli-s3b2-1"},
                    headers=AUTH)
    check("input 200 + accepted(fire-and-forget)",
          r.status_code == 200 and r.json().get("accepted") is True)
    check("input 回 message_id(user 落 canonical)", bool(r.json().get("message_id")))
    check("input ack 回 content(實收 user turn 正文,app 樂觀泡泡原地替換用)",
          r.json().get("content") == USER_SAID)

    # 背景回合在 TestClient 的事件圈跑;等 canonical 出現 assistant 收尾。
    deadline = time.time() + 8
    while time.time() < deadline:
        if ("assistant", REPLY) in _canon_texts():
            break
        time.sleep(0.1)
    check("回合收尾:assistant 回覆落 canonical", ("assistant", REPLY) in _canon_texts())
    check("user 訊息落 canonical", ("user", USER_SAID) in _canon_texts())

    # 冪等:同 client_id 重送 → replayed,不重跑回合
    r2 = client.post(f"/app/v2/sessions/{SID}/input",
                     json={"content": USER_SAID, "client_id": "cli-s3b2-1"},
                     headers=AUTH)
    check("同 client_id 重送 → replayed(冪等)",
          r2.status_code == 200 and r2.json().get("replayed") is True)
    check("重送後 canonical 不長出第二份",
          sum(1 for t in _canon_texts() if t == ("user", USER_SAID)) == 1)

# 空 body 守門
r = client.post(f"/app/v2/sessions/{SID}/input", json={}, headers=AUTH)
check("空 input → 400", r.status_code == 400)

# 卡片流收尾:user 卡 + turn 卡(final 全文)
r = client.get(f"/app/v2/sessions/{SID}/cards", headers=AUTH)
cards = r.json()["cards"]
user_cards = [c for c in cards if c["role"] == "user"
              and USER_SAID in json.dumps(c, ensure_ascii=False)]
turn_cards = [c for c in cards if c.get("final")
              and REPLY in json.dumps(c, ensure_ascii=False)]
check("user 卡進卡片流(v2 input 即時出卡)", len(user_cards) == 1)
check("turn 卡 final 全文 = 回覆(回覆走 S3 事件流)", len(turn_cards) == 1)

# input 後事件流也長出來(user+assistant 都鏡射進 event_log)
evs2 = _sse_events(f"/app/v2/events?session={SESSION}&since_seq={max_seq}&follow=false")
texts = json.dumps([e.get("data") for e in evs2], ensure_ascii=False)
check("event_log 續增:user 發話在補洞範圍", USER_SAID in texts)
check("event_log 續增:assistant 回覆在補洞範圍", REPLY in texts)

# ── 4b. 語音訊息:STT transcript 折入 content 後,ack 原樣回給 app ────────
# (feat/stt-transcript-echo:app 樂觀泡泡「🎤 語音訊息 · 辨識中…」靠這欄
#  原地替換成辨識文字;transcript 本來就落 canonical user turn,這裡只是
#  把實收正文一併帶回 2xx ack。)
VOICE_TRANSCRIPT = "這是語音辨識出來的字"


async def _fake_prepare_voice(session, content, attachments, stt_lang=""):
    folded = (content + "\n" + VOICE_TRANSCRIPT).strip() if content else VOICE_TRANSCRIPT
    att_meta = [{"kind": "audio", "filename": "voice.m4a", "mime": "audio/m4a",
                 "path": None}]
    return folded, att_meta, f"PROMPT::{folded}"


with patch.object(bridge.POOL, "get", _fake_pool_get), \
     patch.object(bridge, "_persona_prepare_turn", _fake_prepare_voice), \
     patch.object(bridge, "_persona_content_stream", _fake_stream):
    r = client.post(
        f"/app/v2/sessions/{SID}/input",
        json={"content": "", "client_id": "cli-s3b2-voice",
              "attachments": [{"kind": "audio", "filename": "voice.m4a",
                               "mime": "audio/m4a",
                               "data": "data:audio/m4a;base64,AAAA"}]},
        headers=AUTH)
    check("語音 input:ack content = STT transcript(泡泡原地替換用)",
          r.status_code == 200 and r.json().get("content") == VOICE_TRANSCRIPT)
    check("語音 input:message_id 仍在(回顯卡對位鍵)",
          bool(r.json().get("message_id")))

# ── 5. v1 相容:同一段對話老路照看 ───────────────────────────────────────
r = client.get("/app/v1/messages", params={"session": SESSION}, headers=AUTH)
check("v1 messages 200", r.status_code == 200)
v1_text = json.dumps(r.json(), ensure_ascii=False)
check("v1 看得到 TG 訊息", TG_TEXT in v1_text)
check("v1 看得到 v2 發的 user/assistant", USER_SAID in v1_text and REPLY in v1_text)

print("=" * 60)
if fails:
    print(f"{len(fails)} FAILED:", *fails, sep="\n  - ")
    sys.exit(1)
print("ALL PASS")
