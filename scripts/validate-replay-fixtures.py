#!/usr/bin/env python3
"""Validate the reviewed T3Code replay manifest.

Default mode is the development gate.  ``--release`` applies the six-scenario
provider gate and is intended for release/staging promotion checks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replay_fixture import ReplayFixtureError, validate_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true",
                        help="require all six release scenarios per provider")
    args = parser.parse_args()
    fixture_dir = ROOT / "tests" / "fixtures" / "replay"
    try:
        with (fixture_dir / "manifest.json").open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        summary = validate_manifest(manifest, fixture_dir, release=args.release)
    except (OSError, ValueError, ReplayFixtureError) as exc:
        print(f"replay manifest invalid: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
