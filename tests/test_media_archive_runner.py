"""Failed copies or verification must never remove the local artifact."""
import _isolation  # noqa: F401
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("media_archive", Path(__file__).resolve().parents[1] / "deploy/media-archive-to-cashbox.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveFailureTests(unittest.TestCase):
    def test_copy_or_probe_failure_keeps_local_files_in_both_modes(self):
        for mode in ("orphans", "retention"):
            for failure in ("copy", "probe"):
                with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    path = root / "blobs/ab/example"
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"abc")
                    rel = str(path.relative_to(root))
                    results = [subprocess.CompletedProcess([], 124 if failure == "copy" else 0, "", "timeout")]
                    if failure == "probe":
                        # Even apparently matching partial stdout is not proof
                        # when the remote verifier exited unsuccessfully.
                        results.append(subprocess.CompletedProcess([], 1,
                                       rel + ("\t3\n" if mode == "orphans" else " 3\n"), "failed"))
                    with patch.object(archive, "ROOT", root), \
                            patch.object(archive, "INDEX", root / "index.db"), \
                            patch.object(archive, "ssh", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                            patch.object(archive, "run_command", side_effect=results), \
                            patch.object(archive, "orphan_blobs", return_value=[(path, 3)]), \
                            patch.object(archive, "candidates", return_value=[{"stored_relpath": rel, "media_id": "example"}]), \
                            patch.object(sys, "argv", ["archive", "--apply"]), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        (root / "index.db").touch()
                        result = archive.archive_orphans(SimpleNamespace(apply=True)) if mode == "orphans" else archive.main()
                    self.assertEqual(result, 1)
                    self.assertEqual(path.read_bytes(), b"abc")

    def test_ssh_uses_bounded_runner(self):
        with patch.object(archive, "run_command") as run:
            archive.ssh("true")
        self.assertEqual(run.call_args.kwargs, {"operation": "archive.ssh", "timeout": 120})


if __name__ == "__main__":
    unittest.main()
