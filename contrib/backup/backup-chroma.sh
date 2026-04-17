#!/usr/bin/env bash
# Back up ChromaDB data from the running container to a timestamped tarball.
#
# Usage:
#   ./backup-chroma.sh [backup_dir]
#
# Defaults to ~/chroma-backups/ if no dir is given.
# Rotates old backups, keeping the last 14 (roughly two weeks of daily backups).
#
# Safe to run while the server is live — Chroma's SQLite supports concurrent reads.
#
# Schedule via cron for daily backups:
#   0 3 * * * /path/to/backup-chroma.sh

set -euo pipefail

BACKUP_DIR="${1:-$HOME/chroma-backups}"
CONTAINER_NAME="${CHROMA_CONTAINER:-mcp-chromadb}"
KEEP_BACKUPS=14

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/chroma-backup-$TIMESTAMP.tar.gz"

echo "Backing up ChromaDB from container '$CONTAINER_NAME' to $BACKUP_FILE..."

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "ERROR: Container '$CONTAINER_NAME' is not running." >&2
    exit 1
fi

# Create the tar inside the container, then copy it out
docker exec "$CONTAINER_NAME" tar czf /tmp/chroma-backup.tar.gz -C / data
docker cp "$CONTAINER_NAME:/tmp/chroma-backup.tar.gz" "$BACKUP_FILE"
docker exec "$CONTAINER_NAME" rm -f /tmp/chroma-backup.tar.gz

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup complete: $BACKUP_FILE ($SIZE)"

# Rotate: keep only the last N backups
COUNT=$(ls -1 "$BACKUP_DIR"/chroma-backup-*.tar.gz 2>/dev/null | wc -l)
if [ "$COUNT" -gt "$KEEP_BACKUPS" ]; then
    REMOVE_COUNT=$((COUNT - KEEP_BACKUPS))
    echo "Rotating: removing $REMOVE_COUNT old backup(s)..."
    ls -1t "$BACKUP_DIR"/chroma-backup-*.tar.gz | tail -n "$REMOVE_COUNT" | xargs rm -v
fi

echo "Total backups: $(ls -1 "$BACKUP_DIR"/chroma-backup-*.tar.gz 2>/dev/null | wc -l)"
