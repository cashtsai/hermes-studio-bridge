"""Validated, deterministic replay fixture I/O for T3Code.

Fixtures are a test contract, not a transcript archive.  This module keeps
the recorder and CI loader on the same boundary: reject secret-like material,
canonicalize JSON object key order, preserve list order, and hash the exact
canonical JSONL frames.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DIGEST_ALGORITHM = "sha256-canonical-jsonl-v1"
RECORDER_VERSION = "t3code-replay-1"
MAX_FRAME_BYTES = 512 * 1024

_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|"
    r"cookie|authorization|bearer|jwt|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"(?:sk|rk)-[A-Za-z0-9._-]{16,}|"
    r"(?:sk|rk|ghp|github_pat|xox[baprs])_[A-Za-z0-9._-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class ReplayFixtureError(ValueError):
    """A fixture is malformed or contains data that must not be recorded."""


def _check_safe(value: Any, path: str = "frame") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                raise ReplayFixtureError(
                    f"secret-like field is not allowed: {path}.{key_text}"
                )
            _check_safe(child, f"{path}.{key_text}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _check_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise ReplayFixtureError(f"secret-like value is not allowed: {path}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ReplayFixtureError(f"non-finite number is not allowed: {path}")


def canonicalize(value: Any) -> Any:
    """Return JSON-safe data with sorted object keys and stable containers."""
    _check_safe(value)
    if isinstance(value, dict):
        return {
            str(key): canonicalize(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    raise ReplayFixtureError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a value without whitespace or runtime-dependent key order."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def normalize_frame(frame: Any) -> dict:
    """Validate one provider frame and return its canonical representation."""
    if not isinstance(frame, dict):
        raise ReplayFixtureError("frame must be an object")
    if not (frame.get("method") or frame.get("event")):
        raise ReplayFixtureError("frame requires method or event")
    normalized = canonicalize(frame)
    if not isinstance(normalized, dict):
        raise ReplayFixtureError("frame must remain an object")
    return normalized


def normalize_frames(frames: Iterable[Any]) -> list[dict]:
    normalized = [normalize_frame(frame) for frame in frames]
    if not normalized:
        raise ReplayFixtureError("fixture must contain at least one frame")
    return normalized


def fixture_digest(frames: Iterable[Any]) -> str:
    """Hash canonical JSONL while preserving the original frame order."""
    normalized = normalize_frames(frames)
    payload = (
        "".join(canonical_json(frame) + "\n" for frame in normalized)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_jsonl(path: str | os.PathLike[str], *, owner_only: bool = False) -> list[dict]:
    """Load and validate JSONL; optionally require mode 0600/owner-only."""
    target = Path(path).expanduser()
    if owner_only:
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode & 0o077:
            raise ReplayFixtureError(
                f"fixture must be owner-only (mode 0600): {target}"
            )
    frames: list[dict] = []
    with target.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            if len(raw) > MAX_FRAME_BYTES:
                raise ReplayFixtureError(
                    f"frame exceeds {MAX_FRAME_BYTES} bytes: {target}:{line_no}"
                )
            try:
                frame = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReplayFixtureError(
                    f"invalid JSON at {target}:{line_no}"
                ) from exc
            try:
                frames.append(normalize_frame(frame))
            except ReplayFixtureError as exc:
                raise ReplayFixtureError(
                    f"{target}:{line_no}: {exc}"
                ) from exc
    return normalize_frames(frames)


def append_frame(path: str | os.PathLike[str], frame: Any) -> None:
    """Append one validated frame to an owner-only recorder output."""
    target = Path(path).expanduser()
    normalized = normalize_frame(frame)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = (canonical_json(normalized) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise ReplayFixtureError(
                f"frame exceeds {MAX_FRAME_BYTES} bytes: {target}"
            )
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
