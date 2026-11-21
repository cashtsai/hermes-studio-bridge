"""/model 全域外溢防護(2026-11-21 機主回報:Pocket 切 session 模型,
官方 app 的全域預設被連帶改掉 —— 近版 CC 的 /model 會持久化寫設定檔)。

驗項:外溢值被還原 / 原本沒有欄位 → 外溢欄位被刪 / 值沒變 → 檔案不動 /
壞 JSON → 跳過不炸。
"""
import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cc-model-guard-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class CCModelGlobalGuardTest(unittest.TestCase):
    def _files(self, n=2):
        return [os.path.join(tempfile.mkdtemp(prefix="guard-"), f"f{i}.json")
                for i in range(n)]

    def test_外溢值被還原(self):
        f1, f2 = self._files()
        json.dump({"model": "claude-fable-5", "other": 1}, open(f1, "w"))
        json.dump({"model": "claude-fable-5"}, open(f2, "w"))
        with patch.object(bridge, "_CC_GLOBAL_MODEL_FILES", (f1, f2)):
            snap = bridge._cc_snapshot_global_model()
            # 模擬 /model 外溢
            json.dump({"model": "claude-opus-5-5", "other": 1}, open(f1, "w"))
            json.dump({"model": "claude-opus-5-5"}, open(f2, "w"))
            bridge._cc_restore_global_model(snap)
        self.assertEqual(json.load(open(f1))["model"], "claude-fable-5")
        self.assertEqual(json.load(open(f1))["other"], 1)   # 其他欄位不動
        self.assertEqual(json.load(open(f2))["model"], "claude-fable-5")

    def test_原本沒有欄位則刪掉外溢(self):
        (f1,) = self._files(1)
        json.dump({"other": 2}, open(f1, "w"))
        with patch.object(bridge, "_CC_GLOBAL_MODEL_FILES", (f1,)):
            snap = bridge._cc_snapshot_global_model()
            json.dump({"model": "claude-opus-5", "other": 2}, open(f1, "w"))
            bridge._cc_restore_global_model(snap)
        data = json.load(open(f1))
        self.assertNotIn("model", data)
        self.assertEqual(data["other"], 2)

    def test_值沒變就不動檔案(self):
        (f1,) = self._files(1)
        json.dump({"model": "claude-fable-5"}, open(f1, "w"))
        with patch.object(bridge, "_CC_GLOBAL_MODEL_FILES", (f1,)):
            snap = bridge._cc_snapshot_global_model()
            before = os.stat(f1).st_mtime_ns
            bridge._cc_restore_global_model(snap)
        self.assertEqual(os.stat(f1).st_mtime_ns, before)

    def test_壞檔跳過不炸(self):
        (f1,) = self._files(1)
        open(f1, "w").write("{not json")
        with patch.object(bridge, "_CC_GLOBAL_MODEL_FILES", (f1,)):
            snap = bridge._cc_snapshot_global_model()
            bridge._cc_restore_global_model(snap)   # 不該丟例外
        self.assertEqual(open(f1).read(), "{not json")   # 壞檔原樣


if __name__ == "__main__":
    unittest.main()
