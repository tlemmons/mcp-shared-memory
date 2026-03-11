"""Search and discovery tools - cross-project search, list projects."""

import json
from datetime import datetime
from typing import Optional, List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.state import active_sessions
from shared_memory.clients import get_chroma
from shared_memory.config import PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import require_session, get_shared_collection


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
