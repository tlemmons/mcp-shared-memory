"""Inter-agent messaging tools - send/receive messages, agent status, discovery."""

import json
import uuid
from datetime import timedelta
from typing import Dict, List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.config import MESSAGE_CATEGORIES, MESSAGE_PRIORITIES, MESSAGE_STATUSES
from shared_memory.helpers import require_session, utc_now
from shared_memory.state import active_sessions
from shared_memory.tools.projects import _fuzzy_match_agent, _is_project_admin


def get_tmux_target_for_instance(instance_name: str) -> str:
    """Find tmux target for a Claude instance from active sessions."""
    for session_id, info in active_sessions.items():
        if info["claude_instance"] == instance_name and info.get("tmux_target"):
            return info["tmux_target"]
    return None


def get_pending_messages_for_instance(instance_name: str, project: str = None) -> List[Dict]:
    """Get undelivered messages for a specific instance (MongoDB-backed)."""
    db = get_mongo()
    if db is None:
        return []

    instance_match = {
        "$or": [
            {"to_instance": instance_name},
            {"to_instance": "*"}
        ]
    }

    query = {"$and": [instance_match, {"status": "pending"}]}

    # Add project scoping if provided
    if project:
        query["$and"].append({
            "$or": [
                {"to_project": project},
                {"to_project": {"$exists": False}},
                {"to_project": ""},
            ]
        })

    messages = []
    for doc in db.messages.find(query):
        entry = {
            "id": doc["_id"],
            "to": doc["to_instance"],
            "to_project": doc.get("to_project", ""),
            "from_instance": doc["from_instance"],
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "message": doc["message"],
            "priority": doc["priority"],
            "created": doc["created_at"].isoformat() if doc["created_at"] else None
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        messages.append(entry)

    return messages


@mcp.tool()
async def memory_send_message(
    session_id: str,
    to_instance: str,
    message: str,
    priority: str = "normal",
    category: str = "info",
    to_project: str = None,
    reply_to: str = None,
    ctx: Context = None
) -> str:
    """
    Send a message to another Claude instance.

    Messages are persisted to MongoDB and delivered via tmux injection.
    Supports full lifecycle tracking: pending → delivered → received → completed/failed.

    Args:
        session_id: Your session ID
        to_instance: Target Claude instance name (e.g., 'frontend', 'backend', or '*' for all)
        message: The message to send
        priority: Message priority (urgent, normal, low) - urgent interrupts, others wait
        category: Message category - determines how receiver should handle it:
            contract - exact format/spec that must be followed, no deviation
            task - work assignment
            question - needs a response
            info - FYI, no action needed (default)
            review - look at this and confirm or flag issues
            blocker - STOP what you are doing until you discuss with coordinator or user
        to_project: Target project (defaults to your project; use for cross-project messages)
        reply_to: Message ID this is replying to (for threading conversations)
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in MESSAGE_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {MESSAGE_PRIORITIES}"})

    if category not in MESSAGE_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Must be one of: {MESSAGE_CATEGORIES}"})

    session_info = active_sessions[session_id]
    from_project = session_info.get("project", "")
    target_project = to_project or from_project
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    # ── Validate target against project registry ──
    # Normalize project name
    normalized_project = target_project.lower().replace("-", "_").replace(" ", "_")
    registered_project = db.projects.find_one({"name": normalized_project})

    if registered_project:
        # Project is registered - validate the target agent
        if to_instance != "*":
            registered_agent = db.registered_agents.find_one({
                "project": normalized_project,
                "name": to_instance
            })
            if not registered_agent:
                # Agent not registered - hard reject with suggestions
                suggestions = _fuzzy_match_agent(db, normalized_project, to_instance)
                error_msg = f"Agent '{to_instance}' is not registered in project '{normalized_project}'."
                if suggestions:
                    error_msg += f" Did you mean: {', '.join(suggestions)}?"
                else:
                    # List all valid agents
                    all_agents = [a["name"] for a in db.registered_agents.find(
                        {"project": normalized_project}, {"name": 1}
                    )]
                    if all_agents:
                        error_msg += f" Valid agents: {', '.join(all_agents)}"
                return json.dumps({"error": error_msg})
    # If project not registered, allow message through (backward compatibility)
    # This lets messaging work before projects are fully set up

    # ── Dedup check ──
    # Reject identical messages to same target within 5 minutes
    dedup_window = now - timedelta(minutes=5)
    existing_msg = db.messages.find_one({
        "to_instance": to_instance,
        "to_project": normalized_project if registered_project else target_project,
        "from_instance": session_info["claude_instance"],
        "message": message,
        "created_at": {"$gte": dedup_window}
    })
    if existing_msg:
        return json.dumps({
            "error": "Duplicate message detected. An identical message was sent to this target within the last 5 minutes.",
            "existing_message_id": existing_msg["_id"]
        })

    # ── Build and store message ──
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    msg_doc = {
        "_id": message_id,
        "to_instance": to_instance,
        "to_project": normalized_project if registered_project else target_project,
        "from_instance": session_info["claude_instance"],
        "from_project": from_project,
        "from_session": session_id,
        "message": message,
        "priority": priority,
        "category": category,
        "reply_to": reply_to,
        "status": "pending",
        "created_at": now,
        "delivered_at": None,
        "received_at": None,
        "completed_at": None
    }

    db.messages.insert_one(msg_doc)

    return json.dumps({
        "status": "queued",
        "message_id": message_id,
        "to": to_instance,
        "to_project": msg_doc["to_project"],
        "from_project": from_project,
        "priority": priority,
        "category": category,
        "reply_to": reply_to,
        "persisted": True
    })


@mcp.tool()
async def memory_get_messages(
    session_id: str,
    include_delivered: bool = False,
    limit: int = 20,
    message_id: str = None,
    for_instance: str = None,
    ctx: Context = None
) -> str:
    """
    Get pending messages for your Claude instance.

    Returns messages sent to you by other Claudes or the orchestrator.
    Messages are scoped by project - you only see messages sent to your project.

    Args:
        session_id: Your session ID
        include_delivered: Include already delivered messages (default False)
        limit: Maximum messages to return (default 20)
        message_id: Fetch a specific message by ID (admin/coordinator only)
        for_instance: View messages for a different agent in your project (admin/coordinator only)
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    my_instance = session_info["claude_instance"]
    my_project = session_info.get("project", "")

    db = get_mongo()
    if db is None:
        return json.dumps({"count": 0, "messages": [], "error": "MongoDB unavailable"})

    # ── Direct message lookup by ID (admin/coordinator only) ──
    if message_id:
        is_admin = _is_project_admin(db, my_project, my_instance)
        doc = db.messages.find_one({"_id": message_id})
        if not doc:
            return json.dumps({"error": f"Message '{message_id}' not found"})
        # Non-admins can only see their own messages
        if not is_admin:
            msg_to = doc.get("to_instance", doc.get("to", ""))
            msg_project = doc.get("to_project", "")
            if msg_to != my_instance and msg_to != "*":
                return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
            if msg_project and msg_project != my_project:
                return json.dumps({"error": "Permission denied. Message belongs to a different project."})
        entry = {
            "id": doc["_id"],
            "from": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "to": doc.get("to_instance", doc.get("to", "?")),
            "to_project": doc.get("to_project", ""),
            "category": doc.get("category", "info"),
            "message": doc["message"],
            "priority": doc.get("priority", "normal"),
            "status": doc.get("status", "?"),
            "created": doc["created_at"].isoformat() if doc.get("created_at") else (doc["created"].isoformat() if doc.get("created") else None),
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        return json.dumps({"count": 1, "messages": [entry]})

    # ── Admin/coordinator querying for another agent's messages ──
    target_instance = my_instance
    if for_instance:
        is_admin = _is_project_admin(db, my_project, my_instance)
        if not is_admin:
            return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
        target_instance = for_instance

    # Build query - match by instance AND project
    # Broadcasts (*) only go to named/admin agents, not workers
    is_worker = target_instance.startswith("worker_")
    if is_worker:
        instance_match = {"to_instance": target_instance}  # Workers only get direct messages
    else:
        instance_match = {
            "$or": [
                {"to_instance": target_instance},
                {"to_instance": "*"}
            ]
        }
    project_match = {
        "$or": [
            {"to_project": my_project},
            {"to_project": {"$exists": False}},  # legacy messages without project
            {"to_project": ""},  # empty project = broadcast
        ]
    }
    query = {"$and": [instance_match, project_match]}

    if not include_delivered:
        query["$and"].append({"status": "pending"})

    # Fetch and format messages (limit to prevent context flooding)
    cursor = db.messages.find(query).sort([
        ("priority", 1),
        ("created_at", -1)
    ]).limit(limit)

    priority_sort = {"urgent": 0, "normal": 1, "low": 2}
    messages = []
    for doc in cursor:
        entry = {
            "id": doc["_id"],
            "from": doc["from_instance"],
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "message": doc["message"],
            "priority": doc["priority"],
            "status": doc["status"],
            "created": doc["created_at"].isoformat() if doc["created_at"] else None,
            "delivered": doc["status"] != "pending"
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        messages.append(entry)

    # Sort by priority then created
    messages.sort(key=lambda x: (priority_sort.get(x["priority"], 99), x["created"] or ""))

    return json.dumps({
        "count": len(messages),
        "messages": messages
    })


@mcp.tool()
async def memory_update_message_status(
    session_id: str,
    message_id: str,
    status: str,
    ctx: Context = None
) -> str:
    """
    Update a message's lifecycle status.

    Call this to track message progress through the system.

    Args:
        session_id: Your session ID
        message_id: The message ID to update
        status: New status (delivered, received, completed, failed)
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in MESSAGE_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {MESSAGE_STATUSES}"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    now = utc_now()
    update = {"status": status}

    # Set appropriate timestamp
    if status == "delivered":
        update["delivered_at"] = now
    elif status == "received":
        update["received_at"] = now
    elif status in ["completed", "failed"]:
        update["completed_at"] = now

    result = db.messages.update_one(
        {"_id": message_id},
        {"$set": update}
    )

    if result.matched_count == 0:
        return json.dumps({"error": f"Message not found: {message_id}"})

    return json.dumps({
        "status": status,
        "message_id": message_id,
        "updated": True
    })


@mcp.tool()
async def memory_acknowledge_message(
    session_id: str,
    message_id: str,
    ctx: Context = None
) -> str:
    """
    Acknowledge receipt of a message (shortcut for status=received).

    Call this after processing a message to mark it handled.

    Args:
        session_id: Your session ID
        message_id: The message ID to acknowledge
    """
    return await memory_update_message_status(session_id, message_id, "received", ctx)


@mcp.tool()
async def memory_heartbeat(
    session_id: str,
    status: str = "idle",
    current_task: str = None,
    ctx: Context = None
) -> str:
    """
    Send a heartbeat to update your agent status.

    Call this periodically to let the system know you're alive.
    Enables load balancing, stuck detection, and routing decisions.

    Args:
        session_id: Your session ID
        status: Current status (idle, busy, error)
        current_task: Description of current task (if busy)
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in ["idle", "busy", "error"]:
        return json.dumps({"error": "Status must be: idle, busy, error"})

    session_info = active_sessions[session_id]
    instance = session_info["claude_instance"]
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    db.agent_status.update_one(
        {"instance": instance},
        {
            "$set": {
                "instance": instance,
                "session_id": session_id,
                "status": status,
                "current_task": current_task,
                "last_heartbeat": now,
                "tmux_target": session_info.get("tmux_target")
            }
        },
        upsert=True
    )

    return json.dumps({
        "status": "ok",
        "instance": instance,
        "agent_status": status,
        "timestamp": now.isoformat()
    })


@mcp.tool()
async def memory_get_agent_status(
    session_id: str,
    instance: str = None,
    ctx: Context = None
) -> str:
    """
    Get status of Claude agents.

    Args:
        session_id: Your session ID
        instance: Specific instance to check (omit for all agents)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    query = {"instance": instance} if instance else {}
    cursor = db.agent_status.find(query)

    agents = []
    now = utc_now()
    for doc in cursor:
        last_hb = doc.get("last_heartbeat")
        stale = False
        if last_hb:
            age_seconds = (now - last_hb).total_seconds()
            stale = age_seconds > 300  # 5 minutes = stale

        agents.append({
            "instance": doc["instance"],
            "status": doc.get("status", "unknown"),
            "current_task": doc.get("current_task"),
            "tmux_target": doc.get("tmux_target"),
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
            "stale": stale
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    })


@mcp.tool()
async def memory_list_agents(
    session_id: str,
    project: str = None,
    query: str = None,
    ctx: Context = None
) -> str:
    """
    Discover registered Claude agents across all projects.

    Use this to find out who exists, what they do, and how to reach them.
    Agents auto-register when they call memory_start_session.

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
        query: Search term to filter by name or role description
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    mongo_query = {}
    if project:
        mongo_query["project"] = project

    cursor = db.agent_directory.find(mongo_query).sort("last_seen", -1)

    agents = []
    now = utc_now()
    for doc in cursor:
        # If query provided, filter by instance name or role_description
        if query:
            q_lower = query.lower()
            instance_match = q_lower in doc.get("instance", "").lower()
            role_match = q_lower in doc.get("role_description", "").lower()
            project_match = q_lower in doc.get("project", "").lower()
            if not (instance_match or role_match or project_match):
                continue

        last_seen = doc.get("last_seen")
        days_ago = None
        if last_seen:
            days_ago = round((now - last_seen).total_seconds() / 86400, 1)

        agents.append({
            "project": doc.get("project"),
            "instance": doc.get("instance"),
            "role_description": doc.get("role_description", ""),
            "last_seen": last_seen.isoformat() if last_seen else None,
            "days_ago": days_ago,
            "session_count": doc.get("session_count", 0),
            "last_task": doc.get("last_task", ""),
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    }, indent=2)
