# Shared Memory MCP Server - Full Installation Guide

Complete installation instructions for deploying the Multi-Claude Shared Memory MCP Server on a fresh Linux server (AWS EC2, Ubuntu/Debian).

## Prerequisites

- Linux server (Ubuntu 22.04+ or Debian 12+ recommended)
- Root or sudo access
- Ports 8080 (MCP) and 8001 (Chroma) accessible
- At least 2GB RAM, 10GB disk

## Quick Install (All-in-One Script)

```bash
# Download and run the install script
curl -fsSL https://raw.githubusercontent.com/YOUR_REPO/mcp-memory/main/install.sh | sudo bash
```

Or follow the manual steps below.

---

## Manual Installation

### Step 1: System Updates & Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to docker group (logout/login required)
sudo usermod -aG docker $USER

# Verify Docker
docker --version
docker compose version
```

### Step 2: Create Directory Structure

```bash
# Create installation directory
sudo mkdir -p /opt/mcp-memory
sudo chown $USER:$USER /opt/mcp-memory
cd /opt/mcp-memory

# Create subdirectories
mkdir -p chroma-data
```

### Step 3: Install Chroma Vector Database

Create `/opt/mcp-memory/docker-compose.chroma.yml`:

```yaml
services:
  chroma:
    image: chromadb/chroma:latest
    container_name: chroma-db
    volumes:
      - ./chroma-data:/chroma/chroma
    environment:
      - ANONYMIZED_TELEMETRY=false
      - ALLOW_RESET=false
      - IS_PERSISTENT=true
    ports:
      - "8001:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v2/heartbeat"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

Start Chroma:

```bash
cd /opt/mcp-memory
docker compose -f docker-compose.chroma.yml up -d

# Verify Chroma is running
curl http://localhost:8001/api/v2/heartbeat
# Should return: {"nanosecond heartbeat": ...}
```

### Step 4: Install MCP Memory Server

Create `/opt/mcp-memory/server.py`:

