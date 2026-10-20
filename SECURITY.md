# Security Policy

## What this software is

This bridge is **an agent gateway that executes tools on the machine it runs on**.
It drives Claude Code / Codex sessions in tmux, serves a terminal over WebSocket,
browses files, and relays approvals. Treat it as a remote-control surface for the
whole host, not as an ordinary web service.

Two consequences:

- **A bridge token is equivalent to shell access on that machine.** Do not paste
  one into an issue, a screenshot, or a chat with a third party.
- **Do not expose the bridge directly to the public internet.** It is designed to
  be reached over your own private network (Tailscale or equivalent) or through a
  tunnel you control. Several endpoints deliberately refuse requests that arrive
  with forwarding headers for exactly this reason.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Email **m@tsai.cash** with:

- what you did (a request, a sequence of UI actions, a script)
- what happened versus what you expected
- the version: `git rev-parse --short HEAD`, or the build number if you hit it
  through the Pocket iOS app
- whether it needs an existing device token, a boot code, or nothing at all —
  this is the single most useful detail for triage

You will get an acknowledgement. This is a small project maintained by one person
plus automation, so please allow a few days before assuming the message was lost.

If you believe a credential of yours was exposed by a bug here, rotate it first
and report second. Rotation instructions:

- **Bridge token** — regenerate it in the LaunchAgent environment and restart the
  service; every paired device must pair again.
- **Console boot code** — delete `~/.pocket/pair-boot-code` and restart. This also
  invalidates every console cookie, because those are derived from it.
- **Device token** — revoke the device (`POST /pair/revoke` with the master token).

## Scope

In scope:

- Anything that lets a request **without** a valid credential reach session data,
  the terminal, the filesystem browser, or approval decisions.
- Privilege escalation between credential tiers: console boot code → device token
  → master token are meant to be strictly increasing, never sideways.
- Credential leakage into logs, QR payloads, URLs, or the repository.

Out of scope (known and accepted):

- The bridge trusts the machine it runs on. Local root is game over by design.
- Denial of service from an already-authenticated client.
- Anything that requires physical access to an unlocked host.

## Supported versions

`main` only. This project ships continuously; there are no maintained release
branches. Fixes land on `main` and are deployed by restarting the service.
