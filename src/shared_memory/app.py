"""
MCP server instance and application setup.

Creates the FastMCP server instance that all tool modules register with.
"""

from mcp.server.fastmcp import FastMCP

from shared_memory.clients import app_lifespan


# Allow connections from any host (for remote access via IP or proxy)
# stateless_http=True fixes -32602 "request before initialization" errors
# See: https://github.com/GregBaugues/tokenbowl-mcp/issues/86
mcp = FastMCP("shared_memory", lifespan=app_lifespan, host="0.0.0.0", stateless_http=True)


def create_app():
    """Import all tool modules to trigger @mcp.tool() registration, then return mcp."""
    # These imports trigger the @mcp.tool() decorators which register tools with mcp
    import shared_memory.tools  # noqa: F401
    return mcp