```python
"""
Shared Memory MCP Server for Multi-Claude Coordination

A centralized knowledge base and coordination system for multiple Claude instances
working across projects. Enforces workflow compliance and tracks active work.

Features:
- Session management (start/end) with compliance enforcement
- Multiple memory types: architecture, learnings, code snippets, task context, work items
- Document lifecycle: active → deprecated → superseded → archived
- Overlap detection: warns when touching areas another Claude recently modified
- Cross-project search for shared patterns and learnings
- Project isolation for project-specific details

Connects to Chroma at localhost:8001 (via host network in Docker).
"""

import json
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context
import chromadb
from chromadb.config import Settings


# =============================================================================
# Configuration
# =============================================================================

import os

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))

# Collection naming
PROJECT_PREFIX = "proj_"      # proj_emailtriage, proj_nimbus
SHARED_PREFIX = "shared_"     # shared_patterns, shared_context

# Overlap detection window
OVERLAP_WINDOW_HOURS = 24

# Active sessions stored in memory (lightweight, no persistence needed)
active_sessions: Dict[str, Dict[str, Any]] = {}

# Valid memory types
MEMORY_TYPES = [
    "api_spec", "architecture", "component", "config",  # Architecture
    "adr",  # Decisions
    "learning", "pattern", "gotcha",  # Learnings
    "code_snippet", "solution",  # Code
    "task_context", "handoff", "work_item"  # Task/Work
]

# Valid doc statuses
DOC_STATUSES = ["active", "deprecated", "superseded", "archived"]

# Valid work statuses
WORK_STATUSES = ["in_progress", "blocked", "completed", "abandoned"]


# =============================================================================
# Chroma Client Setup
# =============================================================================

@asynccontextmanager
async def app_lifespan(app):
    """Initialize Chroma client once, share across all tool calls."""
    client = chromadb.HttpClient(
        host=CHROMA_HOST,
        port=CHROMA_PORT,
        settings=Settings(anonymized_telemetry=False)
    )
    print(f"Connected to Chroma at {CHROMA_HOST}:{CHROMA_PORT}")

    # Ensure shared collections exist
    for name in ["shared_patterns", "shared_context", "shared_work"]:
        client.get_or_create_collection(
            name=name,
            metadata={"type": "shared", "created": datetime.now().isoformat()}
        )

    yield {"chroma": client}


# Allow connections from any host (for remote access via IP or proxy)
mcp = FastMCP("shared_memory", lifespan=app_lifespan, host="0.0.0.0")


# =============================================================================
# Helper Functions
# =============================================================================

def get_chroma_client() -> chromadb.Client:
    """Get a Chroma client instance."""
    return chromadb.HttpClient(
        host=CHROMA_HOST,
        port=CHROMA_PORT,
        settings=Settings(anonymized_telemetry=False)
    )


def get_project_collection(client: chromadb.Client, project: str) -> chromadb.Collection:
    """Get or create a project-specific collection."""
    name = f"{PROJECT_PREFIX}{project.lower().replace('-', '_')}"
    return client.get_or_create_collection(
        name=name,
        metadata={"project": project, "created": datetime.now().isoformat()}
    )


def get_shared_collection(client: chromadb.Client, collection_type: str) -> chromadb.Collection:
    """Get a shared collection (patterns, context, work)."""
    return client.get_or_create_collection(name=f"{SHARED_PREFIX}{collection_type}")


def generate_doc_id(content: str, doc_type: str) -> str:
    """Generate a stable document ID from content hash."""
    hash_input = f"{doc_type}:{content[:500]}:{datetime.now().isoformat()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def check_session(session_id: str) -> bool:
    """Check if a session is registered."""
    return session_id in active_sessions


def require_session(session_id: Optional[str]) -> str:
    """Validate session exists, return error message if not."""
    if not session_id:
        return "ERROR: No session_id provided. You must call memory_start_session first and include the returned session_id in all subsequent calls."
    if not check_session(session_id):
        return f"ERROR: Session '{session_id}' not found. Call memory_start_session first to register your session."
    return ""


def format_status_warning(status: str, superseded_by: str = None) -> str:
    """Generate warning for non-active documents."""
    if status == "active":
        return ""
    if status == "deprecated":
        return "\n⚠️ WARNING: This document is DEPRECATED. It may be outdated or no longer recommended.\n"
    if status == "superseded":
        msg = "\n⚠️ WARNING: This document has been SUPERSEDED."
        if superseded_by:
            msg += f" See newer version: {superseded_by}"
        return msg + "\n"
    if status == "archived":
        return "\n📁 NOTE: This document is ARCHIVED (historical reference only).\n"
    return ""


def check_overlap(client: chromadb.Client, project: str, files_touched: List[str], current_session: str) -> List[Dict]:
    """Check if other Claudes recently touched these files."""
    overlaps = []
    work_collection = get_shared_collection(client, "work")

    cutoff = (datetime.now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    for file_path in files_touched:
        try:
            results = work_collection.query(
                query_texts=[file_path],
                n_results=10,
                where={"status": {"$in": ["in_progress", "completed"]}}
            )
        except Exception:
            continue

        if results["documents"] and results["documents"][0]:
            for meta in results["metadatas"][0]:
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                if meta.get("session_id") != current_session:
                    overlaps.append({
                        "file": file_path,
                        "other_session": meta.get("session_id"),
                        "other_claude": meta.get("claude_instance"),
                        "when": meta.get("updated"),
                        "what": meta.get("title")
                    })

    return overlaps


def format_overlap_warning(overlaps: List[Dict]) -> str:
    """Format overlap warnings for Claude."""
    if not overlaps:
        return ""

    warning = "\n⚠️ OVERLAP DETECTED - Other Claude(s) recently touched these areas:\n"
    for o in overlaps:
        warning += f"  • {o['file']}: {o['other_claude']} worked on '{o['what']}' ({o['when']})\n"
    warning += "Consider checking their work or coordinating to avoid conflicts.\n"
    return warning


# =============================================================================
# Session Management Tools
# =============================================================================

@mcp.tool()
async def memory_start_session(
    project: str,
    claude_instance: str = "unknown",
    task_description: str = "",
    ctx: Context = None
) -> str:
    """
    START HERE - Call this first before any other memory tools.

    Registers your session and returns:
    - Your session ID (required for all other calls)
    - Recent relevant learnings for your project
    - Active work by other Claudes (avoid conflicts)
    - Handoff notes from previous sessions

    You MUST call this at the start of your work.

    Args:
        project: Project you're working on (e.g., 'emailtriage', 'nimbus')
        claude_instance: Identifier for this Claude instance (e.g., 'main', 'agent-1')
        task_description: Brief description of what you're about to work on
    """
    chroma = get_chroma_client()

    # Generate session ID
    session_id = f"{project}_{claude_instance}_{uuid.uuid4().hex[:8]}"

    # Register session
    active_sessions[session_id] = {
        "project": project,
        "claude_instance": claude_instance,
        "task": task_description,
        "started": datetime.now().isoformat(),
        "last_activity": datetime.now().isoformat()
    }

    # Gather context for this Claude
    output = {
        "session_id": session_id,
        "message": "Session registered successfully. Include this session_id in all subsequent memory calls.",
        "project": project,
        "started": active_sessions[session_id]["started"]
    }

    # Get recent learnings for this project
    try:
        proj_collection = get_project_collection(chroma, project)
        recent_learnings = proj_collection.query(
            query_texts=[task_description or "recent learnings"],
            n_results=3,
            where={"type": {"$in": ["learning", "gotcha", "handoff"]}}
        )

        if recent_learnings["documents"] and recent_learnings["documents"][0]:
            output["recent_learnings"] = []
            for doc, meta in zip(recent_learnings["documents"][0], recent_learnings["metadatas"][0]):
                output["recent_learnings"].append({
                    "title": meta.get("title"),
                    "type": meta.get("type"),
                    "snippet": doc[:300] + "..." if len(doc) > 300 else doc
                })
    except Exception:
        pass

    # Get shared patterns relevant to task
    try:
        shared = get_shared_collection(chroma, "patterns")
        patterns = shared.query(
            query_texts=[task_description or project],
            n_results=2
        )
        if patterns["documents"] and patterns["documents"][0]:
            output["relevant_patterns"] = []
            for doc, meta in zip(patterns["documents"][0], patterns["metadatas"][0]):
                output["relevant_patterns"].append({
                    "title": meta.get("title"),
                    "snippet": doc[:200] + "..." if len(doc) > 200 else doc
                })
    except Exception:
        pass

    # Get active work by other Claudes
    other_active = []
    for sid, info in active_sessions.items():
        if sid != session_id:
            other_active.append({
                "session": sid,
                "claude": info["claude_instance"],
                "project": info["project"],
                "task": info["task"],
                "since": info["started"]
            })

    if other_active:
        output["other_active_claudes"] = other_active
        output["coordination_note"] = "Other Claudes are currently active. Check their work areas to avoid conflicts."

    # Reminder of workflow
    output["workflow_reminder"] = {
        "during_work": [
            "Use memory_query to check existing knowledge before implementing",
            "Use memory_update_work to log files you're modifying",
            "Use memory_record_learning when you discover something useful"
        ],
        "when_done": [
            "Call memory_end_session with a summary of what you did",
            "Include any handoff notes for the next Claude"
        ]
    }

    return json.dumps(output, indent=2)


@mcp.tool()
async def memory_end_session(
    session_id: str,
    summary: str,
    files_modified: List[str] = None,
    learnings: str = None,
    handoff_notes: str = None,
    ctx: Context = None
) -> str:
    """
    CALL THIS WHEN DONE - Records your work and cleans up session.

    This stores:
    - Summary of what you accomplished (as handoff for next Claude)
    - Files you modified (for overlap detection)
    - Any learnings you want to share

    Args:
        session_id: Your session ID from memory_start_session
        summary: Summary of what you accomplished
        files_modified: List of files you modified
        learnings: Any learnings worth recording for other Claudes
        handoff_notes: Notes for the next Claude who works on this
    """
    error = require_session(session_id)
    if error:
        return error

    files_modified = files_modified or []
    chroma = get_chroma_client()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Store handoff note
    proj_collection = get_project_collection(chroma, session_info["project"])

    handoff_content = f"""## Session Summary
{summary}

## Files Modified
{chr(10).join('- ' + f for f in files_modified) if files_modified else 'None recorded'}

## Handoff Notes
{handoff_notes or 'None'}

## Session Info
- Claude: {session_info['claude_instance']}
- Started: {session_info['started']}
- Ended: {now}
"""

    handoff_id = f"handoff_{session_id}"
    proj_collection.upsert(
        ids=[handoff_id],
        documents=[handoff_content],
        metadatas=[{
            "title": f"Handoff: {session_info['task'][:50]}",
            "type": "handoff",
            "status": "active",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "files_modified": json.dumps(files_modified),
            "created": now,
            "updated": now
        }]
    )

    # Store learning if provided
    if learnings:
        learning_id = generate_doc_id(learnings, "learning")
        proj_collection.add(
            ids=[learning_id],
            documents=[learnings],
            metadatas=[{
                "title": f"Learning from {session_info['claude_instance']}",
                "type": "learning",
                "status": "active",
                "session_id": session_id,
                "created": now,
                "updated": now
            }]
        )

    # Update work item to completed
    work_collection = get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"
    work_collection.upsert(
        ids=[work_id],
        documents=[summary],
        metadatas=[{
            "title": session_info["task"][:100],
            "status": "completed",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_modified),
            "created": session_info["started"],
            "updated": now
        }]
    )

    # Remove from active sessions
    del active_sessions[session_id]

    return json.dumps({
        "status": "session_ended",
        "session_id": session_id,
        "handoff_stored": True,
        "learning_stored": learnings is not None,
        "message": "Session ended. Your work has been recorded for other Claudes."
    }, indent=2)


# =============================================================================
# Query Tools
# =============================================================================

@mcp.tool()
async def memory_query(
    session_id: str,
    query: str,
    project: str = None,
    memory_types: List[str] = None,
    include_inactive: bool = False,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Search the knowledge base for relevant information.

    Use this BEFORE implementing something to check:
    - Has this been done before?
    - Are there known patterns or gotchas?
    - What decisions were made about this area?

    Args:
        session_id: Your session ID
        query: Natural language query
        project: Project to search (omit to search shared memories only)
        memory_types: Filter by types (api_spec, architecture, learning, pattern, etc.)
        include_inactive: Include deprecated/superseded/archived documents
        limit: Maximum number of results (1-20)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = get_chroma_client()
    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()

    results = []

    # Build where filter
    where_filter = {}
    if not include_inactive:
        where_filter["status"] = "active"
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    where_clause = where_filter if where_filter else None

    # Search project collection if specified
    if project:
        try:
            proj_collection = get_project_collection(chroma, project)
            proj_results = proj_collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_clause
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for doc, meta, dist in zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                ):
                    status = meta.get("status", "active")
                    results.append({
                        "source": f"project:{project}",
                        "id": meta.get("id", "unknown"),
                        "title": meta.get("title", "Untitled"),
                        "type": meta.get("type"),
                        "status": status,
                        "relevance": f"{max(0, 1 - dist):.0%}",
                        "content": doc,
                        "warning": format_status_warning(status, meta.get("superseded_by"))
                    })
        except Exception:
            pass

    # Always search shared collections
    for shared_name in ["patterns", "context"]:
        try:
            shared = get_shared_collection(chroma, shared_name)
            shared_results = shared.query(
                query_texts=[query],
                n_results=min(3, limit),
                where=where_clause
            )

            if shared_results["documents"] and shared_results["documents"][0]:
                for doc, meta, dist in zip(
                    shared_results["documents"][0],
                    shared_results["metadatas"][0],
                    shared_results["distances"][0]
                ):
                    results.append({
                        "source": f"shared:{shared_name}",
                        "title": meta.get("title", "Untitled"),
                        "type": meta.get("type"),
                        "relevance": f"{max(0, 1 - dist):.0%}",
                        "content": doc[:500] + "..." if len(doc) > 500 else doc
                    })
        except Exception:
            pass

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching memories found. This might be new territory - consider recording what you learn!"
        }, indent=2)

    return json.dumps({
        "query": query,
        "result_count": len(results),
        "results": results
    }, indent=2)


@mcp.tool()
async def memory_get_active_work(
    session_id: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    See what other Claudes are currently working on.

    Use this to:
    - Avoid working on the same files
    - Understand what's in progress
    - Coordinate with other Claude instances

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = get_chroma_client()

    # Get from active sessions (in-memory)
    active_work = []
    for sid, info in active_sessions.items():
        if sid != session_id:
            if project is None or info["project"] == project:
                active_work.append({
                    "session_id": sid,
                    "claude_instance": info["claude_instance"],
                    "project": info["project"],
                    "task": info["task"],
                    "started": info["started"],
                    "last_activity": info["last_activity"]
                })

    # Also get recent work items from Chroma
    work_collection = get_shared_collection(chroma, "work")
    cutoff = (datetime.now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    where_filter = None
    if project:
        where_filter = {"project": project}

    try:
        recent = work_collection.get(
            where=where_filter,
            include=["documents", "metadatas"]
        )

        recent_work = []
        if recent["documents"]:
            for doc, meta in zip(recent["documents"], recent["metadatas"]):
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                recent_work.append({
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "claude": meta.get("claude_instance"),
                    "project": meta.get("project"),
                    "files": json.loads(meta.get("files_touched", "[]")),
                    "updated": meta.get("updated")
                })
    except Exception:
        recent_work = []

    return json.dumps({
        "currently_active": active_work,
        "recent_work_items": recent_work,
        "overlap_window_hours": OVERLAP_WINDOW_HOURS
    }, indent=2)


# =============================================================================
# Storage Tools
# =============================================================================

@mcp.tool()
async def memory_store(
    session_id: str,
    title: str,
    content: str,
    memory_type: str,
    project: str = None,
    tags: List[str] = None,
    files_related: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Store a new memory in the knowledge base.

    Use this for:
    - API specs, architecture docs (project-specific)
    - Code snippets and solutions (can be shared)
    - Task context and notes

    For quick learnings, use memory_record_learning instead.

    Args:
        session_id: Your session ID
        title: Title for this memory
        content: Content (markdown supported)
        memory_type: Type of memory (api_spec, architecture, learning, pattern, code_snippet, etc.)
        project: Project this belongs to (omit for shared/cross-project memories)
        tags: Tags for categorization
        files_related: File paths this memory relates to
    """
    error = require_session(session_id)
    if error:
        return error

    if memory_type not in MEMORY_TYPES:
        return json.dumps({"error": f"Invalid memory_type. Must be one of: {MEMORY_TYPES}"}, indent=2)

    tags = tags or []
    files_related = files_related or []
    chroma = get_chroma_client()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Determine collection
    if project:
        collection = get_project_collection(chroma, project)
        location = f"project:{project}"
    else:
        if memory_type in ["pattern", "code_snippet", "solution"]:
            collection = get_shared_collection(chroma, "patterns")
            location = "shared:patterns"
        else:
            collection = get_shared_collection(chroma, "context")
            location = "shared:context"

    doc_id = generate_doc_id(content, memory_type)

    # Check for overlaps if files are specified
    overlap_warning = ""
    if files_related:
        overlaps = check_overlap(chroma, project or "shared", files_related, session_id)
        overlap_warning = format_overlap_warning(overlaps)

    collection.add(
        ids=[doc_id],
        documents=[content],
        metadatas=[{
            "title": title,
            "type": memory_type,
            "status": "active",
            "tags": json.dumps(tags),
            "files_related": json.dumps(files_related),
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": project or "",
            "created": now,
            "updated": now
        }]
    )

    result = {
        "status": "stored",
        "id": doc_id,
        "location": location,
        "title": title,
        "type": memory_type
    }

    if overlap_warning:
        result["warning"] = overlap_warning

    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_record_learning(
    session_id: str,
    title: str,
    details: str,
    project: str = None,
    tags: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Quick way to record something you learned.

    Use this when you discover:
    - A non-obvious behavior
    - A gotcha or pitfall
    - A useful technique
    - Why something was done a certain way

    These help other Claudes avoid repeating your discovery process.

    Args:
        session_id: Your session ID
        title: What did you learn? (short title)
        details: Details of the learning
        project: Project-specific or omit for cross-project learning
        tags: Tags for categorization
    """
    error = require_session(session_id)
    if error:
        return error

    tags = tags or []
    chroma = get_chroma_client()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    if project:
        collection = get_project_collection(chroma, project)
        location = f"project:{project}"
    else:
        collection = get_shared_collection(chroma, "patterns")
        location = "shared:patterns"

    doc_id = f"learning_{generate_doc_id(title, 'learning')}"

    collection.add(
        ids=[doc_id],
        documents=[f"# {title}\n\n{details}"],
        metadatas=[{
            "title": title,
            "type": "learning",
            "status": "active",
            "tags": json.dumps(tags),
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "created": now,
            "updated": now
        }]
    )

    return json.dumps({
        "status": "recorded",
        "id": doc_id,
        "location": location,
        "title": title,
        "message": "Learning recorded. Other Claudes will see this when they query related topics."
    }, indent=2)


@mcp.tool()
async def memory_update_work(
    session_id: str,
    title: str,
    status: str,
    files_touched: List[str] = None,
    notes: str = None,
    ctx: Context = None
) -> str:
    """
    Update your current work status and files touched.

    Call this periodically to:
    - Let other Claudes know what you're working on
    - Enable overlap detection (warns if another Claude touches same files)
    - Track progress on your task

    Args:
        session_id: Your session ID
        title: What you're working on
        status: Current status (in_progress, blocked, completed, abandoned)
        files_touched: Files you've touched (for overlap detection)
        notes: Additional context
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in WORK_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {WORK_STATUSES}"}, indent=2)

    files_touched = files_touched or []
    chroma = get_chroma_client()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Update session
    active_sessions[session_id]["last_activity"] = now
    active_sessions[session_id]["task"] = title

    # Check for overlaps
    overlap_warning = ""
    if files_touched:
        overlaps = check_overlap(chroma, session_info["project"], files_touched, session_id)
        overlap_warning = format_overlap_warning(overlaps)

    # Store/update work item
    work_collection = get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"

    content = f"{title}\n\n{notes or ''}"

    work_collection.upsert(
        ids=[work_id],
        documents=[content],
        metadatas=[{
            "title": title,
            "status": status,
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_touched),
            "created": session_info["started"],
            "updated": now
        }]
    )

    result = {
        "status": "updated",
        "work_status": status,
        "files_tracked": files_touched
    }

    if overlap_warning:
        result["warning"] = overlap_warning

    return json.dumps(result, indent=2)


# =============================================================================
# Lifecycle Management Tools
# =============================================================================

@mcp.tool()
async def memory_change_status(
    session_id: str,
    doc_id: str,
    new_status: str,
    project: str = None,
    superseded_by: str = None,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Change a document's lifecycle status.

    Use this to:
    - Mark outdated docs as DEPRECATED (still searchable with warning)
    - Mark replaced docs as SUPERSEDED (link to replacement)
    - Archive old docs (excluded from normal search)

    Prefer this over deletion - it preserves history and context.

    Args:
        session_id: Your session ID
        doc_id: Document ID to update
        new_status: New status (active, deprecated, superseded, archived)
        project: Project (if project-specific doc)
        superseded_by: ID of replacement document (if superseding)
        reason: Reason for status change
    """
    error = require_session(session_id)
    if error:
        return error

    if new_status not in DOC_STATUSES:
        return json.dumps({"error": f"Invalid new_status. Must be one of: {DOC_STATUSES}"}, indent=2)

    chroma = get_chroma_client()
    now = datetime.now().isoformat()

    # Find the document
    if project:
        collection = get_project_collection(chroma, project)
    else:
        collection = None
        for shared_name in ["patterns", "context"]:
            shared = get_shared_collection(chroma, shared_name)
            result = shared.get(ids=[doc_id])
            if result["ids"]:
                collection = shared
                break

        if not collection:
            return json.dumps({"error": f"Document not found: {doc_id}"}, indent=2)

    result = collection.get(ids=[doc_id], include=["documents", "metadatas"])

    if not result["ids"]:
        return json.dumps({"error": f"Document not found: {doc_id}"}, indent=2)

    # Update metadata
    meta = result["metadatas"][0]
    old_status = meta.get("status", "active")
    meta["status"] = new_status
    meta["updated"] = now
    meta["status_changed_by"] = session_id

    if superseded_by:
        meta["superseded_by"] = superseded_by
    if reason:
        meta["status_change_reason"] = reason

    collection.update(
        ids=[doc_id],
        metadatas=[meta]
    )

    return json.dumps({
        "status": "updated",
        "doc_id": doc_id,
        "old_status": old_status,
        "new_status": new_status,
        "superseded_by": superseded_by,
        "message": f"Document marked as {new_status}. " +
                   ("It will show warnings when retrieved." if new_status != "archived"
                    else "It is now excluded from normal searches.")
    }, indent=2)


@mcp.tool()
async def memory_search_global(
    session_id: str,
    query: str,
    memory_types: List[str] = None,
    limit: int = 10,
    ctx: Context = None
) -> str:
    """
    Search across ALL projects and shared memories.

    Use this to find:
    - Patterns that might apply to your current project
    - How similar problems were solved elsewhere
    - Cross-project learnings and gotchas

    Args:
        session_id: Your session ID
        query: Search query
        memory_types: Filter by types (optional)
        limit: Maximum number of results (1-30)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = get_chroma_client()
    all_results = []

    # Build where filter
    where_filter = {"status": "active"}
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    # Search all project collections
    collections = chroma.list_collections()
    for col in collections:
        if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
            try:
                results = col.query(
                    query_texts=[query],
                    n_results=min(5, limit),
                    where=where_filter
                )

                if results["documents"] and results["documents"][0]:
                    for doc, meta, dist in zip(
                        results["documents"][0],
                        results["metadatas"][0],
                        results["distances"][0]
                    ):
                        all_results.append({
                            "collection": col.name,
                            "title": meta.get("title", "Untitled"),
                            "type": meta.get("type"),
                            "relevance": 1 - dist,
                            "content_preview": doc[:300] + "..." if len(doc) > 300 else doc
                        })
            except Exception:
                continue

    # Sort by relevance and limit
    all_results.sort(key=lambda x: x["relevance"], reverse=True)
    all_results = all_results[:limit]

    # Format relevance as percentage
    for r in all_results:
        r["relevance"] = f"{max(0, r['relevance']):.0%}"

    return json.dumps({
        "query": query,
        "result_count": len(all_results),
        "results": all_results,
        "note": "Results from all projects and shared collections, sorted by relevance."
    }, indent=2)


@mcp.tool()
async def memory_list_projects(ctx: Context = None) -> str:
    """
    List all projects with memory collections.

    No session required - useful for initial orientation.
    """
    chroma = get_chroma_client()
    collections = chroma.list_collections()

    projects = []
    shared = []

    for col in collections:
        if col.name.startswith(PROJECT_PREFIX):
            project_name = col.name[len(PROJECT_PREFIX):]
            try:
                count = col.count()
            except Exception:
                count = 0
            projects.append({
                "project": project_name,
                "collection": col.name,
                "document_count": count
            })
        elif col.name.startswith(SHARED_PREFIX):
            shared_name = col.name[len(SHARED_PREFIX):]
            try:
                count = col.count()
            except Exception:
                count = 0
            shared.append({
                "name": shared_name,
                "collection": col.name,
                "document_count": count
            })

    return json.dumps({
        "projects": projects,
        "shared_collections": shared,
        "active_sessions": len(active_sessions)
    }, indent=2)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Shared Memory MCP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║          Shared Memory MCP Server for Multi-Claude           ║
╠══════════════════════════════════════════════════════════════╣
║  Endpoint: http://{args.host}:{args.port}/sse
║  Chroma:   {CHROMA_HOST}:{CHROMA_PORT}
║                                                              ║
║  Tools:                                                      ║
║    memory_start_session  - START HERE (required first)       ║
║    memory_end_session    - Record work and end session       ║
║    memory_query          - Search project/shared memories    ║
║    memory_store          - Store new memories                ║
║    memory_record_learning - Quick learning capture           ║
║    memory_update_work    - Track current work & files        ║
║    memory_change_status  - Deprecate/supersede/archive       ║
║    memory_search_global  - Cross-project search              ║
║    memory_list_projects  - List all projects                 ║
║    memory_get_active_work - See other Claudes' work          ║
╚══════════════════════════════════════════════════════════════╝
""")

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import JSONResponse

    async def health_check(request):
        """Health check endpoint."""
        try:
            chroma = chromadb.HttpClient(
                host=CHROMA_HOST,
                port=CHROMA_PORT,
                settings=Settings(anonymized_telemetry=False)
            )
            chroma.heartbeat()
            chroma_status = "healthy"
        except Exception as e:
            chroma_status = f"unhealthy: {str(e)}"

        status = "healthy" if chroma_status == "healthy" else "degraded"
        return JSONResponse({
            "status": status,
            "chroma": chroma_status,
            "active_sessions": len(active_sessions)
        }, status_code=200 if status == "healthy" else 503)

    # Get the SSE app from FastMCP
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    sse_app = mcp.sse_app()

    # Create wrapper app with health endpoint
    inner_app = Starlette(
        routes=[
            Route("/health", health_check),
            Mount("/", app=sse_app),
        ]
    )

    # Create ASGI middleware to rewrite host header before it reaches any app
    class HostRewriteMiddleware:
        """ASGI middleware to rewrite Host header to localhost."""
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                new_headers = []
                for k, v in scope["headers"]:
                    if k == b"host":
                        new_headers.append((b"host", b"localhost"))
                    else:
                        new_headers.append((k, v))
                scope = dict(scope)
                scope["headers"] = new_headers
            await self.app(scope, receive, send)

    # Wrap entire app with host rewrite
    app = HostRewriteMiddleware(inner_app)

    # Run with uvicorn
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info"
    )
```

