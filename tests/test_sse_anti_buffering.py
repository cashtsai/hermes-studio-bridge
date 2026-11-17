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
import sys
import unittest

import bridge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from sentinel import corpus_sentinel  # noqa: E402

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

        # 語料下限哨兵:這條的綠色有兩種來源 —— 「每條 SSE 路由都帶標頭」與
        # 「`media_type="text/event-stream"` 這個字面樣式配不到了」
        # (改成單引號、拉成變數、用 StreamingResponse 的別名都會這樣)。
        # 後者是整條守門安靜地退場,必須紅。見 scripts/sentinel/corpus_sentinel.py。
        sites = [i for i, line in enumerate(lines)
                 if 'media_type="text/event-stream"' in line]
        corpus_sentinel.require("sse_headers.response_sites", len(sites),
                                detail="(SSE 回應的寫法改了?掃不到路由 ⇒ "
                                       "0 個漏標頭 ⇒ 這支會「全部都帶了」地綠)")

        misses = []
        for i in sites:
            line = lines[i]
            window = "\n".join(lines[max(0, i - 3):i + 4])
            # 接受兩種寫法:`headers=SSE_HEADERS`,以及需要再加欄位時的
            # `headers={**SSE_HEADERS, ...}`。只認前者會逼人為了過閘而放棄
            # 合法的展開寫法 —— 閘門要擋的是「忘了帶」,不是「寫法不同」。
            if not re.search(r"headers\s*=\s*(SSE_HEADERS\b|\{\s*\*\*\s*SSE_HEADERS\b)",
                             window):
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
