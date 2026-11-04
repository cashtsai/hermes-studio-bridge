# CX per-session API key

## Contract

The interactive CX session creator accepts an optional `config.api_key`:

```json
{
  "text": "initial task",
  "cwd": "~/apps/project",
  "config": {"api_key": "..."}
}
```

An empty key keeps the existing managed/shared CX app-server path. A non-empty
key creates a dedicated `codex app-server --stdio` process for that thread.

## Isolation

Each BYOK thread gets:

- its own process-level `OPENAI_API_KEY` environment variable;
- its own `CODEX_HOME` under `~/.config/pocket/cx-byok/sessions/homes/`;
- a host-local 0600 secret file for restart recovery;
- a 0600 metadata index used to list and restore the session.

The shared subscription `auth.json` is deliberately not copied into the BYOK
home. Existing CX sessions keep using the managed app-server and are not
restarted or migrated.

## API safety

The key is never placed in argv, JSON-RPC payloads, session summaries, list
responses, or bridge logs. Public session rows expose only `byok` and
`has_api_key` booleans.

## Restart and failure behavior

After a bridge restart, the session is reconstructed lazily from the private
home and 0600 secret file. If the local secret is missing, the bridge keeps the
failure isolated to that session and never falls back to the shared account.

This feature requires an app build containing the CX API-key field and a bridge
deployment containing the private app-server manager. Android is not included
in this change.
