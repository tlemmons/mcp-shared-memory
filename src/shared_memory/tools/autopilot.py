"""Autopilot tools (Phase C1) — per-(project, agent) auto-relay configuration.

Phase C1 ships the CRUD surface only: set/pause/status/digest. The actual
budget enforcement (rolling 1-hour count + auto-disable on breach) ships in
Phase C2. Until then `enabled=True` is advisory — it tells channel plugins
they can auto-process inbound messages, but the server does not yet count
or reject auto-traffic.

Configuration is a single document per (project, agent) in `agent_autopilot`:
    {
        project: str,
        agent: str,
        enabled: bool,
        depth_cap: int,        # max chain_depth this agent will auto-process
        hourly_budget: int,    # max auto-processed messages per hour
        destructive_gate: bool # when True, require_human messages go through
                               # to the human regardless of enabled
        updated: datetime,
        updated_by: str,
    }
"""

import json
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import require_session, utc_now
from shared_memory.state import active_sessions


def _autopilot_config(db, project: str, agent: str) -> dict:
    """Return the autopilot config doc, falling back to defaults."""
    doc = db.agent_autopilot.find_one({"project": project, "agent": agent}) or {}
    return {
        "project": project,
        "agent": agent,
        "enabled": bool(doc.get("enabled", False)),
        "depth_cap": int(doc.get("depth_cap", 1)),
        "hourly_budget": int(doc.get("hourly_budget", 10)),
        "destructive_gate": bool(doc.get("destructive_gate", True)),
        "updated": doc.get("updated"),
        "updated_by": doc.get("updated_by", ""),
        "paused_at": doc.get("paused_at"),
        "paused_reason": doc.get("paused_reason", ""),
    }


