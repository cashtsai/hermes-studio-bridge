"""Console W2 consumes declared operations without guessing provider support."""
import _isolation  # noqa: F401
import unittest
from unittest.mock import patch

import bridge
import cc_sdk
import opencode_sdk


class ConsoleOpsContractTests(unittest.TestCase):
    def test_ops_are_derived_from_handlers(self):
        for provider, caps in bridge._provider_caps_payload().items():
            self.assertEqual(caps["ops"], sorted(
                op for op, handlers in bridge._V2_SESSION_OPS.items()
                if provider in handlers))

    def test_compact_and_read_cursor_are_explicit(self):
        caps = bridge._provider_caps_payload()
        self.assertEqual({p for p, c in caps.items() if c["compact"]},
                         {"claude_code", "cc2", "codex", "gemini", "grok"})
        self.assertEqual({p for p, c in caps.items() if c["read_cursor"]}, {"hermes"})
        for c in caps.values():
            self.assertIsInstance(c["read_cursor"], bool)
            self.assertIsInstance(c["compact"], bool)

    def test_codex_spawn_only_fields_are_read_only(self):
        fields = bridge._provider_caps_payload()["codex"]["config_fields"]
        self.assertEqual({k for k, f in fields.items() if f["editable"]},
                         {"model", "approval_policy"})
        self.assertFalse(fields["effort"]["editable"])
        self.assertFalse(fields["sandbox"]["editable"])
        self.assertEqual(fields["approval_policy"]["options"],
                         list(bridge._CODEX_APPROVAL_POLICIES))

    def test_config_schema_only_describes_real_writers(self):
        caps = bridge._provider_caps_payload()
        for c in caps.values():
            if any(f["editable"] for f in c.get("config_fields", {}).values()):
                self.assertIn("config_write", c["ops"])
        self.assertEqual(set(caps["opencode"]["config_fields"]),
                         set(opencode_sdk.KNOWN_CONFIG_IDS))
        self.assertEqual(caps["cc2"]["config_fields"]["permission_mode"]["options"],
                         list(cc_sdk.PERMISSION_MODES))

    def test_hub_question_uses_live_fields_and_index_without_db_write(self):
        row = ["qa-next", "DB old title", "codex:fixture", "low", "DB old detail",
               1, 9999999999, "pending", None, None, "codex:fixture", "codex",
               "question", "[]", None]
        live = {"title": "Second question", "detail": "Current detail",
                "options": [{"key": "b", "label": "Second answer"}],
                "q_index": 1, "q_total": 2}
        with patch.dict(bridge.CODEX_APP.pending_approvals, {"qa-next": live}):
            result = bridge._approval_row(row)
        for key, value in live.items():
            self.assertEqual(result[key], value)
        self.assertEqual(row[1], "DB old title")
        self.assertEqual(result["response_capability"], "live")

    def test_hub_terminal_question_does_not_take_live_fields(self):
        row = ["qa-old", "DB title", "codex:fixture", "low", "DB detail",
               1, 9999999999, "answered", None, None, "codex:fixture", "codex",
               "question", "[]", None]
        with patch.dict(bridge.CODEX_APP.pending_approvals, {"qa-old": {"q_index": 1}}):
            result = bridge._approval_row(row)
        self.assertNotIn("q_index", result)
        self.assertEqual(result["response_capability"], "none")

    def test_live_options_keep_hub_style_vocabulary_without_mutating_provider(self):
        row = ["qa-style", "DB title", "codex:fixture", "low", "DB detail",
               1, 9999999999, "pending", None, None, "codex:fixture", "codex",
               "question", "[]", None]
        live = {"options": [{"key": "deny", "label": "No", "style": "deny"}]}
        with patch.dict(bridge.CODEX_APP.pending_approvals, {"qa-style": live}):
            result = bridge._approval_row(row)
        self.assertEqual(result["options"][0]["style"], "danger")
        self.assertEqual(live["options"][0]["style"], "deny")


if __name__ == "__main__":
    unittest.main()
