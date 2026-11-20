import _isolation  # noqa: F401
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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
            stored = {"description": ""}
            def write_meta(path, **values):
                self.assertEqual(path, home)
                stored.update(values)
            meta = SimpleNamespace(read_profile_meta=Mock(side_effect=lambda _: stored),
                                   write_profile_meta=Mock(side_effect=write_meta))
            with patch.object(bridge, "_check_auth"), patch.object(bridge, "_personas_reload"), \
                 patch.object(bridge, "_persona_row_upsert"), patch.object(bridge, "_persona_row_get",
                     return_value=("alias", "Example", str(home), True, False)), \
                 patch.dict(sys.modules, {"hermes_cli": SimpleNamespace(profiles=meta)}), \
                 patch.object(bridge.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
                for text in ("line one:\nline two", ""):
                    request = AsyncMock()
                    request.json.return_value = {"description": text}
                    row = asyncio.run(bridge.personas_patch("alias", request))
                    self.assertEqual(text, row["description"])
                    self.assertEqual("profile-one", row["profile"])
                    meta.write_profile_meta.assert_called_with(home, description=text, description_auto=False)
                spawn.assert_not_awaited()

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

    def test_create_provisions_once_after_validation(self):
        with tempfile.TemporaryDirectory() as home:
            request = AsyncMock()
            request.json.return_value = {"id": "fixture-new", "name": "Example", "description": "a role"}
            with patch.object(bridge, "_check_auth"), patch.object(bridge, "PERSONAS", {}), \
                 patch.object(bridge, "_persona_row_get", return_value=None), \
                 patch.object(bridge, "_persona_meta_api", return_value=object()), \
                 patch.object(bridge, "_provision_persona_profile", new=AsyncMock(return_value=home)) as spawn, \
                 patch.object(bridge, "_persona_row_upsert") as upsert, \
                 patch.object(bridge, "_personas_reload"), patch.object(bridge, "_persona_public", return_value={"id": "fixture-new"}):
                self.assertEqual(asyncio.run(bridge.personas_create(request)), {"id": "fixture-new"})
                spawn.assert_awaited_once_with("fixture-new", "a role")
                upsert.assert_called_once_with("fixture-new", "Example", home, 1, 0)

    def test_unsupported_create_never_provisions(self):
        for extra in ({"home": "/unused"}, {"profile": "main"}, {}):
            request = AsyncMock()
            request.json.return_value = {"id": "fixture-new", "name": "Example", "description": "a role", **extra}
            with patch.object(bridge, "_check_auth"), patch.object(bridge, "PERSONAS", {}), \
                 patch.object(bridge, "_persona_row_get", return_value=None), \
                 patch.object(bridge, "_persona_meta_api", return_value=None), \
                 patch.object(bridge, "_provision_persona_profile", new_callable=AsyncMock) as spawn:
                with self.assertRaises(bridge.HTTPException) as err:
                    asyncio.run(bridge.personas_create(request))
                self.assertEqual(err.exception.status_code, 400)
                spawn.assert_not_awaited()

    def test_profile_command_has_one_description_and_never_clones_credentials(self):
        with tempfile.TemporaryDirectory() as root, patch.object(bridge, "HOME_ROOT", root):
            home = Path(root, "profiles", "fixture-new")
            home.mkdir(parents=True)
            proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
            with patch.object(bridge.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
                self.assertEqual(asyncio.run(bridge._provision_persona_profile("fixture-new", "--option-like text")), str(home.resolve()))
            self.assertEqual(spawn.call_args.args, (bridge.HERMES_BIN, "profile", "create", "fixture-new",
                                                   "--no-alias", "--description=--option-like text"))


if __name__ == "__main__":
    unittest.main()
