# Shared Memory MCP Server

## Claude Identity (REQUIRED - DO THIS FIRST)

**Your name is: `shared-memory`**

**IMMEDIATELY on session start, run these commands IN ORDER:**

1. Rename this session (for resume list):
```
/rename shared-memory
```

2. Set terminal title:
```bash
echo -ne "\033]0;[shared-memory] MCP Server\007"
```

3. Start shared memory session:
```python
memory_start_session(project="shared_memory", claude_instance="shared-memory",
    role_description="Shared memory MCP server - persistent knowledge base for all Claude instances across all projects")
memory_list_backlog(project="shared_memory", assigned_to="shared-memory")
memory_get_messages()
```

Do NOT ask the user for a name. Do NOT skip the /rename. You are `shared-memory`.

## What This Project Is

The shared memory MCP server - a persistent knowledge base used by all Claude instances across all projects. Built on MongoDB + ChromaDB.

## Key Files

- `server.py` - Main MCP server (all tool implementations)
- `librarian.py` - Standalone function enrichment daemon (uses Haiku)
- `deploy/` - Docker deployment configs
- `start.sh` - Service startup script

## Infrastructure

- **MongoDB:** localhost:27018 (mapped from 27017 in container)
- **ChromaDB:** localhost:8001
- **Librarian webhook:** localhost:8085
- **Systemd service:** `mcp-rag-arch`

## Common Operations

```bash
# Restart server
sudo systemctl restart mcp-rag-arch

# View logs
docker logs mcp-rag-arch

# Check health
systemctl status mcp-rag-arch
```

## Scope

Full development access to all files in this folder. This is the server itself - you maintain and improve it.
