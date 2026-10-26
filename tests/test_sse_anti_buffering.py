"""每一條 SSE 回應都必須帶反緩衝標頭 —— 漏一條就會有人連不上。

2026-10-26 實測(同一條 session、同一時刻、同一支 curl):
    直連 127.0.0.1:8081    → keepalive 精準每 2 秒一行,30 秒不斷
    Cloudflare quick tunnel → HTTP/2 200,**30 秒 0 byte**

當時 11 條 SSE 路由裡只有 3 條帶 `X-Accel-Buffering: no`,而卡片流
`/app/v2/sessions/{id}/events` 正好是漏掉的那條 —— 也正好是手機實際走 tunnel
的那條。後果:app 端 30 秒 timeout 必然觸發 → 串流每 ~36 秒死一次 → 熱重連踏步機
(build 186 實測:逾時 178 次、串流關 348 次其中 319 次短命、降級 1054 秒)。

**為什麼要用靜態掃描而不是逐一打端點**:病根是「新增路由時忘了帶」,
不是「某一條壞了」。掃原始碼才能涵蓋**全部**路由,包含需要 live provider、
測試環境起不起來的那些。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import re
import unittest

import bridge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "bridge.py")


class SSEAntiBufferingTests(unittest.TestCase):

    def test_constant_has_both_headers(self):
        """少任何一個都不夠:no-transform 擋改寫,X-Accel-Buffering 擋緩衝。"""
        self.assertEqual(bridge.SSE_HEADERS.get("X-Accel-Buffering"), "no")
        self.assertIn("no-transform", bridge.SSE_HEADERS.get("Cache-Control", ""))

    def test_every_sse_response_carries_the_headers(self):
        """每一處 `media_type="text/event-stream"` 的鄰近 3 行內要有 headers=SSE_HEADERS。

        鄰近視窗而不是整檔比數量 —— 數量相等也可能是「A 多帶一次、B 漏一次」。
        """
        with open(SRC, encoding="utf-8") as f:
            lines = f.read().split("\n")

        misses = []
        for i, line in enumerate(lines):
            if 'media_type="text/event-stream"' not in line:
                continue
            window = "\n".join(lines[max(0, i - 3):i + 4])
            if "headers=SSE_HEADERS" not in window:
                misses.append(f"bridge.py:{i + 1}: {line.strip()[:90]}")

        self.assertEqual(
            misses, [],
            "這些 SSE 路由沒帶反緩衝標頭 —— 走 Cloudflare / nginx 的客戶端會\n"
            "收不到 keepalive,然後在自己的 timeout 砍掉連線(見本檔 docstring):\n  "
            + "\n  ".join(misses))

    def test_no_hand_rolled_header_dicts_remain(self):
        """不准再出現手寫的那一份 —— 兩種寫法並存就是「下次又漏一條」的溫床。"""
        with open(SRC, encoding="utf-8") as f:
            src = f.read()
        # 常數定義本身那一行不算(它就是唯一該出現字面值的地方)。
        hand_rolled = re.findall(r'"X-Accel-Buffering"\s*:\s*"no"', src)
        self.assertEqual(
            len(hand_rolled), 1,
            f"找到 {len(hand_rolled)} 處字面值,應該只有 SSE_HEADERS 定義那一處;"
            "其餘一律改用 headers=SSE_HEADERS")

    def test_sse_routes_exist_at_all(self):
        """守門自己的守門:如果有人把掃描字串改掉,上面兩條會變成空集合恆真。"""
        with open(SRC, encoding="utf-8") as f:
            src = f.read()
        n = src.count('media_type="text/event-stream"')
        self.assertGreaterEqual(
            n, 8, f"只掃到 {n} 條 SSE 路由,遠少於預期 —— 掃描條件可能已失效")


if __name__ == "__main__":
    unittest.main()
