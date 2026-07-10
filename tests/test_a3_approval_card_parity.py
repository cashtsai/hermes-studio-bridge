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
3. 冷載 seed:`_cc_card_seed` / `_hp_card_digest` 對 DB pending approval 的
   對回(卡誕生時沒人訂閱 → 之後開卡片流仍看得到 pending 卡)。
4. `_approvals_expire`:TTL 掃過期時對存在中的卡片流同卡收尾(expired)。
5. persona-relay:`_approval_fire_callback("persona-relay:")` → 決定注入
   persona 對話(send 缺席退 label;expired/無 key 不注入)。
6. `approval_create`:persona-relay 驗證、options.send 落庫、push:false
   不疊推播。7. ApprovalCardMixin:kind=question/notice 上卡(加值欄位)。
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


# ─────────────── 3a. _cc_card_seed 冷載對回 DB pending approval ─────────────

_seed_jsonl = os.path.join(_TMP, "cold.jsonl")
open(_seed_jsonl, "w").close()   # 空 jsonl:seed 主體無事可做,只驗 approval 對回


async def _fake_cc_session_jsonl(name, workdir):
    return _seed_jsonl


_insert_row("cc-cold1", "claude_code:ColdOps", "claude_code",
            options=[{"key": "1", "label": "Allow", "style": "primary"}],
            source="claude_code:ColdOps")
_insert_row("cc-cold2", "claude_code:ColdDone", "claude_code", status="approved",
            source="claude_code:ColdDone")

cold_store = cd.SessionCardStore()
done_store = cd.SessionCardStore()
bridge._CC_CARD_STORES = {"ColdOps": cold_store, "ColdDone": done_store}
bridge._CC_APPROVAL_ACTIVE["ColdOps"] = {"aid": "cc-cold1", "sig": "s"}
bridge._CC_APPROVAL_ACTIVE["ColdDone"] = {"aid": "cc-cold2", "sig": "s"}
_orig_cc_session_jsonl = bridge._cc_session_jsonl
bridge._cc_session_jsonl = _fake_cc_session_jsonl
try:
    asyncio.run(bridge._cc_card_seed(cold_store, "ColdOps", "/tmp"))
    cold_card = cold_store.cards.get("card-cc-appr-cc-cold1")
    check("CC 冷載 seed 對回 pending 卡", cold_card is not None and
          cold_card["final"] is False)
    asyncio.run(bridge._cc_card_seed(done_store, "ColdDone", "/tmp"))
    check("CC 冷載 seed 略過非 pending 列",
          done_store.cards.get("card-cc-appr-cc-cold2") is None)
finally:
    bridge._cc_session_jsonl = _orig_cc_session_jsonl
    bridge._CC_APPROVAL_ACTIVE.pop("ColdOps", None)
    bridge._CC_APPROVAL_ACTIVE.pop("ColdDone", None)
    bridge._CC_CARD_STORES = _orig_cc_stores


# ─────────────── 3b. _hp_card_digest 冷載對回 DB pending approval ───────────

_persona_mid = next(iter(bridge.PERSONAS))
_insert_row("hp-cold1", f"hermes:{_persona_mid}", "hermes", kind="question",
            options=[{"key": "ok", "label": "看過了", "style": "primary"}])

_orig_hp_merged = bridge._hp_merged_messages
_orig_ensure_hp = bridge._ensure_hp_card_follower
bridge._hp_merged_messages = lambda session, n: []
bridge._ensure_hp_card_follower = lambda session: None
try:
    d_cold = asyncio.run(bridge._hp_card_digest(_persona_mid))
    hp_cold_card = d_cold.store.cards.get("card-hp-appr-hp-cold1")
    check("HP 冷載 seed 對回 pending 卡", hp_cold_card is not None and
          hp_cold_card["final"] is False)
    check("HP 冷載卡帶 kind=question(加值欄位)", hp_cold_card is not None and
          hp_cold_card["body"].get("kind") == "question")
finally:
    bridge._hp_merged_messages = _orig_hp_merged
    bridge._ensure_hp_card_follower = _orig_ensure_hp
    bridge._HP_CARD_DIGESTS.pop(_persona_mid, None)
    # 清掉 pending 列,避免影響後面 _hermes_pending_by_session 相關驗證
    _insert_row("hp-cold1", f"hermes:{_persona_mid}", "hermes", status="answered")


# ─────────────── 4. _approvals_expire 對存在中的卡片流同卡收尾 ──────────────

_exp_feed_calls = []
_orig_cc_feed2 = bridge._cc_cards_feed_approval
_orig_hp_feed2 = bridge._hp_cards_feed_approval
bridge._cc_cards_feed_approval = (
    lambda name, record, resolved="": _exp_feed_calls.append(("cc", name, record["id"], resolved)))
bridge._hp_cards_feed_approval = (
    lambda sid, record, resolved="": _exp_feed_calls.append(("hp", sid, record["id"], resolved)))

