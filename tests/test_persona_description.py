import _isolation  # noqa: F401
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge


class PersonaDescriptionTests(unittest.TestCase):
    def test_external_home_never_exposes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bridge._persona_meta_api(tmp))
            self.assertNotIn("description", bridge._persona_public("test", "Test", tmp, True, False))

    def test_edit_and_clear_use_registered_home_not_persona_id(self):
        with tempfile.TemporaryDirectory() as root, patch.object(bridge, "HOME_ROOT", root):
            home = Path(root, "profiles", "profile-one")
            home.mkdir(parents=True)
            if not bridge._persona_meta_api(str(home)):
                self.skipTest("optional Hermes metadata API not installed")
            with patch.object(bridge, "_check_auth"), patch.object(bridge, "_personas_reload"), \
                 patch.object(bridge, "_persona_row_upsert"), patch.object(bridge, "_persona_row_get",
                     return_value=("alias", "Example", str(home), True, False)):
                for text in ("line one:\nline two", ""):
                    request = AsyncMock()
                    request.json.return_value = {"description": text}
                    row = asyncio.run(bridge.personas_patch("alias", request))
                    self.assertEqual(text, row["description"])
                    self.assertEqual("profile-one", row["profile"])

    def test_duplicate_checked_before_provisioning(self):
        request = AsyncMock()
        request.json.return_value = {"id": "test", "name": "Test", "description": "a role"}
        with patch.object(bridge, "_check_auth"), patch.object(bridge, "_persona_row_get",
                 return_value=("test", "Test", "/unused", True, False)), \
             patch.object(bridge, "_provision_persona_profile", new_callable=AsyncMock) as spawn:
            with self.assertRaises(bridge.HTTPException) as err:
                asyncio.run(bridge.personas_create(request))
            self.assertEqual(err.exception.status_code, 409)
            spawn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
