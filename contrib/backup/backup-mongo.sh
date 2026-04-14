#!/usr/bin/env bash
# Back up MongoDB databases from a running mongo container using mongodump.
#
# mongodump creates a consistent bson snapshot by querying mongo directly,
# unlike a file-level tar which can capture mid-write corruption.
#
# Usage:
#   ./backup-mongo.sh [backup_dir] [container_name]
#
# Defaults:
#   backup_dir: ~/mongo-backups
#   container_name: emailtriage-prod-mongodb
#
# Environment:
#   MONGO_USER, MONGO_PASSWORD — read from the MCP project's .env by default
#
# Rotation: keeps the last 14 backups.
#
# Cron (daily at 3:15am, offset from the chroma backup):
#   15 3 * * * /path/to/backup-mongo.sh

set -euo pipefail

BACKUP_DIR="${1:-$HOME/mongo-backups}"
CONTAINER_NAME="${2:-mcp-mongodb-new}"
KEEP_BACKUPS=14

# Load creds from .env if available
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

# Default to MCP creds but allow override with ADMIN creds for a full-dump.
# To back up ALL databases on a shared mongo instance, pass admin creds:
#   BACKUP_USER=admin BACKUP_PASSWORD=xxxxx ./backup-mongo.sh
MONGO_USER="${BACKUP_USER:-${MONGO_USER:-mcp_orch}}"
MONGO_PASSWORD="${BACKUP_PASSWORD:-${MONGO_PASSWORD:-changeme}}"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/mongo-backup-$TIMESTAMP.archive.gz"

echo "Dumping MongoDB from '$CONTAINER_NAME' to $BACKUP_FILE..."

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "ERROR: Container '$CONTAINER_NAME' is not running." >&2
    exit 1
fi

# mongodump with archive format, gzipped. Dumps ALL databases.
docker exec "$CONTAINER_NAME" mongodump \
    --username "$MONGO_USER" \
    --password "$MONGO_PASSWORD" \
    --authenticationDatabase admin \
    --archive \
    --gzip \
  > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup complete: $BACKUP_FILE ($SIZE)"

# Rotate
COUNT=$(ls -1 "$BACKUP_DIR"/mongo-backup-*.archive.gz 2>/dev/null | wc -l)
if [ "$COUNT" -gt "$KEEP_BACKUPS" ]; then
    REMOVE_COUNT=$((COUNT - KEEP_BACKUPS))
    echo "Rotating: removing $REMOVE_COUNT old backup(s)..."
    ls -1t "$BACKUP_DIR"/mongo-backup-*.archive.gz | tail -n "$REMOVE_COUNT" | xargs rm -v
fi

echo "Total backups: $(ls -1 "$BACKUP_DIR"/mongo-backup-*.archive.gz 2>/dev/null | wc -l)"
