"""
Tool registration module.

Importing this package triggers all @mcp.tool() decorators,
registering every tool with the FastMCP server instance.
"""

from shared_memory.tools import (  # noqa: F401
    sessions,
    query,
    locking,
    storage,
    lifecycle,
    backlog,
    messaging,
    functions,
    search,
    specs,
    projects,
    checklists,
    database,
)
