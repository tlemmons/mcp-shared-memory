#!/usr/bin/env bash
# Sync the latest local backups to storm (offsite Windows host).
#
# Uses scp over SSH (Windows OpenSSH doesn't include rsync by default).
# Each backup file is a full snapshot, so scp is sufficient — no incremental
# transfer needed.
#
# Keeps the last N backups on storm to match local rotation.
#
# Usage:
#   ./sync-to-storm.sh
#
# Cron (daily at 4:00am, after local backups complete):
#   0 4 * * * /path/to/sync-to-storm.sh

set -euo pipefail

# ── Config ──
STORM_HOST="${STORM_HOST:-192.168.15.250}"
STORM_USER="${STORM_USER:-Administrator}"
STORM_DIR="${STORM_DIR:-C:/SageBackup}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/storm-backup}"
KEEP_REMOTE=14

CHROMA_LOCAL_DIR="$HOME/chroma-backups"
MONGO_LOCAL_DIR="$HOME/mongo-backups"

SSH_OPTS="-i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=15"
SSH="ssh $SSH_OPTS $STORM_USER@$STORM_HOST"
SCP="scp $SSH_OPTS"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }

# ── Verify storm is reachable ──
log "Checking storm connectivity..."
if ! $SSH 'exit 0' 2>/dev/null; then
    log "ERROR: Cannot reach storm at $STORM_HOST. Skipping offsite sync."
    exit 1
fi

# ── Create remote subdirs ──
$SSH "if not exist \"$STORM_DIR\\chroma\" mkdir \"$STORM_DIR\\chroma\"" >/dev/null 2>&1 || true
$SSH "if not exist \"$STORM_DIR\\mongo\" mkdir \"$STORM_DIR\\mongo\"" >/dev/null 2>&1 || true

# ── Sync Chroma backups ──
sync_dir() {
    local local_dir="$1"
    local remote_subdir="$2"
    local pattern="$3"

    if [ ! -d "$local_dir" ]; then
        log "Local dir $local_dir does not exist, skipping"
        return 0
    fi

    # Get list of remote files to avoid re-copying
    local remote_files
    remote_files=$($SSH "dir /b \"$STORM_DIR\\$remote_subdir\\$pattern\" 2>nul" 2>/dev/null | tr -d '\r' || true)

    local new_count=0
    local skip_count=0
    for local_file in "$local_dir"/$pattern; do
        [ -f "$local_file" ] || continue
        local basename
        basename=$(basename "$local_file")
        if echo "$remote_files" | grep -qx "$basename"; then
            skip_count=$((skip_count + 1))
            continue
        fi
        log "Copying $basename ($(du -h "$local_file" | cut -f1))..."
        $SCP "$local_file" "$STORM_USER@$STORM_HOST:$STORM_DIR/$remote_subdir/" >/dev/null
        new_count=$((new_count + 1))
    done
    log "  $remote_subdir: $new_count new, $skip_count already present"
}

log "Syncing chroma backups..."
sync_dir "$CHROMA_LOCAL_DIR" "chroma" "chroma-backup-*.tar.gz"

log "Syncing mongo backups..."
sync_dir "$MONGO_LOCAL_DIR" "mongo" "mongo-backup-*.archive.gz"

# ── Rotate remote ──
rotate_remote() {
    local remote_subdir="$1"
    local pattern="$2"
    local keep="$3"

    # Windows 'dir /b /o-d' sorts by date descending; we take names after the Nth
    local to_delete
    to_delete=$($SSH "dir /b /o-d \"$STORM_DIR\\$remote_subdir\\$pattern\" 2>nul" 2>/dev/null | tr -d '\r' | awk -v n=$keep 'NR>n' || true)

    if [ -n "$to_delete" ]; then
        local count
        count=$(echo "$to_delete" | wc -l)
        log "Rotating $remote_subdir: removing $count old file(s)"
        while IFS= read -r fname; do
            [ -n "$fname" ] || continue
            $SSH "del \"$STORM_DIR\\$remote_subdir\\$fname\"" >/dev/null 2>&1 || true
        done <<< "$to_delete"
    fi
}

rotate_remote "chroma" "chroma-backup-*.tar.gz" "$KEEP_REMOTE"
rotate_remote "mongo" "mongo-backup-*.archive.gz" "$KEEP_REMOTE"

# ── Summary ──
log "Done. Remote inventory:"
$SSH "dir /b \"$STORM_DIR\\chroma\" 2>nul | find /c /v \"\"" 2>/dev/null | tr -d '\r' | awk '{print "  chroma backups: " $1}'
$SSH "dir /b \"$STORM_DIR\\mongo\" 2>nul | find /c /v \"\"" 2>/dev/null | tr -d '\r' | awk '{print "  mongo backups:  " $1}'
