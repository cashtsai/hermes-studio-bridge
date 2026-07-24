from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_TMP = tempfile.mkdtemp(prefix="hermes-media-canon-")
os.environ["HOME"] = _TMP
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))

import bridge  # noqa: E402
import hermes_media  # noqa: E402


def _write_profile(home: Path, *, stt_provider: str = "local") -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": [], "disabled": []},
                "stt": {
                    "enabled": True,
                    "provider": stt_provider,
                    "local": {"model": "base"},
                },
                "ocr": {"enabled": False, "provider": "none"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class TestHermesMediaSettings(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hermes-media-profile-"))
        self.home = self.root / "home"
        _write_profile(self.home)

    def test_update_writes_only_hermes_profile_config(self):
        result = hermes_media.update_settings(
            self.home,
            {
                "stt": {
                    "provider": "siege",
                    "siege": {
                        "base_url": "http://siege.test:8081/v1",
                        "model": "whisper-1",
                    },
                },
                "ocr": {
                    "enabled": True,
                    "provider": "siege",
                    "siege": {"base_url": "http://siege.test:8083"},
                },
            },
        )

        raw = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertEqual(raw["stt"]["provider"], "siege")
        self.assertEqual(raw["ocr"]["provider"], "siege")
        self.assertIn("hermes-siege", raw["plugins"]["enabled"])
        self.assertEqual(result["stt"]["provider"], "siege")
        self.assertEqual(result["ocr"]["provider"], "siege")
        self.assertNotIn("api_key", str(result))

    def test_invalid_endpoint_is_rejected_without_writing(self):
        before = (self.home / "config.yaml").read_text()
        with self.assertRaises(hermes_media.HermesMediaError):
            hermes_media.update_settings(
                self.home,
                {
                    "stt": {
                        "provider": "siege",
                        "siege": {"base_url": "file:///etc/passwd"},
                    }
                },
            )
        self.assertEqual((self.home / "config.yaml").read_text(), before)

    def test_profile_settings_are_isolated(self):
        other = self.root / "other"
        _write_profile(other)
        hermes_media.update_settings(
            self.home,
            {"stt": {"provider": "siege"}},
        )

        other_raw = yaml.safe_load((other / "config.yaml").read_text())
        self.assertEqual(other_raw["stt"]["provider"], "local")

    def test_load_plugin_uses_hermes_directory_plugin_manager(self):
        api = types.SimpleNamespace(
            ensure_stt_registered=lambda: None,
            request_options=lambda **_kwargs: None,
            ocr_document=lambda *_args, **_kwargs: {},
            get_media_capabilities=lambda **_kwargs: {},
        )
        loaded = types.SimpleNamespace(
            enabled=True,
            module=types.SimpleNamespace(hermes_siege=api),
        )

        class FakeManager:
            _plugins = {"hermes-siege": loaded}

            def discover_and_load(self, force=False):
                return None

        plugins_module = types.ModuleType("hermes_cli.plugins")
        plugins_module.get_plugin_manager = lambda: FakeManager()
        hermes_media._load_plugin.cache_clear()
        try:
            with mock.patch.dict(
                sys.modules,
                {
                    "hermes_siege": None,
                    "hermes_cli.plugins": plugins_module,
                },
            ):
                self.assertIs(hermes_media._load_plugin(), api)
        finally:
            hermes_media._load_plugin.cache_clear()


class TestBridgeHermesDelegation(unittest.TestCase):
    def test_voice_transcription_delegates_to_hermes(self):
        with mock.patch.object(
            bridge.hermes_media,
            "transcribe_audio",
            return_value={
                "success": True,
                "provider": "siege",
                "transcript": "delegated transcript",
            },
        ) as call:
            result = bridge._transcribe(
                "/tmp/voice.m4a", "/tmp/profile", "zh-Hant"
            )

        self.assertEqual(result, "delegated transcript")
        call.assert_called_once_with(
            "/tmp/profile", "/tmp/voice.m4a", locale="zh-Hant"
        )

    def test_luvlink_language_codes_are_forwarded_to_stt_provider(self):
        self.assertEqual(hermes_media._LOCALE_LANGUAGE["th"], "th")
        self.assertEqual(hermes_media._LOCALE_LANGUAGE["ja"], "ja")
        self.assertEqual(hermes_media._LOCALE_LANGUAGE["ar"], "ar")

    def test_image_ocr_delegates_to_hermes(self):
        with mock.patch.object(
            bridge.hermes_media,
            "ocr_document",
            return_value={
                "success": True,
                "provider": "siege",
                "text": "receipt total 42",
            },
        ) as call:
            result = asyncio.run(
                bridge._ocr_image("/tmp/receipt.png", "/tmp/profile")
            )

        self.assertEqual(result, "receipt total 42")
        call.assert_called_once_with("/tmp/profile", "/tmp/receipt.png")

    def test_media_routes_are_registered(self):
        paths = {route.path for route in bridge.app.routes}
        self.assertIn("/app/v2/hermes/media-capabilities", paths)
        self.assertIn("/app/v2/hermes/media-settings", paths)
        self.assertIn("/v1/audio/transcriptions", paths)
        capabilities = asyncio.run(bridge.capabilities(self._request()))
        self.assertIn("openai_audio_transcriptions", capabilities["features"])
        self.assertIn("/v1/audio/transcriptions", capabilities["endpoints"])

    @staticmethod
    def _request():
        token = f"Bearer {bridge.BRIDGE_TOKEN}".encode()
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/audio/transcriptions",
                "headers": [(b"authorization", token)],
            }
        )

    def test_openai_audio_transcription_uses_selected_hermes_profile(self):
        upload = UploadFile(
            io.BytesIO(b"voice-bytes"),
            filename="voice.ogg",
            headers=Headers({"content-type": "audio/ogg"}),
        )
        with mock.patch.object(
            bridge,
            "_transcribe",
            return_value="delegated transcript",
        ) as call:
            response = asyncio.run(
                bridge.audio_transcriptions(
                    self._request(),
                    upload,
                    model="xcash",
                    language="zh",
                    response_format="json",
                )
            )

        self.assertEqual(json.loads(response.body), {"text": "delegated transcript"})
        path, home, language = call.call_args.args
        self.assertEqual(home, bridge.home_for("xcash"))
        self.assertEqual(language, "zh")
        self.assertFalse(Path(path).exists())

    def test_openai_audio_transcription_rejects_unknown_profile(self):
        upload = UploadFile(io.BytesIO(b"voice"), filename="voice.ogg")
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                bridge.audio_transcriptions(
                    self._request(),
                    upload,
                    model="missing",
                    language="",
                    response_format="json",
                )
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_openai_audio_transcription_enforces_streamed_size_limit(self):
        upload = UploadFile(io.BytesIO(b"12345"), filename="voice.ogg")
        with (
            mock.patch.object(bridge, "_STT_UPLOAD_MAX_BYTES", 4),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(
                bridge.audio_transcriptions(
                    self._request(),
                    upload,
                    model="xcash",
                    language="",
                    response_format="json",
                )
            )
        self.assertEqual(raised.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
