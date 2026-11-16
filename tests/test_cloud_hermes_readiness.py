import _isolation  # noqa: F401
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ensure-hermes-runtime.sh"
INSTALLER = SCRIPT.with_name("install-ubuntu.sh")


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

    def test_installer_repairs_existing_runtime_before_smoke_check(self):
        self.exe("hermes", 'test -f "$HERMES_INSTALL_DIR/ready"')
        self.exe("python", '''case "$1" in
-m) echo repair >> "$HERMES_INSTALL_DIR/calls"; touch "$HERMES_INSTALL_DIR/ready" ;;
-c) test -f "$HERMES_INSTALL_DIR/ready"; echo smoke >> "$HERMES_INSTALL_DIR/calls" ;;
-V) echo Python-3.14 ;;
*) exit 19 ;;
esac''')
        source = INSTALLER.read_text()
        start = source.index("if [ \"$INSTALL_HERMES\" = \"1\" ]; then", source.index("# 煙霧測試:"))
        end = source.index("\nfi", start) + len("\nfi")
        script = 'set -eu\nlog_step() { :; }\ndie() { echo "$*" >&2; exit 1; }\n' + source[start:end]
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, INSTALL_HERMES="1",
                                         HERMES_INSTALL_DIR=str(self.root),
                                         HERMES_HOME_ROOT=str(self.root / "home"),
                                         HERMES_REPO_URL="unused-no-network",
                                         SCRIPT_DIR=str(SCRIPT.parent)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["repair", "smoke"])


if __name__ == "__main__":
    unittest.main()
