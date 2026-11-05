"""Typed visual reply envelope contract and v2 fallback tests."""
import _isolation  # noqa: F401

import hashlib
import json
import sys
import unittest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from visual_envelope import (VisualEnvelopeError, normalize_visual_envelope,
                             visual_envelope_digest)
import carddigest


class VisualEnvelopeTest(unittest.TestCase):
    def test_normalizes_supported_blocks_and_drops_provider_metadata(self):
        value = normalize_visual_envelope({
            "version": 1,
            "blocks": [
                {"type": "text", "text": "摘要"},
                {"type": "image", "url": "https://cdn.example/image.png",
                 "mime": "image/png", "alt": "預覽", "provider_debug": "drop"},
                {"type": "link", "url": "https://example.com", "label": "原文"},
                {"type": "html", "sanitized": True,
                 "html": "<p>安全摘要</p>"},
            ],
            "fallback_text": "摘要",
            "provider_debug": "drop",
        })
        self.assertEqual(value["version"], 1)
        self.assertEqual([b["type"] for b in value["blocks"]],
                         ["text", "image", "link", "html"])
        self.assertNotIn("provider_debug", json.dumps(value))

    def test_rejects_secrets_and_active_html(self):
        with self.assertRaises(VisualEnvelopeError):
            normalize_visual_envelope({
                "blocks": [{"type": "text", "text": "ok"}],
                "api_key": "never-store",
            })
        with self.assertRaises(VisualEnvelopeError):
            normalize_visual_envelope({
                "blocks": [{"type": "html", "sanitized": True,
                             "html": "<script>alert(1)</script>"}],
            })
        with self.assertRaises(VisualEnvelopeError):
            normalize_visual_envelope({
                "blocks": [{"type": "link", "url": "javascript:alert(1)"}],
            })

    def test_digest_is_deterministic(self):
        value = {"blocks": [{"type": "text", "text": "同一份"}]}
        self.assertEqual(visual_envelope_digest(value),
                         visual_envelope_digest({"version": 1,
                                                 "blocks": value["blocks"]}))
        self.assertEqual(len(visual_envelope_digest(value)), 64)

    def test_card_invalid_visual_keeps_fallback(self):
        card = carddigest.make_card(
            "c1", "t1", "assistant", "text",
            {"text": "可見文字", "visual": {"blocks": [{"type": "bad"}]}},
            ts=1,
        )
        self.assertEqual(card["body"]["fallback_text"], "可見文字")
        self.assertNotIn("visual", card["body"])

    def test_card_valid_visual_is_normalized(self):
        card = carddigest.make_card(
            "c1", "t1", "assistant", "text",
            {"text": "摘要", "visual": {
                "blocks": [{"type": "text", "text": "摘要"}],
                "debug": "drop",
            }}, ts=1)
        self.assertEqual(card["body"]["visual"]["version"], 1)
        self.assertNotIn("debug", card["body"]["visual"])


if __name__ == "__main__":
    unittest.main()
