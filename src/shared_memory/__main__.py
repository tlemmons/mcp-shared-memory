"""
Entry point for running as: python -m shared_memory

Usage:
    python -m shared_memory [--host HOST] [--port PORT] [--transport TRANSPORT]
"""

import argparse

from shared_memory.app import create_app
from shared_memory.auth import AUTH_ENABLED
from shared_memory.clients import get_chroma
from shared_memory.config import CHROMA_HOST, CHROMA_PORT
from shared_memory.state import active_sessions, active_signals, file_locks


def main():
    parser = argparse.ArgumentParser(description="Shared Memory MCP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport mode (default: streamable-http)",
    )
    args = parser.parse_args()

    mcp = create_app()

    if args.transport == "stdio":
        transport_line = "║  Transport: stdio"
    else:
        transport_line = f"║  Endpoint: http://{args.host}:{args.port}/mcp (stateless HTTP)"

    auth_line = f"║  Auth:     {'ENABLED (API key required)' if AUTH_ENABLED else 'disabled (open access)'}"

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       Shared Memory MCP Server v1.0.0                        ║
║       Multi-Agent Coordination + Knowledge Base              ║
╠══════════════════════════════════════════════════════════════╣
{transport_line}
║  Chroma:   {CHROMA_HOST}:{CHROMA_PORT}
{auth_line}
║                                                              ║
║  Session Management:                                         ║
║    memory_start_session   - START HERE (gets locks/signals)  ║
║    memory_end_session     - Record work, release locks       ║
║                                                              ║
║  Knowledge Base:                                             ║
║    memory_query / memory_store / memory_record_learning      ║
║    memory_search_global   - Cross-project search             ║
║                                                              ║
║  Coordination:                                               ║
║    memory_lock_files / memory_unlock_files / memory_get_locks║
║    memory_send_message / memory_get_messages                 ║
║    memory_heartbeat / memory_list_agents                     ║
║                                                              ║
║  Task Management:                                            ║
║    memory_add_backlog_item / memory_list_backlog             ║
║    memory_checklist (CRUD)                                   ║
║                                                              ║
║  Function References:                                        ║
║    memory_register_function / memory_find_function           ║
║                                                              ║
║  Specs & Registry:                                           ║
║    memory_define_spec / memory_get_spec / memory_list_specs  ║
║    memory_project (CRUD) / memory_db (read-only SQL)         ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Configure MCP settings
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    # Add custom /health endpoint
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from starlette.responses import JSONResponse
        try:
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

    # Run with the selected transport
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
