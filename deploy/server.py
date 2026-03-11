"""
Shared Memory MCP Server for Multi-Claude Coordination

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.
Created on personal time. Not a work-for-hire. All IP rights retained by author.

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

IMPORTANT: Uses AsyncHttpClient for proper async/await support and connection management.
See: https://github.com/chroma-core/chroma/issues/4296
"""

import json
import hashlib
import uuid
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context
import chromadb
from chromadb.config import Settings


# =============================================================================
# Configuration
# =============================================================================

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))

# Collection naming
PROJECT_PREFIX = "proj_"      # proj_emailtriage, proj_nimbus
SHARED_PREFIX = "shared_"     # shared_patterns, shared_context

# Overlap detection window
OVERLAP_WINDOW_HOURS = 24

# Active sessions stored in memory (lightweight, no persistence needed)
active_sessions: Dict[str, Dict[str, Any]] = {}

# File locks stored in memory (auto-released on session end)
# Structure: { file_path: { session_id, claude_instance, reason, locked_at } }
file_locks: Dict[str, Dict[str, Any]] = {}

# Signals stored in memory (retained for 24 hours)
# Structure: { signal_name: { from_session, from_claude, timestamp, details } }
active_signals: Dict[str, Dict[str, Any]] = {}

# Stale lock timeout (30 minutes of no activity = stale)
STALE_LOCK_MINUTES = 30

# Signal retention (24 hours)
SIGNAL_RETENTION_HOURS = 24

# Valid memory types
MEMORY_TYPES = [
    "api_spec", "architecture", "component", "config",  # Architecture
    "adr",  # Decisions
    "learning", "pattern", "gotcha",  # Learnings
    "code_snippet", "solution",  # Code
    "task_context", "handoff", "work_item",  # Task/Work
    "interface",  # Structured interface contracts
    "function_ref"  # AI-optimized function references
]

# Valid doc statuses
DOC_STATUSES = ["active", "deprecated", "superseded", "archived", "conflicted", "review_pending", "expired"]

# Content size limit (50KB)
MAX_CONTENT_SIZE = 50 * 1024

# Default expiry days by memory type (None = never expires)
# Doubled from original values to accommodate part-time usage
DEFAULT_EXPIRY_DAYS = {
    "learning": 180,      # 6 months (was 90)
    "gotcha": 180,        # 6 months (was 90)
    "task_context": 60,   # 2 months (was 30)
    "handoff": 28,        # 4 weeks (was 14)
    "work_item": 14,      # 2 weeks (was 7)
    # These don't expire by default
    "api_spec": None,
    "architecture": None,
    "component": None,
    "config": None,
    "adr": None,
    "pattern": None,
    "code_snippet": None,
    "solution": None,
    "interface": None,
}

# Valid work statuses
WORK_STATUSES = ["in_progress", "blocked", "completed", "abandoned"]

# Backlog item statuses
BACKLOG_STATUSES = ["open", "in_progress", "deferred", "done", "wont_do", "retest", "blocked", "duplicate", "needs_info"]

# Backlog priorities
BACKLOG_PRIORITIES = ["critical", "high", "medium", "low"]

# Message queue for inter-Claude communication
# Structure: {message_id: {to, from, message, priority, created, delivered, acknowledged}}
message_queue = {}

# Message priorities
MESSAGE_PRIORITIES = ["urgent", "normal", "low"]


# =============================================================================
# Chroma Client Setup - Uses AsyncHttpClient for proper connection management
# =============================================================================

# Global client reference (lazy initialized)
_chroma_client = None
_chroma_lock = None  # Will be created when needed

async def _get_or_create_lock():
    """Get or create the asyncio lock for client initialization."""
    global _chroma_lock
    import asyncio
    if _chroma_lock is None:
        _chroma_lock = asyncio.Lock()
    return _chroma_lock


async def get_chroma():
    """Get the shared async Chroma client (lazy initialization).

    CRITICAL: We use AsyncHttpClient instead of HttpClient because:
    1. HttpClient creates new TCP connections per request that don't get released
    2. This causes port exhaustion and server hangs under load
    3. AsyncHttpClient properly manages connections and supports async/await

    See: https://github.com/chroma-core/chroma/issues/4296

    This uses lazy initialization to work with stateless_http mode where
    the lifespan context may not be available.
    """
    global _chroma_client

    if _chroma_client is not None:
        return _chroma_client

    lock = await _get_or_create_lock()
    async with lock:
        # Double-check after acquiring lock
        if _chroma_client is not None:
            return _chroma_client

        # Create a single AsyncHttpClient instance for the entire application lifetime
        client = await chromadb.AsyncHttpClient(
            host=CHROMA_HOST,
            port=CHROMA_PORT,
            settings=Settings(anonymized_telemetry=False)
        )
        print(f"Connected to Chroma (async) at {CHROMA_HOST}:{CHROMA_PORT}")

        # Ensure shared collections exist
        for name in ["shared_patterns", "shared_context", "shared_work"]:
            await client.get_or_create_collection(
                name=name,
                metadata={"type": "shared", "created": datetime.now().isoformat()}
            )

        _chroma_client = client
        return _chroma_client


@asynccontextmanager
async def app_lifespan(app):
    """Lifespan manager for cleanup (lazy init means startup is handled by get_chroma)."""
    global _chroma_client

    # We don't initialize here anymore - get_chroma() does lazy initialization
    # This avoids issues with stateless_http mode where lifespan might not run properly
    yield {}

    # Cleanup on shutdown
    _chroma_client = None


# Allow connections from any host (for remote access via IP or proxy)
# stateless_http=True fixes -32602 "request before initialization" errors
# See: https://github.com/GregBaugues/tokenbowl-mcp/issues/86
mcp = FastMCP("shared_memory", lifespan=app_lifespan, host="0.0.0.0", stateless_http=True)


# =============================================================================
# Helper Functions - All async for use with AsyncHttpClient
# =============================================================================

async def get_project_collection(client, project: str):
    """Get or create a project-specific collection (async)."""
    name = f"{PROJECT_PREFIX}{project.lower().replace('-', '_')}"
    return await client.get_or_create_collection(
        name=name,
        metadata={"project": project, "created": datetime.now().isoformat()}
    )


async def get_shared_collection(client, collection_type: str):
    """Get a shared collection (patterns, context, work) (async)."""
    return await client.get_or_create_collection(name=f"{SHARED_PREFIX}{collection_type}")


