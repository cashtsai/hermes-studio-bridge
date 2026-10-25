"""公網候選是**預設**,不是進階選項(`_quick_tunnel_url` / `_pair_tunnel_url`)。

建立原因:2026-10-25 機主人在外面(5G、手機沒裝 Tailscale),Pocket 顯示
「桌面沒有回應」。查出來桌面端一切正常 —— bridge 活著、Pocket Connect 開的
免費 quick tunnel 也活著且公網實測 200,但**bridge 不知道那條通道存在**:
寫 `~/.pocket/tunnel-url` 的程式只有 `deploy/install-ubuntu.sh`,macOS 沒有
對應物,而 Pocket Connect 把網址留在自己 app 裡(`autoTunnelURL`)。
於是候選清單只剩 LAN+tailnet,**推播中繼還忠實地把這份殘缺清單推給手機
覆蓋掉原本能用的公網位址** → 一出門就整個連不上。

一般使用者沒有自己的網域、也不該被要求裝 VPN,所以公網保底必須自己找得到。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="public-tunnel-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class _Resp:
    """urlopen 的 context-manager 替身。"""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _ps(lines):
    class _R:
        stdout = "\n".join(lines)
    return _R()


class QuickTunnelDiscovery(unittest.TestCase):
    def setUp(self):
        bridge._QUICK_TUNNEL_CACHE.update({"ts": 0.0, "url": None})

    def test_finds_tunnel_that_serves_bridge_port(self):
        port = bridge._BRIDGE_PORT
        with patch.object(bridge.subprocess, "run") as run:
            run.side_effect = [
                _ps(["33888 /Applications/Pocket.app/Contents/Resources/cloudflared "
                     "tunnel --no-autoupdate --url http://127.0.0.1:%d" % port]),
                _ps(["cloudflared 33888 you 9u IPv4 TCP 127.0.0.1:20244 (LISTEN)"]),
            ]
            with patch.object(bridge.urllib.request, "urlopen",
                              return_value=_Resp({"hostname": "ceiling-juan.trycloudflare.com"})):
                self.assertEqual(bridge._quick_tunnel_url(),
                                 "https://ceiling-juan.trycloudflare.com")

    def test_ignores_tunnels_for_other_services(self):
        """這台常同時跑好幾條通道(官網/後台/其他服務)。撈錯就會把**別人的網址**
        當成 Pocket 的廣告出去,比沒有候選更糟。"""
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value = _ps([
                "1793 /opt/homebrew/bin/cloudflared tunnel --config "
                "/Users/you/.cloudflared/fed-console-studio.yml run",
                "1805 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:8088",
            ])
            self.assertEqual(bridge._cloudflared_metrics_ports(), [])

    def test_absent_tunnel_is_none_not_guess(self):
        with patch.object(bridge, "_cloudflared_metrics_ports", return_value=[]):
            self.assertIsNone(bridge._quick_tunnel_url())


class TunnelUrlPriority(unittest.TestCase):
    """自訂網域(進階/付費)優先於免費 quick tunnel —— 固定網址不會漂。"""

    def setUp(self):
        bridge._QUICK_TUNNEL_CACHE.update({"ts": 0.0, "url": None})

    def test_env_custom_domain_wins(self):
        with patch.dict(os.environ, {"POCKET_TUNNEL_URL": "https://pocket.example.com"}):
            with patch.object(bridge, "_quick_tunnel_url") as q:
                self.assertEqual(bridge._pair_tunnel_url(), "https://pocket.example.com")
                q.assert_not_called()

    def test_falls_back_to_quick_tunnel_when_nothing_configured(self):
        """核心保證:使用者什麼都沒設,照樣要有公網候選。"""
        missing = os.path.join(_TMP, "no-such-tunnel-url")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_TUNNEL_URL", None)
            with patch.object(bridge, "_POCKET_TUNNEL_URL_FILE", missing):
                with patch.object(bridge, "_quick_tunnel_url",
                                  return_value="https://auto.trycloudflare.com"):
                    self.assertEqual(bridge._pair_tunnel_url(),
                                     "https://auto.trycloudflare.com")

    def test_empty_file_does_not_swallow_quick_tunnel(self):
        """空檔案(unit 還沒寫入/被清空)不能擋住自動偵測 —— 原本的寫法
        `return url or None` 會在這裡直接回 None,公網就沒了。"""
        empty = os.path.join(_TMP, "empty-tunnel-url")
        with open(empty, "w", encoding="utf-8") as f:
            f.write("   \n")
        os.environ.pop("POCKET_TUNNEL_URL", None)
        with patch.object(bridge, "_POCKET_TUNNEL_URL_FILE", empty):
            with patch.object(bridge, "_quick_tunnel_url",
                              return_value="https://auto.trycloudflare.com"):
                self.assertEqual(bridge._pair_tunnel_url(),
                                 "https://auto.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()
