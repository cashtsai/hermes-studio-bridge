"""護欄:公開 repo 裡不准出現維護者的私有基礎建設識別。

## 為什麼要有這支

`cashtsai/hermes-studio-bridge` 是**公開 repo**,但從來沒有套過任何出鏡守門
(app 那邊的 `scripts/opensource-check.sh` 早就在擋同一批 pattern)。
2026-10-19 掃出 101 處:tailnet 名、私有網域、`/Users/<user>` 路徑,
散在產品碼註解、測試 fixture 與文件裡。

**沒有金鑰外洩**(token 形狀全部 0 命中),但 bridge 是一個會執行工具的
agent 閘道 —— 把 tailnet 名、主機名與路徑佈局公開,等於把偵察階段送給對方。

最毒的一處是 `pocket-pair.py` 的 `HOST` 預設值直接寫死維護者的 tunnel:
任何人跑那支腳本而沒設 env,就會產生一張指向**別人機器**的配對 QR。

⚠️ 清洗只能管「現在的檔案」,git 歷史裡的仍在。這支的作用是**不再新增**。
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 要擋的形狀。用組字避免這支測試自己命中自己(與 opensource-check.sh 同手法)。
PATTERNS = {
    "tailnet 名": re.compile(r"tail" + r"905550"),
    "私有網域": re.compile(r"tsai" + r"\.cash"),
    "維護者家目錄": re.compile(r"/Users/" + r"xcash"),
}
# 允許的例外:本檔自己。
ALLOW = {"tests/test_no_private_infra_leak.py"}


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [p for p in out.splitlines() if p and p not in ALLOW]


class NoPrivateInfraLeak(unittest.TestCase):
    def test_tracked_files_have_no_private_infra_identifiers(self):
        hits = []
        for rel in tracked_files():
            path = os.path.join(ROOT, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue            # 二進位/符號連結:跳過,不是我們要管的
            for label, rx in PATTERNS.items():
                n = len(rx.findall(text))
                if n:
                    hits.append(f"{rel}: {label} ×{n}")
        self.assertEqual(hits, [], "公開 repo 不准帶私有基礎建設識別:\n  "
                                   + "\n  ".join(hits))

    def test_pair_script_has_no_baked_in_host(self):
        """配對腳本不准預設指向任何人的主機 —— 缺 env 要明確中止,不是安靜地
        產出一張指向別人機器的 QR。"""
        with open(os.path.join(ROOT, "pocket-pair.py"), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'HOST\s*=\s*os\.environ\.get\(\s*"POCKET_FUNNEL_HOST"\s*,\s*"([^"]*)"',
                      src)
        self.assertIsNotNone(m, "找不到 HOST 的取值方式(改版了?請同步更新本測試)")
        self.assertEqual(m.group(1), "", "HOST 不可以有非空預設值")
        self.assertIn("POCKET_FUNNEL_HOST is not set", src,
                      "缺 env 時要明確講出來,不可以安靜繼續")


if __name__ == "__main__":
    unittest.main(verbosity=2)