def generate_doc_id(content: str, doc_type: str) -> str:
    """Generate a stable document ID from content hash."""
    hash_input = f"{doc_type}:{content[:500]}:{datetime.now().isoformat()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def generate_content_hash(content: str) -> str:
    """Generate a hash of normalized content for duplicate detection."""
    # Normalize: lowercase, collapse whitespace, strip
    normalized = ' '.join(content.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


async def check_duplicate(collection, content: str, threshold: float = 0.95) -> Optional[Dict]:
    """Check if similar content already exists in the collection.

    Returns the existing doc info if a duplicate is found, None otherwise.
    Uses both hash matching (exact) and embedding similarity (near-duplicate).
    """
    content_hash = generate_content_hash(content)

    # First, check for exact hash match via metadata
    try:
        results = await collection.get(
            where={"content_hash": content_hash},
            include=["metadatas"]
        )
        if results["ids"]:
            return {
                "type": "exact",
                "doc_id": results["ids"][0],
                "title": results["metadatas"][0].get("title", "Unknown")
            }
    except Exception:
        pass  # content_hash field might not exist on older docs

    # Then check for near-duplicate via embedding similarity
    try:
        results = await collection.query(
            query_texts=[content[:1000]],  # Use first 1000 chars for query
            n_results=1,
            include=["metadatas", "distances"]
        )
        if results["distances"] and results["distances"][0]:
            # Chroma returns L2 distance; convert to similarity
            # Lower distance = more similar. Threshold ~0.1 for very similar
            distance = results["distances"][0][0]
            if distance < 0.15:  # Very similar content
                return {
                    "type": "similar",
                    "doc_id": results["ids"][0][0],
                    "title": results["metadatas"][0][0].get("title", "Unknown"),
                    "similarity": f"{(1 - distance):.0%}"
                }
    except Exception:
        pass

    return None


def calculate_expiry(memory_type: str, custom_days: int = None) -> Optional[str]:
    """Calculate expiry date based on memory type or custom value."""
    if custom_days is not None:
        if custom_days <= 0:
            return None  # Explicitly no expiry
        days = custom_days
    else:
        days = DEFAULT_EXPIRY_DAYS.get(memory_type)

    if days is None:
        return None

    return (datetime.now() + timedelta(days=days)).isoformat()


def is_expired(meta: Dict) -> bool:
    """Check if a document has expired based on expires_at field."""
    expires_at = meta.get("expires_at")
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now()
    except Exception:
        return False


async def update_access_stats(collection, doc_id: str):
    """Update access count and last_accessed for a document."""
    try:
        result = await collection.get(ids=[doc_id], include=["metadatas"])
        if result["ids"]:
            meta = result["metadatas"][0]
            meta["access_count"] = meta.get("access_count", 0) + 1
            meta["last_accessed"] = datetime.now().isoformat()
            await collection.update(ids=[doc_id], metadatas=[meta])
    except Exception:
        pass  # Non-critical, don't fail the query


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


async def check_overlap(client, project: str, files_touched: List[str], current_session: str) -> List[Dict]:
    """Check if other Claudes recently touched these files (async)."""
    overlaps = []
    work_collection = await get_shared_collection(client, "work")

    cutoff = (datetime.now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    for file_path in files_touched:
        try:
            results = await work_collection.query(
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


def format_overlap_warning(overlaps: List[Dict], current_claude: str = None) -> str:
    """Format overlap warnings for Claude. Keeps it brief."""
    if not overlaps:
        return ""

    # Filter out self-overlaps (same claude instance touching same files is normal)
    other_overlaps = [o for o in overlaps if o.get('other_claude') != current_claude]

    if not other_overlaps:
        return ""  # Only self-overlaps, no warning needed

    # Group by other claude to keep it concise
    by_claude = {}
    for o in other_overlaps:
        key = o.get('other_claude', 'unknown')
        if key not in by_claude:
            by_claude[key] = {'files': set(), 'task': o.get('what', '')}
        by_claude[key]['files'].add(o['file'].split('/')[-1])  # Just filename

    if not by_claude:
        return ""

    # One line per other claude
    warning = "\n⚠️ OVERLAP: "
    parts = []
    for claude, info in by_claude.items():
        files_str = ', '.join(list(info['files'])[:3])  # Max 3 files
        if len(info['files']) > 3:
            files_str += f" +{len(info['files'])-3} more"
        parts.append(f"{claude} touched {files_str}")
    warning += "; ".join(parts) + "\n"
    return warning


# =============================================================================
# File Locking Helper Functions
# =============================================================================

def is_lock_stale(lock_info: Dict) -> bool:
    """Check if a lock is stale (session inactive > STALE_LOCK_MINUTES)."""
    session_id = lock_info.get("session_id")
    if session_id not in active_sessions:
        return True  # Session ended, lock is stale

    last_activity = active_sessions[session_id].get("last_activity", "")
    if not last_activity:
        return False

    try:
        last_time = datetime.fromisoformat(last_activity)
        stale_threshold = datetime.now() - timedelta(minutes=STALE_LOCK_MINUTES)
        return last_time < stale_threshold
    except Exception:
        return False


def normalize_path(path: str) -> str:
    """Normalize a file path for consistent lock matching."""
    # Remove leading/trailing slashes, normalize separators
    return path.strip().strip('/').replace('\\', '/')


def path_matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a pattern (supports glob-like wildcards)."""
    import fnmatch
    file_path = normalize_path(file_path)
    pattern = normalize_path(pattern)

    # Directory pattern: "NimbusCommon/" matches all files within
    if pattern.endswith('/'):
        return file_path.startswith(pattern) or file_path.startswith(pattern[:-1] + '/')

    # Exact match or glob pattern
    return fnmatch.fnmatch(file_path, pattern) or file_path == pattern


def get_files_in_directory_lock(dir_path: str) -> List[str]:
    """Get all currently locked files that fall under a directory lock."""
    dir_path = normalize_path(dir_path)
    if not dir_path.endswith('/'):
        dir_path += '/'

    matching = []
    for locked_file in file_locks.keys():
        if locked_file.startswith(dir_path):
            matching.append(locked_file)
    return matching


def release_session_locks(session_id: str) -> List[str]:
    """Release all locks held by a session. Returns list of released files."""
    released = []
    to_remove = [f for f, info in file_locks.items() if info.get("session_id") == session_id]
    for f in to_remove:
        del file_locks[f]
        released.append(f)
    return released


def cleanup_stale_signals():
    """Remove signals older than SIGNAL_RETENTION_HOURS."""
    cutoff = datetime.now() - timedelta(hours=SIGNAL_RETENTION_HOURS)
    to_remove = []
    for signal_name, info in active_signals.items():
        try:
            signal_time = datetime.fromisoformat(info.get("timestamp", ""))
            if signal_time < cutoff:
                to_remove.append(signal_name)
        except Exception:
            pass
    for s in to_remove:
        del active_signals[s]


def get_relevant_locks_for_session(session_id: str, project: str) -> List[Dict]:
    """Get locks relevant to a session based on project and recent file patterns."""
    # For now, return all locks in the same project or shared paths
    relevant = []
    for file_path, lock_info in file_locks.items():
        if lock_info.get("session_id") != session_id:
            relevant.append({
                "file": file_path,
                "held_by": lock_info.get("claude_instance"),
                "session_id": lock_info.get("session_id"),
                "since": lock_info.get("locked_at"),
                "reason": lock_info.get("reason"),
                "stale": is_lock_stale(lock_info)
            })
    return relevant


async def get_recent_modifications(client, project: str, session_id: str) -> List[Dict]:
    """Get files modified recently by other sessions."""
    modifications = []
    cutoff = (datetime.now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    try:
        work_collection = await get_shared_collection(client, "work")
        results = await work_collection.get(
            where={"project": project} if project else None,
            include=["metadatas"]
        )

        if results["metadatas"]:
            for meta in results["metadatas"]:
                if meta.get("session_id") == session_id:
                    continue
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                files = json.loads(meta.get("files_touched", "[]"))
                for f in files[:3]:  # Limit to 3 files per work item
                    modifications.append({
                        "file": f,
                        "modified_by": meta.get("claude_instance"),
                        "when": updated,
                        "summary": meta.get("title", "")[:50]
                    })
    except Exception:
        pass

    return modifications[:10]  # Limit total


def get_pending_signals(claude_instance: str) -> List[Dict]:
    """Get signals that might be relevant to this agent."""
    cleanup_stale_signals()
    signals = []
    for signal_name, info in active_signals.items():
        signals.append({
            "signal": signal_name,
            "from": info.get("from_claude"),
            "timestamp": info.get("timestamp"),
            "details": info.get("details")
        })
    return signals


def get_blocking_others(claude_instance: str) -> List[Dict]:
    """Find agents that are blocked waiting for this agent."""
    blocking = []
    for sid, info in active_sessions.items():
        if info.get("blocked_by") == claude_instance:
            blocking.append({
                "agent": info.get("claude_instance"),
                "session_id": sid,
                "waiting_for": info.get("waiting_for_signal"),
                "reason": info.get("blocked_reason")
            })
    return blocking


async def get_interface_updates(client, project: str, last_session_end: str = None) -> List[Dict]:
    """Get interface contracts that changed recently."""
    updates = []
    try:
        proj_collection = await get_project_collection(client, project)
        results = await proj_collection.get(
            where={"type": "interface"},
            include=["metadatas"]
        )

        cutoff = last_session_end or (datetime.now() - timedelta(hours=24)).isoformat()

        if results["metadatas"]:
            for meta in results["metadatas"]:
                updated = meta.get("updated", "")
                if updated > cutoff:
                    updates.append({
                        "interface": meta.get("interface_name", meta.get("title")),
                        "version": meta.get("interface_version", "unknown"),
                        "changed_by": meta.get("claude_instance"),
                        "when": updated
                    })
    except Exception:
        pass

    return updates


# =============================================================================
# Session Management Tools
# =============================================================================

@mcp.tool()
async def memory_start_session(
    project: str,
    claude_instance: str = "unknown",
    task_description: str = "",
    tmux_target: str = None,
    ctx: Context = None
) -> str:
    """
    START HERE - Call this first before any other memory tools.

    Registers your session and returns:
    - Your session ID (required for all other calls)
    - Recent relevant learnings for your project
    - Active work by other Claudes (avoid conflicts)
    - Handoff notes from previous sessions
    - Active file locks in your working area
    - Signals from other agents (completion notifications)
    - Who you might be blocking
    - Interface updates since your last session

    You MUST call this at the start of your work.

    Args:
        project: Project you're working on (e.g., 'emailtriage', 'nimbus')
        claude_instance: Identifier for this Claude instance (e.g., 'main', 'agent-1')
        task_description: Brief description of what you're about to work on
        tmux_target: tmux target for message injection (e.g., 'emailtriage:frontend.0')
    """
    chroma = await get_chroma()

    # Generate session ID
    session_id = f"{project}_{claude_instance}_{uuid.uuid4().hex[:8]}"

    # Register session
    active_sessions[session_id] = {
        "project": project,
        "claude_instance": claude_instance,
        "task": task_description,
        "started": datetime.now().isoformat(),
        "last_activity": datetime.now().isoformat(),
        "blocked_by": None,
        "blocked_reason": None,
        "waiting_for_signal": None,
        "tmux_target": tmux_target
    }

    # Gather context for this Claude - keep output compact
    output = {
        "session_id": session_id,
        "project": project
    }

    # Get recent learnings for this project
    try:
        proj_collection = await get_project_collection(chroma, project)
        recent_learnings = await proj_collection.query(
            query_texts=[task_description or "recent learnings"],
            n_results=3,
            where={"type": {"$in": ["learning", "gotcha", "handoff"]}}
        )

        if recent_learnings["documents"] and recent_learnings["documents"][0]:
            # Compact: just titles
            titles = [meta.get("title") for meta in recent_learnings["metadatas"][0]]
            if titles:
                output["learnings"] = titles
    except Exception:
        pass

    # Get shared patterns - just titles, skip if none
    try:
        shared = await get_shared_collection(chroma, "patterns")
        patterns = await shared.query(
            query_texts=[task_description or project],
            n_results=2
        )
        if patterns["documents"] and patterns["documents"][0]:
            titles = [meta.get("title") for meta in patterns["metadatas"][0]]
            if titles:
                output["patterns"] = titles
    except Exception:
        pass

    # Get active work by other Claudes - compact format
    other_active = []
    for sid, info in active_sessions.items():
        if sid != session_id:
            other_active.append(f"{info['claude_instance']}@{info['project']}: {info['task'][:50]}")

    if other_active:
        output["other_claudes"] = other_active

    # NEW: Get relevant file locks
    relevant_locks = get_relevant_locks_for_session(session_id, project)
    if relevant_locks:
        output["relevant_locks"] = relevant_locks

    # NEW: Get recent file modifications by others
    recent_mods = await get_recent_modifications(chroma, project, session_id)
    if recent_mods:
        output["recent_modifications"] = recent_mods

    # NEW: Get pending signals (completion notifications)
    signals = get_pending_signals(claude_instance)
    if signals:
        output["signals"] = signals

    # NEW: Check if you're blocking others
    blocking = get_blocking_others(claude_instance)
    if blocking:
        output["blocking_others"] = blocking

    # NEW: Get interface updates
    interface_updates = await get_interface_updates(chroma, project)
    if interface_updates:
        output["interface_updates"] = interface_updates

    # Always include a tip pointing to the usage guide
    output["tip"] = "New? Run memory_query(query='shared memory usage guide') for best practices and backlog tools."

    return json.dumps(output)


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
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Store handoff note
    proj_collection = await get_project_collection(chroma, session_info["project"])

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
    await proj_collection.upsert(
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
        await proj_collection.add(
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
    work_collection = await get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"
    await work_collection.upsert(
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

    # Auto-release any file locks held by this session
    released_locks = release_session_locks(session_id)

    # Remove from active sessions
    del active_sessions[session_id]

    result = {"status": "ended"}
    if released_locks:
        result["released_locks"] = released_locks

    return json.dumps(result)


# =============================================================================
# Query Tools
# =============================================================================

# Relevance threshold - results below this are excluded
# Chroma L2 distance: 0 = identical, 1 = quite different, 2+ = very different
# We convert to similarity: 1 - (dist/2) gives 0-1 range
MIN_RELEVANCE_THRESHOLD = 0.3  # 30% minimum relevance


def calculate_relevance(distance: float) -> float:
    """Convert Chroma L2 distance to 0-1 relevance score.

    L2 distances typically range 0-2 for normalized embeddings.
    We clamp and convert to similarity percentage.
    """
    # Clamp distance to reasonable range
    dist = max(0, min(distance, 2.0))
    # Convert to similarity (0-1 range)
    return 1 - (dist / 2)


@mcp.tool()
async def memory_query(
    session_id: str,
    query: str,
    project: str = None,
    memory_types: List[str] = None,
    include_inactive: bool = False,
    include_shared: bool = True,
    limit: int = 3,
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
        include_shared: Search shared patterns/context (default True, set False for project-only)
        limit: Maximum number of results (1-10, default 3)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
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
            proj_collection = await get_project_collection(chroma, project)
            proj_results = await proj_collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_clause
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                )):
                    # Skip expired documents
                    if is_expired(meta):
                        continue

                    # Calculate relevance and skip if below threshold
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    status = meta.get("status", "active")
                    doc_id = proj_results["ids"][0][i] if proj_results["ids"] else None

                    results.append({
                        "source": f"project:{project}",
                        "id": doc_id or meta.get("id", "unknown"),
                        "title": meta.get("title", "Untitled"),
                        "type": meta.get("type"),
                        "status": status,
                        "relevance": f"{relevance:.0%}",
                        "content": doc,
                        "access_count": meta.get("access_count", 0),
                        "warning": format_status_warning(status, meta.get("superseded_by"))
                    })

                    # Track access (fire-and-forget)
                    if doc_id:
                        await update_access_stats(proj_collection, doc_id)
        except Exception:
            pass

    # Search shared collections only if requested and with higher threshold
    if include_shared:
        # Shared results need higher relevance to be included (reduces noise)
        shared_threshold = MIN_RELEVANCE_THRESHOLD + 0.1  # 40% for shared

        for shared_name in ["patterns", "context"]:
            try:
                shared = await get_shared_collection(chroma, shared_name)
                shared_results = await shared.query(
                    query_texts=[query],
                    n_results=min(2, limit),  # Max 2 from each shared collection
                    where=where_clause
                )

                if shared_results["documents"] and shared_results["documents"][0]:
                    for i, (doc, meta, dist) in enumerate(zip(
                        shared_results["documents"][0],
                        shared_results["metadatas"][0],
                        shared_results["distances"][0]
                    )):
                        # Skip expired documents
                        if is_expired(meta):
                            continue

                        # Calculate relevance and skip if below threshold
                        relevance = calculate_relevance(dist)
                        if relevance < shared_threshold:
                            continue

                        doc_id = shared_results["ids"][0][i] if shared_results["ids"] else None

                        results.append({
                            "source": f"shared:{shared_name}",
                            "id": doc_id,
                            "title": meta.get("title", "Untitled"),
                            "type": meta.get("type"),
                            "relevance": f"{relevance:.0%}",
                            "content": doc[:500] + "..." if len(doc) > 500 else doc,
                            "access_count": meta.get("access_count", 0)
                        })

                        # Track access (fire-and-forget)
                        if doc_id:
                            await update_access_stats(shared, doc_id)
            except Exception:
                pass

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching memories found. This might be new territory - consider recording what you learn!"
        }, indent=2)

    # Sort by relevance (highest first) and limit total results
    results.sort(key=lambda x: x["relevance"], reverse=True)
    results = results[:limit]

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

    chroma = await get_chroma()

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
    work_collection = await get_shared_collection(chroma, "work")
    cutoff = (datetime.now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    where_filter = None
    if project:
        where_filter = {"project": project}

    try:
        recent = await work_collection.get(
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

    # NEW: Include blocked agents info
    blocked_agents = []
    for sid, info in active_sessions.items():
        if info.get("blocked_by"):
            blocked_agents.append({
                "agent": info.get("claude_instance"),
                "waiting_for": info.get("blocked_by"),
                "signal": info.get("waiting_for_signal"),
                "reason": info.get("blocked_reason")
            })

    # NEW: Include recent signals
    cleanup_stale_signals()
    recent_signals = list(active_signals.values())[:10]

    return json.dumps({
        "currently_active": active_work,
        "blocked_agents": blocked_agents,
        "recent_signals": recent_signals,
        "recent_work_items": recent_work,
        "overlap_window_hours": OVERLAP_WINDOW_HOURS
    }, indent=2)


# =============================================================================
# File Locking Tools
# =============================================================================

@mcp.tool()
async def memory_lock_files(
    session_id: str,
    files: List[str],
    reason: str,
    ctx: Context = None
) -> str:
    """
    Atomically lock files for exclusive editing.

    Use this before editing shared code (like NimbusCommon) to prevent conflicts.
    Locks are automatically released when your session ends.

    Args:
        session_id: Your session ID
        files: List of file paths to lock (e.g., ["NimbusCommon/FrameRedis.cs"])
               Use trailing "/" for directory locks (e.g., "NimbusCommon/")
        reason: Why you need these files (e.g., "Implementing MQTT status handler")

    Returns:
        {
            "success": true/false,
            "locked": [...],      # Files you now hold
            "conflicts": [...]    # Files held by others (with stale flag)
        }

    Behavior:
        - Atomic: Either all files lock or none do
        - Auto-release: Locks released on memory_end_session
        - Directory locks: "path/" locks all files within
        - Stale detection: Locks from inactive sessions (>30min) marked as stale
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Update session activity
    active_sessions[session_id]["last_activity"] = now

    # Normalize all paths
    normalized_files = [normalize_path(f) for f in files]

    # Check for conflicts first (atomic - check all before locking any)
    conflicts = []
    for file_path in normalized_files:
        # Check if this exact file is locked
        if file_path in file_locks:
            lock_info = file_locks[file_path]
            if lock_info.get("session_id") != session_id:
                conflicts.append({
                    "file": file_path,
                    "held_by": lock_info.get("claude_instance"),
                    "session_id": lock_info.get("session_id"),
                    "since": lock_info.get("locked_at"),
                    "reason": lock_info.get("reason"),
                    "stale": is_lock_stale(lock_info)
                })
                continue

        # Check if any existing lock conflicts with this path
        for locked_path, lock_info in file_locks.items():
            if lock_info.get("session_id") == session_id:
                continue  # Own lock, skip

            # Check if paths conflict (one is prefix of other, or same)
            if (path_matches_pattern(file_path, locked_path) or
                path_matches_pattern(locked_path, file_path)):
                if not any(c["file"] == locked_path for c in conflicts):
                    conflicts.append({
                        "file": locked_path,
                        "held_by": lock_info.get("claude_instance"),
                        "session_id": lock_info.get("session_id"),
                        "since": lock_info.get("locked_at"),
                        "reason": lock_info.get("reason"),
                        "stale": is_lock_stale(lock_info)
                    })

    # If any conflicts exist, don't lock anything (atomic)
    if conflicts:
        return json.dumps({
            "success": False,
            "locked": [],
            "conflicts": conflicts,
            "message": "Cannot acquire locks due to conflicts. Use memory_get_locks to see details."
        })

    # No conflicts - acquire all locks
    locked = []
    for file_path in normalized_files:
        file_locks[file_path] = {
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "reason": reason,
            "locked_at": now
        }
        locked.append(file_path)

    return json.dumps({
        "success": True,
        "locked": locked,
        "conflicts": []
    })


@mcp.tool()
async def memory_unlock_files(
    session_id: str,
    files: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Release file locks before session ends.

    Args:
        session_id: Your session ID
        files: Specific files to unlock (None = unlock all held by this session)

    Returns:
        {
            "released": [...],    # Files that were unlocked
            "still_held": [...]   # Files you still hold (if you specified specific files)
        }
    """
    error = require_session(session_id)
    if error:
        return error

    # Update session activity
    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()

    if files is None:
        # Release all locks for this session
        released = release_session_locks(session_id)
        return json.dumps({
            "released": released,
            "still_held": []
        })

    # Release specific files
    normalized_files = [normalize_path(f) for f in files]
    released = []
    still_held = []

    for file_path in list(file_locks.keys()):
        lock_info = file_locks[file_path]
        if lock_info.get("session_id") != session_id:
            continue  # Not our lock

        if file_path in normalized_files:
            del file_locks[file_path]
            released.append(file_path)
        else:
            still_held.append(file_path)

    return json.dumps({
        "released": released,
        "still_held": still_held
    })


@mcp.tool()
async def memory_get_locks(
    session_id: str,
    path_pattern: str = None,
    ctx: Context = None
) -> str:
    """
    View current file locks.

    Use this to see what files are locked before deciding what to work on.

    Args:
        session_id: Your session ID
        path_pattern: Optional glob pattern to filter (e.g., "NimbusCommon/*")

    Returns:
        {
            "locks": [...],       # List of current locks with stale flags
            "total_count": N,
            "your_locks": [...]   # Locks held by your session
        }
    """
    error = require_session(session_id)
    if error:
        return error

    # Update session activity
    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()

    all_locks = []
    your_locks = []

    for file_path, lock_info in file_locks.items():
        # Filter by pattern if specified
        if path_pattern and not path_matches_pattern(file_path, path_pattern):
            continue

        lock_data = {
            "file": file_path,
            "held_by": lock_info.get("claude_instance"),
            "session_id": lock_info.get("session_id"),
            "since": lock_info.get("locked_at"),
            "reason": lock_info.get("reason"),
            "stale": is_lock_stale(lock_info)
        }

        if lock_info.get("session_id") == session_id:
            your_locks.append(lock_data)
        else:
            all_locks.append(lock_data)

    return json.dumps({
        "locks": all_locks,
        "your_locks": your_locks,
        "total_count": len(all_locks) + len(your_locks)
    })


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
    interface_name: str = None,
    interface_version: str = None,
    interface_owner: str = None,
    interface_schema: Dict = None,
    expires_in_days: int = None,
    force_store: bool = False,
    ctx: Context = None
) -> str:
    """
    Store a new memory in the knowledge base.

    Use this for:
    - API specs, architecture docs (project-specific)
    - Code snippets and solutions (can be shared)
    - Task context and notes
    - Interface contracts (with schema validation)

    For quick learnings, use memory_record_learning instead.

    Args:
        session_id: Your session ID
        title: Title for this memory
        content: Content (markdown supported, max 50KB)
        memory_type: Type of memory (api_spec, architecture, learning, pattern, code_snippet, interface, etc.)
        project: Project this belongs to (omit for shared/cross-project memories)
        tags: Tags for categorization
        files_related: File paths this memory relates to
        interface_name: For interfaces - unique name (e.g., "mqtt:frame-status")
        interface_version: For interfaces - version string (e.g., "1.2")
        interface_owner: For interfaces - owning team/agent (e.g., "frames-team")
        interface_schema: For interfaces - JSON schema dict for validation
        expires_in_days: Custom expiry (default: 90 for learnings, never for architecture)
        force_store: Set True to store even if duplicate detected
    """
    error = require_session(session_id)
    if error:
        return error

    if memory_type not in MEMORY_TYPES:
        return json.dumps({"error": f"Invalid memory_type. Must be one of: {MEMORY_TYPES}"}, indent=2)

    # Check content size limit
    if len(content.encode('utf-8')) > MAX_CONTENT_SIZE:
        return json.dumps({
            "error": f"Content exceeds maximum size of {MAX_CONTENT_SIZE // 1024}KB",
            "size": f"{len(content.encode('utf-8')) // 1024}KB",
            "suggestion": "Break into smaller documents or summarize"
        })

    tags = tags or []
    files_related = files_related or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Determine collection
    if project:
        collection = await get_project_collection(chroma, project)
        location = f"project:{project}"
    else:
        if memory_type in ["pattern", "code_snippet", "solution", "interface"]:
            collection = await get_shared_collection(chroma, "patterns")
            location = "shared:patterns"
        else:
            collection = await get_shared_collection(chroma, "context")
            location = "shared:context"

    # Check for duplicates (unless force_store or interface update)
    duplicate_warning = None
    if not force_store and not (memory_type == "interface" and interface_name):
        duplicate = await check_duplicate(collection, content)
        if duplicate:
            if duplicate["type"] == "exact":
                return json.dumps({
                    "error": "Exact duplicate already exists",
                    "existing_doc_id": duplicate["doc_id"],
                    "existing_title": duplicate["title"],
                    "suggestion": "Use force_store=True to store anyway, or update the existing doc"
                })
            else:
                # Near-duplicate - warn but allow
                duplicate_warning = f"Similar doc exists: '{duplicate['title']}' ({duplicate['similarity']} similar)"

    # For interfaces with a name, use that as the doc_id for easy updates
    if memory_type == "interface" and interface_name:
        doc_id = f"interface_{interface_name.replace(':', '_').replace('/', '_')}"
    else:
        doc_id = generate_doc_id(content, memory_type)

    # Calculate expiry date
    expires_at = calculate_expiry(memory_type, expires_in_days)

    # Generate content hash for future duplicate detection
    content_hash = generate_content_hash(content)

    # Check for overlaps if files are specified
    overlap_warning = ""
    if files_related:
        overlaps = await check_overlap(chroma, project or "shared", files_related, session_id)
        overlap_warning = format_overlap_warning(overlaps, session_info.get("claude_instance"))

    # Build metadata
    metadata = {
        "title": title,
        "type": memory_type,
        "status": "active",
        "tags": json.dumps(tags),
        "files_related": json.dumps(files_related),
        "session_id": session_id,
        "claude_instance": session_info["claude_instance"],
        "project": project or "",
        "created": now,
        "updated": now,
        "content_hash": content_hash,
        "access_count": 0,
        "last_accessed": now
    }

    # Add expiry if applicable
    if expires_at:
        metadata["expires_at"] = expires_at

    # Add interface-specific fields
    if memory_type == "interface":
        if interface_name:
            metadata["interface_name"] = interface_name
        if interface_version:
            metadata["interface_version"] = interface_version
        if interface_owner:
            metadata["interface_owner"] = interface_owner
        if interface_schema:
            metadata["interface_schema"] = json.dumps(interface_schema)

    # Use upsert for interfaces (allows updates)
    if memory_type == "interface" and interface_name:
        await collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata]
        )
    else:
        await collection.add(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata]
        )

    result = {"status": "stored", "id": doc_id[:12]}
    if memory_type == "interface" and interface_name:
        result["interface_name"] = interface_name
        result["interface_version"] = interface_version
    if expires_at:
        result["expires_at"] = expires_at
    if overlap_warning:
        result["overlap_warning"] = overlap_warning
    if duplicate_warning:
        result["duplicate_warning"] = duplicate_warning
    return json.dumps(result)


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
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    if project:
        collection = await get_project_collection(chroma, project)
        location = f"project:{project}"
    else:
        collection = await get_shared_collection(chroma, "patterns")
        location = "shared:patterns"

    doc_id = f"learning_{generate_doc_id(title, 'learning')}"

    await collection.add(
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

    return json.dumps({"status": "recorded", "id": doc_id[:12]})


@mcp.tool()
async def memory_update_work(
    session_id: str,
    title: str,
    status: str,
    files_touched: List[str] = None,
    notes: str = None,
    blocked_by: str = None,
    blocked_reason: str = None,
    waiting_for_signal: str = None,
    signals: List[str] = None,
    signal_details: str = None,
    ctx: Context = None
) -> str:
    """
    Update your current work status and files touched.

    Call this periodically to:
    - Let other Claudes know what you're working on
    - Enable overlap detection (warns if another Claude touches same files)
    - Track progress on your task
    - Signal dependencies (blocked_by) or completion (signals)

    Args:
        session_id: Your session ID
        title: What you're working on
        status: Current status (in_progress, blocked, completed, abandoned)
        files_touched: Files you've touched (for overlap detection)
        notes: Additional context
        blocked_by: Agent ID you're waiting for (e.g., "frames-team")
        blocked_reason: What you need from them
        waiting_for_signal: Signal name you're waiting for (e.g., "status-schema-ready")
        signals: Signals to broadcast on completion (e.g., ["status-schema-ready"])
        signal_details: Additional context for signals
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in WORK_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {WORK_STATUSES}"}, indent=2)

    files_touched = files_touched or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Update session
    active_sessions[session_id]["last_activity"] = now
    active_sessions[session_id]["task"] = title

    # Update blocker info in session
    if blocked_by:
        active_sessions[session_id]["blocked_by"] = blocked_by
        active_sessions[session_id]["blocked_reason"] = blocked_reason
        active_sessions[session_id]["waiting_for_signal"] = waiting_for_signal
    elif status != "blocked":
        # Clear blocker info if not blocked
        active_sessions[session_id]["blocked_by"] = None
        active_sessions[session_id]["blocked_reason"] = None
        active_sessions[session_id]["waiting_for_signal"] = None

    # Broadcast signals if provided (typically on completion)
    signals_broadcast = []
    if signals:
        for signal_name in signals:
            active_signals[signal_name] = {
                "from_session": session_id,
                "from_claude": session_info["claude_instance"],
                "timestamp": now,
                "details": signal_details or ""
            }
            signals_broadcast.append(signal_name)

    # Check for overlaps
    overlap_warning = ""
    if files_touched:
        overlaps = await check_overlap(chroma, session_info["project"], files_touched, session_id)
        overlap_warning = format_overlap_warning(overlaps, session_info.get("claude_instance"))

    # Store/update work item
    work_collection = await get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"

    content = f"{title}\n\n{notes or ''}"

    await work_collection.upsert(
        ids=[work_id],
        documents=[content],
        metadatas=[{
            "title": title,
            "status": status,
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_touched),
            "blocked_by": blocked_by or "",
            "blocked_reason": blocked_reason or "",
            "waiting_for_signal": waiting_for_signal or "",
            "created": session_info["started"],
            "updated": now
        }]
    )

    result = {"status": "updated", "work": status}
    if overlap_warning:
        result["warning"] = overlap_warning
    if signals_broadcast:
        result["signals_broadcast"] = signals_broadcast
    return json.dumps(result)


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

    chroma = await get_chroma()
    now = datetime.now().isoformat()

    # Find the document
    if project:
        collection = await get_project_collection(chroma, project)
    else:
        collection = None
        for shared_name in ["patterns", "context"]:
            shared = await get_shared_collection(chroma, shared_name)
            result = await shared.get(ids=[doc_id])
            if result["ids"]:
                collection = shared
                break

        if not collection:
            return json.dumps({"error": f"Document not found: {doc_id}"}, indent=2)

    result = await collection.get(ids=[doc_id], include=["documents", "metadatas"])

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

    await collection.update(
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
async def memory_archive_by_tag(
    session_id: str,
    tag: str,
    reason: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Bulk archive all documents with a specific tag.

    Perfect for:
    - End of version cleanup (tag="v5" when moving to v6)
    - Feature completion (tag="oauth-feature" when shipped)
    - Sprint cleanup (tag="sprint-42")

    Archived docs are excluded from normal searches but can be restored.

    Args:
        session_id: Your session ID
        tag: Tag to match (e.g., "v5", "oauth-feature")
        reason: Why archiving (e.g., "Moving to v6", "Feature shipped")
        project: Limit to specific project (omit for all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    now = datetime.now().isoformat()
    archived_count = 0
    archived_docs = []

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    for col in collections:
        if project:
            # Only the specified project
            if col.name == f"{PROJECT_PREFIX}{project.lower().replace('-', '_')}":
                target_collections.append(col)
        else:
            # All project and shared collections
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    for col in target_collections:
        try:
            # Get all docs - we'll filter by tag in Python since Chroma's JSON querying is limited
            results = await col.get(include=["metadatas"])

            for i, meta in enumerate(results["metadatas"]):
                doc_id = results["ids"][i]

                # Check if tag matches (tags stored as JSON string)
                tags = json.loads(meta.get("tags", "[]"))
                if tag not in tags:
                    continue

                # Skip already archived
                if meta.get("status") == "archived":
                    continue

                # Archive it
                meta["status"] = "archived"
                meta["archived_at"] = now
                meta["archived_by"] = session_id
                meta["archive_reason"] = reason or f"Bulk archive by tag: {tag}"
                meta["previous_status"] = meta.get("status", "active")

                await col.update(ids=[doc_id], metadatas=[meta])
                archived_count += 1
                archived_docs.append({
                    "id": doc_id[:12],
                    "title": meta.get("title", "Untitled"),
                    "collection": col.name
                })
        except Exception as e:
            continue

    return json.dumps({
        "status": "completed",
        "tag": tag,
        "archived_count": archived_count,
        "archived_docs": archived_docs[:20],  # Limit output
        "note": f"Archived {archived_count} docs. Use memory_restore_by_tag to undo."
    }, indent=2)


@mcp.tool()
async def memory_restore_by_tag(
    session_id: str,
    tag: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Bulk restore archived documents with a specific tag.

    Use this to bring back previously archived version/feature docs.

    Args:
        session_id: Your session ID
        tag: Tag to match (e.g., "v5", "oauth-feature")
        project: Limit to specific project (omit for all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    now = datetime.now().isoformat()
    restored_count = 0
    restored_docs = []

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    for col in collections:
        if project:
            if col.name == f"{PROJECT_PREFIX}{project.lower().replace('-', '_')}":
                target_collections.append(col)
        else:
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    for col in target_collections:
        try:
            # Get archived docs
            results = await col.get(
                where={"status": "archived"},
                include=["metadatas"]
            )

            for i, meta in enumerate(results["metadatas"]):
                doc_id = results["ids"][i]

                # Check if tag matches
                tags = json.loads(meta.get("tags", "[]"))
                if tag not in tags:
                    continue

                # Restore to previous status or active
                previous_status = meta.get("previous_status", "active")
                meta["status"] = previous_status
                meta["restored_at"] = now
                meta["restored_by"] = session_id

                await col.update(ids=[doc_id], metadatas=[meta])
                restored_count += 1
                restored_docs.append({
                    "id": doc_id[:12],
                    "title": meta.get("title", "Untitled"),
                    "collection": col.name
                })
        except Exception:
            continue

    return json.dumps({
        "status": "completed",
        "tag": tag,
        "restored_count": restored_count,
        "restored_docs": restored_docs[:20],
        "note": f"Restored {restored_count} docs to their previous status."
    }, indent=2)


# =============================================================================
# Backlog Tools
# =============================================================================

@mcp.tool()
async def memory_add_backlog_item(
    session_id: str,
    title: str,
    description: str,
    priority: str = "medium",
    project: str = None,
    assigned_to: str = None,
    tags: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Add an item to the backlog for future work.

    Use this to track:
    - Features to implement later
    - Tech debt to address
    - Ideas to explore
    - Tasks for other agents

    Args:
        session_id: Your session ID
        title: Short title for the backlog item
        description: Detailed description of what needs to be done
        priority: Priority level (critical, high, medium, low) - default medium
        project: Project this belongs to (omit for cross-project items)
        assigned_to: Agent/team this is assigned to (e.g., "triage-team", "gmail-team")
        tags: Tags for categorization (e.g., ["tech-debt", "v7"])
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})

    tags = tags or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Store in project collection if specified, otherwise shared
    if project:
        collection = await get_project_collection(chroma, project)
    else:
        collection = await get_shared_collection(chroma, "work")

    # Generate ID
    backlog_id = f"backlog_{hashlib.sha256(f'{title}:{now}'.encode()).hexdigest()[:12]}"

    content = f"# {title}\n\n{description}"

    metadata = {
        "title": title,
        "type": "backlog",
        "backlog_status": "open",
        "priority": priority,
        "project": project or "",
        "assigned_to": assigned_to or "",
        "tags": json.dumps(tags),
        "created_by": session_info["claude_instance"],
        "created": now,
        "updated": now
    }

    await collection.add(
        ids=[backlog_id],
        documents=[content],
        metadatas=[metadata]
    )

    return json.dumps({
        "status": "added",
        "id": backlog_id,
        "title": title,
        "priority": priority,
        "project": project or "shared",
        "assigned_to": assigned_to
    })


@mcp.tool()
async def memory_list_backlog(
    session_id: str,
    project: str = None,
    status: str = None,
    priority: str = None,
    assigned_to: str = None,
    include_done: bool = False,
    ctx: Context = None
) -> str:
    """
    List backlog items with optional filters.

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects + shared)
        status: Filter by status (open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info)
        priority: Filter by priority (critical, high, medium, low)
        assigned_to: Filter by assignee
        include_done: Include completed items (default False)
    """
    error = require_session(session_id)
    if error:
        return error

    if status and status not in BACKLOG_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {BACKLOG_STATUSES}"})
    if priority and priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})

    chroma = await get_chroma()
    items = []

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    for col in collections:
        if project:
            if col.name == f"{PROJECT_PREFIX}{project.lower().replace('-', '_')}":
                target_collections.append(col)
        else:
            # All project and shared collections
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    # Priority order for sorting
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    for col in target_collections:
        try:
            # Get all backlog items
            results = await col.get(
                where={"type": "backlog"},
                include=["metadatas", "documents"]
            )

            for i, meta in enumerate(results["metadatas"]):
                item_status = meta.get("backlog_status", "open")
                item_priority = meta.get("priority", "medium")
                item_assigned = meta.get("assigned_to", "")

                # Apply filters
                if status and item_status != status:
                    continue
                if priority and item_priority != priority:
                    continue
                if assigned_to and item_assigned != assigned_to:
                    continue
                if not include_done and item_status in ["done", "wont_do"]:
                    continue

                items.append({
                    "id": results["ids"][i],
                    "title": meta.get("title", "Untitled"),
                    "status": item_status,
                    "priority": item_priority,
                    "priority_order": priority_order.get(item_priority, 99),
                    "project": meta.get("project", "shared"),
                    "assigned_to": item_assigned or None,
                    "created_by": meta.get("created_by", "unknown"),
                    "created": meta.get("created"),
                    "updated": meta.get("updated"),
                    "tags": json.loads(meta.get("tags", "[]"))
                })
        except Exception:
            continue

    # Sort by priority (critical first), then by created date
    items.sort(key=lambda x: (x["priority_order"], x["created"]))

    # Remove priority_order from output
    for item in items:
        del item["priority_order"]

    return json.dumps({
        "count": len(items),
        "items": items
    }, indent=2)


@mcp.tool()
async def memory_update_backlog_item(
    session_id: str,
    item_id: str,
    status: str = None,
    priority: str = None,
    assigned_to: str = None,
    title: str = None,
    description: str = None,
    ctx: Context = None
) -> str:
    """
    Update a backlog item's status, priority, or assignment.

    Args:
        session_id: Your session ID
        item_id: The backlog item ID
        status: New status (open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info)
        priority: New priority (critical, high, medium, low)
        assigned_to: New assignee (use empty string to unassign)
        title: New title
        description: New description
    """
    error = require_session(session_id)
    if error:
        return error

    if status and status not in BACKLOG_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {BACKLOG_STATUSES}"})
    if priority and priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Search all collections for this item
    collections = await chroma.list_collections()
    found = False

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[item_id], include=["metadatas", "documents"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                # Update fields
                if status:
                    meta["backlog_status"] = status
                if priority:
                    meta["priority"] = priority
                if assigned_to is not None:
                    meta["assigned_to"] = assigned_to
                if title:
                    meta["title"] = title
                    # Update document too
                    doc = f"# {title}\n\n" + doc.split("\n\n", 1)[-1] if "\n\n" in doc else f"# {title}\n\n{doc}"
                if description:
                    doc = f"# {meta['title']}\n\n{description}"

                meta["updated"] = now
                meta["updated_by"] = session_info["claude_instance"]

                await col.update(
                    ids=[item_id],
                    documents=[doc] if (title or description) else None,
                    metadatas=[meta]
                )

                found = True
                return json.dumps({
                    "status": "updated",
                    "id": item_id,
                    "title": meta["title"],
                    "backlog_status": meta.get("backlog_status"),
                    "priority": meta.get("priority"),
                    "assigned_to": meta.get("assigned_to") or None
                })
        except Exception:
            continue

    if not found:
        return json.dumps({"error": f"Backlog item not found: {item_id}"})


@mcp.tool()
async def memory_complete_backlog_item(
    session_id: str,
    item_id: str,
    resolution: str = None,
    wont_do: bool = False,
    ctx: Context = None
) -> str:
    """
    Mark a backlog item as completed or won't do.

    Args:
        session_id: Your session ID
        item_id: The backlog item ID
        resolution: Optional notes about how it was resolved
        wont_do: If True, marks as "wont_do" instead of "done"
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()

    # Search all collections for this item
    collections = await chroma.list_collections()

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[item_id], include=["metadatas", "documents"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                new_status = "wont_do" if wont_do else "done"
                meta["backlog_status"] = new_status
                meta["completed_at"] = now
                meta["completed_by"] = session_info["claude_instance"]
                if resolution:
                    meta["resolution"] = resolution
                    doc += f"\n\n## Resolution\n{resolution}"

                meta["updated"] = now

                await col.update(
                    ids=[item_id],
                    documents=[doc],
                    metadatas=[meta]
                )

                return json.dumps({
                    "status": new_status,
                    "id": item_id,
                    "title": meta["title"],
                    "completed_by": session_info["claude_instance"],
                    "resolution": resolution
                })
        except Exception:
            continue

    return json.dumps({"error": f"Backlog item not found: {item_id}"})


# =============================================================================
# Inter-Claude Messaging Tools
# =============================================================================

@mcp.tool()
async def memory_send_message(
    session_id: str,
    to_instance: str,
    message: str,
    priority: str = "normal",
    ctx: Context = None
) -> str:
    """
    Send a message to another Claude instance.

    Messages are queued and delivered via tmux injection when the target
    is available. Use this for coordination between Claude agents.

    Args:
        session_id: Your session ID
        to_instance: Target Claude instance name (e.g., 'frontend', 'backend', or '*' for all)
        message: The message to send
        priority: Message priority (urgent, normal, low) - urgent interrupts, others wait
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in MESSAGE_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {MESSAGE_PRIORITIES}"})

    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    message_queue[message_id] = {
        "id": message_id,
        "to": to_instance,
        "from_instance": session_info["claude_instance"],
        "from_session": session_id,
        "message": message,
        "priority": priority,
        "created": now,
        "delivered": False,
        "acknowledged": False
    }

    return json.dumps({
        "status": "queued",
        "message_id": message_id,
        "to": to_instance,
        "priority": priority
    })


@mcp.tool()
async def memory_get_messages(
    session_id: str,
    include_delivered: bool = False,
    ctx: Context = None
) -> str:
    """
    Get pending messages for your Claude instance.

    Returns messages sent to you by other Claudes or the orchestrator.

    Args:
        session_id: Your session ID
        include_delivered: Include already delivered messages (default False)
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    my_instance = session_info["claude_instance"]

    messages = []
    for msg_id, msg in message_queue.items():
        # Message is for me or broadcast
        if msg["to"] == my_instance or msg["to"] == "*":
            # Skip delivered unless requested
            if msg["delivered"] and not include_delivered:
                continue
            messages.append({
                "id": msg_id,
                "from": msg["from_instance"],
                "message": msg["message"],
                "priority": msg["priority"],
                "created": msg["created"],
                "delivered": msg["delivered"]
            })

    # Sort by priority (urgent first), then by created
    priority_order = {"urgent": 0, "normal": 1, "low": 2}
    messages.sort(key=lambda x: (priority_order.get(x["priority"], 99), x["created"]))

    return json.dumps({
        "count": len(messages),
        "messages": messages
    })


@mcp.tool()
async def memory_acknowledge_message(
    session_id: str,
    message_id: str,
    ctx: Context = None
) -> str:
    """
    Acknowledge receipt of a message.

    Call this after processing a message to mark it handled.

    Args:
        session_id: Your session ID
        message_id: The message ID to acknowledge
    """
    error = require_session(session_id)
    if error:
        return error

    if message_id not in message_queue:
        return json.dumps({"error": f"Message not found: {message_id}"})

    message_queue[message_id]["acknowledged"] = True
    message_queue[message_id]["acknowledged_at"] = datetime.now().isoformat()

    return json.dumps({
        "status": "acknowledged",
        "message_id": message_id
    })


def get_tmux_target_for_instance(instance_name: str) -> str:
    """Find tmux target for a Claude instance from active sessions."""
    for session_id, info in active_sessions.items():
        if info["claude_instance"] == instance_name and info.get("tmux_target"):
            return info["tmux_target"]
    return None


def get_pending_messages_for_instance(instance_name: str) -> List[Dict]:
    """Get undelivered messages for a specific instance."""
    messages = []
    for msg_id, msg in message_queue.items():
        if (msg["to"] == instance_name or msg["to"] == "*") and not msg["delivered"]:
            messages.append(msg)
    return messages


# =============================================================================
# Function Reference Tools (AI-Optimized Code Documentation)
# =============================================================================

# Enrichment queue for librarian processing (in-memory, processed async)
# Structure: { func_id: { project, file, name, registered_at, enriched: bool } }
function_enrichment_queue: Dict[str, Dict[str, Any]] = {}

# Librarian webhook URL (set to None to disable)
LIBRARIAN_WEBHOOK_URL = os.getenv("LIBRARIAN_WEBHOOK_URL", "http://localhost:8085/webhook")


async def notify_librarian(func_info: Dict[str, Any]):
    """Send webhook notification to librarian service."""
    if not LIBRARIAN_WEBHOOK_URL:
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                LIBRARIAN_WEBHOOK_URL,
                json={
                    "event": "function_registered",
                    "function": func_info
                }
            )
    except Exception as e:
        # Don't fail the registration if librarian is down
        print(f"[MCP] Librarian webhook failed (non-fatal): {e}")


@mcp.tool()
async def memory_register_function(
    session_id: str,
    name: str,
    file: str,
    purpose: str,
    project: str = None,
    gotchas: str = None,
    prefer_over: str = None,
    requires: List[str] = None,
    code: str = None,
    ctx: Context = None
) -> str:
    """
    Register a function for AI-optimized reference.

    MINIMAL INPUT - just register what you know, librarian enriches the rest.

    For simple functions:
        memory_register_function(name="get_user", file="src/users.py:45", purpose="Fetch user by ID")

    For tricky/weird functions, include code for librarian analysis:
        memory_register_function(name="parse_email", file="src/parser.py:145",
            purpose="Parse raw email", gotchas="Use over v1 - attachment bug", code="def parse_email...")

    Args:
        session_id: Your session ID
        name: Function name
        file: File path with line number (e.g., "src/parser.py:145")
        purpose: One-line description of what this function does
        project: Project this belongs to (omit for shared/cross-project)
        gotchas: Non-obvious behaviors, pitfalls, or warnings (optional)
        prefer_over: Other functions this should be used instead of (optional)
        requires: Functions/setup that must be called first (optional)
        code: Full function code - include for tricky functions so librarian can analyze (optional)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = datetime.now().isoformat()
    requires = requires or []

    # Determine collection
    if project:
        collection = await get_project_collection(chroma, project)
    else:
        collection = await get_shared_collection(chroma, "patterns")

    # Generate stable ID based on project + file + name
    id_base = f"{project or 'shared'}:{file}:{name}"
    func_id = f"func_{hashlib.sha256(id_base.encode()).hexdigest()[:12]}"

    # Check if function already exists and preserve enrichment data
    existing = await collection.get(ids=[func_id], include=["metadatas", "documents"])
    existing_meta = existing["metadatas"][0] if existing["metadatas"] else None
    existing_doc = existing["documents"][0] if existing["documents"] else None
    is_update = existing_meta is not None

    # Build the document content (AI-readable format)
    doc_parts = [
        f"# Function: {name}",
        f"**Location:** {file}",
        f"**Purpose:** {purpose}"
    ]

    if gotchas:
        doc_parts.append(f"**Gotchas:** {gotchas}")
    if prefer_over:
        doc_parts.append(f"**Prefer over:** {prefer_over}")
    if requires:
        doc_parts.append(f"**Requires:** {', '.join(requires)}")
    if code:
        doc_parts.append(f"\n**Code:**\n```\n{code}\n```")

    content = "\n\n".join(doc_parts)

    # Build metadata - preserve enrichment fields if they exist
    metadata = {
        "title": f"{name} - {purpose[:50]}",
        "type": "function_ref",
        "status": "active",
        "func_name": name,
        "func_file": file,
        "func_purpose": purpose,
        "project": project or "",
        "session_id": session_id,
        "claude_instance": session_info["claude_instance"],
        "created": existing_meta.get("created", now) if existing_meta else now,
        "updated": now,
        "enriched": existing_meta.get("enriched", "false") if existing_meta else "false",
        "has_code": "true" if code else "false",
        "access_count": existing_meta.get("access_count", 0) if existing_meta else 0,
        "last_accessed": now
    }

    # Preserve librarian enrichment fields if they exist
    if existing_meta:
        enrichment_fields = ["signature", "parameters", "returns", "calls",
                           "side_effects", "complexity", "search_summary"]
        for field in enrichment_fields:
            if field in existing_meta:
                metadata[field] = existing_meta[field]

        # Preserve search_summary in document content if it exists
        if existing_meta.get("search_summary") and existing_meta.get("enriched") == "true":
            content = f"**Search Summary:** {existing_meta['search_summary']}\n\n" + content

    if gotchas:
        metadata["gotchas"] = gotchas
    if prefer_over:
        metadata["prefer_over"] = prefer_over
    if requires:
        metadata["requires"] = json.dumps(requires)

    # Upsert (allows updates to same function)
    await collection.upsert(
        ids=[func_id],
        documents=[content],
        metadatas=[metadata]
    )

    # Add to enrichment queue for librarian
    queue_entry = {
        "id": func_id,
        "project": project,
        "file": file,
        "name": name,
        "purpose": purpose,
        "gotchas": gotchas,
        "has_code": bool(code),
        "registered_at": now,
        "enriched": False
    }
    function_enrichment_queue[func_id] = queue_entry

    # Notify librarian service (async, non-blocking)
    asyncio.create_task(notify_librarian(queue_entry))

    result = {
        "status": "updated" if is_update else "registered",
        "id": func_id,
        "name": name,
        "file": file,
        "queued_for_enrichment": True,
        "preserved_enrichment": is_update and existing_meta.get("enriched") == "true"
    }

    if is_update and existing_meta.get("enriched") == "true":
        result["note"] = "Updated - preserved existing librarian enrichment"
    elif code:
        result["note"] = "Code included - librarian will perform deep analysis"
    else:
        result["note"] = "Basic registration - librarian will enrich if file accessible"

    return json.dumps(result)


@mcp.tool()
async def memory_find_function(
    session_id: str,
    query: str,
    project: str = None,
    include_shared: bool = True,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Find functions by purpose, name, or description.

    Use this BEFORE implementing something to check:
    - Does a function for this already exist?
    - Which function should I use for X?
    - Are there gotchas I should know about?

    Args:
        session_id: Your session ID
        query: What you're looking for (e.g., "parse email", "user authentication", "database connection")
        project: Limit to specific project (omit to search project + shared)
        include_shared: Include cross-project functions (default True)
        limit: Maximum results (default 5)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()
    session_info = active_sessions[session_id]

    results = []
    where_filter = {"$and": [{"type": "function_ref"}, {"status": "active"}]}

    # Search project collection
    search_project = project or session_info.get("project")
    if search_project:
        try:
            proj_collection = await get_project_collection(chroma, search_project)
            proj_results = await proj_collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                )):
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    doc_id = proj_results["ids"][0][i]
                    results.append({
                        "source": f"project:{search_project}",
                        "id": doc_id,
                        "name": meta.get("func_name"),
                        "file": meta.get("func_file"),
                        "purpose": meta.get("func_purpose"),
                        "gotchas": meta.get("gotchas"),
                        "prefer_over": meta.get("prefer_over"),
                        "requires": json.loads(meta.get("requires", "[]")),
                        "relevance": f"{relevance:.0%}",
                        "enriched": meta.get("enriched") == "true",
                        "access_count": meta.get("access_count", 0)
                    })

                    # Track access
                    await update_access_stats(proj_collection, doc_id)
        except Exception as e:
            print(f"[memory_find_function] Error searching project collection: {e}")

    # Search shared collection
    if include_shared:
        try:
            shared = await get_shared_collection(chroma, "patterns")
            shared_results = await shared.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter
            )

            if shared_results["documents"] and shared_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    shared_results["documents"][0],
                    shared_results["metadatas"][0],
                    shared_results["distances"][0]
                )):
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    doc_id = shared_results["ids"][0][i]
                    results.append({
                        "source": "shared",
                        "id": doc_id,
                        "name": meta.get("func_name"),
                        "file": meta.get("func_file"),
                        "purpose": meta.get("func_purpose"),
                        "gotchas": meta.get("gotchas"),
                        "prefer_over": meta.get("prefer_over"),
                        "requires": json.loads(meta.get("requires", "[]")),
                        "relevance": f"{relevance:.0%}",
                        "enriched": meta.get("enriched") == "true",
                        "access_count": meta.get("access_count", 0)
                    })

                    await update_access_stats(shared, doc_id)
        except Exception as e:
            print(f"[memory_find_function] Error searching shared collection: {e}")

    # Sort by relevance
    results.sort(key=lambda x: x["relevance"], reverse=True)
    results = results[:limit]

    # Clean up None values for cleaner output
    for r in results:
        r = {k: v for k, v in r.items() if v is not None and v != [] and v != ""}

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching functions found. Consider registering functions as you write them!"
        })

    return json.dumps({
        "query": query,
        "result_count": len(results),
        "results": results
    }, indent=2)


