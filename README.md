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
# 1. bridge (already installed as a LaunchAgent)
launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge

# 2. Open WebUI (needs Docker/Colima)
colima start
BRIDGE_TOKEN=<your token> ./deploy/run-openwebui.sh
```

Reach it from your phone over Tailscale: **http://100.67.0.12:3000**
(first visit: create the admin account → pick a persona model → chat).

## Security

- **bridge**: bearer-token gated (`BRIDGE_TOKEN`, set in the LaunchAgent env; never commit it). Bound to `0.0.0.0:8081` but only reachable on LAN/tailnet.
- **Open WebUI**: login required (`WEBUI_AUTH` default on).
- HTTP today; tighten to HTTPS later via **Tailscale Serve**.

## Roadmap / known limits

- **Streaming is heartbeat-based** (keepalive every 2s, then the full reply) — this fixes the long-turn "connection lost". Real token-by-token + tool-progress streaming = drive Hermes **ACP** instead of `hermes -z` (next optimization).
- One ongoing session per persona (`--continue owui-<persona>`); no per-conversation isolation yet.
- `hermes -z` shares the persona `state.db` with its live Telegram gateway — fine in practice, watch for write contention on very long turns.

See `docs/HANDOFF.md` for maintenance & troubleshooting.
