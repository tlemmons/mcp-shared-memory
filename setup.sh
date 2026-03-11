#!/bin/bash
# Setup script for Shared Memory MCP Server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_IP=$(hostname -I | awk '{print $1}')

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       Shared Memory MCP Server Setup                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check prerequisites
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed. Please install Docker first."
    exit 1
fi

if ! docker compose version &> /dev/null; then
    echo "ERROR: Docker Compose is not available. Please install Docker Compose."
    exit 1
fi

# Check for .env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Creating .env from template..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "IMPORTANT: Edit .env to set your MongoDB password before continuing."
    echo "  nano $SCRIPT_DIR/.env"
    exit 1
fi

echo "1. Building and starting services..."
cd "$SCRIPT_DIR"
docker compose up -d --build

echo ""
echo "2. Waiting for health check..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo "   Server is healthy!"
        break
    fi
    sleep 2
    echo "   Waiting... (${i})"
done

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                     Setup Complete                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "MCP Server endpoint: http://${SERVER_IP}:8080/mcp"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Add to Claude Code MCP config (~/.claude.json or .mcp.json):"
echo ""
echo '   {
     "mcpServers": {
       "shared-memory": {
         "type": "http",
         "url": "http://'"${SERVER_IP}"':8080/mcp"
       }
     }
   }'
echo ""
echo "2. Copy CLAUDE.md.template to each project and customize:"
echo "   cp ${SCRIPT_DIR}/CLAUDE.md.template /path/to/project/CLAUDE.md"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Useful commands:"
echo "  docker compose logs -f         # View logs"
echo "  docker compose restart         # Restart"
echo "  docker compose down            # Stop"
echo "  curl http://localhost:8080/health  # Health check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
