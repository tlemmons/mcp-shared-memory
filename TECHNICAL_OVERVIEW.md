# MCP Shared Memory Server - Technical Overview

## What It Is

A Model Context Protocol (MCP) server that provides persistent shared memory for AI coding agents. Enables multiple agents (Claude Code, Cursor, Windsurf, or any MCP client) to coordinate work, share knowledge, and avoid conflicts across projects.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              AI Coding Agents (MCP Clients)              │
│     Claude Code · Cursor · Windsurf · Claude Desktop     │
│              (multiple concurrent sessions)              │
└─────────────────────┬───────────────────────────────────┘
                      │ MCP Protocol (streamable-http)
                      ▼
┌─────────────────────────────────────────────────────────┐
│                    MCP Server (Python)                    │
│  - FastMCP + Starlette + Uvicorn                         │
│  - Stateless HTTP transport                              │
│  - 38 tools exposed via MCP protocol                     │
│  - Session management (in-memory)                        │
│  - File locking coordination                             │
└───────────┬───────────────────────┬─────────────────────┘
            │                       │
            ▼                       ▼
┌───────────────────────┐ ┌─────────────────────────────┐
│  ChromaDB (Vectors)   │ │     MongoDB (Documents)      │
│  - Semantic search    │ │  - Messages & agent status   │
│  - Knowledge base     │ │  - Backlog items             │
│  - Function refs      │ │  - Checklists                │
│  - Per-project +      │ │  - Project/agent registry    │
│    shared collections │ │  - TTL indexes               │
└───────────────────────┘ └─────────────────────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Protocol | MCP (Model Context Protocol) via streamable-http |
| Server | FastMCP + Starlette + Uvicorn |
| Vector Store | ChromaDB with default embeddings |
| Document Store | MongoDB 7 |
| Deployment | Docker Compose (3 containers) |
| Language | Python 3.11 |

## Package Structure

```
src/shared_memory/
├── __init__.py          # Package metadata
├── __main__.py          # Entry point (python -m shared_memory)
├── app.py               # FastMCP instance, create_app()
├── config.py            # All constants, env vars, enums
├── state.py             # In-memory state (sessions, locks, signals)
├── clients.py           # Chroma + MongoDB connection management
├── helpers.py           # Shared utilities
└── tools/
    ├── sessions.py      # Start/end sessions (2 tools)
    ├── query.py         # Search knowledge base (3 tools)
    ├── storage.py       # Store documents (2 tools)
    ├── locking.py       # File locking (3 tools)
    ├── lifecycle.py     # Document lifecycle (4 tools)
    ├── backlog.py       # Task management (4 tools)
    ├── messaging.py     # Inter-agent messaging (7 tools)
    ├── functions.py     # Function registry (5 tools)
    ├── search.py        # Cross-project search (2 tools)
    ├── specs.py         # Versioned specs (3 tools)
    ├── projects.py      # Project/agent registry (1 CRUD tool)
    ├── checklists.py    # Shared checklists (1 CRUD tool)
    └── database.py      # External DB queries (1 CRUD tool)
```

## Core Features

### 1. Session Management
- Agents register on start, end with summary
- Tracks active work per agent
- Detects file overlap between agents
- Handoff notes for context continuity
- Stale session auto-cleanup

### 2. Knowledge Base
- **Types**: architecture, learning, pattern, code_snippet, api_spec, interface, function_ref, spec, and more (18 types)
- **Lifecycle**: active → deprecated → superseded → archived
- **Search**: Semantic (embedding-based) + metadata filtering
- **Expiry**: Configurable TTL per type, auto-cleanup on query
- **Deduplication**: Content hashing prevents duplicate entries

### 3. Function References
- Agents register functions with minimal input (name, file, purpose)
- Optional librarian service enriches with deep analysis
- Semantic search to find existing functions before implementing

### 4. File Locking
- Atomic lock acquisition with conflict detection
- Prevents concurrent edits to same files
- Auto-release on session end
- Stale lock detection (>30min inactive)

### 5. Inter-Agent Messaging
- Full lifecycle: pending → delivered → received → completed
- Priority levels (urgent, normal, low)
- Categories (contract, task, question, info, review, blocker)
- Project-scoped addressing
- Admin/coordinator message retrieval

### 6. Backlog Management
- Persistent task tracking across sessions
- Priority levels, status workflow, assignment
- 9 statuses: open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info

### 7. Project & Agent Registry
- Register projects with admin agents
- Named agent registration with role descriptions
- Path-to-identity matching
- Tiered access (human, coordinator, named, worker)

### 8. Specs & Checklists
- Versioned specifications with owner-only enforcement
- Shared checklists for coordinated workflows

### 9. External Database Queries
- Read-only SQL queries against registered databases
- SQL injection prevention (keyword blocking, SELECT-only enforcement)
- Schema exploration tools
- Dynamically configured via environment variables

## Data Model

### ChromaDB Collections
```
├── proj_{name}          # Project-specific memories
├── shared_patterns      # Cross-project patterns
├── shared_context       # Cross-project context
└── shared_work          # Active/completed work items
```

### MongoDB Collections
```
├── messages             # Inter-agent message queue (7-day TTL)
├── agent_status         # Heartbeats and current task (1-hour TTL)
├── agent_directory      # Auto-populated activity tracking
├── checklists           # Shared checklists
├── projects             # Project registry
└── registered_agents    # Agent registry per project
```

## API (38 MCP Tools)

**Session**: `memory_start_session`, `memory_end_session`

**Knowledge**: `memory_query`, `memory_store`, `memory_record_learning`, `memory_search_global`, `memory_get_by_id`

**Functions**: `memory_register_function`, `memory_find_function`, `memory_enrich_function`, `memory_become_librarian`, `memory_get_enrichment_queue`

**Locking**: `memory_lock_files`, `memory_unlock_files`, `memory_get_locks`

**Messaging**: `memory_send_message`, `memory_get_messages`, `memory_update_message_status`, `memory_acknowledge_message`, `memory_heartbeat`, `memory_get_agent_status`, `memory_list_agents`

**Backlog**: `memory_add_backlog_item`, `memory_list_backlog`, `memory_update_backlog_item`, `memory_complete_backlog_item`

**Lifecycle**: `memory_update_work`, `memory_get_active_work`, `memory_change_status`, `memory_archive_by_tag`, `memory_restore_by_tag`

**Specs**: `memory_define_spec`, `memory_get_spec`, `memory_list_specs`

**Registry**: `memory_project` (CRUD), `memory_list_projects`

**Checklists**: `memory_checklist` (CRUD)

**Database**: `memory_db` (list/schema/query)

## Deployment

Three containers via Docker Compose:
- `mcp-server` - MCP protocol server (port 8080)
- `chromadb` - Vector database (port 8001)
- `mongodb` - Document database (port 27018)

Quick start: `cp .env.example .env && docker compose up -d`

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

Not included in base Docker deployment (requires local file access and Anthropic API key).
