"""
Mutable global state for the Shared Memory MCP Server.

These in-memory dictionaries track runtime state that does not need
persistence -- they are rebuilt each time the server starts.
"""

from typing import Dict, Any


# Active sessions stored in memory (lightweight, no persistence needed)
active_sessions: Dict[str, Dict[str, Any]] = {}

# File locks stored in memory (auto-released on session end)
# Structure: { file_path: { session_id, claude_instance, reason, locked_at } }
file_locks: Dict[str, Dict[str, Any]] = {}

# Signals stored in memory (retained for 24 hours)
# Structure: { signal_name: { from_session, from_claude, timestamp, details } }
active_signals: Dict[str, Dict[str, Any]] = {}
