"""AppThread ≠ ProviderThread 雙層 ID(疊加層)—— binding 狀態機與端點契約。

涵蓋:
- **狀態機**:建/綁/換綁;換綁 = 舊列標 dead/detached + 新列 active,
  歷史全留(audit 鏈不重寫);rebind 同目標冪等(changed=false)。
- **不變量**:一個 app thread 同時至多一個 active binding(要換就走 rebind);
  同一個 provider thread 不准同時 active 綁兩個 app thread。
- **端點**:POST /app/v2/threads(建+綁既有 provider session;綁定目標被佔
  時不留孤兒 thread)、GET /app/v2/threads/{id}(含 binding 鏈)、
  POST /app/v2/threads/{id}/rebind(落 event_log `thread.rebound`,
  session=app thread id)。
- **auth**:三個端點都吃 _check_auth,壞 token 一律 401。
- **相容**:這層是疊加層 —— 不碰 messages/卡片流,/app/v2/sessions 不受影響。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="app-thread-bindings-test-")
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


def _get(thread_id, token="test-unit-token"):
    return asyncio.run(bridge.v2_app_thread_get(thread_id,
                                                FakeRequest(token=token)))


def _rebind(thread_id, body, token="test-unit-token"):
    return asyncio.run(bridge.v2_app_thread_rebind(thread_id,
                                                   FakeRequest(body, token)))


# ───────────────────────── 純函式層(t3code_runtime)─────────────────────────


class BindingStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(os.path.join(self.tmp.name, "canonical.db"))
        t3.ensure_schema(self.db)
        self.thread = t3.create_app_thread(self.db, "xcash", "dual-id")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bind_then_current_then_chain(self):
        bound = t3.bind_app_thread(self.db, self.thread["id"],
                                   "codex", "codex:abc")
        self.assertEqual(bound["state"], "active")
        current = t3.current_binding(self.db, self.thread["id"])
        self.assertEqual(current["id"], bound["id"])
        self.assertEqual(current["provider_thread_id"], "codex:abc")
        chain = t3.list_bindings(self.db, self.thread["id"])
        self.assertEqual([row["id"] for row in chain], [bound["id"]])

    def test_bind_same_target_is_idempotent(self):
        first = t3.bind_app_thread(self.db, self.thread["id"],
                                   "codex", "codex:abc")
        again = t3.bind_app_thread(self.db, self.thread["id"],
                                   "codex", "codex:abc")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(t3.list_bindings(self.db, self.thread["id"])), 1)

    def test_second_active_binding_requires_rebind(self):
        t3.bind_app_thread(self.db, self.thread["id"], "codex", "codex:abc")
        with self.assertRaises(t3.T3CodeError):
            t3.bind_app_thread(self.db, self.thread["id"],
                               "claude_code", "cc:tirith")

    def test_rebind_retires_old_row_and_keeps_history(self):
        old = t3.bind_app_thread(self.db, self.thread["id"],
                                 "codex", "codex:dead-thread")
        result = t3.rebind_app_thread(
            self.db, self.thread["id"], "codex", "codex:fresh-thread",
            reason="desktop app stole the writer lock", prior_state="dead")
        self.assertTrue(result["changed"])
        self.assertEqual(result["previous"]["id"], old["id"])
        self.assertEqual(result["previous"]["state"], "dead")
        self.assertEqual(result["previous"]["reason"],
                         "desktop app stole the writer lock")
        self.assertIsNotNone(result["previous"]["unbound_at"])
        self.assertEqual(result["binding"]["state"], "active")
        chain = t3.list_bindings(self.db, self.thread["id"])
        self.assertEqual([row["state"] for row in chain], ["dead", "active"])
        current = t3.current_binding(self.db, self.thread["id"])
        self.assertEqual(current["provider_thread_id"], "codex:fresh-thread")

    def test_rebind_detached_state_and_bad_state_rejected(self):
        t3.bind_app_thread(self.db, self.thread["id"], "codex", "codex:a")
        result = t3.rebind_app_thread(self.db, self.thread["id"],
                                      "claude_code", "cc:b",
                                      reason="handoff", prior_state="detached")
        self.assertEqual(result["previous"]["state"], "detached")
        with self.assertRaises(t3.T3CodeError):
            t3.rebind_app_thread(self.db, self.thread["id"], "codex",
                                 "codex:c", prior_state="active")
        with self.assertRaises(t3.T3CodeError):
            t3.rebind_app_thread(self.db, self.thread["id"], "codex",
                                 "codex:c", prior_state="nonsense")

    def test_rebind_same_target_is_noop(self):
        bound = t3.bind_app_thread(self.db, self.thread["id"],
                                   "codex", "codex:abc")
        result = t3.rebind_app_thread(self.db, self.thread["id"],
                                      "codex", "codex:abc")
        self.assertFalse(result["changed"])
        self.assertEqual(result["binding"]["id"], bound["id"])
        self.assertEqual(len(t3.list_bindings(self.db, self.thread["id"])), 1)

    def test_rebind_without_active_binding_just_binds(self):
        result = t3.rebind_app_thread(self.db, self.thread["id"],
                                      "codex", "codex:first")
        self.assertTrue(result["changed"])
        self.assertIsNone(result["previous"])
        self.assertEqual(result["binding"]["state"], "active")

    def test_provider_thread_cannot_be_active_under_two_app_threads(self):
        other = t3.create_app_thread(self.db, "xcash", "second thread")
        t3.bind_app_thread(self.db, self.thread["id"], "codex", "codex:shared")
        with self.assertRaises(t3.T3CodeError):
            t3.bind_app_thread(self.db, other["id"], "codex", "codex:shared")
        t3.bind_app_thread(self.db, other["id"], "codex", "codex:other")
        with self.assertRaises(t3.T3CodeError):
            t3.rebind_app_thread(self.db, other["id"], "codex", "codex:shared")
        # 原綁定標 dead 之後,同一個 provider thread 才可以被接手。
        t3.rebind_app_thread(self.db, self.thread["id"], "codex",
                             "codex:replacement", reason="dead", prior_state="dead")
        taken = t3.rebind_app_thread(self.db, other["id"], "codex",
                                     "codex:shared", prior_state="detached")
        self.assertTrue(taken["changed"])

    def test_unique_active_invariant_lives_in_the_schema_too(self):
        # 防呆不是只有 Python 守 —— 直接繞過 API 寫第二列 active 必須撞
        # partial unique index。
        t3.bind_app_thread(self.db, self.thread["id"], "codex", "codex:abc")
        con = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO app_thread_bindings"
                "(id,app_thread_id,provider,provider_thread_id,state,bound_at)"
                " VALUES('x',?,?,?,'active',1)",
                (self.thread["id"], "claude_code", "cc:evil"))
        con.close()

    def test_validation(self):
        with self.assertRaises(t3.T3CodeError):
            t3.bind_app_thread(self.db, self.thread["id"], "", "codex:abc")
        with self.assertRaises(t3.T3CodeError):
            t3.bind_app_thread(self.db, self.thread["id"], "codex", "")
        with self.assertRaises(t3.T3CodeError):
            t3.bind_app_thread(self.db, "nope", "codex", "codex:abc")
        self.assertIsNone(t3.current_binding(self.db, self.thread["id"]))
        self.assertEqual(t3.list_bindings(self.db, self.thread["id"]), [])

    def test_active_binding_owner_lookup(self):
        self.assertIsNone(t3.active_binding_owner(self.db, "codex", "codex:abc"))
        t3.bind_app_thread(self.db, self.thread["id"], "codex", "codex:abc")
        self.assertEqual(t3.active_binding_owner(self.db, "codex", "codex:abc"),
                         self.thread["id"])


# ───────────────────────── 端點層(/app/v2/threads)─────────────────────────


class AppThreadEndpointTests(unittest.TestCase):
    def test_create_bind_get_roundtrip(self):
        created = _create({"title": "殺手級第二檔", "provider": "codex",
                           "provider_thread_id": "codex:ep-roundtrip"})
        self.assertTrue(created["ok"])
        thread = created["thread"]
        self.assertEqual(thread["binding"]["provider_thread_id"],
                         "codex:ep-roundtrip")
        got = _get(thread["id"])
        self.assertEqual(got["thread"]["binding"]["state"], "active")
        self.assertEqual(len(got["thread"]["bindings"]), 1)

    def test_create_without_binding_then_rebind_binds(self):
        created = _create({"title": "bare thread"})
        self.assertIsNone(created["thread"]["binding"])
        result = _rebind(created["thread"]["id"],
                         {"provider": "claude_code",
                          "provider_thread_id": "claude_code:tirith"})
        self.assertTrue(result["changed"])
        self.assertIsNone(result["previous"])

    def test_create_with_taken_target_leaves_no_orphan_thread(self):
        first = _create({"title": "owner", "provider": "codex",
                         "provider_thread_id": "codex:ep-taken"})
        with self.assertRaises(HTTPException) as cm:
            _create({"title": "thief", "provider": "codex",
                     "provider_thread_id": "codex:ep-taken"})
        self.assertEqual(cm.exception.status_code, 400)
        # 被拒的那次不准留孤兒 app thread。
        con = sqlite3.connect(bridge.CANON_DB)
        count = con.execute("SELECT COUNT(*) FROM app_threads WHERE title='thief'"
                            ).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)
        self.assertTrue(first["ok"])

    def test_rebind_endpoint_full_cycle_and_event(self):
        created = _create({"title": "CX 鎖死續命", "provider": "codex",
                           "provider_thread_id": "codex:ep-locked"})
        thread_id = created["thread"]["id"]
        result = _rebind(thread_id, {
            "provider": "codex", "provider_thread_id": "codex:ep-fresh",
            "reason": "writer lock stolen", "prior_state": "dead"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["previous"]["state"], "dead")
        got = _get(thread_id)
        states = [row["state"] for row in got["thread"]["bindings"]]
        self.assertEqual(states, ["dead", "active"])
        # event_log 落 `thread.rebound`,session=app thread id。
        con = sqlite3.connect(bridge.CANON_DB)
        rows = con.execute(
            "SELECT type,payload FROM event_log WHERE session=? AND type=?",
            (thread_id, "thread.rebound")).fetchall()
        con.close()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][1])
        self.assertEqual(payload["from"]["provider_thread_id"], "codex:ep-locked")
        self.assertEqual(payload["to"]["provider_thread_id"], "codex:ep-fresh")
        self.assertEqual(payload["reason"], "writer lock stolen")

    def test_rebind_noop_emits_no_event(self):
        created = _create({"title": "noop", "provider": "codex",
                           "provider_thread_id": "codex:ep-noop"})
        thread_id = created["thread"]["id"]
        result = _rebind(thread_id, {"provider": "codex",
                                     "provider_thread_id": "codex:ep-noop"})
        self.assertFalse(result["changed"])
        con = sqlite3.connect(bridge.CANON_DB)
        count = con.execute(
            "SELECT COUNT(*) FROM event_log WHERE session=? AND type=?",
            (thread_id, "thread.rebound")).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_unknown_thread_404_and_bad_input_400(self):
        with self.assertRaises(HTTPException) as cm:
            _get("app_missing")
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(HTTPException) as cm:
            _rebind("app_missing", {"provider": "codex",
                                    "provider_thread_id": "codex:x"})
        self.assertEqual(cm.exception.status_code, 400)
        created = _create({"title": "half"})
        with self.assertRaises(HTTPException) as cm:
            _rebind(created["thread"]["id"], {"provider": "codex"})
        self.assertEqual(cm.exception.status_code, 400)
        # 建立時只給一半的綁定參數也要擋。
        with self.assertRaises(HTTPException) as cm:
            _create({"title": "half-bind", "provider": "codex"})
        self.assertEqual(cm.exception.status_code, 400)

    def test_all_three_endpoints_require_auth(self):
        created = _create({"title": "auth probe"})
        thread_id = created["thread"]["id"]
        for call in (
            lambda: _create({"title": "x"}, token="wrong-token"),
            lambda: _get(thread_id, token="wrong-token"),
            lambda: _rebind(thread_id, {"provider": "codex",
                                        "provider_thread_id": "codex:x"},
                            token="wrong-token"),
        ):
            with self.assertRaises(HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
