"""Antigravity(agy)stream-json 引擎 —— wire 契約釘死。

全部形狀來自 SG 龍蝦 2026-11-01 實測(agy 1.4.12),不是文件推論:
  in : {"event":"user","message":{"role":"user","content":[{type,text}]}}
  out: init(conversation_id)→ step_update* → result(status/response/usage)
  續接: --conversation <id>;同行程連發 context 延續。

釘住的契約:
  1. user 事件形狀(Claude 式 {"type":"user"} 會被 agy 拒收 —— 形狀錯=全死)
  2. 非文字附件:略過要**說出來**(文字註記+日誌),不默吞
  3. result SUCCESS → 全文以 agent_message_chunk emit(bridge 累積邏輯不變)
     + conversation_id 落名錄(pending 佔位換真 id)
  4. result 非 SUCCESS → GeminiError,錯誤文字保留(人話層在 bridge)
  5. 中斷:cancel 後行程 EOF = stopReason "cancelled",不是報錯
  6. 舊 conversation 載不回 → 只有「對話不存在」類錯誤准開新局重試;
     額度/認證錯誤不准(會重複燒 quota 還把真錯蓋掉)
  7. 門面:不支援的 method 大聲拒絕,不假裝 ACP 全集都在
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_provider as gp  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeStdout:
    def __init__(self, lines):
        self._lines = [(l if isinstance(l, bytes) else l.encode())
                       for l in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class FakeProc:
    returncode = None

    def __init__(self, lines):
        self.stdout = FakeStdout(lines)


class UserEventShapeTest(unittest.TestCase):

    def test_wire_shape_is_event_user(self):
        """agy 只收 {"event":"user",...};Claude 式 {"type":"user"} 被拒。"""
        ev, skipped = gp._agy_user_event([{"type": "text", "text": "hi"}])
        self.assertEqual(ev["event"], "user")
        self.assertNotIn("type", ev)
        self.assertEqual(ev["message"]["content"], [{"type": "text",
                                                     "text": "hi"}])
        self.assertEqual(skipped, 0)

    def test_multiple_text_blocks_joined(self):
        ev, _ = gp._agy_user_event([{"type": "text", "text": "a"},
                                    {"type": "text", "text": "b"}])
        self.assertEqual(ev["message"]["content"][0]["text"], "a\nb")

    def test_non_text_blocks_skipped_loudly(self):
        """附件略過要在文字裡**說出來** —— 默吞=使用者以為圖送到了。"""
        ev, skipped = gp._agy_user_event(
            [{"type": "text", "text": "看圖"},
             {"type": "image", "mimeType": "image/png", "data": "xx"}])
        self.assertEqual(skipped, 1)
        self.assertIn("略過", ev["message"]["content"][0]["text"])


class ReopenPolicyTest(unittest.TestCase):

    def test_conversation_not_found_reopens(self):
        for t in ("conversation abc not found", "no such conversation",
                  "unknown id"):
            self.assertTrue(gp._agy_should_reopen(t), t)

    def test_quota_and_auth_never_reopen(self):
        """重試會重複燒額度、把真錯蓋掉 —— 這兩類絕對不准開新局。"""
        for t in ("quota exceeded", "429 resource_exhausted",
                  "please sign in", "unauthenticated"):
            self.assertFalse(gp._agy_should_reopen(t), t)


class ArgvTest(unittest.TestCase):

    def test_argv_fresh_vs_resume(self):
        c = gp.AntigravityClient()
        with patch.object(gp, "agy_bin", return_value="/x/agy"):
            fresh = c._argv("")
            resume = c._argv("conv-1")
        self.assertIn("--dangerously-skip-permissions", fresh)
        self.assertIn("stream-json", " ".join(fresh))
        self.assertNotIn("--conversation", fresh)
        self.assertEqual(resume[-2:], ["--conversation", "conv-1"])


class ReadTurnTest(unittest.TestCase):
    """_read_turn:實測事件序列 → bridge 門面行為。"""

    def _client(self):
        c = gp.AntigravityClient()
        c._record_conv = lambda *_a, **_k: None   # 名錄落檔另測
        return c

    def test_success_emits_full_text_once(self):
        c = self._client()
        got = []
        c.on_update = lambda sid, u: got.append((sid, u))
        lines = [
            '{"event":"init","conversation_id":"c1","init":{"cwd":"/w"}}\n',
            '{"event":"step_update","step":{}}\n',
            '{"event":"result","result":{"conversation_id":"c1",'
            '"status":"SUCCESS","response":"4\\n",'
            '"usage":{"input_tokens":6711}}}\n',
        ]
        res = _run(c._read_turn("sid-1", "k1", FakeProc(lines)))
        self.assertEqual(res["stopReason"], "end_turn")
        self.assertEqual(res["usage"]["input_tokens"], 6711)
        chunks = [u for _, u in got
                  if u["sessionUpdate"] == "agent_message_chunk"]
        self.assertEqual(len(chunks), 1, "全文一次 emit,不多不少")
        self.assertEqual(chunks[0]["content"]["text"], "4\n")
        # emit 的 sid 必須是呼叫端註冊的那個 —— 中途換名會把 _GM_TURNS 全文弄丟
        self.assertTrue(all(s == "sid-1" for s, _ in got))
        self.assertEqual(c.auth_used, "google-antigravity")

    def test_error_status_raises_with_message(self):
        c = self._client()
        lines = ['{"event":"result","result":{"status":"ERROR",'
                 '"error":"please sign in"}}\n']
        with self.assertRaises(gp.GeminiError) as cm:
            _run(c._read_turn("s", "k", FakeProc(lines)))
        self.assertIn("sign in", str(cm.exception),
                      "錯誤文字要保留 —— bridge 的人話規則靠它分類")

    def test_cancelled_eof_is_not_an_error(self):
        c = self._client()
        c._cancelled.add("k")
        res = _run(c._read_turn("s", "k", FakeProc([])))
        self.assertEqual(res["stopReason"], "cancelled")

    def test_uncancelled_eof_raises_process_lost(self):
        c = self._client()
        with self.assertRaises(gp.GeminiError) as cm:
            _run(c._read_turn("s", "k", FakeProc(['not-json banner\n'])))
        self.assertEqual(cm.exception.code, "PROCESS_LOST")


class FacadeTest(unittest.TestCase):

    def test_unsupported_method_is_loud(self):
        c = gp.AntigravityClient()
        with self.assertRaises(gp.GeminiError) as cm:
            _run(c.call("session/load", {}))
        self.assertEqual(cm.exception.code, "UNSUPPORTED")

    def test_ensure_session_without_binary_raises(self):
        c = gp.AntigravityClient()
        with patch.object(gp, "agy_bin", return_value=""):
            with self.assertRaises(gp.GeminiError) as cm:
                _run(c.ensure_session("k"))
        self.assertEqual(cm.exception.code, "NO_BINARY")

    def test_configured_tracks_binary(self):
        c = gp.AntigravityClient()
        with patch.object(gp, "agy_bin", return_value="/x/agy"):
            self.assertTrue(c.configured())
        with patch.object(gp, "agy_bin", return_value=""):
            self.assertFalse(c.configured())


if __name__ == "__main__":
    unittest.main()
