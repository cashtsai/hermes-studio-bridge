"""Run the cut4 virtual-list contracts (sidebar window + card window) via Node."""
import _isolation  # noqa: F401
from pathlib import Path
import shutil
import subprocess
import unittest


class ConsoleVirtualListTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Console behavior checks")
    def test_shipped_console_virtual_lists(self):
        for name in ("console_list_virtual.test.cjs", "console_cards_window.test.cjs"):
            with self.subTest(suite=name):
                result = subprocess.run(
                    [shutil.which("node"), "--test", str(Path(__file__).with_name(name))],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
