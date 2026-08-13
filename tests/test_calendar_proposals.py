"""行事曆/提醒「提案」—— agent 只能提議,寫入權在裝置上的使用者手裡。

涵蓋:
- **卡片形狀**:kind=calendar_event、proposal_id/state/tz 齊全、
  `fallback_text` 必存在(舊 client 只認得它)。
- **驗證**:缺 tz / 壞 tz / end<=start / 荒謬 epoch(1970、毫秒當秒)/
  標題過長 / 壞 target / 壞 recurrence / 壞 alarm → 400 + zh-TW 訊息。
- **round-trip**:resolve 更新同一張卡(rev++、state、fallback_text 換人話),
  且**冪等**(第二次回第一次的結果、不再改卡)。
- **未知 id → 404**(索引不跨重啟 —— app 必須優雅處理)。
- **旗標**:CALENDAR_PROPOSALS 未開 → 兩個端點都 404、卡片流不會出現這個 kind。
- **MCP 工具**:payload 挑對欄位、target 正確、bridge 連不上時安靜回錯不炸。
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="calendar-proposals-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB",
                      os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("HARNESS_DB", os.path.join(_TMP, "harness.db"))
os.environ.setdefault("OPENCLAW_CONFIG_FILE",
                      os.path.join(_TMP, "openclaw.json"))
os.environ.setdefault("POCKET_CODEX_HOME", os.path.join(_TMP, "codex-home"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import bridge  # noqa: E402
import carddigest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

SESSION = "claude_code:tirith"
TPE = "Asia/Taipei"
# 2026-08-14 15:00 Asia/Taipei = 2026-08-14T07:00:00Z
START = 1786690800.0
END = START + 3600.0


class FakeRequest:
    headers = {"authorization": "Bearer test-unit-token"}
    client = None

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


class _Env:
    """提案端點的標準 patch 組:旗標開、session store 用假的。"""

    def __init__(self, extra_env=None, known=(SESSION,)):
        env = {"CALENDAR_PROPOSALS": "1"}
        env.update(extra_env or {})
        self.stores = {}
        self.known = set(known)

        async def _store_for(sid):
            if sid not in self.known:
                raise bridge.http_err(404, "SESSION_NOT_FOUND",
                                      "unknown session")
            if sid not in self.stores:
                self.stores[sid] = carddigest.SessionCardStore()
            return self.stores[sid]

        self._patches = [patch.dict(os.environ, env),
                         patch.object(bridge, "_v2_card_store",
                                      side_effect=_store_for)]

    def __enter__(self):
        bridge._CALENDAR_PROPOSALS.clear()
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()

    def cards(self, sid=SESSION):
        store = self.stores.get(sid)
        return list(store.cards.values()) if store else []

    def card(self, proposal_id, sid=SESSION):
        return self.stores[sid].cards[f"card-{proposal_id}"]

    def events(self, sid=SESSION):
        store = self.stores.get(sid)
        return list(store.events) if store else []


def _propose(body, session=SESSION):
    return asyncio.run(bridge.v2_propose_calendar(session, FakeRequest(body)))


def _resolve(proposal_id, body):
    return asyncio.run(bridge.v2_resolve_proposal(proposal_id,
                                                  FakeRequest(body)))


def _ok_event(**over):
    body = {"target": "calendar", "title": "與王總開會", "start": START,
            "end": END, "tz": TPE}
    body.update(over)
    return body


def _expect_400(testcase, body, needle=""):
    with testcase.assertRaises(HTTPException) as cm:
        _propose(body)
    testcase.assertEqual(cm.exception.status_code, 400,
                         f"{body} 應該被擋下")
    testcase.assertEqual(getattr(cm.exception, "code", ""),
                         "CALENDAR_PROPOSAL_INVALID")
    msg = getattr(cm.exception, "message", "") or str(cm.exception.detail)
    if needle:
        testcase.assertIn(needle, msg)
    return msg


# ───────────────────────── 卡片形狀 ─────────────────────────

class TestCardShape(unittest.TestCase):
    def test_calendar_card_has_contract_fields(self):
        with _Env() as env:
            res = _propose(_ok_event(notes="帶上季報表", location="台北 101",
                                     alarm_minutes_before=[10]))
            pid = res["proposal_id"]
            self.assertTrue(pid.startswith("cal-"))
            self.assertEqual(len(pid), len("cal-") + 8)
            self.assertEqual(res["state"], "proposed")
            cards = env.cards()
            self.assertEqual(len(cards), 1)
            card = cards[0]
            self.assertEqual(card["kind"], "calendar_event")
            self.assertEqual(card["id"], f"card-{pid}")
            self.assertTrue(card["id"].startswith("card-cal-"))
            self.assertEqual(card["role"], "assistant")
            b = card["body"]
            self.assertEqual(b["proposal_id"], pid)
            self.assertEqual(b["state"], "proposed")
            self.assertEqual(b["target"], "calendar")
            self.assertEqual(b["title"], "與王總開會")
            self.assertEqual(b["start"], START)
            self.assertEqual(b["end"], END)
            self.assertEqual(b["tz"], TPE)
            self.assertFalse(b["all_day"])
            self.assertEqual(b["recurrence"], "none")
            self.assertEqual(b["alarm_minutes_before"], [10])
            self.assertEqual(b["notes"], "帶上季報表")
            self.assertEqual(b["location"], "台北 101")

    def test_fallback_text_is_always_present_and_human(self):
        """舊 client 只渲染 fallback_text —— 缺了就是白卡。"""
        with _Env() as env:
            pid = _propose(_ok_event())["proposal_id"]
            fb = env.card(pid)["body"]["fallback_text"]
            self.assertTrue(fb)
            self.assertIn("建議行程", fb)
            self.assertIn("與王總開會", fb)
            self.assertIn("8/14", fb)          # 在 Asia/Taipei 是 8/14 不是 8/13
            self.assertIn("15:00", fb)
            self.assertIn("16:00", fb)

    def test_fallback_uses_proposal_tz_not_server_tz(self):
        with _Env() as env:
            pid = _propose(_ok_event(tz="America/New_York"))["proposal_id"]
            fb = env.card(pid)["body"]["fallback_text"]
            self.assertIn("03:00", fb)         # 同一 epoch,紐約是凌晨三點
            self.assertIn("8/14", fb)

    def test_reminder_shape(self):
        with _Env() as env:
            res = _propose({"target": "reminder", "title": "繳電費",
                            "due": START, "tz": TPE})
            b = env.card(res["proposal_id"])["body"]
            self.assertEqual(b["target"], "reminder")
            self.assertEqual(b["due"], START)
            self.assertNotIn("end", b)         # 提醒沒有結束時間
            self.assertIn("建議提醒", b["fallback_text"])
            self.assertIn("繳電費", b["fallback_text"])

    def test_calendar_end_defaults_to_plus_one_hour(self):
        with _Env() as env:
            pid = _propose({"title": "站立會議", "start": START,
                            "tz": TPE})["proposal_id"]
            self.assertEqual(env.card(pid)["body"]["end"], START + 3600.0)

    def test_all_day_event_keeps_no_synthetic_end(self):
        with _Env() as env:
            pid = _propose({"title": "年假", "start": START, "all_day": True,
                            "tz": TPE})["proposal_id"]
            b = env.card(pid)["body"]
            self.assertTrue(b["all_day"])
            self.assertNotIn("end", b)
            self.assertNotIn(":", b["fallback_text"].split("整天")[0][-6:])

    def test_reminder_without_due_falls_back_to_start(self):
        with _Env() as env:
            pid = _propose({"target": "reminder", "title": "回電",
                            "start": START, "tz": TPE})["proposal_id"]
            self.assertEqual(env.card(pid)["body"]["due"], START)

    def test_recurrence_shows_in_fallback(self):
        with _Env() as env:
            pid = _propose(_ok_event(recurrence="weekly"))["proposal_id"]
            self.assertIn("每週", env.card(pid)["body"]["fallback_text"])

    def test_card_upsert_event_is_pushed(self):
        with _Env() as env:
            _propose(_ok_event())
            evs = [e for e in env.events() if e["type"] == "card.upsert"]
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["data"]["card"]["kind"], "calendar_event")

    def test_agent_supplied_fallback_text_wins(self):
        with _Env() as env:
            pid = _propose(_ok_event(
                fallback_text="📅 老王的會,別遲到"))["proposal_id"]
            self.assertEqual(env.card(pid)["body"]["fallback_text"],
                             "📅 老王的會,別遲到")

    def test_unknown_session_is_404(self):
        with _Env():
            with self.assertRaises(HTTPException) as cm:
                _propose(_ok_event(), session="claude_code:nope")
            self.assertEqual(cm.exception.status_code, 404)

    def test_extra_keys_from_agent_are_dropped(self):
        """agent 亂加的欄位不進卡片(契約是白名單,不是黑名單)。"""
        with _Env() as env:
            pid = _propose(_ok_event(
                attendees=["a@b.c"], calendar_id="hack",
                state="accepted", proposal_id="cal-forged"))["proposal_id"]
            b = env.card(pid)["body"]
            self.assertNotIn("attendees", b)
            self.assertNotIn("calendar_id", b)
            self.assertEqual(b["proposal_id"], pid)     # 不接受 agent 自訂 id
            self.assertEqual(b["state"], "proposed")    # 更不接受自封 accepted


# ───────────────────────── 驗證 ─────────────────────────

class TestValidation(unittest.TestCase):
    def test_missing_tz_rejected(self):
        with _Env():
            msg = _expect_400(self, {"title": "會議", "start": START}, "tz")
            self.assertIn("必填", msg)

    def test_bad_tz_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(tz="Mars/Olympus"), "IANA")
            _expect_400(self, _ok_event(tz="GMT+8"), "IANA")

    def test_end_not_after_start_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(end=START), "end 必須晚於 start")
            _expect_400(self, _ok_event(end=START - 1), "end 必須晚於 start")

    def test_absurd_epochs_rejected(self):
        with _Env():
            for bad in (0, -1, 946684799.0, 4102444801.0, 1.0e18):
                _expect_400(self, _ok_event(start=bad, end=None), "範圍")
            # 毫秒當秒是最常見的模型錯誤 —— 一定要擋
            _expect_400(self, _ok_event(start=START * 1000, end=None), "毫秒")

    def test_non_finite_epoch_rejected(self):
        with _Env():
            for bad in (float("nan"), float("inf"), float("-inf")):
                _expect_400(self, _ok_event(start=bad, end=None))
            _expect_400(self, _ok_event(start="下週三", end=None), "epoch")
            _expect_400(self, _ok_event(start=True, end=None), "epoch")

    def test_missing_start_for_calendar_rejected(self):
        with _Env():
            _expect_400(self, {"title": "會議", "tz": TPE}, "start 必填")

    def test_reminder_without_due_or_start_rejected(self):
        with _Env():
            _expect_400(self, {"target": "reminder", "title": "沒時間的提醒",
                               "tz": TPE}, "due 或 start")

    def test_end_without_start_rejected(self):
        with _Env():
            _expect_400(self, {"target": "reminder", "title": "怪提醒",
                               "due": START, "end": END, "tz": TPE},
                        "給了 end")

    def test_oversized_title_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(title="長" * 201), "title 過長")
            # 剛好 200 要過
            self.assertTrue(_propose(_ok_event(title="長" * 200))["ok"])

    def test_empty_title_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(title="   "), "title 必填")

    def test_oversized_notes_and_location_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(notes="字" * 2001), "notes 過長")
            _expect_400(self, _ok_event(location="地" * 201), "location 過長")

    def test_bad_target_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(target="google_calendar"), "target")

    def test_bad_recurrence_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(recurrence="yearly"), "recurrence")

    def test_bad_alarms_rejected(self):
        with _Env():
            _expect_400(self, _ok_event(alarm_minutes_before=10), "陣列")
            _expect_400(self, _ok_event(alarm_minutes_before=["十分鐘"]), "數字")
            _expect_400(self, _ok_event(alarm_minutes_before=[-5]), "0–")
            _expect_400(self, _ok_event(alarm_minutes_before=[999999]), "0–")
            _expect_400(self, _ok_event(alarm_minutes_before=[1, 2, 3, 4, 5, 6]),
                        "最多")

    def test_rejected_proposal_writes_no_card(self):
        with _Env() as env:
            _expect_400(self, _ok_event(tz=""))
            self.assertEqual(env.cards(), [])
            self.assertFalse(bridge._CALENDAR_PROPOSALS)


# ───────────────────────── 確認 round-trip ─────────────────────────

class TestResolve(unittest.TestCase):
    def test_accept_updates_card(self):
        with _Env() as env:
            pid = _propose(_ok_event())["proposal_id"]
            rev0 = env.card(pid)["rev"]
            res = _resolve(pid, {"state": "accepted",
                                 "calendar_event_id": "EK-123"})
            self.assertTrue(res["ok"])
            self.assertEqual(res["state"], "accepted")
            self.assertFalse(res["already_resolved"])
            card = env.card(pid)
            self.assertGreater(card["rev"], rev0)
            self.assertEqual(card["body"]["state"], "accepted")
            self.assertEqual(card["body"]["calendar_event_id"], "EK-123")
            self.assertIn("已加入行事曆", card["body"]["fallback_text"])
            self.assertIn("與王總開會", card["body"]["fallback_text"])
            # 提案內容不會在確認後被抹掉
            self.assertEqual(card["body"]["start"], START)

    def test_accept_reminder_says_reminders(self):
        with _Env() as env:
            pid = _propose({"target": "reminder", "title": "繳電費",
                            "due": START, "tz": TPE})["proposal_id"]
            _resolve(pid, {"state": "accepted", "calendar_event_id": "EK-9"})
            self.assertIn("已加入提醒事項",
                          env.card(pid)["body"]["fallback_text"])

    def test_decline_updates_card(self):
        with _Env() as env:
            pid = _propose(_ok_event())["proposal_id"]
            res = _resolve(pid, {"state": "declined"})
            self.assertEqual(res["state"], "declined")
            body = env.card(pid)["body"]
            self.assertEqual(body["state"], "declined")
            self.assertIn("已略過", body["fallback_text"])
            self.assertNotIn("calendar_event_id", body)

    def test_error_is_carried_into_card(self):
        with _Env() as env:
            pid = _propose(_ok_event())["proposal_id"]
            _resolve(pid, {"state": "declined", "error": "沒有行事曆權限"})
            self.assertEqual(env.card(pid)["body"]["error"], "沒有行事曆權限")

    def test_resolve_is_idempotent(self):
        with _Env() as env:
            pid = _propose(_ok_event())["proposal_id"]
            first = _resolve(pid, {"state": "accepted",
                                   "calendar_event_id": "EK-1"})
            rev_after_first = env.card(pid)["rev"]
            second = _resolve(pid, {"state": "declined",
                                    "calendar_event_id": "EK-2"})
            self.assertTrue(second["already_resolved"])
            self.assertEqual(second["state"], first["state"])
            self.assertEqual(second["calendar_event_id"], "EK-1")
            card = env.card(pid)
            self.assertEqual(card["rev"], rev_after_first)   # 沒有再改卡
            self.assertEqual(card["body"]["state"], "accepted")
            self.assertEqual(card["body"]["calendar_event_id"], "EK-1")

    def test_unknown_proposal_is_404(self):
        with _Env():
            with self.assertRaises(HTTPException) as cm:
                _resolve("cal-deadbeef", {"state": "accepted"})
            self.assertEqual(cm.exception.status_code, 404)
            self.assertEqual(getattr(cm.exception, "code", ""),
                             "CALENDAR_PROPOSAL_NOT_FOUND")
            self.assertIn("再請 agent 提一次",
                          getattr(cm.exception, "message", ""))

    def test_bad_state_rejected(self):
        with _Env():
            pid = _propose(_ok_event())["proposal_id"]
            for bad in ({}, {"state": "proposed"}, {"state": "maybe"}):
                with self.assertRaises(HTTPException) as cm:
                    _resolve(pid, bad)
                self.assertEqual(cm.exception.status_code, 400)

    def test_index_is_bounded(self):
        with _Env(extra_env={"CALENDAR_PROPOSAL_MAX": "16"}):
            ids = [_propose(_ok_event())["proposal_id"] for _ in range(20)]
            self.assertEqual(len(bridge._CALENDAR_PROPOSALS), 16)
            with self.assertRaises(HTTPException) as cm:  # 最舊的被擠掉
                _resolve(ids[0], {"state": "accepted"})
            self.assertEqual(cm.exception.status_code, 404)
            self.assertTrue(_resolve(ids[-1], {"state": "accepted"})["ok"])


# ───────────────────────── 旗標 ─────────────────────────

class TestFlag(unittest.TestCase):
    def test_default_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALENDAR_PROPOSALS", None)
            self.assertFalse(bridge._calendar_proposals_enabled())

    def test_disabled_returns_404_and_emits_no_card(self):
        with _Env(extra_env={"CALENDAR_PROPOSALS": "0"}) as env:
            with self.assertRaises(HTTPException) as cm:
                _propose(_ok_event())
            self.assertEqual(cm.exception.status_code, 404)
            self.assertEqual(getattr(cm.exception, "code", ""),
                             "CALENDAR_PROPOSALS_DISABLED")
            with self.assertRaises(HTTPException) as cm:
                _resolve("cal-12345678", {"state": "accepted"})
            self.assertEqual(cm.exception.status_code, 404)
            # 關著就是完全不存在:沒有卡、沒有 calendar_event kind
            self.assertEqual(env.cards(), [])
            self.assertEqual(
                [c for c in env.cards() if c["kind"] == "calendar_event"], [])

    def test_capability_advertised_only_when_on(self):
        req = FakeRequest()
        with patch.dict(os.environ, {"CALENDAR_PROPOSALS": "1"}):
            self.assertIn("calendar_proposals",
                          asyncio.run(bridge.capabilities(req))["features"])
        with patch.dict(os.environ, {"CALENDAR_PROPOSALS": "0"}):
            self.assertNotIn("calendar_proposals",
                             asyncio.run(bridge.capabilities(req))["features"])


# ───────────────────────── MCP 工具 ─────────────────────────

def _load_mcp():
    path = os.path.join(_ROOT, "scripts", "agent-call-mcp.py")
    spec = importlib.util.spec_from_file_location("agent_call_mcp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMcpTools(unittest.TestCase):
    def setUp(self):
        self.mcp = _load_mcp()
        self.mcp.SELF_ID = "claude_code:tirith"

    def test_tools_are_listed(self):
        names = {t["name"] for t in self.mcp.TOOLS}
        self.assertIn("propose_calendar_event", names)
        self.assertIn("propose_reminder", names)
        for t in self.mcp.TOOLS:
            if t["name"] == "propose_calendar_event":
                self.assertEqual(set(t["inputSchema"]["required"]),
                                 {"title", "start", "tz"})
            if t["name"] == "propose_reminder":
                self.assertEqual(set(t["inputSchema"]["required"]),
                                 {"title", "tz"})

    def _capture(self, name, args):
        seen = {}

        def _fake_http(method, path, body=None, timeout=630.0):
            seen.update(method=method, path=path, body=body)
            return {"ok": True, "proposal_id": "cal-abcd1234"}

        with patch.object(self.mcp, "_http", _fake_http):
            out = self.mcp._tool_call(name, args)
        return seen, out

    def test_calendar_payload(self):
        seen, out = self._capture("propose_calendar_event", {
            "title": "與王總開會", "start": START, "end": END, "tz": TPE,
            "notes": "帶報表", "alarm_minutes_before": [10]})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(
            seen["path"],
            "/app/v2/sessions/claude_code:tirith/proposals/calendar")
        self.assertEqual(seen["body"], {
            "target": "calendar", "title": "與王總開會", "start": START,
            "end": END, "tz": TPE, "notes": "帶報表",
            "alarm_minutes_before": [10]})
        self.assertFalse(out["isError"])
        self.assertIn("cal-abcd1234", out["text"])

    def test_reminder_payload_and_target(self):
        seen, _ = self._capture("propose_reminder",
                                {"title": "繳電費", "due": START, "tz": TPE})
        self.assertEqual(seen["body"]["target"], "reminder")
        self.assertNotIn("start", seen["body"])
        self.assertNotIn("end", seen["body"])

    def test_empty_fields_are_not_sent(self):
        """空值不上送 —— 讓 bridge 的預設值生效,不要送 '' 去撞驗證。"""
        seen, _ = self._capture("propose_calendar_event", {
            "title": "站立會議", "start": START, "tz": TPE,
            "notes": "", "location": None, "alarm_minutes_before": []})
        self.assertEqual(set(seen["body"]),
                         {"target", "title", "start", "tz"})

    def test_session_override(self):
        seen, _ = self._capture("propose_reminder", {
            "title": "繳電費", "tz": TPE, "due": START,
            "session": "hermes:yuanfang"})
        self.assertIn("hermes:yuanfang", seen["path"])

    def test_bridge_unreachable_fails_quietly(self):
        """bridge 沒開/沒旗標時不能炸掉 agent 的回合,只回一則錯誤文字。"""
        def _boom(req, timeout=0):
            raise OSError("Connection refused")

        with patch.object(self.mcp.urllib.request, "urlopen", _boom):
            out = self.mcp._tool_call("propose_calendar_event", {
                "title": "會議", "start": START, "tz": TPE})
        self.assertTrue(out["isError"])
        self.assertIn("bridge 連不上", out["text"])
        self.assertEqual(json.loads(out["text"])["_http_error"], 0)


if __name__ == "__main__":
    unittest.main()
