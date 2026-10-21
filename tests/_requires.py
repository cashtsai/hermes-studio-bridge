"""缺依賴時**誠實跳過**,而不是紅著或假裝通過。

## 為什麼

這個 repo 有一批測試依賴的不是程式碼,而是**某台機器上裝了什麼**:
Hermes agent 安裝、`codex` binary、macOS 的 zsh、PyYAML。它們在維護者的機器
上跑得好好的,在 CI 與別人的機器上一律紅。

過去的處理是把它們列進 `tests/ci-skip.txt` —— 但那只在 CI 生效,本機還是
紅著。而**一條沒人修的紅線會讓整份測試報告失去意義**:反正本來就有一條紅的,
再多一條也看不出來。`test_hermes_media` 就這樣被當成「既有環境債」帶了好幾週。

這支把它變成三態中的第三態:

    綠 = 跑過了    紅 = 真的壞了    跳過 = 這台機器沒有它需要的東西

## 為什麼不是直接裝那個套件

`pip install PyYAML` 當然更快,但那只修好**這一台**。下一個 clone 這個 repo
的人會再踩一次,而且他不會知道那條紅線是環境問題還是程式壞了。

## 退出碼 77

`scripts/run-tests.sh` 是逐檔 `python tests/x.py`,所以模組層丟 `SkipTest`
會變成未捕捉例外 → 非 0 → 被算成紅的。這裡改用 **exit 77**(autotools 的
慣例碼),runner 認得它並記成 SKIP。

⚠️ 刻意**不**用 exit 0:那會讓跳過的檔案在報告裡顯示成 PASS —— 「看起來全綠、
其實沒跑」比紅色更危險,這正是本 repo 一再踩到的那種病。
"""
import importlib
import os
import sys
import unittest

SKIP_EXIT_CODE = 77


def _bail(message: str):
    """直接執行 → exit 77;被 discover import → 正常的 SkipTest。

    判準是**呼叫端**的模組名:逐檔跑時測試檔自己就是 `__main__`。
    (往上兩層:_bail ← module()/executable() ← 測試檔。)
    """
    caller = sys._getframe(2).f_globals.get("__name__")
    if caller == "__main__":
        print(f"SKIP: {message}")
        raise SystemExit(SKIP_EXIT_CODE)
    raise unittest.SkipTest(message)


def module(name: str, why: str):
    """需要某個 Python 模組,沒有就跳過。回傳該模組。"""
    try:
        return importlib.import_module(name)
    except ImportError:
        _bail(f"這台機器沒有 {name} —— {why}")


def executable(path: str, why: str):
    """需要某個可執行檔(絕對路徑),沒有就跳過。"""
    if not os.access(path, os.X_OK):
        _bail(f"這台機器沒有 {path} —— {why}")
    return path
