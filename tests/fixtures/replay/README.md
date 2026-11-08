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

### Isolated CX lifecycle captures (2026-11-08)

Recorded with Codex CLI 0.167.0 over stdio. The probe starts a new process,
temporary home, and read-only test workspace, with no account or model key.
Only fixed placeholder texts, enumerated statuses and mapped IDs are emitted.
The stream scenario uses a scripted loopback Responses endpoint, not paid inference.
Both captures can be reproduced independently:

```sh
python3 scripts/probe-cx-lifecycle.py stream --check
python3 scripts/probe-cx-lifecycle.py interrupt --check
```

- `stream_resume`: the provider emitted two text chunks for the same item.
  `TestRecordedCxStream` disconnects the v2 SSE iterator after the first
  chunk, then reopens the same store with the phone cursor. It checks every
  remaining sequence, the final card and the turn boundary. This tests
  the Bridge reconnect contract, not iPhone networking or a daemon restart.
- `cx-interrupt.jsonl` is a pinned **candidate**, not release promotion
  evidence yet. A real `turn/interrupt` acknowledgement and an
  `interrupted` terminal frame are required for capture. The digest ends its
  busy state, but the success-notification path does not yet distinguish a
  voluntary stop. Keep the release slot open until this is tested across surfaces.
