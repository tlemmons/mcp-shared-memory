"""Guidelines tools - server-managed behavioral rules for agents."""

import json
from datetime import datetime

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo


def get_guidelines_for_session(project: str = None) -> list:
    """Fetch all applicable guidelines for a session.

    Returns global guidelines + project-specific guidelines, sorted by priority.
    Called internally by memory_start_session.
    """
    db = get_mongo()
    if db is None:
        return []

    guidelines = []

    # Global guidelines (scope="global")
    for doc in db.guidelines.find({"scope": "global", "active": True}).sort("priority", 1):
        guidelines.append({
            "name": doc["name"],
            "scope": "global",
            "rule": doc["rule"],
            "priority": doc.get("priority", 50),
        })

    # Project-specific guidelines
    if project:
        normalized = project.lower().replace("-", "_").replace(" ", "_")
        for doc in db.guidelines.find({"scope": normalized, "active": True}).sort("priority", 1):
            guidelines.append({
                "name": doc["name"],
                "scope": f"project:{project}",
                "rule": doc["rule"],
                "priority": doc.get("priority", 50),
            })

    # Sort by priority (lower = more important)
    guidelines.sort(key=lambda g: g["priority"])

    return guidelines


@mcp.tool()
async def memory_guidelines(
    action: str,
    name: str = None,
    rule: str = None,
    scope: str = "global",
    priority: int = 50,
    ctx: Context = None
) -> str:
    """
    Manage server-side behavioral guidelines that ALL agents receive at session start.

    These replace CLAUDE.md rules — update once here, every agent on every machine
    picks them up immediately on their next session.

    Actions:
        list   - Show all guidelines (optionally filtered by scope)
        set    - Create or update a guideline (requires name + rule)
        delete - Remove a guideline by name
        get    - Get a single guideline by name

    Args:
        action: One of: list, set, delete, get
        name: Guideline name (e.g., "freshness_check", "function_registry")
        rule: The behavioral rule text. Be explicit — agents will follow this literally.
        scope: "global" (all projects) or a project name for project-specific rules
        priority: 1-100, lower = shown first. Default 50.
    """
    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB not available"})

    normalized_scope = scope.lower().replace("-", "_").replace(" ", "_")

    if action == "list":
        query = {}
        if scope != "global":
            query["scope"] = normalized_scope
        docs = list(db.guidelines.find(query).sort("priority", 1))
        guidelines = []
        for doc in docs:
            guidelines.append({
                "name": doc["name"],
                "scope": doc["scope"],
                "rule": doc["rule"],
                "priority": doc.get("priority", 50),
                "active": doc.get("active", True),
                "updated": doc.get("updated", ""),
                "updated_by": doc.get("updated_by", ""),
            })
        return json.dumps({"guidelines": guidelines, "count": len(guidelines)}, indent=2)

    elif action == "set":
        if not name or not rule:
            return json.dumps({"error": "Both 'name' and 'rule' are required for set action"})

        now = datetime.now().isoformat()
        existing = db.guidelines.find_one({"name": name})

        db.guidelines.update_one(
            {"name": name},
            {"$set": {
                "name": name,
                "rule": rule,
                "scope": normalized_scope,
                "priority": max(1, min(100, priority)),
                "active": True,
                "updated": now,
            }},
            upsert=True
        )

        action_taken = "updated" if existing else "created"
        return json.dumps({"status": action_taken, "name": name, "scope": normalized_scope, "priority": priority})

    elif action == "delete":
        if not name:
            return json.dumps({"error": "'name' is required for delete action"})
        result = db.guidelines.delete_one({"name": name})
        if result.deleted_count:
            return json.dumps({"status": "deleted", "name": name})
        return json.dumps({"error": f"Guideline '{name}' not found"})

    elif action == "get":
        if not name:
            return json.dumps({"error": "'name' is required for get action"})
        doc = db.guidelines.find_one({"name": name})
        if not doc:
            return json.dumps({"error": f"Guideline '{name}' not found"})
        return json.dumps({
            "name": doc["name"],
            "scope": doc["scope"],
            "rule": doc["rule"],
            "priority": doc.get("priority", 50),
            "active": doc.get("active", True),
            "updated": doc.get("updated", ""),
        }, indent=2)

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use: list, set, delete, get"})
