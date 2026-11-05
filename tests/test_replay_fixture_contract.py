import json
import stat
import tempfile
import unittest
from pathlib import Path

from replay_fixture import (
    ReplayFixtureError,
    append_frame,
    canonical_json,
    fixture_digest,
    load_jsonl,
    normalize_frame,
)


class ReplayFixtureContractTests(unittest.TestCase):
    def test_digest_is_stable_for_object_key_order(self):
        left = [{"method": "turn/started",
                 "params": {"turn": {"id": "t-1", "status": "running"}}}]
        right = [{"params": {"turn": {"status": "running", "id": "t-1"}},
                  "method": "turn/started"}]
        self.assertEqual(fixture_digest(left), fixture_digest(right))
        self.assertEqual(canonical_json(left[0]), canonical_json(right[0]))

    def test_rejects_sensitive_nested_key(self):
        with self.assertRaisesRegex(ReplayFixtureError, "secret-like field"):
            normalize_frame({
                "method": "turn/started",
                "params": {"metadata": {"api_key": "should-not-land"}},
            })

    def test_rejects_secret_like_value_without_sensitive_key(self):
        with self.assertRaisesRegex(ReplayFixtureError, "secret-like value"):
            normalize_frame({
                "method": "turn/started",
                "params": {"detail": "Bearer abcdefghijklmnop"},
            })

        with self.assertRaisesRegex(ReplayFixtureError, "secret-like value"):
            normalize_frame({
                "method": "turn/started",
                "params": {"detail": "sk-abcdefghijklmnop"},
            })

    def test_rejects_malformed_frame(self):
        with self.assertRaises(ReplayFixtureError):
            normalize_frame({"params": {}})

    def test_append_and_load_are_owner_only_and_canonical(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nested" / "fixture.jsonl"
            append_frame(path, {
                "params": {"b": 2, "a": 1},
                "method": "turn/started",
            })
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
            self.assertEqual(
                load_jsonl(path),
                [{"method": "turn/started", "params": {"a": 1, "b": 2}}],
            )

    def test_load_rejects_secret_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "unsafe.jsonl"
            path.write_text(json.dumps({
                "method": "turn/started",
                "params": {"access_token": "secret"},
            }) + "\n", encoding="utf-8")
            with self.assertRaises(ReplayFixtureError):
                load_jsonl(path)


if __name__ == "__main__":
    unittest.main()
