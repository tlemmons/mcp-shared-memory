#!/bin/bash
set -e

# MCP Shared Memory Server - Upgrade Script

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check for docker compose
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}ERROR: docker compose not found${NC}"
    exit 1
fi

CURRENT_VERSION=$(cat "${INSTALL_DIR}/VERSION" 2>/dev/null || echo "unknown")

echo "========================================"
echo "MCP Shared Memory Server Upgrade"
echo "========================================"
echo ""
echo "Current version: ${CURRENT_VERSION}"
echo "Install dir: ${INSTALL_DIR}"
echo ""

# Check if new files are provided
if [ "$1" == "" ]; then
    echo "Usage: ./upgrade.sh <path-to-new-deploy-folder>"
    echo ""
    echo "Example: ./upgrade.sh ~/downloads/mcp-deploy-v1.1.0"
    echo ""
    echo "The new deploy folder should contain:"
    echo "  - server.py"
    echo "  - requirements.txt"
    echo "  - Dockerfile"
    echo "  - docker-compose.yml"
    echo "  - VERSION (optional)"
    exit 1
fi

NEW_DEPLOY="$1"

if [ ! -d "$NEW_DEPLOY" ]; then
    echo -e "${RED}ERROR: $NEW_DEPLOY is not a directory${NC}"
    exit 1
fi

if [ ! -f "$NEW_DEPLOY/server.py" ]; then
    echo -e "${RED}ERROR: $NEW_DEPLOY/server.py not found${NC}"
    exit 1
fi

NEW_VERSION=$(cat "${NEW_DEPLOY}/VERSION" 2>/dev/null || echo "unknown")
echo "New version: ${NEW_VERSION}"
echo ""

read -p "Proceed with upgrade? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Upgrade cancelled."
    exit 0
fi

echo ""
echo "Creating backup..."
BACKUP_DIR="${INSTALL_DIR}/backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "${BACKUP_DIR}"
cp "${INSTALL_DIR}/server.py" "${BACKUP_DIR}/" 2>/dev/null || true
cp "${INSTALL_DIR}/requirements.txt" "${BACKUP_DIR}/" 2>/dev/null || true
cp "${INSTALL_DIR}/Dockerfile" "${BACKUP_DIR}/" 2>/dev/null || true
cp "${INSTALL_DIR}/docker-compose.yml" "${BACKUP_DIR}/" 2>/dev/null || true
cp "${INSTALL_DIR}/VERSION" "${BACKUP_DIR}/" 2>/dev/null || true
echo "Backup saved to: ${BACKUP_DIR}"

echo ""
echo "Copying new files..."
cp "${NEW_DEPLOY}/server.py" "${INSTALL_DIR}/"
cp "${NEW_DEPLOY}/requirements.txt" "${INSTALL_DIR}/" 2>/dev/null || true
cp "${NEW_DEPLOY}/Dockerfile" "${INSTALL_DIR}/" 2>/dev/null || true
cp "${NEW_DEPLOY}/docker-compose.yml" "${INSTALL_DIR}/" 2>/dev/null || true
cp "${NEW_DEPLOY}/VERSION" "${INSTALL_DIR}/" 2>/dev/null || echo "${NEW_VERSION}" > "${INSTALL_DIR}/VERSION"

echo ""
echo "Rebuilding and restarting services..."
cd "${INSTALL_DIR}"
${COMPOSE_CMD} build
${COMPOSE_CMD} up -d

echo ""
echo "Waiting for services..."
sleep 5

for i in {1..30}; do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo ""
        echo -e "${GREEN}========================================"
        echo "Upgrade Complete!"
        echo "========================================${NC}"
        echo ""
        echo "Previous version: ${CURRENT_VERSION}"
        echo "New version: ${NEW_VERSION}"
        echo "Backup: ${BACKUP_DIR}"
        echo ""
        curl -s http://localhost:8080/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8080/health
        echo ""
        exit 0
    fi
    echo -n "."
    sleep 2
done

echo ""
echo -e "${YELLOW}WARNING: Services restarted but health check timed out.${NC}"
echo "Check logs: ${COMPOSE_CMD} logs"
echo ""
echo "To rollback: cp ${BACKUP_DIR}/* ${INSTALL_DIR}/ && ${COMPOSE_CMD} build && ${COMPOSE_CMD} up -d"
exit 1
