#!/bin/bash
set -e

# MCP Shared Memory Server - Install Script
# Version: 1.0.0

VERSION="1.0.0"
INSTALL_DIR="${INSTALL_DIR:-/opt/mcp-memory}"

echo "========================================"
echo "MCP Shared Memory Server Installer v${VERSION}"
echo "========================================"
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check for required commands
check_requirement() {
    if ! command -v "$1" &> /dev/null; then
        echo -e "${RED}ERROR: $1 is required but not installed.${NC}"
        echo "Please install $1 and try again."
        exit 1
    fi
    echo -e "${GREEN}OK${NC} - $1 found"
}

echo "Checking requirements..."
check_requirement docker
check_requirement curl

# Check for docker compose (v2) or docker-compose (v1)
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
    echo -e "${GREEN}OK${NC} - docker compose (v2) found"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
    echo -e "${GREEN}OK${NC} - docker-compose (v1) found"
else
    echo -e "${RED}ERROR: docker compose is required but not installed.${NC}"
    echo "Please install docker compose and try again."
    exit 1
fi

# Check if Docker daemon is running
if ! docker info &> /dev/null; then
    echo -e "${RED}ERROR: Docker daemon is not running.${NC}"
    echo "Please start Docker and try again."
    exit 1
fi
echo -e "${GREEN}OK${NC} - Docker daemon is running"

echo ""
echo "All requirements satisfied!"
echo ""

# Create install directory
echo "Installing to ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"
sudo chown $(whoami):$(whoami) "${INSTALL_DIR}"

# Copy files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "${SCRIPT_DIR}/docker-compose.yml" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/Dockerfile" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/upgrade.sh" "${INSTALL_DIR}/" 2>/dev/null || true

# Store version
echo "${VERSION}" > "${INSTALL_DIR}/VERSION"

echo ""
echo "Building and starting services..."
cd "${INSTALL_DIR}"
${COMPOSE_CMD} build
${COMPOSE_CMD} up -d

echo ""
echo "Waiting for services to be healthy..."
sleep 5

# Check health
for i in {1..30}; do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo ""
        echo -e "${GREEN}========================================"
        echo "Installation Complete!"
        echo "========================================${NC}"
        echo ""
        echo "MCP Server:  http://localhost:8080/mcp"
        echo "Health:      http://localhost:8080/health"
        echo "Chroma:      http://localhost:8001"
        echo ""
        echo "Installed to: ${INSTALL_DIR}"
        echo "Version: ${VERSION}"
        echo ""
        echo "To configure Claude Code, add to ~/.claude.json or .mcp.json:"
        echo ""
        echo '  "mcpServers": {'
        echo '    "shared-memory": {'
        echo '      "type": "http",'
        echo '      "url": "http://<server-ip>:8080/mcp"'
        echo '    }'
        echo '  }'
        echo ""
        echo "To upgrade later: cd ${INSTALL_DIR} && ./upgrade.sh"
        echo ""
        exit 0
    fi
    echo -n "."
    sleep 2
done

echo ""
echo -e "${YELLOW}WARNING: Services started but health check timed out.${NC}"
echo "Check logs with: cd ${INSTALL_DIR} && ${COMPOSE_CMD} logs"
exit 1
