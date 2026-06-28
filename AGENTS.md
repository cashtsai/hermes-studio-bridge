# Agent Governance

This bridge is the stable API surface between PocketAgent and Studio/Hermes.
Treat it as infrastructure, not as an experiment scratchpad.

## Before Starting Work

1. Run `git status --short --branch`, `git log --oneline -5`, and `git remote -v`.
2. If `bridge.py` is dirty, inspect the diff first. Large diffs usually mean
   another agent is actively changing runtime behavior.
3. Keep app-facing behavior aligned with `docs/APP_BRIDGE_CONTRACT.md`.

## Contract Rules

- `/app/v1/*` is the stable PocketAgent contract.
- Unknown app sessions must fail clearly; never silently fall back to `xcash`.
- `dry_run` must not call Hermes and must not write canonical messages.
- Do not log bearer tokens, attachment bytes, or full private user content.
- Additive endpoints are allowed; breaking response changes require updating
  the contract doc and the iOS client in the same work block.

## Runtime Rules

- Restart through launchd from outside the bridge process:
  `launchctl kickstart -k gui/501/ai.studio.hermes-bridge`
- Do not make the gateway restart itself from inside a request handler.
- Keep production mutations behind approval or dry-run.

## Required Verification

- `python3 -m py_compile bridge.py`
- `/health`
- `/capabilities`
- `/app/v1/messages` dry-run
- If persistence changed, compare canonical DB counts before and after dry-run.
- If auth changed, verify both valid-token success and invalid-token failure.

## Commit Rules

- Stage only files that belong to the current task.
- Commit docs separately from runtime code when runtime code is already dirty.
- Push only after live smoke tests or after explicitly documenting why they were
  not safe to run.