### Step 5: Create requirements.txt

Create `/opt/mcp-memory/requirements.txt`:

```
mcp[cli]>=1.0.0
chromadb>=0.4.0
pydantic>=2.0.0
uvicorn>=0.30.0
starlette>=0.38.0
```

### Step 6: Create Dockerfile

Create `/opt/mcp-memory/Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Default environment variables
ENV CHROMA_HOST=localhost
ENV CHROMA_PORT=8001

EXPOSE 8080

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
```

### Step 7: Create docker-compose.yml

Create `/opt/mcp-memory/docker-compose.yml`:

```yaml
services:
  mcp-memory:
    build: .
    container_name: mcp-memory
    network_mode: host
    environment:
      - CHROMA_HOST=localhost
      - CHROMA_PORT=8001
    restart: unless-stopped
    depends_on:
      - chroma
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  chroma:
    image: chromadb/chroma:latest
    container_name: chroma-db
    volumes:
      - ./chroma-data:/chroma/chroma
    environment:
      - ANONYMIZED_TELEMETRY=false
      - ALLOW_RESET=false
      - IS_PERSISTENT=true
    ports:
      - "8001:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v2/heartbeat"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

### Step 8: Create Systemd Service

Create `/opt/mcp-memory/mcp-memory.service`:

```ini
[Unit]
Description=MCP Shared Memory Server (Docker)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/mcp-memory
ExecStart=/usr/bin/docker compose up -d --build
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose restart

