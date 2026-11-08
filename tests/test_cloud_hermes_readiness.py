import _isolation  # noqa: F401
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ensure-hermes-runtime.sh"


class CloudHermesStartupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "existing host"
        self.bin = self.root / "runtime/venv/bin"
        self.bin.mkdir(parents=True)
        (self.root / "pyproject.toml").touch()
        self.exe("hermes", 'test "$1" = --help && test -f "$HERMES_INSTALL_DIR/ready"')

    def exe(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        path.chmod(0o700)

    def check(self):
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                              env=dict(os.environ, HERMES_INSTALL_DIR=str(self.root)))

    def test_broken_existing_entrypoint_repaired_once(self):
        self.exe("python", '''test "$1 $2 $3 $4 $5" = "-m pip install -q -e"
test "$6" = "$HERMES_INSTALL_DIR"
echo install >> "$HERMES_INSTALL_DIR/calls"
touch "$HERMES_INSTALL_DIR/ready"''')
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["install"])

    def test_pip_success_is_not_runtime_readiness(self):
        self.exe("python", "exit 0")
        self.assertNotEqual(self.check().returncode, 0)

    def test_install_failure_is_not_masked(self):
        self.exe("python", "exit 17")
        self.assertEqual(self.check().returncode, 17)


if __name__ == "__main__":
    unittest.main()
