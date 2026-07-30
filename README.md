# Studio · Hermes — OpenAI-compatible bridge + Open WebUI

A thin, stable front door to a **Hermes** multi-persona agent: it exposes each
Hermes persona as an OpenAI "model", so any OpenAI-compatible client
(**Open WebUI**, mobile apps, scripts) can chat with it — with the persona's
**shared long-term memory** intact. Runs **alongside** the existing Telegram
gateways; nothing about Telegram changes.

```
 Phone / PWA / app ─▶ Open WebUI (mature chat UI, login-gated)
                       └─▶ bridge.py (OpenAI /v1, token-gated)  :8081
                             └─▶ hermes -z --continue owui-<persona>
                                   └─ HERMES_HOME=<profile home>  (= shared memory)
 Telegram bots (unchanged) ─▶ same Hermes homes/state.db  ── coexists, no conflict
```

## Components

| Piece | What / where |
|---|---|
| **bridge.py** | FastAPI app. `GET /v1/models` (personas), `POST /v1/chat/completions` (SSE streaming). Token-gated (`BRIDGE_TOKEN`). Runs `hermes -z` per turn. |
| **bridge LaunchAgent** | `deploy/ai.studio.hermes-bridge.plist` → `~/Library/LaunchAgents/`. KeepAlive, RunAtLoad, port 8081, injects `BRIDGE_TOKEN`. |
| **Open WebUI** | Docker (`deploy/run-openwebui.sh`), port 3000, points at `host.docker.internal:8081/v1`. Login required (first user = admin). |
| **Personas** | `yuanfang` 袁方(main)· `pantianqing` 潘天晴(fliper)· `xcash` 善彰 · `shuijing` 水鏡. Mapped to Hermes profile homes in `bridge.py` → `PERSONAS`. |

## Quickstart (this host = cashcamp)

```bash
# 1. bridge (install or upgrade the per-user LaunchAgent)
./deploy/install-local-bridge.sh

# 2. bridge restart
launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge

# 3. Open WebUI (needs Docker/Colima)
colima start
BRIDGE_TOKEN=<your token> ./deploy/run-openwebui.sh
```

Reach it from your phone over Tailscale: **http://100.67.0.12:3000**
(first visit: create the admin account → pick a persona model → chat).

## Security

- **bridge**: bearer-token gated (`BRIDGE_TOKEN`, set in the LaunchAgent env; never commit it). Bound to `0.0.0.0:8081` but only reachable on LAN/tailnet.
- **Open WebUI**: login required (`WEBUI_AUTH` default on).
- HTTP today; tighten to HTTPS later via **Tailscale Serve**.

## Local Bridge Install Contract

PocketConnect should install the bridge as a per-user component instead of
asking users to clone repos and hand-edit paths. The supported installer entry
point is:

```bash
./deploy/install-local-bridge.sh
```

The installer copies this bridge bundle to
`~/Library/Application Support/PocketConnect/bridge/current`, writes a per-user
LaunchAgent, preserves an existing `BRIDGE_TOKEN` across upgrades, and injects
runtime paths through environment variables:

| Variable | Purpose |
|---|---|
| `BRIDGE_TOKEN` | Per-machine app-to-bridge bearer token. |
| `HERMES_BIN` | Absolute path to the Hermes CLI. |
| `HERMES_HOME_ROOT` | Persona/profile root, usually `~/apps/hermes-agent/home`. |
| `OPENCLAW_CONFIG_FILE` | Optional OpenClaw provider config, usually `~/.pocket/openclaw.json`. |

This keeps clean installs separate from production: do not reuse
`pocket.tsai.cash`, Telegram gateway LaunchAgents, or copied production Hermes
credentials on a test host.

For a full clean-machine bootstrap, set `POCKET_PROVIDER=auto` before running
the installer. The installer first adopts existing user installs:

- existing Hermes CLI from `~/apps/hermes-agent/.../hermes` or `~/.local/bin/hermes`
- existing OpenClaw config from `~/.pocket/openclaw.json`

If neither provider is already present, `POCKET_DEFAULT_PROVIDER` decides which
single provider to fresh-install (`hermes`, `openclaw`, or `none`). It never
installs both providers in one pass. The fresh-install paths are:

- Hermes from `https://github.com/NousResearch/hermes-agent.git` into
  `~/apps/hermes-agent`
- Node `24.18.0` from nodejs.org into `~/apps/node-v24.18.0-darwin-arm64`
- OpenClaw `2026.7.1-2` from npm into `~/apps/openclaw-clean`
- OpenClaw LaunchAgent `com.pocketconnect.openclaw` on `127.0.0.1:19801`

No production profiles, credentials, Telegram gateway LaunchAgents, or
Cloudflare named tunnels are copied.

## Roadmap / known limits

- **Streaming is live over a persistent ACP session** (`acp_client.py`): one warm `hermes acp` process per persona removes the ~5s `hermes -z` cold start (warm turn ≈1.6s vs ≈6s) and streams real text chunks. SSE keepalives cover pre-first-token gaps. Cold `hermes -z` remains a fallback. Tool permission prompts are auto-approved.
- One ongoing session per persona (`--continue owui-<persona>`); no per-conversation isolation yet.
- `hermes -z` shares the persona `state.db` with its live Telegram gateway — fine in practice, watch for write contention on very long turns.

See `docs/HANDOFF.md` for maintenance & troubleshooting.
