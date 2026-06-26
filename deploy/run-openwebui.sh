#!/bin/bash
# Run Open WebUI, pointed at the Hermes bridge. Requires Docker (Colima) up:
#   colima start
# Set BRIDGE_TOKEN to the same value the bridge uses (LaunchAgent env).
set -euo pipefail
BRIDGE_TOKEN="${BRIDGE_TOKEN:?set BRIDGE_TOKEN to match the bridge}"

docker rm -f open-webui 2>/dev/null || true
docker run -d -p 3000:8080 \
  --add-host=host.docker.internal:host-gateway \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8081/v1 \
  -e OPENAI_API_KEY="$BRIDGE_TOKEN" \
  -e ENABLE_OLLAMA_API=false \
  -e WEBUI_NAME="Studio · Hermes" \
  -v open-webui:/app/backend/data \
  --name open-webui \
  --restart unless-stopped \
  ghcr.io/open-webui/open-webui:main

echo "Open WebUI → http://<this-host>:3000  (login required; first user = admin)"
