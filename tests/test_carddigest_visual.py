"""Visual studio-card fence projection and v2 fallback tests."""

import _isolation  # noqa: F401

import unittest

import carddigest


class CardDigestVisualTest(unittest.TestCase):
    def test_visual_fence_projects_to_typed_body_and_clean_fallback(self):
        source = """前文
```studio-card
{"kind":"visual","fallback_text":"看圖","blocks":[
  {"type":"image","url":"https://cdn.example/a.png","alt":"預覽"},
  {"type":"link","url":"https://example.com","label":"原文"}
]}
```
後文"""
        clean, bodies = carddigest.extract_studio_cards(source)
        self.assertIn("看圖", clean)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["kind"], "visual")

        digest = carddigest.PersonaDigest()
        digest._emit_studio_cards("card-hp-test", bodies, ts=1)
        card = next(iter(digest.store.cards.values()))
        self.assertEqual(card["kind"], "markdown")
        self.assertEqual([b["type"] for b in card["body"]["visual"]["blocks"]],
                         ["image", "link"])
        self.assertEqual(card["body"]["fallback_text"], "看圖")

    def test_invalid_visual_block_keeps_fallback_only(self):
        body = carddigest.make_card(
            "visual-invalid", "t1", "assistant", "markdown",
            {"fallback_text": "安全文字", "visual": {
                "version": 1,
                "blocks": [{"type": "html", "html": "<script>x</script>",
                            "sanitized": True}],
            }})
        self.assertNotIn("visual", body["body"])
        self.assertEqual(body["body"]["fallback_text"], "安全文字")


if __name__ == "__main__":
    unittest.main(verbosity=2)
