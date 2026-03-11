#!/bin/bash
# Watchdog script for MCP Memory Server
# Checks health every 30 seconds and restarts if unresponsive

HEALTH_URL="http://localhost:8080/health"
CONTAINER="mcp-rag-arch"
CHECK_INTERVAL=30
FAIL_THRESHOLD=2
LOG_FILE="/var/log/mcp-watchdog.log"

fail_count=0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

while true; do
    # Check health with 5 second timeout
    if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        log "Health check failed (count: $fail_count/$FAIL_THRESHOLD)"

        if [ $fail_count -ge $FAIL_THRESHOLD ]; then
            log "Restarting $CONTAINER..."
            docker restart "$CONTAINER" >> "$LOG_FILE" 2>&1
            fail_count=0
            # Wait a bit for container to start
            sleep 10
        fi
    fi

    sleep $CHECK_INTERVAL
done
