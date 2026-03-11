#!/bin/bash
# Startup script for MCP Memory Server
# Waits for Chroma to be ready before starting MCP server

set -e

CHROMA_URL="http://localhost:8001/api/v2/heartbeat"
MAX_WAIT=60
WAIT_INTERVAL=2

echo "Starting Chroma container..."
docker start claude-chroma 2>/dev/null || true

echo "Waiting for Chroma to be ready (max ${MAX_WAIT}s)..."
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
    if curl -s --max-time 2 "$CHROMA_URL" > /dev/null 2>&1; then
        echo "Chroma is ready after ${elapsed}s"
        break
    fi
    sleep $WAIT_INTERVAL
    elapsed=$((elapsed + WAIT_INTERVAL))
    echo "  Waiting... (${elapsed}s)"
done

if [ $elapsed -ge $MAX_WAIT ]; then
    echo "WARNING: Chroma did not respond within ${MAX_WAIT}s, starting MCP server anyway"
fi

echo "Starting MCP Memory Server..."
cd "$(dirname "${BASH_SOURCE[0]}")"

# Only rebuild if no cached image exists; avoids needing Docker Hub on boot
if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q 'mcp_ragarch-mcp-rag-arch:latest'; then
    echo "Using cached image (skipping build)"
    docker compose up -d
else
    echo "No cached image found, building..."
    docker compose up -d --build
fi

echo "Startup complete"