[Install]
WantedBy=multi-user.target
```

Install the service:

```bash
sudo cp /opt/mcp-memory/mcp-memory.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mcp-memory
sudo systemctl start mcp-memory
```

### Step 9: Verify Installation

```bash
# Check services are running
docker ps

# Check health
curl http://localhost:8080/health

# Check Chroma
curl http://localhost:8001/api/v2/heartbeat

# Check MCP tools are accessible
curl http://localhost:8080/sse
```

Expected output:
```json
{"status":"healthy","chroma":"healthy","active_sessions":0}
```

---

## AWS Security Group Configuration

Open these ports in your EC2 security group:

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | Your IP | SSH access |
| 8080 | TCP | Your IP / VPC | MCP Server |
| 8001 | TCP | 127.0.0.1 only | Chroma (internal) |

**Important:** Only expose port 8080 to trusted IPs or use a VPN. The MCP server has no authentication.

---

## Client Configuration

### Claude Code (Local Machine)

Add to `~/.claude.json`:

```bash
claude mcp add --transport sse shared-memory http://YOUR_SERVER_IP:8080/sse --scope user
```

Or manually edit `~/.claude.json`:

```json
{
  "mcpServers": {
    "shared-memory": {
      "type": "sse",
      "url": "http://YOUR_SERVER_IP:8080/sse"
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "shared-memory": {
      "url": "http://YOUR_SERVER_IP:8080/sse"
    }
  }
}
```

### Project CLAUDE.md

Add to each project's `CLAUDE.md`:

```markdown
## Shared Memory System (REQUIRED)

This project uses a shared memory system at `http://YOUR_SERVER_IP:8080/sse`.

Tools appear as `mcp__shared-memory__<tool_name>`

### Session Workflow

**START**: Call `memory_start_session(project="your-project", claude_instance="main", task_description="...")`

**DURING**:
- `memory_query(session_id="...", query="...", project="your-project")`
- `memory_update_work(session_id="...", files_touched=[...])`
- `memory_record_learning(session_id="...", title="...", details="...")`

**END**: Call `memory_end_session(session_id="...", summary="...", files_modified=[...])`
```

---

## Maintenance

### View Logs

```bash
cd /opt/mcp-memory
docker compose logs -f
docker compose logs mcp-memory
docker compose logs chroma
```

### Restart Services

```bash
sudo systemctl restart mcp-memory
# or
cd /opt/mcp-memory && docker compose restart
```

### Backup Chroma Data

```bash
# Stop services
sudo systemctl stop mcp-memory

# Backup
tar -czvf chroma-backup-$(date +%Y%m%d).tar.gz /opt/mcp-memory/chroma-data

# Restart
sudo systemctl start mcp-memory
```

### Update Server

```bash
cd /opt/mcp-memory

# Pull latest code (if using git)
git pull

# Rebuild and restart
docker compose up -d --build
```

---

## Troubleshooting

### MCP Server Not Starting

```bash
# Check container logs
docker logs mcp-memory

# Check if Chroma is running
curl http://localhost:8001/api/v2/heartbeat

# Check port availability
sudo netstat -tlnp | grep -E '8080|8001'
```

### "Invalid Host Header" Error

The server is configured to accept any host. If you still get this error:

1. Check you're using the correct URL format: `http://IP:8080/sse`
2. Verify the HostRewriteMiddleware is in server.py
3. Rebuild the container: `docker compose up -d --build`

### Chroma Connection Failed

```bash
# Check Chroma container
docker logs chroma-db

# Verify Chroma is accessible
curl http://localhost:8001/api/v2/heartbeat

# Check environment variables
docker exec mcp-memory env | grep CHROMA
```

### Client Can't Connect

1. Check security group allows port 8080 from client IP
2. Verify server is running: `curl http://SERVER_IP:8080/health`
3. Check client config URL is correct
4. Restart Claude Code to reload MCP config

---

## Available Tools

| Tool | Purpose |
|------|---------|
| `memory_start_session` | Register session, get context (REQUIRED FIRST) |
| `memory_end_session` | Record work summary and handoff |
| `memory_query` | Search project/shared knowledge |
| `memory_store` | Store architecture docs, specs, etc. |
| `memory_record_learning` | Quick capture of discoveries |
| `memory_update_work` | Track current work & files |
| `memory_change_status` | Mark docs deprecated/superseded |
| `memory_search_global` | Search across all projects |
| `memory_list_projects` | List all projects |
| `memory_get_active_work` | See other Claudes' work |

## Memory Types

- `api_spec`, `architecture`, `component`, `config` - Project structure
- `adr` - Architecture Decision Records
- `learning`, `pattern`, `gotcha` - Knowledge sharing
- `code_snippet`, `solution` - Reusable code
- `task_context`, `handoff`, `work_item` - Work tracking

## Document Lifecycle

- `active` - Current and valid (default)
- `deprecated` - Outdated, shows warning
- `superseded` - Replaced by newer doc
- `archived` - Historical only, excluded from search
