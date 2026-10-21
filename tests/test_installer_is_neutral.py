"""護欄:安裝器是給**別人**跑的,不准帶維護者自己的東西。

## 為什麼要有這支

2026-10-21 上架前走查「全新使用者從安裝到配對」的流程,抓到兩件:

1. `install-local-bridge.sh` 會建 `profiles/fliper`、`profiles/xcash`、
   `profiles/shuijing` 三個目錄 —— 維護者自己的三個人格。而 bridge 的
   `_personas_from_disk()` **直接拿目錄名當人格 id 與顯示名**,所以每個新使用者
   裝完、配對完、打開 app 的**第一眼**,人格清單是三個陌生人的名字。

2. `POCKET_INSTALL_OPENCLAW` 預設 `1`:下載 Node、npm 裝 openclaw、寫一個
   **開機常駐**的 LaunchAgent —— 而出貨版 app 沒有任何 OpenClaw 入口。
   每個新使用者都被裝了一個用不到的常駐服務。

兩件都不會報錯,只會讓產品看起來像「別人的東西裝到我電腦上」。這支把它們
釘住 —— 與 `test_no_private_infra_leak.py` 是同一個家族(那支管識別字串,
這支管**安裝器的行為**),但擋的是不同的東西,所以分開寫。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "deploy", "install-local-bridge.sh")


def installer_src():
    with open(INSTALLER, encoding="utf-8") as f:
        return f.read()


class InstallerIsNeutral(unittest.TestCase):
    def test_does_not_create_named_persona_profiles(self):
        """不准預先建任何 `profiles/<名字>`。

        判準是形狀而不是名單:只要出現 `$HERMES_HOME_ROOT/profiles/<某個名字>`
        就紅 —— 這樣換一批名字照樣擋得到。使用者要幾個人格、叫什麼,是他的事。
        """
        hits = re.findall(r'HERMES_HOME_ROOT/profiles/([A-Za-z0-9_-]+)', installer_src())
        self.assertEqual(hits, [],
                         "安裝器不該預先建具名的人格目錄(bridge 會拿目錄名當人格名):"
                         + ", ".join(sorted(set(hits))))

    def test_openclaw_is_opt_in(self):
        """OpenClaw 預設不裝 —— 出貨版 app 沒有它的入口。

        要開放的話是使用者自己帶 `POCKET_INSTALL_OPENCLAW=1`,不是預設替他決定
        要不要多一個開機常駐的背景服務。
        """
        m = re.search(r'INSTALL_OPENCLAW="\$\{POCKET_INSTALL_OPENCLAW:-(\d)\}"',
                      installer_src())
        self.assertIsNotNone(m, "找不到 INSTALL_OPENCLAW 的取值方式(改版了?同步更新本測試)")
        self.assertEqual(m.group(1), "0",
                         "OpenClaw 預設要是 0:出貨版 app 沒有入口,裝了等於白裝一個常駐服務")

    def test_hermes_stays_opt_out_not_removed(self):
        """反向釘住:Hermes **預設要裝**。

        它跟 OpenClaw 不同 —— 人格是出貨版 app 看得到、用得到的功能,而且是
        預設 provider。寫這條是怕有人照著上面兩條的邏輯「順手」也把 Hermes
        關掉,那會讓新使用者配對完看到一個空的人格清單。
        """
        m = re.search(r'INSTALL_HERMES="\$\{POCKET_INSTALL_HERMES:-(\d)\}"', installer_src())
        self.assertIsNotNone(m, "找不到 INSTALL_HERMES 的取值方式")
        self.assertEqual(m.group(1), "1", "Hermes 預設要裝 —— 它是出貨版用得到的 provider")


if __name__ == "__main__":
    unittest.main(verbosity=2)
