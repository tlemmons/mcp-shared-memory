#!/usr/bin/env python3
"""
Message Dispatcher - Delivers queued messages to Claude instances via tmux.

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.

Event-driven dispatcher using MongoDB change streams for instant message delivery.
Falls back to polling if change streams unavailable.

Usage:
    ./dispatcher.py [--mongo-url URL] [--mcp-url URL] [--mode MODE]

Examples:
    ./dispatcher.py                                    # Auto-detect mode
    ./dispatcher.py --mode stream                      # Force change streams
    ./dispatcher.py --mode poll --poll-interval 5     # Force polling
"""

import argparse
import subprocess
import sys
import os
import json
import time
import requests
import signal
from datetime import datetime
from typing import Dict, List, Any, Optional
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Default configuration
DEFAULT_MCP_URL = "http://localhost:8080/mcp"
DEFAULT_MONGO_URL = os.getenv("MONGO_URL", "mongodb://mcp_orch:changeme@localhost:27018/mcp_orchestrator")
DEFAULT_MONGO_DB = "mcp_orchestrator"
DEFAULT_POLL_INTERVAL = 5  # seconds (only used in poll mode)

# Dispatcher session info
DISPATCHER_SESSION_ID = None
RUNNING = True


def log(msg: str):
    """Print timestamped log message."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def signal_handler(sig, frame):
    """Handle shutdown signals."""
    global RUNNING
    log("Received shutdown signal...")
    RUNNING = False


def mcp_call(url: str, method: str, params: Dict[str, Any]) -> Optional[Dict]:
    """Make an MCP tool call."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": method,
            "arguments": params
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        # Parse SSE response
        for line in response.text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "result" in data:
                    content = data["result"].get("content", [])
                    if content and content[0].get("type") == "text":
                        return json.loads(content[0]["text"])
        return None
    except Exception as e:
        log(f"MCP call error: {e}")
        return None


def start_dispatcher_session(mcp_url: str) -> Optional[str]:
    """Start a dispatcher session with MCP."""
    result = mcp_call(mcp_url, "memory_start_session", {
        "project": "orchestrator",
        "claude_instance": "dispatcher",
        "task_description": "Message delivery daemon (event-driven)"
    })

    if result and "session_id" in result:
        return result["session_id"]
    return None


def get_agent_status(db) -> Dict[str, Dict]:
    """Get all agent statuses from MongoDB."""
    agents = {}
    for doc in db.agent_status.find():
        agents[doc["instance"]] = {
            "instance": doc["instance"],
            "session_id": doc.get("session_id"),
            "tmux_target": doc.get("tmux_target"),
            "status": doc.get("status", "unknown"),
            "last_heartbeat": doc.get("last_heartbeat")
        }
    return agents


def send_to_tmux(target: str, message: str) -> bool:
    """Send message to tmux target."""
    try:
        # Escape special characters for tmux
        escaped = message.replace("'", "'\\''")
        subprocess.run(
            ["tmux", "send-keys", "-t", target, f"# MESSAGE: {escaped}", "Enter"],
            check=True,
            capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"Failed to send to {target}: {e}")
        return False
    except FileNotFoundError:
        log("tmux not found")
        return False


