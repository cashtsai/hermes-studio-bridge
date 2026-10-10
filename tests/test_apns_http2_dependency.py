"""APNs 走 HTTP/2,依賴宣告必須帶 h2(2026-10-10 實害)。

`bridge.py` 用 `httpx.AsyncClient(http2=True)` 發 APNs。httpx **不帶** h2,
少了它每一通推播都丟 ImportError 並被 `exc_swallowed` 吞掉 —— 機主那台
`push_notify_failed` × 204、`sent: 0`,整整幾週「回合跑完手機不會響」,
而推播正是遠端遙控這個產品的核心價值。

更糟的是依賴清單有**兩個抄本**(requirements.txt 與 Mac 安裝器 install-local-
bridge.sh 的 pip 行),兩邊都漏了 extras —— 也就是每個照 DMG 裝桌面端的使用者
都一樣壞,不是只有機主那台。

這條把兩個抄本一起釘住:只要還在用 http2=True,兩處就都得宣告 httpx[http2]。
"""
import _isolation  # noqa: F401
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class APNsHTTP2DependencyTest(unittest.TestCase):
    def test_bridge_still_uses_http2(self):
        """前提:真的還在用 HTTP/2。哪天不用了,下面兩條才可以放寬。"""
        self.assertIn("http2=True", _read("bridge.py"),
                      "bridge 不再用 http2 的話,請一併更新本測試的前提")

    def test_requirements_declares_http2_extra(self):
        req = _read("requirements.txt")
        self.assertRegex(req, r"(?m)^httpx\[http2\]",
                         "requirements.txt 要宣告 httpx[http2],不是裸 httpx")
        self.assertNotRegex(req, r"(?m)^httpx\s*$",
                            "還留著裸 httpx 那行")

    def test_mac_installer_declares_http2_extra(self):
        """DMG 跑的就是這支;它自己寫死一份依賴清單,不讀 requirements.txt。"""
        sh = _read("deploy/install-local-bridge.sh")
        pip_lines = [l for l in sh.splitlines()
                     if "pip install" in l or re.search(r"^\s+fastapi uvicorn", l)]
        joined = "\n".join(pip_lines)
        self.assertIn("httpx[http2]", joined,
                      "Mac 安裝器的 pip 行要帶 [http2] extras")
        self.assertNotRegex(joined, r"\shttpx\s",
                            "Mac 安裝器還留著裸 httpx")


if __name__ == "__main__":
    unittest.main()
