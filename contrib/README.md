# Experimental / Contrib

These components are experimental and not part of the core MCP server.

## orchestrator/

Claude-powered orchestration system for automatically spawning and coordinating
multiple Claude instances. Includes:
- `brain.py` - Decision-making engine (requires Anthropic API key)
- `dispatcher.py` - Message delivery via tmux injection
- `claude-spawn.py` - Claude instance launcher

Status: Early prototype. Not production-ready.

## ui/

Web dashboard for monitoring and managing orchestrated Claude sessions. Includes:
- `app.py` - FastAPI backend
- `tmux-proxy.py` - Terminal proxy for web-based tmux interaction
- `static/` - Frontend HTML

Status: Early prototype. Not production-ready.
