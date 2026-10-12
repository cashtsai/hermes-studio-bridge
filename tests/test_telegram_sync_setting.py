"""app→TG 鏡射改成使用者可自行決定(`feat/telegram-sync-setting`)。

機主 2026-10-12:「打開,並且把他修改成使用者可以自行決定要不要同步 TG」。

原本這條鏡射只有 env 旗標 `POCKET_TG_MIRROR_OUTBOUND`,而兩支 installer
都沒設 → **出貨等於永遠關著**,但 App Store 文案寫「桌面與手機同一份對話」
(本輪上架前稽核查到的不實宣稱之一)。

改成 **env > 使用者偏好 > 預設(on)**:
  ‧ env 有值一槌定音(含明示 `off`)—— 維運要能強制關掉,不被 App 設定蓋過
  ‧ 預設開是安全的 —— `mirror()` 解不到 target(沒接 TG bot)就
    `skipped="no_target"` 空轉,沒接 Telegram 的人完全不受影響
  ‧ 鏡射內容是使用者自己的訊息與自己人格的回覆,投到自己的 bot 聊天室,
    沒有第三方 → 不觸發送審問卷的 UGC 題
"""
import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="tg-sync-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_TG_SYNC_FILE"] = os.path.join(_TMP, "telegram-sync.json")
os.environ.pop("POCKET_TG_MIRROR_OUTBOUND", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_outbound  # noqa: E402

SYNC = os.environ["POCKET_TG_SYNC_FILE"]
# 模組常數在 import 時綁 env —— 確認指到暫存檔,不要污染機主的 ~/.pocket。
assert tg_outbound._SYNC_FILE == SYNC, tg_outbound._SYNC_FILE


class ModePrecedenceTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("POCKET_TG_MIRROR_OUTBOUND", None)
        try:
            os.remove(SYNC)
        except FileNotFoundError:
            pass

    tearDown = setUp

    def test_default_is_on(self):
        """沒 env、沒設過 → on。這就是「打開」那一半。"""
        self.assertIsNone(tg_outbound.user_enabled())
        self.assertEqual(tg_outbound.mode(), "on")

    def test_user_off_wins_over_default(self):
        tg_outbound.set_user_enabled(False)
        self.assertEqual(tg_outbound.mode(), "off")

    def test_user_on_is_explicit_too(self):
        tg_outbound.set_user_enabled(True)
        self.assertIs(tg_outbound.user_enabled(), True)
        self.assertEqual(tg_outbound.mode(), "on")

    def test_env_off_beats_user_on(self):
        """維運要能強制關掉 —— 不被 App 設定蓋過。"""
        tg_outbound.set_user_enabled(True)
        os.environ["POCKET_TG_MIRROR_OUTBOUND"] = "off"
        self.assertEqual(tg_outbound.mode(), "off")

    def test_env_on_beats_user_off(self):
        tg_outbound.set_user_enabled(False)
        os.environ["POCKET_TG_MIRROR_OUTBOUND"] = "on"
        self.assertEqual(tg_outbound.mode(), "on")

    def test_env_dry_still_supported(self):
        os.environ["POCKET_TG_MIRROR_OUTBOUND"] = "dry"
        self.assertEqual(tg_outbound.mode(), "dry")

    def test_env_garbage_means_off_not_default(self):
        """env 有值就是明示 —— 打錯字也不該悄悄退回預設的 on。"""
        tg_outbound.set_user_enabled(True)
        os.environ["POCKET_TG_MIRROR_OUTBOUND"] = "yes-please"
        self.assertEqual(tg_outbound.mode(), "off")

    def test_corrupt_pref_file_falls_back_to_default(self):
        with open(SYNC, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIsNone(tg_outbound.user_enabled())
        self.assertEqual(tg_outbound.mode(), "on")

    def test_pref_file_shape(self):
        tg_outbound.set_user_enabled(False)
        with open(SYNC, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"mirror_outbound": False})


class SafetyOfDefaultOnTest(unittest.TestCase):
    """預設開之所以安全,靠的是「解不到 target 就空轉」。

    這條測試是那個前提的守門 —— 哪天有人讓 `mirror()` 在沒有 target 時
    也往外送,預設開就會變成**替沒接 Telegram 的人開始對外投遞**。
    """

    def setUp(self):
        os.environ.pop("POCKET_TG_MIRROR_OUTBOUND", None)
        try:
            os.remove(SYNC)
        except FileNotFoundError:
            pass
        tg_outbound.reset_state()

    def test_no_target_skips_without_posting(self):
        import asyncio
        posted = []
        orig_resolve, orig_post = tg_outbound.resolve_target, tg_outbound._post

        async def fake_post(*a, **k):
            posted.append(a)
            return True

        tg_outbound.resolve_target = lambda home: None   # 沒接 TG bot
        tg_outbound._post = fake_post
        try:
            res = asyncio.run(tg_outbound.mirror(
                "/no/such/home", "xcash", "user", "哈囉", "mid-1"))
        finally:
            tg_outbound.resolve_target = orig_resolve
            tg_outbound._post = orig_post
        self.assertEqual(tg_outbound.mode(), "on", "前提:預設是開的")
        self.assertEqual(res.get("skipped"), "no_target")
        self.assertEqual(posted, [], "沒有 target 時一則都不准往外送")


if __name__ == "__main__":
    unittest.main(verbosity=2)
