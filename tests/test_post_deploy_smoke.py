"""煙測腳本自我驗證 —— 用合成資料證明每條不變式**真的會叫**。

「拿去線上跑一次全綠」證明不了任何事:那可能只代表當下沒壞。每一條檢查都要
用「已知壞掉的資料」餵進去看它會不會失敗,否則它就只是一排永遠亮綠的裝飾。

這裡的壞資料全部取自 2026-08-11~16 的實機數字,不是想像出來的。
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "post_deploy_smoke",
    os.path.join(_HERE, "..", "scripts", "post-deploy-smoke.py"))
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def session(**kw):
    base = {"name": "Cashcamp", "thread_id": "019f39d3", "providerStatus": "idle",
            "activeTurn": False, "updatedAt": 1786800000, "lastEventAt": 1786800000}
    base.update(kw)
    return base


class ResultShapeTests(unittest.TestCase):
    def test_detail_is_suppressed_when_passing(self):
        """通過時不該印失敗說明 —— 否則會出現「✓ 順序亂了」這種矛盾輸出。"""
        r = smoke.Result()
        r.add("某條", True, detail="壞掉時才講的話", note="量到的數字")
        c = r.checks[0]
        self.assertEqual(c["detail"], "")
        self.assertEqual(c["note"], "量到的數字")

    def test_detail_kept_when_failing(self):
        r = smoke.Result()
        r.add("某條", False, detail="這裡壞了")
        self.assertEqual(r.checks[0]["detail"], "這裡壞了")

    def test_warn_does_not_fail_the_run(self):
        r = smoke.Result()
        r.add("警告項", False, "有點怪", warn=True)
        self.assertEqual(r.failed, [])
        self.assertEqual(len(r.warned), 1)


class InvariantTests(unittest.TestCase):
    """把壞資料餵進去,每條檢查都必須失敗。"""

    def _run(self, sessions):
        res = smoke.Result()
        smoke.check_codex_sessions.__wrapped__ if False else None
        # 直接複用檢查邏輯:以 monkeypatch 的 get 餵資料
        orig = smoke.get
        smoke.get = lambda *_a, **_k: {"sessions": sessions}
        try:
            smoke.check_codex_sessions("http://x", "tok", res)
        finally:
            smoke.get = orig
        return {c["check"]: c for c in res.checks}

    def test_usage_over_capacity_is_caught(self):
        """實機數字:517,323,425 / 258,400 = 200,203%。"""
        out = self._run([session(usage={"used": 517_323_425, "size": 258_400})])
        self.assertFalse(out["CX 用量條不爆表"]["ok"])
        self.assertIn("200203%", out["CX 用量條不爆表"]["detail"].replace(",", ""))

    def test_sane_usage_passes(self):
        out = self._run([session(usage={"used": 120_000, "size": 258_400})])
        self.assertTrue(out["CX 用量條不爆表"]["ok"])

    def test_stale_updated_at_is_caught(self):
        """實機:bridge 報 07-07,codex 自己的 DB 是 08-15 —— 差 39 天。"""
        out = self._run([session(updatedAt=1783381615, lastEventAt=1786774003)])
        self.assertFalse(out["CX updatedAt 不落後"]["ok"])
        self.assertIn("落後", out["CX updatedAt 不落後"]["detail"])

    def test_small_lag_is_tolerated(self):
        """一小時內的落差是正常抖動,不該吵。"""
        out = self._run([session(updatedAt=1786800000, lastEventAt=1786801000)])
        self.assertTrue(out["CX updatedAt 不落後"]["ok"])

    def test_sticky_error_on_idle_session_is_caught(self):
        """實機:卡過一次之後,即使後續回合成功也永遠顯示紅色錯誤。"""
        out = self._run([session(error="Codex turn stalled (no provider event)")])
        self.assertFalse(out["CX 無黏住的錯誤"]["ok"])

    def test_error_while_actually_running_is_not_flagged(self):
        """正在跑、而且 provider 也說在跑 → 那是真的出事,不是黏住的殘留。"""
        out = self._run([session(error="boom", providerStatus="running",
                                 activeTurn=True)])
        self.assertTrue(out["CX 無黏住的錯誤"]["ok"])

    def test_clean_sessions_pass_everything(self):
        out = self._run([session(usage={"used": 1000, "size": 258_400})])
        for name in ("CX 用量條不爆表", "CX updatedAt 不落後", "CX 無黏住的錯誤"):
            self.assertTrue(out[name]["ok"], f"{name} 對乾淨資料誤報")


class CardStreamInvariantTests(unittest.TestCase):
    def _run(self, few, many):
        res = smoke.Result()
        orig = smoke.get
        calls = {"n": 0}

        def fake(_base, path, *_a, **_k):
            calls["n"] += 1
            return {"cards": few if "limit=5" in path else many}

        smoke.get = fake
        try:
            smoke.check_card_stream("http://x", "tok",
                                    [{"thread_id": "t1"}], res)
        finally:
            smoke.get = orig
        return {c["check"]: c for c in res.checks}

    def test_out_of_order_cards_are_caught(self):
        many = [{"ts": 300}, {"ts": 100}, {"ts": 200}]
        out = self._run([{"ts": 200}], many)
        self.assertFalse(out["卡片按時間遞增"]["ok"])

    def test_small_limit_returning_oldest_is_caught(self):
        """進場只抓少量時若拿到最舊那段,使用者一進去就只看得到老訊息。"""
        many = [{"ts": 100}, {"ts": 200}, {"ts": 300}]
        out = self._run([{"ts": 100}], many)      # 少量卻回了最舊
        self.assertFalse(out["小 limit 給最新段"]["ok"])

    def test_correct_stream_passes(self):
        many = [{"ts": 100}, {"ts": 200}, {"ts": 300}]
        out = self._run([{"ts": 300}], many)
        self.assertTrue(out["卡片按時間遞增"]["ok"])
        self.assertTrue(out["小 limit 給最新段"]["ok"])


if __name__ == "__main__":
    unittest.main()