def check_tmux_target_exists(target: str) -> bool:
    """Check if tmux target exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", target.split(":")[0]],
            capture_output=True
        )
        return result.returncode == 0
    except:
        return False


def deliver_message(db, mcp_url: str, msg: Dict) -> bool:
    """Deliver a single message to its target."""
    to_instance = msg.get("to_instance")
    message_id = msg.get("_id")

    # Get target's tmux info
    agent = db.agent_status.find_one({"instance": to_instance})

    if not agent:
        # Check for broadcast
        if to_instance == "*":
            # Deliver to all active agents
            delivered_any = False
            for agent in db.agent_status.find({"status": {"$ne": "offline"}}):
                if deliver_to_agent(db, mcp_url, msg, agent):
                    delivered_any = True
            return delivered_any
        else:
            log(f"Target agent '{to_instance}' not found for message {message_id}")
            return False

    return deliver_to_agent(db, mcp_url, msg, agent)


def deliver_to_agent(db, mcp_url: str, msg: Dict, agent: Dict) -> bool:
    """Deliver message to a specific agent."""
    message_id = msg.get("_id")
    tmux_target = agent.get("tmux_target")
    instance = agent.get("instance")

    if not tmux_target:
        log(f"No tmux target for {instance}")
        return False

    if not check_tmux_target_exists(tmux_target):
        log(f"tmux target {tmux_target} not available for {instance}")
        return False

    # Format and deliver message
    from_instance = msg.get("from_instance", "unknown")
    priority = msg.get("priority", "normal")
    message_text = msg.get("message", "")

    if priority == "urgent":
        formatted = f"[URGENT from {from_instance}] {message_text}"
    else:
        formatted = f"[From {from_instance}] {message_text}"

    if send_to_tmux(tmux_target, formatted):
        log(f"Delivered to {instance}: {message_text[:50]}...")

        # Update message status to delivered
        db.messages.update_one(
            {"_id": message_id},
            {"$set": {
                "status": "delivered",
                "delivered_at": datetime.now()
            }}
        )
        return True

    return False


def run_with_change_streams(db, mcp_url: str):
    """Run dispatcher using MongoDB change streams (event-driven)."""
    global RUNNING

    log("Starting in STREAM mode (event-driven)")

    # Watch for new messages with status=pending
    pipeline = [
        {"$match": {
            "operationType": "insert",
            "fullDocument.status": "pending"
        }}
    ]

    try:
        with db.messages.watch(pipeline, full_document="updateLookup") as stream:
            # Also process any existing pending messages first
            process_pending_messages(db, mcp_url)

            log("Watching for new messages...")

            while RUNNING:
                # Use next with timeout to allow checking RUNNING flag
                try:
                    if stream.try_next() is not None:
                        change = stream.try_next()
                        if change:
                            msg = change.get("fullDocument")
                            if msg:
                                log(f"New message detected: {msg.get('_id')}")
                                deliver_message(db, mcp_url, msg)
                    else:
                        # No change, check for pending messages periodically
                        time.sleep(0.5)

                except StopIteration:
                    time.sleep(0.1)

    except Exception as e:
        log(f"Change stream error: {e}")
        log("Falling back to polling mode...")
        run_with_polling(db, mcp_url, DEFAULT_POLL_INTERVAL)


def run_with_polling(db, mcp_url: str, poll_interval: int):
    """Run dispatcher using polling (fallback mode)."""
    global RUNNING

    log(f"Starting in POLL mode (interval: {poll_interval}s)")

    while RUNNING:
        try:
            process_pending_messages(db, mcp_url)
        except Exception as e:
            log(f"Error in poll loop: {e}")

        time.sleep(poll_interval)


def process_pending_messages(db, mcp_url: str):
    """Process all pending messages."""
    pending = db.messages.find({"status": "pending"}).sort("created_at", 1)

    for msg in pending:
        deliver_message(db, mcp_url, msg)


def run_dispatcher(mongo_url: str, mongo_db: str, mcp_url: str, mode: str, poll_interval: int):
    """Main dispatcher entry point."""
    global DISPATCHER_SESSION_ID, RUNNING

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log(f"Connecting to MongoDB at {mongo_url}...")

    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        db = client[mongo_db]
        log(f"Connected to MongoDB database: {mongo_db}")
    except ConnectionFailure as e:
        log(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)

    # Start MCP session
    log(f"Starting MCP session...")
    DISPATCHER_SESSION_ID = start_dispatcher_session(mcp_url)
    if not DISPATCHER_SESSION_ID:
        log("Failed to start dispatcher session. Is MCP server running?")
        sys.exit(1)

    log(f"Dispatcher session: {DISPATCHER_SESSION_ID}")

    try:
        # Determine mode
        if mode == "auto":
            # Try change streams, fall back to polling
            try:
                # Test if change streams work (requires replica set)
                with db.messages.watch(max_await_time_ms=100) as stream:
                    stream.try_next()
                mode = "stream"
            except Exception as e:
                log(f"Change streams not available ({e}), using polling")
                mode = "poll"

        if mode == "stream":
            run_with_change_streams(db, mcp_url)
        else:
            run_with_polling(db, mcp_url, poll_interval)

    except Exception as e:
        log(f"Dispatcher error: {e}")
    finally:
        log("Shutting down dispatcher...")
        mcp_call(mcp_url, "memory_end_session", {
            "session_id": DISPATCHER_SESSION_ID,
            "summary": "Dispatcher shutdown"
        })
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Event-driven message dispatcher for Claude orchestration"
    )
    parser.add_argument(
        "--mongo-url",
        default=DEFAULT_MONGO_URL,
        help=f"MongoDB URL (default: {DEFAULT_MONGO_URL})"
    )
    parser.add_argument(
        "--mongo-db",
        default=DEFAULT_MONGO_DB,
        help=f"MongoDB database (default: {DEFAULT_MONGO_DB})"
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help=f"MCP server URL (default: {DEFAULT_MCP_URL})"
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "stream", "poll"],
        default="auto",
        help="Dispatcher mode: auto (try streams, fall back to poll), stream (force change streams), poll (force polling)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Polling interval in seconds for poll mode (default: {DEFAULT_POLL_INTERVAL})"
    )

    args = parser.parse_args()

    run_dispatcher(
        args.mongo_url,
        args.mongo_db,
        args.mcp_url,
        args.mode,
        args.poll_interval
    )


if __name__ == "__main__":
    main()
