"""
Tool registration module.

Importing this package triggers all @mcp.tool() decorators,
registering every tool with the FastMCP server instance.
"""

from shared_memory.tools import (  # noqa: F401
    backlog,
    checklists,
    database,
    functions,
    guidelines,
    lifecycle,
    locking,
    messaging,
    projects,
    query,
    search,
    sessions,
    specs,
    storage,
)
