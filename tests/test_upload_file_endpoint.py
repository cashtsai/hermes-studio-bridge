"""逐件 multipart 上傳端點(/app/v1/uploads/file)。

存在的理由:舊的 /app/v1/uploads 是「一次收整包 base64 JSON」,client 在整個
請求送完之前拿不到任何回饋 —— app 端因此**畫不出**單一附件的上傳進度。
逐件 + 原始位元組之後才有辦法用 URLSession 的 didSendBodyData 畫進度條。

這組測試釘住:
  1. 正常收檔:落盤、回傳 path/size,而且內容位元組一致
  2. 超過單檔上限 → 413,**且不留下半套截斷檔**
  3. 舊端點沒被動到(舊版 app 與離線補送都還走它)
  4. 兩條收檔路徑用同一套命名規則
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="upload-file-canon-")
os.environ["HOME"] = tempfile.mkdtemp(prefix="upload-file-home-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {"Authorization": "Bearer test-unit-token"}


def _reset_auth_throttle():
    """認證失敗限流器是行程全域的 —— 同視窗內失敗超過 `_AUTH_FAIL_MAX`
    就改回 429，於是 test_requires_auth 期待的 401 在全套
    `unittest discover` 一起跑時會變成 429(單檔跑卻是綠的)。
    只在測試側歸零，不動正式限流行為。

    容器名稱隨 main 演進過(全域 `_AUTH_FAILS` deque → per-client 分桶
    `_AUTH_FAILS_BY_CLIENT`)，兩種名字都清、都用 getattr 取。"""
    with bridge._AUTH_LOCK:
        for attr in ("_AUTH_FAILS", "_AUTH_FAILS_BY_CLIENT", "_AUTH_FAIL_AGG"):
            container = getattr(bridge, attr, None)
            if container is not None:
                container.clear()


class UploadFileEndpointTest(unittest.TestCase):
    def setUp(self):
        _reset_auth_throttle()
        self.client = TestClient(bridge.app)
        self.before = set(self._uploads())

    def tearDown(self):
        _reset_auth_throttle()

    def _uploads(self):
        try:
            return [p.name for p in bridge.UPLOAD_DIR.iterdir()]
        except FileNotFoundError:
            return []

    def _new_files(self):
        return set(self._uploads()) - self.before

    def test_saves_bytes_and_reports_size(self):
        raw = os.urandom(4096)
        r = self.client.post("/app/v1/uploads/file", headers=AUTH,
                             files={"file": ("shot.png", raw, "image/png")},
                             data={"kind": "image", "filename": "shot.png",
                                   "mime": "image/png"})
        self.assertEqual(r.status_code, 200, r.text)
        att = r.json()["attachment"]
        self.assertEqual(att["size"], len(raw))
        self.assertEqual(att["kind"], "image")
        self.assertEqual(att["filename"], "shot.png")
        # 真的落到磁碟,而且位元組一模一樣(不是只回了個路徑)
        with open(att["path"], "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_missing_filename_falls_back_to_part_name(self):
        r = self.client.post("/app/v1/uploads/file", headers=AUTH,
                             files={"file": ("fallback.bin", b"xyz",
                                             "application/octet-stream")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["attachment"]["filename"], "fallback.bin")

    def test_over_single_file_cap_is_413_and_leaves_no_partial(self):
        """超限要中止 **並刪掉半套檔**。留下截斷檔比直接失敗更糟 —— 它看起來
        像上傳成功了,之後誰都分不出那是壞檔。"""
        # Keep the test small; the production cap is intentionally 2GiB.
        with patch.object(bridge, "_ATT_MAX_FILE_BYTES", 64 * 1024):
            big = b"\0" * (bridge._ATT_MAX_FILE_BYTES + 1024)
            r = self.client.post("/app/v1/uploads/file", headers=AUTH,
                                 files={"file": ("huge.mov", big, "video/quicktime")},
                                 data={"kind": "file"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self._new_files(), set(), "超限卻留下了檔案")

    def test_requires_auth(self):
        r = self.client.post("/app/v1/uploads/file",
                             files={"file": ("a.txt", b"hi", "text/plain")})
        self.assertIn(r.status_code, (401, 403))

    def test_raw_stream_saves_bytes_and_decodes_filename_metadata(self):
        import base64
        raw = b"raw stream bytes"
        headers = {
            **AUTH,
            "Content-Type": "application/octet-stream",
            "X-Pocket-Kind": "file",
            "X-Pocket-Mime": "application/octet-stream",
            "X-Pocket-Filename-B64": base64.b64encode("簡報.bin".encode()).decode(),
        }
        # Raw attachment streams bypass the legacy JSON body guard.
        with patch.object(bridge, "_BODY_MAX_BYTES", 1):
            r = self.client.post("/app/v1/uploads/raw", headers=headers, content=raw)
        self.assertEqual(r.status_code, 200, r.text)
        att = r.json()["attachment"]
        self.assertEqual(att["filename"], "簡報.bin")
        self.assertEqual(att["size"], len(raw))
        with open(att["path"], "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_raw_stream_over_cap_is_413_and_leaves_no_partial(self):
        with patch.object(bridge, "_ATT_MAX_FILE_BYTES", 64 * 1024):
            raw = b"\0" * (bridge._ATT_MAX_FILE_BYTES + 1024)
            r = self.client.post("/app/v1/uploads/raw", headers={
                **AUTH,
                "Content-Type": "application/octet-stream",
                "X-Pocket-Filename-B64": "aGlnZS5iaW4=",
            }, content=raw)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self._new_files(), set(), "raw 超限卻留下了檔案")

    def test_legacy_batch_endpoint_still_works(self):
        """舊端點不准被動到:舊版 app 還在用,離線補送也走同一支。"""
        import base64
        raw = b"legacy bytes"
        uri = "data:text/plain;base64," + base64.b64encode(raw).decode()
        r = self.client.post("/app/v1/uploads", headers=AUTH,
                             json={"attachments": [{"kind": "file",
                                                    "filename": "old.txt",
                                                    "mime": "text/plain",
                                                    "data": uri}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["attachments"][0]["path"])


class SwiftClientBodyTest(unittest.TestCase):
    """跨語言契約:app 端的 multipart 是**手刻**的,少一個 \\r\\n 就會壞,而
    Swift 編譯器對這種事一律沒有意見。這裡不用 requests 幫忙組 body,而是逐位元組
    重現 app 送出的那一串(與 iOS 的 UploadMultipartBodyTests 的 expected 同形),
    確認 bridge 真的解得出來。兩邊在同一串 bytes 上會合。"""

    BOUNDARY = "pocket-TEST-BOUNDARY"

    def _swift_body(self, payload: bytes) -> bytes:
        b = self.BOUNDARY
        head = (
            f"--{b}\r\n"
            'Content-Disposition: form-data; name="kind"\r\n\r\n'
            "image\r\n"
            f"--{b}\r\n"
            'Content-Disposition: form-data; name="filename"\r\n\r\n'
            "shot.png\r\n"
            f"--{b}\r\n"
            'Content-Disposition: form-data; name="mime"\r\n\r\n'
            "image/png\r\n"
            f"--{b}\r\n"
            'Content-Disposition: form-data; name="file"; filename="shot.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        return head + payload + f"\r\n--{b}--\r\n".encode()

    def test_bridge_parses_the_body_the_app_actually_sends(self):
        client = TestClient(bridge.app)
        payload = bytes([1, 2, 3])
        r = client.post(
            "/app/v1/uploads/file",
            headers={**AUTH,
                     "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"},
            content=self._swift_body(payload))
        self.assertEqual(r.status_code, 200, r.text)
        att = r.json()["attachment"]
        self.assertEqual(att["kind"], "image")
        self.assertEqual(att["filename"], "shot.png")
        self.assertEqual(att["mime"], "image/png")
        self.assertEqual(att["size"], len(payload))
        with open(att["path"], "rb") as f:
            self.assertEqual(f.read(), payload)


class UploadDestPathTest(unittest.TestCase):
    """兩條收檔路徑共用同一個命名器 —— 各寫一份遲早會漂移。"""

    def test_sanitises_and_uniquifies(self):
        a = bridge._upload_dest_path("../../etc/passwd", "text/plain")
        self.assertNotIn("/", a.name)
        self.assertNotIn("..", a.name)
        b = bridge._upload_dest_path("../../etc/passwd", "text/plain")
        self.assertNotEqual(a.name, b.name, "同名檔要各自有唯一路徑,不能互相覆蓋")

    def test_adds_extension_from_mime_when_missing(self):
        p = bridge._upload_dest_path("noext", "image/png")
        self.assertTrue(p.name.endswith(".png"), p.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