@mcp.tool()
async def memory_get_enrichment_queue(
    session_id: str,
    ctx: Context = None
) -> str:
    """
    Get pending function references awaiting librarian enrichment.

    For librarian use - returns functions that need code analysis.

    Args:
        session_id: Your session ID
    """
    error = require_session(session_id)
    if error:
        return error

    pending = [
        {
            "id": func_id,
            "name": info["name"],
            "file": info["file"],
            "project": info["project"],
            "has_code": info["has_code"],
            "registered_at": info["registered_at"]
        }
        for func_id, info in function_enrichment_queue.items()
        if not info["enriched"]
    ]

    return json.dumps({
        "pending_count": len(pending),
        "items": pending
    }, indent=2)


@mcp.tool()
async def memory_enrich_function(
    session_id: str,
    func_id: str,
    signature: str = None,
    parameters: List[Dict] = None,
    returns: str = None,
    calls: List[str] = None,
    called_by: List[str] = None,
    side_effects: List[str] = None,
    complexity: str = None,
    additional_gotchas: str = None,
    search_summary: str = None,
    ctx: Context = None
) -> str:
    """
    Enrich a function reference with analyzed details.

    For librarian use - adds deep analysis to existing function refs.

    Args:
        session_id: Your session ID
        func_id: Function reference ID to enrich
        signature: Full function signature
        parameters: List of {name, type, description} dicts
        returns: Return type and description
        calls: Functions this calls internally
        called_by: Functions that call this
        side_effects: Side effects (file I/O, network, state mutation)
        complexity: Performance/complexity notes
        additional_gotchas: Extra gotchas discovered during analysis
        search_summary: Rich description for semantic search (generated by librarian)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    now = datetime.now().isoformat()

    # Find the function in any collection
    collections = await chroma.list_collections()

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[func_id], include=["documents", "metadatas"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                # Add enrichment data to document
                enrichment_parts = []
                if signature:
                    enrichment_parts.append(f"**Signature:** `{signature}`")
                if parameters:
                    param_lines = [f"  - `{p['name']}`: {p.get('type', 'any')} - {p.get('description', '')}"
                                   for p in parameters]
                    enrichment_parts.append("**Parameters:**\n" + "\n".join(param_lines))
                if returns:
                    enrichment_parts.append(f"**Returns:** {returns}")
                if calls:
                    enrichment_parts.append(f"**Calls:** {', '.join(calls)}")
                if called_by:
                    enrichment_parts.append(f"**Called by:** {', '.join(called_by)}")
                if side_effects:
                    enrichment_parts.append(f"**Side effects:** {', '.join(side_effects)}")
                if complexity:
                    enrichment_parts.append(f"**Complexity:** {complexity}")
                if additional_gotchas:
                    existing_gotchas = meta.get("gotchas", "")
                    if existing_gotchas:
                        meta["gotchas"] = f"{existing_gotchas}; {additional_gotchas}"
                    else:
                        meta["gotchas"] = additional_gotchas

                # Add search summary at the TOP of the document for better embedding
                if search_summary:
                    doc = f"**Search Summary:** {search_summary}\n\n" + doc
                    meta["search_summary"] = search_summary

                # Append enrichment to document
                if enrichment_parts:
                    doc += "\n\n## Librarian Analysis\n" + "\n\n".join(enrichment_parts)

                # Update metadata
                meta["enriched"] = "true"
                meta["enriched_at"] = now
                meta["updated"] = now
                if signature:
                    meta["signature"] = signature
                if calls:
                    meta["calls"] = json.dumps(calls)
                if called_by:
                    meta["called_by"] = json.dumps(called_by)
                if side_effects:
                    meta["side_effects"] = json.dumps(side_effects)

                await col.update(
                    ids=[func_id],
                    documents=[doc],
                    metadatas=[meta]
                )

                # Mark as enriched in queue
                if func_id in function_enrichment_queue:
                    function_enrichment_queue[func_id]["enriched"] = True

                return json.dumps({
                    "status": "enriched",
                    "id": func_id,
                    "name": meta.get("func_name"),
                    "enriched_at": now
                })
        except Exception:
            continue

    return json.dumps({"error": f"Function reference not found: {func_id}"})


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

    chroma = await get_chroma()
    all_results = []

    # Build where filter
    where_filter = {"status": "active"}
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    # Search all project collections
    collections = await chroma.list_collections()
    for col in collections:
        if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
            try:
                results = await col.query(
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
    chroma = await get_chroma()
    collections = await chroma.list_collections()

    projects = []
    shared = []

    for col in collections:
        if col.name.startswith(PROJECT_PREFIX):
            project_name = col.name[len(PROJECT_PREFIX):]
            try:
                count = await col.count()
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
                count = await col.count()
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
║       Shared Memory MCP Server for Multi-Claude v3.2         ║
║       + Function Reference System for AI code navigation     ║
╠══════════════════════════════════════════════════════════════╣
║  Endpoint: http://{args.host}:{args.port}/mcp (stateless HTTP)
║  Chroma:   {CHROMA_HOST}:{CHROMA_PORT}
║                                                              ║
║  Session Management:                                         ║
║    memory_start_session   - START HERE (gets locks/signals)  ║
║    memory_end_session     - Record work, release locks       ║
║                                                              ║
║  Function References (NEW):                                  ║
║    memory_register_function - Register func (minimal input)  ║
║    memory_find_function     - Find funcs by purpose/name     ║
║    memory_get_enrichment_queue - Librarian: pending items    ║
║    memory_enrich_function   - Librarian: add deep analysis   ║
║                                                              ║
║  File Locking:                                               ║
║    memory_lock_files      - Lock files for exclusive edit    ║
║    memory_unlock_files    - Release locks early              ║
║    memory_get_locks       - View current file locks          ║
║                                                              ║
║  Knowledge Base:                                             ║
║    memory_query           - Search (filters expired, tracks) ║
║    memory_store           - Store (50KB limit, dedup check)  ║
║    memory_record_learning - Quick learning (180 day expiry)  ║
║    memory_search_global   - Cross-project search             ║
║                                                              ║
║  Backlog:                                                    ║
║    memory_add_backlog_item     - Add task for later          ║
║    memory_list_backlog         - List tasks by filters       ║
║    memory_update_backlog_item  - Update status/priority      ║
║    memory_complete_backlog_item - Mark done/won't do         ║
║                                                              ║
║  Lifecycle & Coordination:                                   ║
║    memory_change_status   - Deprecate/supersede/archive      ║
║    memory_update_work     - Track work, blockers, signals    ║
║    memory_get_active_work - See work/blockers/signals        ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Configure MCP settings
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    # Add custom /health endpoint using FastMCP's custom_route decorator
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from starlette.responses import JSONResponse
        try:
            # Use the shared async client for health checks too
            # This avoids creating new connections
            chroma = await get_chroma()
            await chroma.heartbeat()
            chroma_status = "healthy"
        except Exception as e:
            chroma_status = f"unhealthy: {str(e)}"

        status = "healthy" if chroma_status == "healthy" else "degraded"
        return JSONResponse({
            "status": status,
            "chroma": chroma_status,
            "active_sessions": len(active_sessions),
            "active_locks": len(file_locks),
            "active_signals": len(active_signals)
        }, status_code=200 if status == "healthy" else 503)

    # Run with streamable HTTP transport (supports stateless mode)
    mcp.run(transport="streamable-http")
