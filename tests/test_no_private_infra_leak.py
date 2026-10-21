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
import hashlib
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 要擋的形狀。用組字避免這支測試自己命中自己(與 opensource-check.sh 同手法)。
# ── 守門分兩層 ────────────────────────────────────────────────────────────
#
# 2026-10-20 改寫。原版把要擋的識別用組字寫死在這裡(`"tail" + "905550"`)。
# 那個手法只防「這支測試自己命中自己」,**完全不防揭露** —— 任何人打開這個
# 檔案就拿到了 tailnet 名、私有網域與維護者帳號。守門檔本身是全 repo 最顯眼
# 的那份清單,這很荒謬。
#
# 改成:
#   第一層 **形狀**(SHAPES)—— 不含任何識別,擋的是「長得像私有基礎建設」
#     的東西:任何 `*.ts.net` 主機名、任何 `/Users/<人名>` 路徑。好處是連
#     **別人的** tailnet 也會被擋下來(外部貢獻者貼自己的除錯輸出是最常見的
#     洩漏路徑,原版完全攔不到)。
#   第二層 **雜湊**(SECRET_HASHES)—— 形狀攔不到的具名識別(私有網域)只存
#     salted SHA-256。掃描時把文字裡每個候選網域取出來比雜湊,比得中才紅。
#     檔案裡看不到原文。
#
# 例外一律用「明顯是佔位」的字樣白名單,不是用識別本身。
#   這份白名單是**形狀守門唯一的放行依據**,所以寧可明確列舉這個 repo 實際
#   在用的佔位寫法,也不要用模糊的規則 —— 放行規則一模糊,守門就會在某天
#   安靜地放過真東西。新增佔位寫法時一起加進來。
_PLACEHOLDER = re.compile(
    r"your|example|sample|placeholder|<|__|\{\{|changeme|redacted|REPLACE"
    r"|\byou\b"          # /Users/you
    r"|tail1234",        # 測試 fixture 虛構的 tailnet(lobster.tail1234.ts.net)
    re.I)

SHAPES = {
    # tailnet 主機名:`<host>.<tailnet>.ts.net`。佔位(`<your-tailnet>` /
    # `tailnet-example`)放行。
    "tailnet 主機名": re.compile(r"[^\s/\"'`]*\.ts\.net\b"),
    # 家目錄:macOS 的 `/Users/<人名>`。`/Users/you`、`/Users/<you>` 這種
    # 佔位放行;`$HOME` 與 `~` 本來就不會命中(這也是正解,見下面的 ⚠️)。
    "家目錄路徑": re.compile(r"/Users/[^\s/\"'`:)\]]+"),
}

# salt 寫死沒關係:這不是密碼學用途,只是避免原文出現在公開 repo 裡。
# 要新增一條:`python -c "import hashlib;print(hashlib.sha256(b'leak-guard-v1:'+input().strip().encode()).hexdigest())"`
_SALT = b"leak-guard-v1:"
SECRET_HASHES = {
    # 維護者的私有網域(註冊網域層,不含子網域)。
    "ef3a40657b7affc831ea05732d44d2f1c1dff4d3739e7d0e953c892aa167d072":
        "私有網域",
}
# 候選網域:`<label>.<tld>` 的註冊網域形式。前面是 `@` 的(信箱)刻意**不取**
# —— `m@<domain>` 是公開聯絡位址(隱私政策、App Store 聯絡人、本 repo 的
# SECURITY.md 都印著它),一個收漏洞回報的 repo 沒有信箱才是問題。
_DOMAIN_CANDIDATE = re.compile(r"(?<![@\w])([\w-]+\.[a-z]{2,12})(?![\w])", re.I)

# 允許的例外:本檔自己(形狀 pattern 不含識別,但註解裡會提到 `.ts.net`)。
ALLOW = {"tests/test_no_private_infra_leak.py"}

# ⚠️ 2026-10-19 的自傷教訓,寫在這裡給下一個人:`scripts/run-tests.sh` 的
#    venv 路徑曾被盲目替換成佔位字串,98 個測試檔當場全紅。**操作性路徑要用
#    `$HOME`,不是佔位字串** —— 佔位字串是給文件看的,不是給 shell 執行的。


def _sample(hit):
    """失敗訊息裡的例子。**預設不印原文** —— 這支會在公開 repo 的 CI 裡跑,
    而 Actions 日誌是公開的:把洩漏內容印進日誌,等於換個地方洩一次。
    本機修的時候用 `LEAK_GUARD_VERBOSE=1 python tests/test_no_private_infra_leak.py`
    看原文。"""
    if os.environ.get("LEAK_GUARD_VERBOSE") == "1":
        return f"  例:{hit}"
    return ""


def _domain_hits(text):
    """文字裡出現、且雜湊比得中的註冊網域。回傳 {label: 次數}。"""
    counts = {}
    for m in _DOMAIN_CANDIDATE.finditer(text):
        h = hashlib.sha256(_SALT + m.group(1).lower().encode()).hexdigest()
        label = SECRET_HASHES.get(h)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return counts


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
            for label, rx in SHAPES.items():
                real = [m for m in rx.findall(text) if not _PLACEHOLDER.search(m)]
                if real:
                    hits.append(f"{rel}: {label} ×{len(real)}{_sample(real[0])}")
            for label, n in _domain_hits(text).items():
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
