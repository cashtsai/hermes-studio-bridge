# Handoff & Maintenance — Studio · Hermes bridge

Audience: whoever maintains this on **cashcamp** (the Studio Mac). Assumes the
Hermes install lives at `~/apps/hermes-agent`.

## Key facts

- **Host**: cashcamp (Tailscale `100.67.0.12`). Phone reaches services over Tailscale.
- **Bridge token**: stored in the LaunchAgent env `BRIDGE_TOKEN` (`~/Library/LaunchAgents/ai.studio.hermes-bridge.plist`). Open WebUI's `OPENAI_API_KEY` must match it.
- **Ports**: bridge `8081`, Open WebUI `3000`.
- **Hermes homes** (= persona memory/state, shared with Telegram bots):
  - 袁方/main → `~/apps/hermes-agent/home`
  - 潘天晴 → `~/apps/hermes-agent/home/profiles/fliper`
  - 善彰/xcash → `~/apps/hermes-agent/home/profiles/xcash`
  - 水鏡 → `~/apps/hermes-agent/home/profiles/shuijing`

## Everyday ops

```bash
# bridge
launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge   # restart
tail -f ~/apps/hermes-openwebui-bridge/bridge.err.log          # logs
curl -s -H "Authorization: Bearer $BRIDGE_TOKEN" http://127.0.0.1:8081/v1/models   # health

# Open WebUI
colima status            # the Docker VM must be up
docker logs -f open-webui
docker restart open-webui
BRIDGE_TOKEN=<token> ~/apps/hermes-openwebui-bridge/deploy/run-openwebui.sh   # recreate

# update Open WebUI
docker pull ghcr.io/open-webui/open-webui:main && BRIDGE_TOKEN=<token> ./deploy/run-openwebui.sh
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Open WebUI shows no models | bridge down or token mismatch. `curl …/v1/models` with the token; check `OPENAI_API_KEY` == `BRIDGE_TOKEN`. |
| "network connection lost" on long replies | Heartbeat streaming should prevent this. Confirm the client sends `stream:true`; check bridge is the streaming build (SSE `: keepalive`). |
| Reply is generic / no memory | Wrong `HERMES_HOME` mapping in `bridge.py` `PERSONAS`, or that profile's `memories/` is empty. |
| Persona "stuck" 60–90s | Normal — agentic turns with tools are slow. Heartbeat keeps the socket alive; it will finish. |
| Open WebUI unreachable from phone | Tailscale off on phone, or Colima/`open-webui` container down. |
| bridge 401 | Missing/wrong bearer token. |

## Adding / changing a persona

Edit `PERSONAS` in `bridge.py` (id → display name + `HERMES_HOME`), then restart
the bridge. The id becomes the model id Open WebUI shows.

## Coexistence with Telegram

The Telegram gateways (`ai.hermes.gateway*` LaunchAgents) and this bridge are
independent processes that read the same Hermes homes. Running both is expected;
neither restarts or reconfigures the other.

## Next optimizations (not yet done)

1. ~~Real ACP streaming~~ ✅ DONE (`acp_client.py`, persistent warm process + live chunks + auto-permission). Next: surface tool-call progress as visible steps.
2. **HTTPS** via Tailscale Serve; drop the iOS `NSAllowsArbitraryLoads`.
3. Per-conversation sessions (currently one `--continue owui-<persona>` per persona).

## M3 — CC/Codex 調度(delegations; `/dispatch` is legacy)

- **bridge `POST /app/v1/delegations`** `{parent_persona, provider, title, objective, cwd}` → create a durable work-order session with `work_order`, parent ownership, and takeover metadata.
- Codex/CX uses the Codex app-server native thread id; Claude Code/CC uses a named ccsess remote-control session.
- Pocket reads `/app/v1/delegations` or `/app/v2/sessions`; provider-native surfaces resume by Codex thread id or Claude Code session name.
- **`POST /dispatch` and `deploy/studio-dispatch` are legacy only**. Do not use them for formal persona dispatch because they are not the durable cross-surface contract.

## M4 — MCP memory(CC/Codex 共享 Hermes 記憶)

- **`studio_memory_mcp.py`** — 原生 stdio MCP server,工具 `read_memory / search_memory / write_memory`,讀 `$STUDIO_MEMORY_HOME/memories/`(MEMORY.md + USER.md)。
- dispatch claude-code 時自動帶 `--mcp-config`(studio-memory 指向 parent persona 的 home)+ `--append-system-prompt` 提示先讀記憶。
- 實測:CC 子 agent 用 `mcp__studio-memory__read_memory` 讀到善彰身份/事業。
- 待優化:也讀 `shared/memory/*_SHARED.md`(MEMORY.md 已轉向 shared 結構);Codex 的 MCP 接法。
