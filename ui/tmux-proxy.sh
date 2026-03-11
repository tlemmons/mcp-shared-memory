#!/bin/bash
# Proxy script for ttyd to interact with a tmux pane without tmux keybindings
# Usage: tmux-proxy.sh session:window

TARGET="$1"

if [ -z "$TARGET" ]; then
    echo "Usage: $0 session:window"
    exit 1
fi

# Check if target exists
if ! tmux has-session -t "${TARGET%%:*}" 2>/dev/null; then
    echo "Error: tmux session not found: ${TARGET%%:*}"
    exit 1
fi

# Set terminal size to match
export TERM=xterm-256color

# Initial display
tmux capture-pane -t "$TARGET" -p -e

# Watch for changes and relay input
while true; do
    # Read a single character with timeout
    if read -r -t 0.1 -n 1 char; then
        if [ -n "$char" ]; then
            # Send the character to tmux
            tmux send-keys -t "$TARGET" -l "$char"
        else
            # Empty read means Enter was pressed
            tmux send-keys -t "$TARGET" Enter
        fi
    fi

    # Capture and display current pane content
    # Only refresh if there's no pending input
    if ! read -r -t 0 2>/dev/null; then
        clear
        tmux capture-pane -t "$TARGET" -p -e
    fi
done
