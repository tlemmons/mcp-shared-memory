#!/bin/bash
# Setup script for Shared Memory MCP Server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_IP=$(hostname -I | awk '{print $1}')

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       Shared Memory MCP Server Setup                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if docker compose is available
if ! docker compose version &> /dev/null; then
    echo "ERROR: Docker Compose is not available. Please install Docker Compose."
    exit 1
fi

echo "1. Building Docker image..."
cd "$SCRIPT_DIR"
docker compose build

echo ""
echo "2. Installing systemd service..."
sudo cp "$SCRIPT_DIR/mcp-rag-arch.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo ""
echo "3. Enabling service to start on boot..."
sudo systemctl enable mcp-rag-arch

echo ""
echo "4. Starting service..."
sudo systemctl start mcp-rag-arch

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                     Setup Complete                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Service status:"
sudo systemctl status mcp-rag-arch --no-pager || true
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "MCP Server endpoint: http://${SERVER_IP}:8080/sse"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Add to Claude Code MCP config (~/.claude/settings.json):"
echo ""
echo '   {
     "mcpServers": {
       "shared-memory": {
         "url": "http://'"${SERVER_IP}"':8080/sse"
       }
     }
   }'
echo ""
echo "2. Copy CLAUDE.md.template to each project and customize:"
echo "   cp ${SCRIPT_DIR}/CLAUDE.md.template /path/to/project/CLAUDE.md"
echo "   # Then edit [PROJECT_NAME] and [SERVER_IP] placeholders"
echo ""
echo "3. For remote machines, use this server's IP: ${SERVER_IP}"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Useful commands:"
echo "  sudo systemctl status mcp-rag-arch   # Check status"
echo "  sudo systemctl restart mcp-rag-arch  # Restart"
echo "  sudo systemctl stop mcp-rag-arch     # Stop"
echo "  docker compose logs -f               # View logs"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
