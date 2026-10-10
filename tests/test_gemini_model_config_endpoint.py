"""`GET/PUT /app/v1/gemini/config`:Gemini 模型可從 app 改(全域)。

機主 2026-10-10:「現在所有功能請都要留意考慮到 gemini」+「gemini 頁面內的
ui ux 都還沒有改好」。對話頁缺的其中一塊是模型選擇 —— CC 有、Codex 有、
Gemini 沒有,而 bridge 10-09 補的 `GEMINI_MODEL` 只是**模組層 env 變數**,
app 改不動。

**為什麼是全域而不是 per-session**:模型是 gemini CLI 的 spawn 參數
(`gemini --acp -m <model>`),整個 provider 共用一個 ACP 行程。要做成
per-session 得一條對話一個行程,那是另一個量級;這裡誠實做成全域,
並由端點回 `model_scope: "global"` 讓 app 照這個語意標示。

這支釘住三件容易寫壞的事:
  1. 只帶 model 的 PUT 不可以把 api_key 洗掉(反向亦然)—— 原本
     `save_config` 無條件寫 `{"api_key": ...}`,加欄位後照舊就會洗掉。
  2. 兩個欄位都不帶要回 400,而不是靜靜寫一個空設定。
  3. 改完一定要 `_drop_proc()` —— 模型是 spawn 參數,不重新握手等於沒生效
     (「改了沒反應」會被當成功能壞掉)。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="gm-model-cfg-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["GEMINI_CONFIG_FILE"] = os.path.join(_TMP, "gemini.json")
os.environ.pop("GEMINI_MODEL", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import gemini_provider  # noqa: E402


class _Req:
    def __init__(self, body: dict | None = None):
        self.headers = {"authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.query_params = {}
        self._body = body or {}

    async def json(self):
        return self._body

    class _URL:
        path = "/app/v1/gemini/config"
    url = _URL()


class GeminiModelConfigEndpointTest(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(os.environ["GEMINI_CONFIG_FILE"])
        except FileNotFoundError:
            pass
        self.drops = 0
        self._orig_drop = bridge.GEMINI._drop_proc

        async def fake_drop():
            self.drops += 1

        bridge.GEMINI._drop_proc = fake_drop

    def tearDown(self):
        bridge.GEMINI._drop_proc = self._orig_drop

    def _put(self, body):
        return asyncio.run(bridge.gemini_config_put(_Req(body)))

    def _get(self):
        return asyncio.run(bridge.gemini_config_get(_Req()))

    def test_get_reports_model_and_scope(self):
        out = self._get()
        self.assertEqual(out["model"], "")            # 空 = CLI 預設
        self.assertEqual(out["model_source"], "none")
        self.assertEqual(out["model_scope"], "global")

    def test_put_model_only_sets_model_and_respawns(self):
        out = self._put({"model": "gemini-3.5-flash"})
        self.assertEqual(out["model"], "gemini-3.5-flash")
        self.assertEqual(out["model_source"], "file")
        self.assertEqual(self.drops, 1, "改模型必須重新握手才生效")

    def test_put_model_only_keeps_api_key(self):
        gemini_provider.save_config("file-key")
        self._put({"model": "gemini-3.5-flash"})
        cfg = gemini_provider.load_config()
        self.assertEqual(cfg["api_key"], "file-key")
        self.assertEqual(cfg["model"], "gemini-3.5-flash")

    def test_put_key_only_keeps_model(self):
        self._put({"model": "gemini-3.5-flash"})
        self._put({"api_key": "k2"})
        cfg = gemini_provider.load_config()
        self.assertEqual(cfg["model"], "gemini-3.5-flash")
        self.assertEqual(cfg["api_key"], "k2")

    def test_put_empty_body_is_400(self):
        with self.assertRaises(Exception) as ctx:
            self._put({})
        self.assertIn("400", str(getattr(ctx.exception, "status_code", "")
                                 or ctx.exception))

    def test_put_empty_model_string_clears_back_to_cli_default(self):
        """清空 = 回到 CLI 預設,要能清得掉(不是「只能設不能取消」)。"""
        self._put({"model": "gemini-3.5-flash"})
        out = self._put({"model": ""})
        self.assertEqual(out["model"], "")
        self.assertEqual(out["model_source"], "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
