"""Run Console card-text extraction checks (persona visual cards) through the Python suite."""
import _isolation  # noqa: F401
from pathlib import Path
import shutil
import subprocess
import unittest


class ConsoleCardTextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Console behavior checks")
    def test_shipped_console_card_text(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(Path(__file__).with_name("console_card_text.test.cjs"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
