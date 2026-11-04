# Codex Replay Fixtures

These JSONL files contain the provider frames that the card digest consumes.
They are intentionally small, deterministic contract fixtures, not production
conversation exports. `cx-*.jsonl` uses recorded app-server frame shape;
`gemini-*.jsonl` uses the normalized ACP turn contract; `openclaw-*.jsonl`
uses gateway event shape. The live recorder is opt-in and writes to the path in
`POCKET_RECORD_FIXTURE`; do not commit a recording containing private prompts,
paths, secrets, or tool output without reviewing it first.

```sh
POCKET_RECORD_FIXTURE=/private/tmp/codex-session.jsonl \
  launchctl kickstart -k gui/$(id -u)/com.pocketconnect.bridge
```

Replay all reviewed fixtures with:

```sh
python3 tests/test_replay_fixtures.py
```