def _format_dt(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@mcp.tool()
async def memory_set_autopilot(
    session_id: str,
    project: str,
    agent: str,
    enabled: bool = None,
    depth_cap: int = None,
    hourly_budget: int = None,
    destructive_gate: bool = None,
    ctx: Context = None,
) -> str:
    """
    Configure autopilot for a (project, agent) pair.

    Autopilot lets channel plugins (e.g., ClaudeTerminal cterm-inbox) auto-process
    inbound messages without human prompting. The server enforces:
      - depth_cap: messages with chain_depth > depth_cap require human review
      - hourly_budget: rolling 1-hour count of auto-processed messages
        (enforced in Phase C2; advisory in C1)
      - destructive_gate: when True, messages flagged require_human always go
        to the human regardless of `enabled`

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent within that project
        enabled: Turn autopilot on or off
        depth_cap: Max chain_depth to auto-process (default 1 — only direct
            human-originated messages count, replies go to human)
        hourly_budget: Max auto-processed messages per hour (default 10)
        destructive_gate: Force human review on require_human messages (default True;
            recommended to leave True)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    session_info = active_sessions[session_id]
    actor = session_info.get("claude_instance", "unknown")

    update: dict = {"updated": utc_now(), "updated_by": actor}
    if enabled is not None:
        update["enabled"] = bool(enabled)
    if depth_cap is not None:
        update["depth_cap"] = max(0, int(depth_cap))
    if hourly_budget is not None:
        update["hourly_budget"] = max(0, int(hourly_budget))
    if destructive_gate is not None:
        update["destructive_gate"] = bool(destructive_gate)

    # Re-enabling? Clear the paused_at/paused_reason fields so a future
    # auto-disable-on-budget-breach is recorded fresh.
    if update.get("enabled") is True:
        update["paused_at"] = None
        update["paused_reason"] = ""

    db.agent_autopilot.update_one(
        {"project": project, "agent": agent},
        {
            "$set": update,
            "$setOnInsert": {"project": project, "agent": agent},
        },
        upsert=True,
    )

    return json.dumps(
        {
            "status": "ok",
            **{k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
        },
        indent=2,
    )


@mcp.tool()
async def memory_pause_autopilot(
    session_id: str,
    project: str,
    agent: str,
    reason: str = "",
    ctx: Context = None,
) -> str:
    """
    Pause autopilot for a (project, agent) pair without losing config.

    Equivalent to memory_set_autopilot(enabled=False) plus a paused_reason
    note that persists across re-enables. Use when you want to temporarily
    halt auto-processing for an agent (debugging, runaway loop, deploy
    window) without forgetting the depth_cap / hourly_budget tuning.

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent
        reason: Why autopilot was paused (shown in status output)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    session_info = active_sessions[session_id]
    actor = session_info.get("claude_instance", "unknown")

    db.agent_autopilot.update_one(
        {"project": project, "agent": agent},
        {
            "$set": {
                "enabled": False,
                "paused_at": utc_now(),
                "paused_reason": reason,
                "updated": utc_now(),
                "updated_by": actor,
            },
            "$setOnInsert": {
                "project": project,
                "agent": agent,
                "depth_cap": 1,
                "hourly_budget": 10,
                "destructive_gate": True,
            },
        },
        upsert=True,
    )

    return json.dumps(
        {
            "status": "paused",
            **{k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
        },
        indent=2,
    )


@mcp.tool()
async def memory_autopilot_status(
    session_id: str,
    project: str = None,
    agent: str = None,
    ctx: Context = None,
) -> str:
    """
    Inspect autopilot configuration.

    Without filters, returns every configured (project, agent) pair. With
    project only, returns all agents in that project. With both, returns
    just that one config (or defaults if no config exists).

    Args:
        session_id: Your session ID
        project: Optional project filter
        agent: Optional agent filter (requires project)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    if project and agent:
        return json.dumps(
            {k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
            indent=2,
        )

    query: dict = {}
    if project:
        query["project"] = project

    configs = []
    for doc in db.agent_autopilot.find(query).sort([("project", 1), ("agent", 1)]):
        configs.append(
            {
                "project": doc.get("project"),
                "agent": doc.get("agent"),
                "enabled": bool(doc.get("enabled", False)),
                "depth_cap": int(doc.get("depth_cap", 1)),
                "hourly_budget": int(doc.get("hourly_budget", 10)),
                "destructive_gate": bool(doc.get("destructive_gate", True)),
                "updated": _format_dt(doc.get("updated")),
                "updated_by": doc.get("updated_by", ""),
                "paused_at": _format_dt(doc.get("paused_at")),
                "paused_reason": doc.get("paused_reason", ""),
            }
        )

    return json.dumps({"count": len(configs), "configs": configs}, indent=2)


@mcp.tool()
async def memory_autopilot_digest(
    session_id: str,
    project: str,
    agent: str,
    hours: int = 24,
    ctx: Context = None,
) -> str:
    """
    Summarize autopilot activity for a (project, agent) over a recent window.

    Counts messages received and broken down by chain_depth, require_human,
    and user_originated. Useful for tuning depth_cap and hourly_budget.

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent
        hours: Lookback window in hours (default 24)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))

    base_query = {
        "to_project": project,
        "to_instance": agent,
        "created_at": {"$gte": cutoff},
    }

    total = db.messages.count_documents(base_query)
    require_human = db.messages.count_documents({**base_query, "require_human": True})
    user_originated = db.messages.count_documents({**base_query, "user_originated": True})

    by_depth = {}
    for doc in db.messages.find(base_query, {"chain_depth": 1}):
        depth = doc.get("chain_depth", 0) or 0
        by_depth[str(depth)] = by_depth.get(str(depth), 0) + 1

    config = _autopilot_config(db, project, agent)

    return json.dumps(
        {
            "project": project,
            "agent": agent,
            "window_hours": hours,
            "since": cutoff.isoformat(),
            "totals": {
                "all_messages": total,
                "require_human": require_human,
                "user_originated": user_originated,
            },
            "by_chain_depth": by_depth,
            "current_config": {k: _format_dt(v) for k, v in config.items()},
        },
        indent=2,
    )