con = sqlite3.connect(bridge.CANON_DB)
past = time.time() - 5
con.execute("INSERT OR REPLACE INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
            "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exp-hp1", "t", "hermes:xcash", "", "", past - 60, past, "pending",
             None, None, None, "hermes:xcash", "hermes", "permission", None))
con.execute("INSERT OR REPLACE INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
            "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exp-cc1", "t", "claude_code:Ops", "", "", past - 60, past, "pending",
             None, None, None, "claude_code:Ops", "claude_code", "permission", None))
try:
    bridge._approvals_expire(con)
    con.commit()
    rows = dict(con.execute(
        "SELECT id, status FROM approvals WHERE id IN ('exp-hp1','exp-cc1')").fetchall())
    check("過期掃描翻 DB 狀態", rows == {"exp-hp1": "expired", "exp-cc1": "expired"})
    check("過期掃描觸發 hp 卡收尾(expired)",
          ("hp", "hermes:xcash", "exp-hp1", "expired") in _exp_feed_calls)
    check("過期掃描觸發 cc 卡收尾(expired,session 名已去前綴)",
          ("cc", "Ops", "exp-cc1", "expired") in _exp_feed_calls)
finally:
    con.close()
    bridge._cc_cards_feed_approval = _orig_cc_feed2
    bridge._hp_cards_feed_approval = _orig_hp_feed2


# ─────────────── 5. persona-relay:決定 → persona 指令注入 ──────────────────

_inject_calls = []


async def _fake_inject(session, content, via):
    _inject_calls.append((session, content, via))


_insert_row("rl-1", f"hermes:{_persona_mid}", "hermes", kind="question",
            options=[{"key": "go", "label": "動工", "style": "primary",
                      "send": "請開始執行方案A"},
                     {"key": "hold", "label": "先不要", "style": "secondary"}])

_orig_inject = bridge._persona_inject_turn
bridge._persona_inject_turn = _fake_inject
try:
    asyncio.run(bridge._approval_fire_callback("rl-1", "persona-relay:",
                                               "answered", "go", key="go"))
    check("persona-relay 注入 send 文字",
          len(_inject_calls) == 1 and "請開始執行方案A" in _inject_calls[0][1]
          and _inject_calls[0][0] == _persona_mid
          and _inject_calls[0][2] == "approval_relay")
    _inject_calls.clear()
    asyncio.run(bridge._approval_fire_callback("rl-1", "persona-relay:",
                                               "answered", "hold", key="hold"))
    check("persona-relay send 缺席退 label",
          len(_inject_calls) == 1 and "先不要" in _inject_calls[0][1])
    _inject_calls.clear()
    asyncio.run(bridge._approval_fire_callback("rl-1", "persona-relay:",
                                               "expired", "", key="go"))
    check("persona-relay expired 不注入", _inject_calls == [])
    asyncio.run(bridge._approval_fire_callback("rl-1", "persona-relay:",
                                               "answered", "", key=""))
    check("persona-relay 無 key 不注入", _inject_calls == [])
finally:
    bridge._persona_inject_turn = _orig_inject


# ─────────────── 6. approval_create:persona-relay 驗證 / send / push ───────

class _FakeReq:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


_push_calls = []
_orig_check_auth = bridge._check_auth
_orig_push = bridge._approval_push
bridge._check_auth = lambda r: None
bridge._approval_push = lambda aid, title, body, sid: _push_calls.append(aid)
try:
    r = asyncio.run(bridge.approval_create(_FakeReq(
        {"id": "cr-1", "title": "審稿", "kind": "question",
         "session_id": f"hermes:{_persona_mid}", "callback_url": "persona-relay:",
         "options": [{"key": "go", "label": "動工", "style": "primary",
                      "send": "請開始執行方案A"}]})))
    row = bridge._approval_get_row("cr-1")
    check("approval_create 收 persona-relay callback", r["status"] == "pending"
          and row is not None)
    check("approval_create options.send 落庫",
          (row.get("options") or [{}])[0].get("send") == "請開始執行方案A")
    check("approval_create 預設會推播", _push_calls == ["cr-1"])

    _push_calls.clear()
    r2 = asyncio.run(bridge.approval_create(_FakeReq(
        {"id": "cr-2", "title": "報告", "kind": "notice",
         "session_id": f"hermes:{_persona_mid}", "push": False})))
    check("approval_create push:false 不疊推播", r2["status"] == "pending"
          and _push_calls == [])

    err = None
    try:
        asyncio.run(bridge.approval_create(_FakeReq(
            {"id": "cr-3", "title": "x", "session_id": "claude_code:Ops",
             "callback_url": "persona-relay:"})))
    except Exception as e:  # noqa: BLE001
        err = e
    check("approval_create persona-relay 非 hermes persona 被擋", err is not None)
finally:
    bridge._check_auth = _orig_check_auth
    bridge._approval_push = _orig_push


# ─────────────── 7. ApprovalCardMixin:kind 加值欄位 ────────────────────────

