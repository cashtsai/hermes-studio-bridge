"""T3Code typed visual reply envelope.

The envelope is deliberately additive: a card still carries ``fallback_text``
and older clients can ignore the ``visual`` field.  Provider adapters should
normalize into this module before emitting a card so the iOS client never has
to guess whether a string is HTML, Markdown, or an attachment URL.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse


VERSION = 1
MAX_BLOCKS = 32
MAX_TEXT_CHARS = 20_000
MAX_HTML_CHARS = 20_000

_BLOCK_TYPES = {"text", "markdown", "image", "file", "link", "html"}
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|cookie|jwt)",
    re.IGNORECASE,
)
_UNSAFE_HTML = re.compile(
    r"<\s*(?:script|iframe|object|embed)|\bon[a-z]+\s*=|javascript\s*:",
    re.IGNORECASE,
)


class VisualEnvelopeError(ValueError):
    """The provider supplied a visual envelope outside the public contract."""


def _reject_sensitive_keys(value, path: str = "envelope") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise VisualEnvelopeError(f"secret-like field is not allowed: {path}.{key}")
            _reject_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{i}]")


def _bounded_text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisualEnvelopeError(f"{field} must be non-empty text")
    value = value.strip()
    if len(value) > limit:
        raise VisualEnvelopeError(f"{field} exceeds {limit} characters")
    return value


def _safe_url(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisualEnvelopeError(f"{field} must be a URL")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise VisualEnvelopeError(f"{field} must use http or https")
    if not parsed.netloc:
        raise VisualEnvelopeError(f"{field} must include a host")
    return value


def _normalise_block(raw: object, index: int) -> dict:
    if not isinstance(raw, dict):
        raise VisualEnvelopeError(f"block {index} must be an object")
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in _BLOCK_TYPES:
        raise VisualEnvelopeError(f"block {index} has unsupported type")

    if kind in {"text", "markdown"}:
        return {"type": kind, "text": _bounded_text(raw.get("text"),
                                                        f"blocks[{index}].text",
                                                        MAX_TEXT_CHARS)}

    if kind in {"image", "file"}:
        url = raw.get("download_url") or raw.get("url")
        out = {"type": kind, "url": _safe_url(url, f"blocks[{index}].url")}
        mime = raw.get("mime")
        if mime is not None:
            mime = _bounded_text(mime, f"blocks[{index}].mime", 120)
            if "/" not in mime:
                raise VisualEnvelopeError(f"blocks[{index}].mime is invalid")
            out["mime"] = mime
        for key in ("filename", "alt"):
            if raw.get(key) is not None:
                out[key] = _bounded_text(raw[key], f"blocks[{index}].{key}", 512)
        return out

    if kind == "link":
        out = {"type": kind, "url": _safe_url(raw.get("url"),
                                                 f"blocks[{index}].url")}
        if raw.get("label") is not None:
            out["label"] = _bounded_text(raw["label"],
                                          f"blocks[{index}].label", 512)
        return out

    # HTML is an explicit capability.  It must already be sanitized by the
    # server-side producer; the bridge still rejects common active-content
    # markers as a second boundary before the value reaches a client.
    if raw.get("sanitized") is not True:
        raise VisualEnvelopeError("html blocks require sanitized=true")
    html = _bounded_text(raw.get("html"), f"blocks[{index}].html", MAX_HTML_CHARS)
    if _UNSAFE_HTML.search(html):
        raise VisualEnvelopeError("html block contains active content")
    return {"type": kind, "html": html, "sanitized": True}


def normalize_visual_envelope(value: object, fallback_text: str = "") -> dict:
    """Validate and return a stable, JSON-safe visual envelope.

    Unknown fields are intentionally dropped.  This keeps provider-specific
    metadata out of the app contract and makes replay digests deterministic.
    """
    if not isinstance(value, dict):
        raise VisualEnvelopeError("visual envelope must be an object")
    _reject_sensitive_keys(value)
    try:
        version = int(value.get("version", VERSION))
    except (TypeError, ValueError) as exc:
        raise VisualEnvelopeError("version must be an integer") from exc
    if version != VERSION:
        raise VisualEnvelopeError("unsupported visual envelope version")
    blocks = value.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise VisualEnvelopeError("blocks must be a non-empty array")
    if len(blocks) > MAX_BLOCKS:
        raise VisualEnvelopeError(f"blocks exceeds {MAX_BLOCKS} items")
    out = {
        "version": VERSION,
        "blocks": [_normalise_block(block, i) for i, block in enumerate(blocks)],
    }
    text = str(value.get("fallback_text") or fallback_text or "").strip()
    if text:
        out["fallback_text"] = text[:MAX_TEXT_CHARS]
    return out


def visual_envelope_digest(value: object) -> str:
    """Return the deterministic digest used by replay fixture assertions."""
    normal = normalize_visual_envelope(value)
    encoded = json.dumps(normal, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
