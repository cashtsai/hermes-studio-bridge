"""CC↔CX 接力 v1(ContextTransfer)—— 摘要萃取 + transfer 狀態機 + 端點契約。

涵蓋:
- **摘要萃取**(t3code_runtime 純函式層):event_log card.milestone →
  回合分組(user 開回合、最後一張 assistant 終態卡=最終回覆)、同文字
  user 回聲卡去重、max_turns 截窗、單段/整段截斷留痕(「…[截斷]」/
  「較早內容已截斷」+ truncated=True)、無里程碑 → 「無歷史脈絡」照樣組。
- **transfer 狀態機**(端點層):成功 → 目標 provider 開新會話(首訊=
  交接 prompt)+ rebind(舊綁定 detached、reason="transfer")+ 落
  `thread.transferred` 事件(from/to);目標建立失敗 → 502、原 binding
  原封不動、不落事件;rebind 撞 active 綁定 → 409。
- **雙 provider 方向**:codex(monkeypatch codex_session_create)與
  cc2(monkeypatch v2_cc2_create + cc_sdk registry)。
- **auth**:壞 token 401;無 binding/未知 thread 404;壞 provider/mode 400。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid

_TMP = tempfile.mkdtemp(prefix="context-transfer-test-")
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
import t3code_runtime as t3  # noqa: E402
from fastapi import HTTPException  # noqa: E402


_EVENT_LOG_DDL = """CREATE TABLE IF NOT EXISTS event_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    type TEXT NOT NULL,
    external_id TEXT UNIQUE,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL)"""


def _seed_milestones(db_path: str, session: str, items) -> None:
    """items: list of (role, text)。落成 card.milestone(final 終態卡)。"""
    con = sqlite3.connect(db_path)
    try:
        con.execute(_EVENT_LOG_DDL)
        for role, text in items:
            card = {"id": f"c-{uuid.uuid4().hex[:8]}", "turn_id": "t",
                    "role": role, "kind": "text", "rev": 1, "final": True,
                    "body": {"text": text, "fallback_text": text}}
            con.execute(
                "INSERT INTO event_log(session,type,external_id,payload,created_at)"
                " VALUES(?,?,?,?,?)",
                (session, "card.milestone", None,
                 json.dumps({"card": card}, ensure_ascii=False), time.time()))
        con.commit()
    finally:
        con.close()


# ───────────────────────── 摘要萃取(t3code_runtime)─────────────────────────


class TransferPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "canonical.db")
        t3.ensure_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_turn_grouping_takes_final_reply(self):
        _seed_milestones(self.db, "codex:src", [
            ("user", "幫我修 bug A"),
            ("assistant", "跑了測試(工具摘要卡)"),
            ("assistant", "bug A 修好了,commit abc123"),
            ("user", "再加個測試"),
            ("assistant", "測試加好了"),
        ])
        turns = t3.extract_transfer_turns(self.db, "codex:src")
        self.assertEqual(len(turns), 2)
        # 回合內最後一張 assistant 終態卡才是最終回覆,中途工具摘要被覆蓋。
        self.assertEqual(turns[0], {"user": "幫我修 bug A",
                                    "reply": "bug A 修好了,commit abc123"})
        self.assertEqual(turns[1], {"user": "再加個測試",
                                    "reply": "測試加好了"})
        out = t3.build_transfer_prompt(self.db, "codex:src", "codex")
        self.assertIn("幫我修 bug A", out["prompt"])
        self.assertIn("測試加好了", out["prompt"])
        self.assertNotIn("工具摘要卡", out["prompt"])
        self.assertIn("【回合 2】", out["prompt"])
        self.assertFalse(out["truncated"])
        self.assertEqual(out["turns"], 2)

    def test_duplicate_user_echo_is_merged(self):
        # input.accepted 回聲卡 + transcript 合併可能各落一筆同文字 user 卡。
        _seed_milestones(self.db, "codex:echo", [
            ("user", "同一句話"),
            ("user", "同一句話"),
            ("assistant", "收到"),
        ])
        turns = t3.extract_transfer_turns(self.db, "codex:echo")
        self.assertEqual(turns, [{"user": "同一句話", "reply": "收到"}])

    def test_assistant_without_user_opens_turn(self):
        _seed_milestones(self.db, "cc2:lone", [("assistant", "孤兒回覆")])
        turns = t3.extract_transfer_turns(self.db, "cc2:lone")
        self.assertEqual(turns, [{"user": "", "reply": "孤兒回覆"}])
        out = t3.build_transfer_prompt(self.db, "cc2:lone", "cc2")
        self.assertIn("孤兒回覆", out["prompt"])

    def test_max_turns_keeps_newest(self):
        items = []
        for i in range(10):
            items.append(("user", f"請求{i}"))
            items.append(("assistant", f"回覆{i}"))
        _seed_milestones(self.db, "codex:many", items)
        turns = t3.extract_transfer_turns(self.db, "codex:many", max_turns=3)
        self.assertEqual([t["user"] for t in turns], ["請求7", "請求8", "請求9"])

    def test_no_history_prompt_is_graceful(self):
        # 連 event_log 表都沒有(純 t3 schema 新庫)也不炸 —— 註明無歷史照樣轉。
        out = t3.build_transfer_prompt(self.db, "codex:ghost", "codex")
        self.assertIn("無歷史脈絡", out["prompt"])
        self.assertEqual(out["turns"], 0)
        self.assertFalse(out["truncated"])
        self.assertIn("交接", out["prompt"])

    def test_long_single_text_is_clipped_with_marker(self):
        _seed_milestones(self.db, "codex:fat", [
            ("user", "塞爆它"), ("assistant", "回" * 30000)])
        out = t3.build_transfer_prompt(self.db, "codex:fat", "codex")
        self.assertTrue(out["truncated"])
        self.assertIn("…[截斷]", out["prompt"])
        self.assertLessEqual(len(out["prompt"]),
                             t3.TRANSFER_PROMPT_MAX_CHARS + 600)

    def test_budget_drops_oldest_turns_with_note(self):
        items = []
        for i in range(6):
            items.append(("user", f"大請求{i} " + "x" * 1800))
            items.append(("assistant", f"大回覆{i} " + "y" * 1800))
        _seed_milestones(self.db, "codex:budget", items)
        out = t3.build_transfer_prompt(self.db, "codex:budget", "codex")
        self.assertTrue(out["truncated"])
        self.assertIn("較早內容已截斷", out["prompt"])
        # 犧牲最舊:最新一回合必在,最舊一回合必不在。
        self.assertIn("大請求5", out["prompt"])
        self.assertNotIn("大請求0", out["prompt"])
        self.assertLessEqual(len(out["prompt"]),
                             t3.TRANSFER_PROMPT_MAX_CHARS + 600)


# ───────────────────────── 端點層(/app/v2/threads/{id}/transfer)──────────────


class FakeRequest:
    client = None

    class _URL:
        path = "/app/v2/threads"

        def __str__(self):
            return "http://test/app/v2/threads"

    url = _URL()

    def __init__(self, body=None, token="test-unit-token"):
        self.headers = {"authorization": f"Bearer {token}"}
        self._body = body or {}

    async def json(self):
        return self._body


def _create(body, token="test-unit-token"):
    return asyncio.run(bridge.v2_app_thread_create(FakeRequest(body, token)))


def _transfer(thread_id, body, token="test-unit-token"):
    return asyncio.run(
        bridge.v2_app_thread_transfer(thread_id, FakeRequest(body, token)))


def _events(session, etype):
    con = sqlite3.connect(bridge.CANON_DB)
    try:
        return [json.loads(r[0]) for r in con.execute(
            "SELECT payload FROM event_log WHERE session=? AND type=?",
            (session, etype)).fetchall()]
    finally:
        con.close()


class TransferEndpointTests(unittest.TestCase):
    def setUp(self):
        self._orig_codex_create = bridge.codex_session_create
        self._orig_cc2_create = bridge.v2_cc2_create
        self._orig_cc_sdk = bridge.cc_sdk

    def tearDown(self):
        bridge.codex_session_create = self._orig_codex_create
        bridge.v2_cc2_create = self._orig_cc2_create
        bridge.cc_sdk = self._orig_cc_sdk

    # ── 成功面:codex 方向 ────────────────────────────────────────────────

    def test_transfer_to_codex_rebinds_and_logs_event(self):
        src = f"cc2:src-{uuid.uuid4().hex[:6]}"
        created = _create({"title": "接力 codex", "provider": "cc2",
                           "provider_thread_id": src})
        thread_id = created["thread"]["id"]
        _seed_milestones(bridge.CANON_DB, src, [
            ("user", "把 relay 上線"), ("assistant", "relay 已上線,frpc 活著")])
        captured = {}

        async def fake_codex_create(request):
            captured["body"] = await request.json()
            return {"ok": True, "thread_id": "fresh-ct-1"}

        bridge.codex_session_create = fake_codex_create
        out = _transfer(thread_id, {"to_provider": "codex", "mode": "summary"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["new_session_id"], "codex:fresh-ct-1")
        # 首訊=交接 prompt,含來源里程碑原文。
        prompt = captured["body"]["text"]
        self.assertIn("把 relay 上線", prompt)
        self.assertIn("relay 已上線,frpc 活著", prompt)
        self.assertIn("交接", prompt)
        # rebind:舊綁定 detached + reason="transfer",新綁定 active。
        self.assertEqual(out["binding"]["state"], "active")
        self.assertEqual(out["binding"]["provider_thread_id"], "codex:fresh-ct-1")
        self.assertEqual(out["previous"]["state"], "detached")
        self.assertEqual(out["previous"]["reason"], "transfer")
        chain = t3.list_bindings(bridge.CANON_DB, thread_id)
        self.assertEqual([row["state"] for row in chain], ["detached", "active"])
        # event_log 落 thread.transferred(from/to)。
        events = _events(thread_id, "thread.transferred")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from"],
                         {"provider": "cc2", "provider_thread_id": src})
        self.assertEqual(events[0]["to"],
                         {"provider": "codex",
                          "provider_thread_id": "codex:fresh-ct-1"})
        self.assertEqual(events[0]["turns"], 1)
        self.assertFalse(events[0]["truncated"])

    # ── 成功面:cc2 方向 ─────────────────────────────────────────────────

    def test_transfer_to_cc2_submits_prompt_as_first_message(self):
        src = f"codex:src-{uuid.uuid4().hex[:6]}"
        created = _create({"title": "接力 cc2", "provider": "codex",
                           "provider_thread_id": src})
        thread_id = created["thread"]["id"]
        _seed_milestones(bridge.CANON_DB, src, [
            ("user", "查 SSE 斷線"), ("assistant", "是 quick tunnel 的鍋")])
        captured = {}

        async def fake_cc2_create(name, request):
            captured["name"] = name
            captured["create_body"] = await request.json()
            return {"ok": True, "session_id": f"cc2:{name}"}

        class FakeSess:
            async def submit(self, text, attachments=None, client_id=""):
                captured["prompt"] = text
                return {"delivery": "accepted", "queued": False}

        class FakeRegistry:
            def get(self, name):
                return FakeSess()

        class FakeCcSdk:
            @staticmethod
            def registry():
                return FakeRegistry()

        bridge.v2_cc2_create = fake_cc2_create
        bridge.cc_sdk = FakeCcSdk()
        out = _transfer(thread_id, {"to_provider": "cc2", "name": "relay-ct",
                                    "cwd": "/tmp/wd"})
        self.assertEqual(out["new_session_id"], "cc2:relay-ct")
        self.assertEqual(captured["name"], "relay-ct")
        self.assertEqual(captured["create_body"].get("workdir"), "/tmp/wd")
        self.assertIn("查 SSE 斷線", captured["prompt"])
        self.assertIn("是 quick tunnel 的鍋", captured["prompt"])
        self.assertEqual(out["binding"]["provider"], "cc2")
        self.assertEqual(len(_events(thread_id, "thread.transferred")), 1)

    def test_transfer_without_history_notes_it_and_still_moves(self):
        src = f"cc2:empty-{uuid.uuid4().hex[:6]}"
        created = _create({"title": "無歷史接力", "provider": "cc2",
                           "provider_thread_id": src})
        thread_id = created["thread"]["id"]
        captured = {}

        async def fake_codex_create(request):
            captured["body"] = await request.json()
            return {"ok": True, "thread_id": "fresh-empty-1"}

        bridge.codex_session_create = fake_codex_create
        out = _transfer(thread_id, {"to_provider": "codex"})
        self.assertEqual(out["transfer"]["turns"], 0)
        self.assertIn("無歷史脈絡", captured["body"]["text"])
        self.assertEqual(out["binding"]["provider_thread_id"],
                         "codex:fresh-empty-1")

    # ── 失敗面 ───────────────────────────────────────────────────────────

    def test_target_failure_leaves_binding_untouched(self):
        src = f"cc2:keep-{uuid.uuid4().hex[:6]}"
        created = _create({"title": "失敗不動原綁定", "provider": "cc2",
                           "provider_thread_id": src})
        thread_id = created["thread"]["id"]

        async def exploding_create(request):
            raise HTTPException(status_code=429, detail="quota exceeded")

        bridge.codex_session_create = exploding_create
        with self.assertRaises(HTTPException) as cm:
            _transfer(thread_id, {"to_provider": "codex"})
        self.assertEqual(cm.exception.status_code, 502)
        self.assertEqual(getattr(cm.exception, "code", ""),
                         "TRANSFER_TARGET_FAILED")
        self.assertIn("429", str(cm.exception.detail))
        # 原 binding 原封不動,沒有事件。
        binding = t3.current_binding(bridge.CANON_DB, thread_id)
        self.assertEqual(binding["provider_thread_id"], src)
        self.assertEqual(binding["state"], "active")
        self.assertEqual(len(t3.list_bindings(bridge.CANON_DB, thread_id)), 1)
        self.assertEqual(_events(thread_id, "thread.transferred"), [])

    def test_rebind_conflict_is_409_and_source_binding_survives(self):
        taken = f"codex:taken-{uuid.uuid4().hex[:6]}"
        _create({"title": "占住目標", "provider": "codex",
                 "provider_thread_id": taken})
        src = f"cc2:race-{uuid.uuid4().hex[:6]}"
        created = _create({"title": "撞綁定", "provider": "cc2",
                           "provider_thread_id": src})
        thread_id = created["thread"]["id"]

        async def fake_codex_create(request):
            return {"ok": True, "thread_id": taken.split(":", 1)[1]}

        bridge.codex_session_create = fake_codex_create
        with self.assertRaises(HTTPException) as cm:
            _transfer(thread_id, {"to_provider": "codex"})
        self.assertEqual(cm.exception.status_code, 409)
        binding = t3.current_binding(bridge.CANON_DB, thread_id)
        self.assertEqual(binding["provider_thread_id"], src)
        self.assertEqual(_events(thread_id, "thread.transferred"), [])

    def test_slow_spawn_does_not_overwrite_an_intervening_rebind(self):
        thread_id = _create({"provider": "cc2", "provider_thread_id": "cc2:slow-source"})["thread"]["id"]

        async def slow_create(request):
            t3.rebind_app_thread(bridge.CANON_DB, thread_id, "cc2", "cc2:device-b")
            return {"ok": True, "thread_id": "spawned-but-unbound"}

        bridge.codex_session_create = slow_create
        with self.assertRaises(HTTPException) as cm:
            _transfer(thread_id, {"to_provider": "codex"})
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("spawned-but-unbound", str(cm.exception.detail))
        chain = t3.list_bindings(bridge.CANON_DB, thread_id)
        self.assertEqual([b["provider_thread_id"] for b in chain], ["cc2:slow-source", "cc2:device-b"])
        self.assertEqual(_events(thread_id, "thread.transferred"), [])

    # ── 驗證/graceful 面 ─────────────────────────────────────────────────

    def test_unknown_thread_404_and_no_binding_404(self):
        with self.assertRaises(HTTPException) as cm:
            _transfer("app_missing", {"to_provider": "codex"})
        self.assertEqual(cm.exception.status_code, 404)
        created = _create({"title": "沒綁定"})
        with self.assertRaises(HTTPException) as cm:
            _transfer(created["thread"]["id"], {"to_provider": "codex"})
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(getattr(cm.exception, "code", ""),
                         "APP_THREAD_NO_BINDING")

    def test_bad_provider_and_bad_mode_400(self):
        created = _create({"title": "參數驗證", "provider": "cc2",
                           "provider_thread_id":
                               f"cc2:v-{uuid.uuid4().hex[:6]}"})
        thread_id = created["thread"]["id"]
        for body in ({"to_provider": "gemini"}, {},
                     {"to_provider": "codex", "mode": "llm"}):
            with self.assertRaises(HTTPException) as cm:
                _transfer(thread_id, body)
            self.assertEqual(cm.exception.status_code, 400)

    def test_transfer_requires_auth(self):
        created = _create({"title": "auth", "provider": "cc2",
                           "provider_thread_id":
                               f"cc2:a-{uuid.uuid4().hex[:6]}"})
        with self.assertRaises(HTTPException) as cm:
            _transfer(created["thread"]["id"], {"to_provider": "codex"},
                      token="wrong-token")
        self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
