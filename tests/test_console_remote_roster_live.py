"""W1-⑤:遠端(借用裝置)清單即時化改走 roster 差分 SSE —— 不准外洩訊息內容。

背景:遠端原本每 10 秒輪詢 `/app/v2/sessions`(有過濾,只是慢)。改接現成的
`/app/v2/sessions/roster/events`(SSE,差分推播)時查到一個這支 roster 疊加層
自己造出來的洞:`_console_remote_list`(套在 `v2_sessions` 裡)只剝
capabilities/meta,**不碰訊息內容** —— 因為它的前提是「`/app/v2/sessions`
本來就不吐訊息預覽」。但 `_roster_build` 疊加的 `last_turn_summary` 直接從
`event_log` 撈最新訊息原文,完全不看 remote scope。若原封不動把遠端接上這條,
借來的電腦會看到**全部 session 的真實訊息內容**,比原本 `/app/v2/sessions`
的揭露面大得多。

這份測試釘住修法:遠端時 `last_turn_summary` 一律空字串,無論 event_log
裡實際寫了什麼敏感內容。
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-roster-live-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET_TEXT = "hunter2機密"   # 短:規則式摘要會截斷長文,太長會被省略號吃掉


class ConsoleRemoteRosterLiveTest(unittest.TestCase):
    TOKEN = "pdev-console-roster-live"

    def setUp(self):
        self.http = TestClient(bridge.app)
        self.addCleanup(self.http.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wd = Path(tmp.name) / "shared"
        self.wd.mkdir()
        self.sid = "claude_code:demo"

        # 一條真的 CC session + 一則真的、含敏感字串的 event_log 訊息。
        con = sqlite3.connect(bridge.CANON_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS event_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
            type TEXT NOT NULL, external_id TEXT UNIQUE,
            payload TEXT NOT NULL, created_at REAL NOT NULL)""")
        card = {"id": "c-1", "turn_id": "t", "role": "assistant", "kind": "text",
                "rev": 1, "final": True, "body": {"text": SECRET_TEXT, "fallback_text": SECRET_TEXT}}
        con.execute(
            "INSERT INTO event_log(session,type,external_id,payload,created_at) VALUES(?,?,?,?,?)",
            (self.sid, "card.milestone", None, json.dumps({"card": card}, ensure_ascii=False), time.time()))
        con.commit()
        con.close()

        self.enterContext(patch.object(bridge, "PERSONAS", {}))
        self.enterContext(patch.object(bridge, "_CONSOLE_GRANTS", {}))
        self.enterContext(patch.object(bridge, "CONSOLE_REMOTE_FILE_ROOTS", [str(self.wd.resolve())]))
        for name, val in [("_delegation_v2_sessions", []), ("_delegated_codex_thread_ids", set()),
                          ("_codex_v2_visible_threads", []), ("_hermes_pending_by_session", {}),
                          ("_cc_conf_rows", [("demo", str(self.wd), "1")]),
                          ("_v2_cc_state", ("idle", None)), ("_registry_call_safe", None)]:
            self.enterContext(patch.object(bridge, name, return_value=val))
        for m, k in [(bridge.GEMINI, "configured"), (bridge.OPENCLAW, "configured"), (bridge.cc_sdk, "enabled")]:
            self.enterContext(patch.object(m, k, return_value=False))
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN] = {
                "name": "borrowed laptop", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": time.time() + 600,
                "created": time.time(), "last_seen": time.time(),
            }
        self.addCleanup(lambda: bridge._DEVICE_TOKENS.pop(self.TOKEN, None))

    def _hdr(self, token=None):
        return {"Authorization": "Bearer " + (token or os.environ["BRIDGE_TOKEN"])}

    def test_owner_roster_sees_real_message_summary(self):
        """對照組:機主自己看,summary 本該有內容(不是這支測試要擋的東西)。"""
        r = self.http.get("/app/v2/sessions/roster", headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        rows = {row["id"]: row for row in r.json()["sessions"]}
        self.assertIn(SECRET_TEXT, rows[self.sid]["last_turn_summary"])

    # ⚠️ 不透過 TestClient 開一條真的 SSE 連線:這個 repo 在 console-remote 路徑
    # 上有過真實死鎖前科(見 test_console_remote_scope.py 的 tearDown 註解)。
    # 改成直接餵一個最小的假 Request(只有 _roster_build/_check_auth 真的會讀
    # 的 .headers/.state/.query_params)給路由函式,手動從回傳的
    # StreamingResponse.body_iterator 拉一個 chunk,全程套 asyncio.wait_for
    # 的硬逾時 —— 絕對不會掛住整個測試套件。
    @staticmethod
    async def _not_disconnected():
        return False

    def _fake_remote_request(self):
        return SimpleNamespace(
            headers={"authorization": "Bearer " + self.TOKEN},
            query_params={},
            state=SimpleNamespace(console_remote=True),
            is_disconnected=self._not_disconnected,
        )

    def test_remote_roster_build_blanks_message_summary(self):
        """核心斷言:_roster_build 對遠端 scope 絕不能帶出真實訊息內容。"""
        async def run():
            return await bridge._roster_build(self._fake_remote_request())
        out = asyncio.run(asyncio.wait_for(run(), timeout=5))
        rows = {row["id"]: row for row in out["sessions"]}
        self.assertIn(self.sid, rows, "session 本身仍該列出(只是不給內容)")
        self.assertEqual(rows[self.sid]["last_turn_summary"], "",
                         "遠端絕不能看到 last_turn_summary 裡的真實訊息內容")
        self.assertNotIn(SECRET_TEXT, json.dumps(out))

    def test_roster_events_whitelisted_and_first_chunk_is_sanitized(self):
        """白名單生效 + 吐出的第一包資料一樣被消毒過(直接拉生成器,不開真連線)。"""
        self.assertIn("/app/v2/sessions/roster/events", bridge._CONSOLE_GET,
                      "roster/events 應在遠端白名單內")

        async def run():
            resp = await bridge.v2_sessions_roster_events(self._fake_remote_request())
            chunk = await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=5)
            await resp.body_iterator.aclose()
            return chunk
        chunk = asyncio.run(run())
        self.assertIn("sessions.changed", chunk)
        self.assertNotIn(SECRET_TEXT, chunk, "SSE 第一包推播也不准帶出真實訊息內容")

    def test_unwhitelisted_global_events_still_blocked_for_remote(self):
        """反面:千萬別把全域 /app/v2/events(無過濾、跨 session)誤放進白名單。"""
        r = self.http.get("/app/v2/events?follow=false", headers=self._hdr(self.TOKEN))
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
