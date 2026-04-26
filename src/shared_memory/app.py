"""
MCP server instance and application setup.

Creates the FastMCP server instance that all tool modules register with.
"""

from mcp.server.fastmcp import FastMCP

from shared_memory.clients import app_lifespan

# Allow connections from any host (for remote access via IP or proxy)
# stateless_http=False (the default): persistent client sessions, required
# for resource subscriptions (Phase C2 inbox://) and progress notifications.
# Original stateless_http=True was a workaround for an older FastMCP -32602
# "request before initialization" race; verified resolved in current FastMCP
# via concurrent client harness (2026-04-26, see /tmp/mcp_stress_harness.py).
mcp = FastMCP("shared_memory", lifespan=app_lifespan, host="0.0.0.0", stateless_http=False)


def create_app():
    """Import all tool modules to trigger @mcp.tool() registration, then return mcp."""
    # These imports trigger the @mcp.tool() decorators which register tools with mcp
    import shared_memory.tools  # noqa: F401
    return mcp
