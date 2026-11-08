"""Gemini's account indicator follows the selected local session backend."""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import gemini_provider as gm


class LocalGeminiStatus(unittest.TestCase):
    def probe(self, *, backend="gemini", installed=True, cfg=None, oauth=False):
        with (patch.object(gm, "provider_backend", return_value=backend),
              patch.object(gm, "gemini_bin", return_value="gemini-test"),
              patch.object(gm, "agy_bin", return_value="agy-test"),
              patch.object(bridge.shutil, "which", return_value="/fixture/bin" if installed else None) as which,
              patch.object(gm, "load_config", return_value=cfg or {}) as load,
              patch.object(gm, "oauth_available", return_value=oauth)):
            result = bridge._local_gemini_auth()
            which.assert_called_once_with("agy-test" if backend == "antigravity" else "gemini-test")
            if backend == "antigravity" or not installed:
                load.assert_not_called()
            return result

    def test_configured_method_is_reported_without_returning_credentials(self):
        for mode, oauth, key, account in [
                ("auto", True, "fixture", "Google"),
                ("api-key", True, "fixture", "API key"),
                ("auto", False, "fixture", "API key"),
                ("oauth", False, "fixture", None),
                ("auto", False, "", None)]:
            with self.subTest(mode=mode, oauth=oauth, key=bool(key)):
                self.assertEqual(self.probe(cfg={"auth": mode, "api_key": key}, oauth=oauth),
                                 dict(installed=True, logged_in=account is not None, account=account))

    def test_missing_binary_is_not_installed_even_with_credentials(self):
        self.assertEqual(self.probe(installed=False, cfg={"api_key": "fixture"}, oauth=True),
                         dict(installed=False, logged_in=False, account=None))

    def test_agy_installation_alone_is_not_proof_of_login(self):
        self.assertEqual(self.probe(backend="antigravity"),
                         dict(installed=True, logged_in=None, account=None))
        self.assertFalse(self.probe(backend="antigravity", installed=False)["installed"])

    def test_failed_probe_is_not_reported_as_logged_out(self):
        with (patch.object(gm, "provider_backend", return_value="gemini"),
              patch.object(bridge.shutil, "which", return_value="/fixture/bin"),
              patch.object(gm, "load_config", side_effect=OSError("fixture"))):
            self.assertEqual(bridge._local_gemini_auth(),
                             dict(installed=True, logged_in=None, account=None))

    def test_cold_response_has_unknown_login(self):
        with (patch.dict(bridge._AGENT_AUTH_CACHE, {"data": None}),
              patch.object(bridge, "_AGENT_AUTH_REFRESHING", True)):
            result = asyncio.run(bridge._agent_auth_status())
            self.assertTrue(result["checking"])
            self.assertIsNone(result["gemini"]["logged_in"])


if __name__ == "__main__":
    unittest.main()
