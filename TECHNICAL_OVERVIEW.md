# MCP Shared Memory Server - Technical Overview

## What It Is

A Model Context Protocol (MCP) server that provides persistent shared memory for Claude Code agents. Enables multiple AI agents to coordinate work, share knowledge, and avoid conflicts across projects.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Claude Code Agents                     │
│              (multiple concurrent sessions)              │
└─────────────────────┬───────────────────────────────────┘
                      │ MCP Protocol (HTTP + SSE)
                      ▼
┌─────────────────────────────────────────────────────────┐
│                    MCP Server                            │
│  - FastMCP (Python)                                      │
│  - Stateless HTTP transport                              │
│  - 23 tools exposed via MCP protocol                     │
│  - Session management (in-memory)                        │
│  - File locking coordination                             │
└─────────────────────┬───────────────────────────────────┘
                      │ Async HTTP
                      ▼
┌─────────────────────────────────────────────────────────┐
│                 ChromaDB (Vector Store)                  │
│  - Embedding-based semantic search                       │
│  - Collections per project + shared                      │
│  - Persistent Docker volume                              │
└─────────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Protocol | MCP (Model Context Protocol) via streamable-http |
| Server | FastMCP + Starlette + Uvicorn |
| Storage | ChromaDB with default embedding function |
| Deployment | Docker Compose (2 containers) |
| Language | Python 3.11 |

## Core Features

### 1. Session Management
- Agents register on start, end with summary
- Tracks active work per agent
- Detects file overlap between agents
- Handoff notes for context continuity

### 2. Knowledge Base
- **Types**: architecture, learning, pattern, code_snippet, api_spec, interface, function_ref
- **Lifecycle**: active → deprecated → superseded → archived
- **Search**: Semantic (embedding-based) + metadata filtering
- **Expiry**: Configurable TTL, auto-cleanup on query

### 3. Function References
- Agents register functions with minimal input (name, file, purpose)
- Optional librarian service enriches with deep analysis (signature, parameters, calls, side effects)
- Semantic search to find existing functions before implementing

### 4. File Locking
- Atomic lock acquisition
- Prevents concurrent edits to same files
- Auto-release on session end
- Stale lock detection (>30min inactive)

### 5. Backlog System
- Persistent task tracking across sessions
- Priority levels, status workflow, assignment
- Queryable by project, status, assignee

### 6. Signals & Coordination
- Agents can signal completion of work
- Other agents can wait for signals
- Enables async coordination patterns

## Data Model

```
Collections:
├── proj_{name}          # Project-specific memories
├── shared_patterns      # Cross-project patterns
├── shared_context       # Cross-project context
└── backlog             # Global task backlog

Document Metadata:
├── type                # Memory type
├── status              # Lifecycle status
├── project             # Project association
├── session_id          # Creating session
├── created/updated     # Timestamps
├── expires_at          # Optional TTL
├── access_count        # Usage tracking
└── [type-specific]     # Additional fields
```

## API (MCP Tools)

**Session**: `memory_start_session`, `memory_end_session`, `memory_update_work`, `memory_get_active_work`

**Knowledge**: `memory_query`, `memory_store`, `memory_record_learning`, `memory_search_global`

**Functions**: `memory_register_function`, `memory_find_function`, `memory_enrich_function`

**Locking**: `memory_lock_files`, `memory_unlock_files`, `memory_get_locks`

**Backlog**: `memory_add_backlog_item`, `memory_list_backlog`, `memory_update_backlog_item`, `memory_complete_backlog_item`

**Lifecycle**: `memory_change_status`, `memory_archive_by_tag`, `memory_restore_by_tag`

## Deployment

Two containers via Docker Compose:
- `mcp-server` - MCP protocol server (port 8080)
- `chroma` - Vector database (port 8001)

Install: `./install.sh` (checks prereqs, builds, starts)
Upgrade: `./upgrade.sh /path/to/new/version` (backup, replace, rebuild)

## Client Configuration

```json
{
  "mcpServers": {
    "shared-memory": {
      "type": "http",
      "url": "http://<server>:8080/mcp"
    }
  }
}
```

## Optional: Librarian Service

Separate host-based service that:
- Receives webhooks when functions are registered
- Reads source files to analyze code
- Uses Claude API (Haiku) for deep analysis
- Enriches function refs with signatures, call graphs, complexity, gotchas
- Generates semantic search summaries

Not included in base deployment (requires local file access).
