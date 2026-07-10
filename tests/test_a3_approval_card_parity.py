"""A3 approval 卡片流三 provider 補齊 — 收尾驗證(spec §4/§6 A3)。

裸 assert 風格,仿 test_carddigest_s2.py:不依賴 pytest fixture,可直接
`python3 tests/test_a3_approval_card_parity.py` 執行。

跑法同 test_approval_hub_a1.py:POCKET_CANON_DB 指到 tmp 庫再 import bridge
(import 本身會跑 _canon_init())。

涵蓋:
1. `_cc_cards_feed_approval` / `_hp_cards_feed_approval` 直接呼叫 —— 假的
   `_CC_CARD_STORES` / `_HP_CARD_DIGESTS` 物件(真正的 carddigest 物件,只是
   不透過 bridge 正常掛載路徑登記),驗證 pending 卡真的寫進 ring buffer、
   resolved 呼叫真的 upsert 成同一張卡並清空 options。
2. `_approval_decide_core` 的 claude_code: 分支與 fallback(hermes)分支 ——
   monkeypatch 掉 tmux 相關呼叫,驗證決定發生時真的觸發了對應的
   `_cc_cards_feed_approval` / `_hp_cards_feed_approval` resolve 呼叫
   (不起真的 tmux)。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="a3-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表 + migration)
import carddigest as cd  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def _insert_row(aid, session_id, provider, kind="permission", options=None,
                 status="pending", source=None):
    con = sqlite3.connect(bridge.CANON_DB)
    now = time.time()
    con.execute(
        "INSERT OR REPLACE INTO approvals"
        "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
        "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, f"t-{aid}", source or session_id, "", "", now, now + 600, status,
         None, None, None, session_id, provider, kind,
         json.dumps(options, ensure_ascii=False) if options else None))
    con.commit()
    con.close()


# ───────────────────────── 1a. _cc_cards_feed_approval 直接呼叫 ─────────────

_orig_cc_stores = bridge._CC_CARD_STORES
_orig_hp_digests = bridge._HP_CARD_DIGESTS

cc_store = cd.SessionCardStore()
bridge._CC_CARD_STORES = {"Ops": cc_store}   # 假的登記表 —— 只有 Ops 有人訂閱

cc_rec = {"id": "cc-t1", "title": "wants to run rm", "detail": "rm -rf x",
          "options": [{"key": "1", "label": "Allow", "style": "primary"},
                      {"key": "2", "label": "Don't allow", "style": "danger"}]}
bridge._cc_cards_feed_approval("Ops", cc_rec)
cc_card = cc_store.cards.get("card-cc-appr-cc-t1")
check("CC pending 卡寫進 ring buffer", cc_card is not None)
check("CC pending 卡 options 正確", cc_card is not None and
      [o["key"] for o in cc_card["body"]["options"]] == ["1", "2"])
check("CC pending 卡 source=claude_code", cc_card is not None and
      cc_card["body"]["source"] == "claude_code")
check("CC pending 卡 final=False(未決)", cc_card is not None and cc_card["final"] is False)

# 沒登記的 session 是安全 no-op(不建 store、不炸)
bridge._cc_cards_feed_approval("NotSubscribed", cc_rec)
check("CC 未登記 session 是 no-op", "NotSubscribed" not in bridge._CC_CARD_STORES)

# resolve → 同一張卡 upsert 成 resolved、options 清空
bridge._cc_cards_feed_approval("Ops", cc_rec, resolved="approved")
cc_card2 = cc_store.cards.get("card-cc-appr-cc-t1")
check("CC resolve 是同一張卡(id 不變)", cc_card2 is not None and
      cc_card2["id"] == "card-cc-appr-cc-t1")
check("CC resolve 後 options 清空", cc_card2 is not None and cc_card2["body"]["options"] == [])
check("CC resolve 後 resolved=approved", cc_card2 is not None and
      cc_card2["body"]["resolved"] == "approved")
check("CC resolve 後 final=True", cc_card2 is not None and cc_card2["final"] is True)
check("CC resolve 後 rev 遞增(同卡覆蓋,非新卡)", cc_card2["rev"] > cc_card["rev"])


# ───────────────────────── 1b. _hp_cards_feed_approval 直接呼叫 ─────────────

hp_digest = cd.PersonaDigest()
bridge._HP_CARD_DIGESTS = {"shuijing": hp_digest}   # 假的登記表

hp_rec = {"id": "hp-t1", "title": "需要核准的事", "detail": "detail here"}
bridge._hp_cards_feed_approval("hermes:shuijing", hp_rec)
hp_card = hp_digest.store.cards.get("card-hp-appr-hp-t1")
check("HP pending 卡寫進 ring buffer", hp_card is not None)
check("HP pending 卡 source=hermes", hp_card is not None and
      hp_card["body"]["source"] == "hermes")
check("HP pending 卡 options 走預設 allow/deny(record 未宣告)", hp_card is not None and
      [o["key"] for o in hp_card["body"]["options"]] == ["approve", "deny"])
check("HP prompt 旗標同步(status label=等待核准)", hp_digest.prompt == "需要核准的事" and
      hp_digest.store.status["label"] == "等待核准")

# 非 hermes: 前綴的 session_id 安全 no-op(不動任何 persona digest)
bridge._hp_cards_feed_approval("claude_code:Ops", hp_rec)
check("HP 非 hermes: 前綴是 no-op(未新增卡)", len(hp_digest.store.cards) == 1)

# 沒登記的 persona 是安全 no-op
bridge._hp_cards_feed_approval("hermes:not_subscribed", hp_rec)
check("HP 未登記 persona 是 no-op", "not_subscribed" not in bridge._HP_CARD_DIGESTS)

bridge._hp_cards_feed_approval("hermes:shuijing", hp_rec, resolved="answered")
hp_card2 = hp_digest.store.cards.get("card-hp-appr-hp-t1")
check("HP resolve 是同一張卡(id 不變)", hp_card2 is not None and
      hp_card2["id"] == "card-hp-appr-hp-t1")
check("HP resolve 後 options 清空", hp_card2 is not None and hp_card2["body"]["options"] == [])
check("HP resolve 後 resolved=answered", hp_card2 is not None and
      hp_card2["body"]["resolved"] == "answered")
check("HP prompt 旗標清掉(status label 回待命)", hp_digest.prompt is None and
      hp_digest.store.status["label"] == "待命")

bridge._CC_CARD_STORES = _orig_cc_stores
bridge._HP_CARD_DIGESTS = _orig_hp_digests


# ─────────────── 2a. _approval_decide_core 的 claude_code: 分支接線 ─────────

_cc_feed_calls = []


def _fake_cc_feed(name, record, resolved=""):
    _cc_feed_calls.append((name, record.get("id") if record else None, resolved))


async def _fake_cc_status_core(name):
    return {"busy": True, "running": True, "mode": None,
            "prompt": {"kind": "menu", "options": [
                {"key": "1", "label": "Allow", "style": "primary"},
                {"key": "2", "label": "Don't allow", "style": "danger"}]}}


_cc_key_calls = []


async def _fake_cc_key_core(name, key):
    _cc_key_calls.append((name, key))
    return {"ok": True}


_insert_row("cc-int1", "claude_code:Ops", "claude_code", kind="permission",
            options=[{"key": "1", "label": "Allow", "style": "primary"},
                     {"key": "2", "label": "Don't allow", "style": "danger"}],
            source="claude_code:Ops")
bridge._CC_APPROVAL_ACTIVE["Ops"] = {"aid": "cc-int1", "sig": "sig1"}

_orig_cc_feed = bridge._cc_cards_feed_approval
_orig_cc_status_core = bridge._cc_status_core
_orig_cc_key_core = bridge._cc_key_core
bridge._cc_cards_feed_approval = _fake_cc_feed
bridge._cc_status_core = _fake_cc_status_core
bridge._cc_key_core = _fake_cc_key_core
try:
    r = asyncio.run(bridge._approval_decide_core("cc-int1", {"key": "1"}))
    check("CC 分支決定回傳 approved", r["status"] == "approved")
    check("CC 分支真的送了 tmux 鍵(mock)", _cc_key_calls == [("Ops", "1")])
    check("CC 分支決定後觸發 resolve 卡片呼叫",
          _cc_feed_calls == [("Ops", "cc-int1", "approved")])
finally:
    bridge._cc_cards_feed_approval = _orig_cc_feed
    bridge._cc_status_core = _orig_cc_status_core
    bridge._cc_key_core = _orig_cc_key_core
    bridge._CC_APPROVAL_ACTIVE.pop("Ops", None)


# ─────────────── 2b. _approval_decide_core 的 fallback(hermes)分支接線 ─────

_hp_feed_calls = []


def _fake_hp_feed(session_id, record, resolved=""):
    _hp_feed_calls.append((session_id, record.get("id") if record else None, resolved))


_insert_row("hp-int1", "hermes:xcash", "hermes", kind="permission")

_orig_hp_feed = bridge._hp_cards_feed_approval
bridge._hp_cards_feed_approval = _fake_hp_feed
try:
    r = asyncio.run(bridge._approval_decide_core("hp-int1", {"key": "approve"}))
    check("hermes fallback 分支決定回傳 approved", r["status"] == "approved")
    check("hermes fallback 分支決定後觸發 resolve 卡片呼叫",
          _hp_feed_calls == [("hermes:xcash", "hp-int1", "approved")])
finally:
    bridge._hp_cards_feed_approval = _orig_hp_feed


print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
