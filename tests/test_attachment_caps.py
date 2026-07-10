"""附件限制修復單(bridge 端)測試:件數閥、單檔閥、估算器。"""
import base64
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="caps-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class TestAttGuard(unittest.TestCase):
    def test_within_limit_pass(self):
        bridge._att_guard([{}] * bridge._ATT_MAX_COUNT)   # 不炸
        bridge._att_guard(None)
        bridge._att_guard("not-a-list")

    def test_over_limit_413(self):
        with self.assertRaises(HTTPException) as cm:
            bridge._att_guard([{}] * (bridge._ATT_MAX_COUNT + 1))
        self.assertEqual(cm.exception.status_code, 413)


class TestSizeEstimate(unittest.TestCase):
    def test_estimate_close_to_actual(self):
        raw = os.urandom(9000)
        uri = "data:application/octet-stream;base64," + base64.b64encode(raw).decode()
        est = bridge._data_uri_estimated_bytes(uri)
        self.assertAlmostEqual(est, len(raw), delta=4)

    def test_non_base64_zero(self):
        self.assertEqual(bridge._data_uri_estimated_bytes("hello"), 0)
        self.assertEqual(bridge._data_uri_estimated_bytes(""), 0)


class TestSaveDataURICap(unittest.TestCase):
    def setUp(self):
        # 落盤導到 tmp,不碰 production UPLOAD_DIR
        from pathlib import Path
        self._orig = bridge.UPLOAD_DIR
        bridge.UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="caps-up-"))

    def tearDown(self):
        bridge.UPLOAD_DIR = self._orig

    def test_oversize_rejected(self):
        big = base64.b64encode(b"x" * (bridge._ATT_MAX_FILE_BYTES + 1024)).decode()
        self.assertIsNone(bridge._save_data_uri(f"data:text/plain;base64,{big}", "big.txt"))

    def test_normal_saved(self):
        uri = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
        path = bridge._save_data_uri(uri, "hello.txt")
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
