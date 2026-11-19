"""Read-only contract checks: no bridge import, credentials or live provider."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConsoleInteractionAssetsTests(unittest.TestCase):
    def test_csp_only_allows_same_origin_fonts(self):
        tree = ast.parse((ROOT / "bridge.py").read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CONSOLE_SECURITY_HEADERS" for t in n.targets))
        headers = ast.literal_eval(assignment.value)
        directives = {parts[0]: parts[1:] for raw in headers["Content-Security-Policy"].split(";")
                      if (parts := raw.strip().split())}
        self.assertEqual(directives["font-src"], ["'self'"])
        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["connect-src"], ["'self'"])
        self.assertEqual(directives["frame-ancestors"], ["'none'"])

    def test_fonts_are_real_woff2_with_license_and_precache(self):
        assets = ROOT / "static/console"
        sw = (assets / "sw.js").read_text()
        for name in ["JetBrainsMono-Regular.woff2", "JetBrainsMono-SemiBold.woff2"]:
            data = (assets / name).read_bytes()
            self.assertEqual(data[:4], b"wOF2")
            self.assertIn(name, sw)
        self.assertIn("SIL OPEN FONT LICENSE", (assets / "JetBrainsMono-OFL.txt").read_text())
        self.assertIn('"interaction.css"', sw)


if __name__ == "__main__":
    unittest.main()