d7 = cd.PersonaDigest()
d7.handle_approval({"id": "k-1", "title": "問題", "kind": "question"})
d7.handle_approval({"id": "k-2", "title": "許可", "kind": "permission"})
k1 = d7.store.cards.get("card-hp-appr-k-1")
k2 = d7.store.cards.get("card-hp-appr-k-2")
check("mixin kind=question 上卡", k1 is not None and k1["body"].get("kind") == "question")
check("mixin kind=permission 不帶 kind 欄位", k2 is not None and "kind" not in k2["body"])


# ─────────────── 8. A3-3:cron 報告 → kind=notice approval ──────────────────

import hashlib  # noqa: E402


def _ntc_aid(rid):
    return "ntc-" + hashlib.sha1(rid.encode()).hexdigest()[:20]


_orig_notice_jobs = bridge.NOTICE_REPORT_JOBS
bridge.NOTICE_REPORT_JOBS = {"xcash": {"testjob-brief"}}

_rp1 = {"id": "rp-ntc-1", "name": "testjob-brief", "label": "測試晨報",
        "content": "內容" * 300, "ts": time.time()}
try:
    bridge._notice_for_report("xcash", _rp1)
    row = bridge._approval_get_row(_ntc_aid("rp-ntc-1"))
    check("notice:新報告建 pending notice", row is not None and
          row["status"] == "pending" and row["kind"] == "notice" and
          row["session_id"] == "hermes:xcash")
    check("notice:detail 有截斷(200 + _clip_text 後綴)", row is not None and
          len(row.get("detail") or "") <= 240)
    check("notice:單鍵 ack 選項", row is not None and
          [o["key"] for o in (row.get("options") or [])] == ["ack"])

    # 冪等:ack 後重同步不翻回 pending
    asyncio.run(bridge._approval_decide_core(_ntc_aid("rp-ntc-1"), {"key": "ack"}))
    bridge._notice_for_report("xcash", _rp1)
    row2 = bridge._approval_get_row(_ntc_aid("rp-ntc-1"))
    check("notice:同 id 不重建(ack 不被翻回 pending)",
          row2 is not None and row2["status"] == "acknowledged")

    # 名單外 / 過舊 → 不建
    bridge._notice_for_report("xcash", {"id": "rp-ntc-2", "name": "other-job",
                                        "label": "x", "content": "y",
                                        "ts": time.time()})
    check("notice:名單外 job 不建", bridge._approval_get_row(_ntc_aid("rp-ntc-2")) is None)
    bridge._notice_for_report("xcash", {"id": "rp-ntc-3", "name": "testjob-brief",
                                        "label": "x", "content": "y",
                                        "ts": time.time() - 13 * 3600})
    check("notice:過舊報告不建(防回灌)",
          bridge._approval_get_row(_ntc_aid("rp-ntc-3")) is None)

    # 卡片 feed 排回主圈(_MAIN_LOOP + call_soon_threadsafe)
    _ntc_feed_calls = []
    _orig_hp_feed3 = bridge._hp_cards_feed_approval
    bridge._hp_cards_feed_approval = (
        lambda sid, record, resolved="": _ntc_feed_calls.append((sid, record["id"])))

    async def _run_in_loop():
        bridge._MAIN_LOOP = asyncio.get_running_loop()
        bridge._notice_for_report("xcash", {"id": "rp-ntc-4", "name": "testjob-brief",
                                            "label": "測試晨報", "content": "hi",
                                            "ts": time.time()})
        await asyncio.sleep(0.01)
    try:
        asyncio.run(_run_in_loop())
        check("notice:卡片 feed 排回主圈執行",
              _ntc_feed_calls == [("hermes:xcash", _ntc_aid("rp-ntc-4"))])
    finally:
        bridge._hp_cards_feed_approval = _orig_hp_feed3
        bridge._MAIN_LOOP = None

    # _sync_persona_reports 接線:新 upsert 觸發 notice
    _orig_persona_reports = bridge._persona_reports
    _orig_write_mem = bridge._write_report_memory
    bridge._persona_reports = lambda session, limit=20: [
        {"id": "rp-ntc-5", "external_id": "x:5", "external_source": "hermes-cron",
         "session_id": "cron_ab_1", "name": "testjob-brief", "label": "測試晨報",
         "content": "sync 內容", "ts": time.time()}]
    bridge._write_report_memory = lambda session, reports: None
    try:
        bridge._sync_persona_reports("xcash", 50)
        check("notice:_sync_persona_reports 新 upsert 觸發 notice",
              bridge._approval_get_row(_ntc_aid("rp-ntc-5")) is not None)
        bridge._sync_persona_reports("xcash", 50)   # 同內容重同步:無新 upsert
        row5 = bridge._approval_get_row(_ntc_aid("rp-ntc-5"))
        check("notice:重同步不重建", row5 is not None and row5["status"] == "pending")
    finally:
        bridge._persona_reports = _orig_persona_reports
        bridge._write_report_memory = _orig_write_mem
finally:
    bridge.NOTICE_REPORT_JOBS = _orig_notice_jobs


print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
