"""隔離閂(tests/_isolation.py)本身的驗收 —— 閂要真的會咬。

2026-08-15 事故(測試把 production registry 表清掉)後補的防線分兩層:
env 導流 + audit-hook 寫入守衛。這支證明兩層都活著:
1. env 層:所有 production 把手 env 都指在 tmp,不在紅線目錄下。
2. guard 層:對紅線前綴的寫模式 open / 非唯讀 sqlite connect 立刻 raise,
   而且**檔案真的沒被建出來**(raise 在 open 之前,不是之後補救)。
3. 誤殺檢查:唯讀 open / mode=ro sqlite / tmp 寫入照常放行 ——
   閂只咬寫 production 的手,不咬正常測試。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import

import os
import sqlite3
import tempfile
import unittest

from _isolation import (_ENV_TMP_DEFAULTS, PRODUCTION_ROOTS,
                        ProductionWriteBlocked)

# 錨在閂 import 當下凍結的紅線根,不能自己再 expanduser("~"):全套 discover
# 一個行程跑到這裡時,前面的測試檔可能已把 HOME 改到 tmp,現場展開會漂掉。
_POCKET = PRODUCTION_ROOTS["pocket"]
_HERMES_HOME = PRODUCTION_ROOTS["hermes_home"]
_PA_SHARE = PRODUCTION_ROOTS["pocket_agent_share"]

# 用一個幾乎不可能存在的檔名當靶,避免 stat 到真檔案。
_PROBE = "isolation-latch-probe-do-not-create.tmp"


class TestEnvLayer(unittest.TestCase):
    def test_all_sensitive_envs_point_outside_production(self):
        for key in _ENV_TMP_DEFAULTS:
            val = os.environ.get(key, "")
            self.assertTrue(val, f"{key} 沒被設定")
            for bad in (_POCKET, _HERMES_HOME, _PA_SHARE):
                self.assertFalse(
                    os.path.abspath(os.path.expanduser(val)).startswith(bad),
                    f"{key}={val} 仍指向 production 目錄 {bad}")


class TestGuardBlocks(unittest.TestCase):
    def test_sqlite_connect_to_pocket_raises_and_creates_nothing(self):
        target = os.path.join(_POCKET, _PROBE)
        with self.assertRaises(ProductionWriteBlocked):
            sqlite3.connect(target)
        self.assertFalse(os.path.exists(target),
                         "raise 之後不該留下任何檔案")

    def test_sqlite_connect_to_pocket_agent_share_raises(self):
        with self.assertRaises(ProductionWriteBlocked):
            sqlite3.connect(os.path.join(_PA_SHARE, _PROBE))

    def test_open_for_write_under_pocket_raises(self):
        target = os.path.join(_POCKET, _PROBE)
        for mode in ("w", "a", "ab", "r+"):
            with self.assertRaises(ProductionWriteBlocked, msg=f"mode={mode}"):
                open(target, mode)
        self.assertFalse(os.path.exists(target))

    def test_os_open_write_flags_under_hermes_home_raise(self):
        target = os.path.join(_HERMES_HOME, _PROBE)
        with self.assertRaises(ProductionWriteBlocked):
            os.open(target, os.O_WRONLY | os.O_CREAT)
        self.assertFalse(os.path.exists(target))

    def test_sqlite_connect_to_hermes_state_db_without_ro_raises(self):
        # 紅線原文:state.db 只准讀。非 ro 的 connect 一律當寫擋下。
        with self.assertRaises(ProductionWriteBlocked):
            sqlite3.connect(os.path.join(_HERMES_HOME, "state.db"))

    def test_symlink_detour_is_caught(self):
        # tmp 裡放一個指向 ~/.pocket 的 symlink,寫它也要被咬(realpath 比對)。
        d = tempfile.mkdtemp(prefix="latch-symlink-")
        link = os.path.join(d, "sneaky")
        os.symlink(_POCKET, link)
        with self.assertRaises(ProductionWriteBlocked):
            open(os.path.join(link, _PROBE), "w")


class TestGuardDoesNotOverbite(unittest.TestCase):
    def test_read_only_open_passes_through(self):
        # 唯讀不擋:missing 檔就該是 FileNotFoundError,不是 latch 的 raise。
        with self.assertRaises(FileNotFoundError):
            open(os.path.join(_POCKET, _PROBE), "r")

    def test_mode_ro_sqlite_uri_passes_through(self):
        # harness 的合法讀法(file:...?mode=ro)要放行:missing 檔是
        # OperationalError,不是 latch 的 raise。
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(
                "file:" + os.path.join(_POCKET, _PROBE) + "?mode=ro",
                uri=True)

    def test_tmp_writes_are_untouched(self):
        d = tempfile.mkdtemp(prefix="latch-ok-")
        p = os.path.join(d, "fine.db")
        with open(os.path.join(d, "fine.txt"), "w", encoding="utf-8") as f:
            f.write("ok")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (x)")
        con.close()
        self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
