# Codex Replay Fixtures

These JSONL files contain the provider frames that the card digest consumes.
They are intentionally small, deterministic contract fixtures, not production
conversation exports. `cx-*.jsonl` uses recorded app-server frame shape;
`gemini-*.jsonl` uses the normalized ACP turn contract; `openclaw-*.jsonl`
uses gateway event shape. The live recorder is opt-in and writes to the path in
`POCKET_RECORD_FIXTURE`. It runs every frame through `replay_fixture` before
writing: object keys are canonicalized, secret-like keys/values are rejected,
and output is owner-only (`0600`). A rejected frame is reported by type only;
its contents are never written to the fixture or event log. Do not commit a
recording containing private prompts, paths, secrets, or tool output without
reviewing it first. Raw transcripts are local-only and are never a CI artifact.

```sh
POCKET_RECORD_FIXTURE=/private/tmp/codex-session.jsonl \
  launchctl kickstart -k gui/$(id -u)/com.pocketconnect.bridge
```

The path must be outside the repository while recording. Review and reduce the
file to the smallest non-sensitive scenario before copying it into this
directory. `load_jsonl()` applies the same validation in CI, so a manually
copied fixture cannot bypass the recorder policy.

Replay all reviewed fixtures with:

```sh
python3 tests/test_replay_fixtures.py
```

The release digest is SHA-256 over canonical JSONL in original frame order
(`sha256-canonical-jsonl-v1`). In a Python shell:

```python
from replay_fixture import fixture_digest, load_jsonl

frames = load_jsonl("tests/fixtures/replay/cx-simple.jsonl")
print(fixture_digest(frames))
```

The manifest is the source of truth for schema, recorder version, scenario
coverage, and the rule that raw transcripts are not committed.
