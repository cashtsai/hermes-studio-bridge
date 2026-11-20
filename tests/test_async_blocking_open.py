"""`async def` 裡的同步 `open()` 只准變少,不准變多。

## 為什麼要這條守門(2026-11-20 事故)

bridge 卡死 17.5 小時。`sample` 顯示 **20 條 uvloop 檔案 I/O 執行緒 100%
全部停在 `open()` 這個 syscall**,池子耗盡 → 任何要碰檔案的端點永遠排不進
隊伍;而不碰檔案的路由(例如比對不到的路由回 404)照樣 3 毫秒回應。
行程活著、launchd 正常、CPU 1.4%、`S` 睡眠 —— **所有存活指標都說健康**,
所以是機主用「指令送不進去」發現的,不是監控。

## 為什麼是棘輪,不是「全部包逾時」

想過把每一處都改成 `wait_for(to_thread(open…))`。**沒用**:`wait_for` 只取消
「等待」,底下那條執行緒還是卡在 syscall 裡、照樣佔著池子的格子。逾時能讓
單一請求快點失敗,擋不住池子被榨乾。

真正有效的是兩件,這一檔管第二件:

  1. **不要去碰會卡住的路徑** —— `media_artifacts` 已擋掉虛擬/裝置掛載
     (`_VIRTUAL_MOUNT_MARKERS`;家目錄底下的 `CoreDevice/DeviceFS` 是
     fskit 掛載點,裝置一拔 `open()` 就永遠回不來)。
  2. **不要再長出新的** —— 就是這一檔。現有的 23 處是已知的債,凍結;
     再加就要先說明為什麼。

## 加新的一處該怎麼辦

先問自己:這個路徑有沒有可能落在網路磁碟、使用者空間檔案系統、或是
agent 餵進來的任意位置?會的話**不要用同步 open**。真的只是固定的本機
小檔(console 資產、設定檔),把數字 +1 並在這裡寫一行理由。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "bridge.py")

#: 2026-11-20 凍結。**只准往下調。**
#: 當時的分布:8 處固定路徑(console 資產 / CLIENT_LOG / 診斷目錄),
#: 15 處路徑可變(session 逐字稿、provider 輸入、上傳)。
BASELINE = 23


def _blocking_opens(path: str) -> list[tuple[int, str]]:
    """`async def` 內**直接**呼叫 `open()` 的位置。

    只認裸 `open(...)` —— `await to_thread(open, …)`、`anyio.open_file()`
    之類不算,那些正是我們希望別人改過去的寫法。
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found: list[tuple[int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.fn = None

        def visit_AsyncFunctionDef(self, node):
            prev, self.fn = self.fn, node.name
            self.generic_visit(node)
            self.fn = prev

        def visit_FunctionDef(self, node):
            # 同步函式裡的 open 不在範圍內(它本來就不該跑在事件迴圈上,
            # 那是另一個問題),巢狀的同步 def 也要把狀態清掉。
            prev, self.fn = self.fn, None
            self.generic_visit(node)
            self.fn = prev

        def visit_Call(self, node):
            if self.fn and getattr(node.func, "id", None) == "open":
                found.append((node.lineno, self.fn))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


class AsyncBlockingOpenRatchetTests(unittest.TestCase):

    def test_count_never_grows(self):
        hits = _blocking_opens(SRC)
        self.assertLessEqual(
            len(hits), BASELINE,
            "async def 裡的同步 open() 變多了。\n"
            "2026-11-20 的事故就是 20 條檔案 I/O 執行緒全部卡在 open() ——\n"
            "新增的位置:\n  "
            + "\n  ".join(f"bridge.py:{ln} 在 {fn}()"
                          for ln, fn in hits[BASELINE:])
            + "\n先確認那個路徑不可能落在網路磁碟/使用者空間檔案系統/"
              "agent 餵進來的任意位置;真的只是固定本機小檔,"
              "再把 BASELINE 調高並在此寫下理由。")

    def test_baseline_is_not_stale(self):
        """債還完了就要把數字調下來,否則棘輪會鬆掉。

        這條是守門自己的守門:如果有人把一半的 open 改成非阻塞寫法卻沒有
        調降 BASELINE,棘輪就等於留了一堆空額,之後新增的債會偷偷溜進來。
        """
        hits = _blocking_opens(SRC)
        self.assertGreaterEqual(
            len(hits), BASELINE,
            f"實際只剩 {len(hits)} 處,低於 BASELINE={BASELINE} —— "
            f"請把 BASELINE 調成 {len(hits)}(棘輪只准往前轉)")

    def test_media_store_never_opens_virtual_mounts(self):
        """同一場事故的另一半防線,放在這裡一起看守。

        `media_artifacts` 的允許範圍含整個家目錄,而家目錄底下有
        `CoreDevice/DeviceFS`(fskit 裝置掛載點)。那條守門若被拿掉,
        逐字稿裡出現一個裝置路徑就足以讓整台 bridge 再停擺一次。
        """
        import media_artifacts
        self.assertTrue(media_artifacts._VIRTUAL_MOUNT_MARKERS)
        self.assertTrue(media_artifacts._is_virtual_mount(
            os.path.expanduser(
                "~/Library/Developer/CoreDevice/DeviceFS/d/x.mp4")))
        self.assertFalse(media_artifacts._is_virtual_mount(
            os.path.expanduser("~/apps/project/out.pdf")))


if __name__ == "__main__":
    unittest.main()
