#!/bin/bash
# Startup script for MCP Memory Server — compose-managed since 2026-04-17.
# Brings up chromadb + mongodb first, waits for Chroma health, then brings
# up mcp-server.

set -e

CHROMA_URL="http://localhost:8001/api/v2/heartbeat"
MAX_WAIT=60
WAIT_INTERVAL=2

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Starting Chroma + MongoDB via docker compose..."
docker compose up -d chromadb mongodb

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

# Only rebuild if no cached image exists; avoids needing Docker Hub on boot
if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q 'mcp_ragarch-mcp-server:latest'; then
    echo "Using cached image (skipping build)"
    docker compose up -d mcp-server
else
    echo "No cached image found, building..."
    docker compose up -d --build mcp-server
fi

echo "Startup complete"
