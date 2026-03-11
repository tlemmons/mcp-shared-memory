# MCP Shared Memory Server - AI Integration Guide

This document describes how to connect to and use the MCP Shared Memory Server from any AI coding assistant.

## Server Details

| Property | Value |
|----------|-------|
| Protocol | MCP (Model Context Protocol) over HTTP |
| Endpoint | `http://<server-ip>:8080/mcp` |
| Method | POST |
| Content-Type | `application/json` |
| Accept | `application/json, text/event-stream` |

## Protocol Overview

The server uses JSON-RPC 2.0 over HTTP with Server-Sent Events (SSE) responses.

### Request Format
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "<tool_name>",
    "arguments": {
      "<arg1>": "<value1>",
      "<arg2>": "<value2>"
    }
  }
}
```

### Response Format
Response comes as SSE with `data:` prefix:
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{...json result...}"}]}}
```

## Quick Start

### 1. Start a Session (Required First)

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "memory_start_session",
      "arguments": {
        "project": "myproject",
        "claude_instance": "cursor-ai",
        "task_description": "Working on feature X"
      }
    }
  }'
```

Response includes `session_id` - save this for all subsequent calls.

### 2. Query Knowledge

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "memory_query",
      "arguments": {
        "session_id": "<your-session-id>",
        "query": "authentication patterns",
        "project": "myproject"
      }
    }
  }'
```

### 3. End Session

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "memory_end_session",
      "arguments": {
        "session_id": "<your-session-id>",
        "summary": "Implemented feature X, added tests"
      }
    }
  }'
```

## Available Tools

### Session Management

| Tool | Purpose | Required Args |
|------|---------|---------------|
| `memory_start_session` | Start session (call first) | `project` |
| `memory_end_session` | End session with summary | `session_id`, `summary` |
| `memory_update_work` | Update current work status | `session_id`, `title`, `status` |
| `memory_get_active_work` | See what others are working on | `session_id` |

### Knowledge Base

| Tool | Purpose | Required Args |
|------|---------|---------------|
| `memory_query` | Search knowledge base | `session_id`, `query` |
| `memory_store` | Store documentation/patterns | `session_id`, `title`, `content`, `memory_type` |
| `memory_record_learning` | Quick learning capture | `session_id`, `title`, `details` |
| `memory_search_global` | Search across all projects | `session_id`, `query` |

### Function References

| Tool | Purpose | Required Args |
|------|---------|---------------|
| `memory_register_function` | Register a function | `session_id`, `name`, `file`, `purpose` |
| `memory_find_function` | Find existing functions | `session_id`, `query` |

### File Locking

| Tool | Purpose | Required Args |
|------|---------|---------------|
| `memory_lock_files` | Lock files for editing | `session_id`, `files`, `reason` |
| `memory_unlock_files` | Release file locks | `session_id` |
| `memory_get_locks` | View current locks | `session_id` |

### Backlog

| Tool | Purpose | Required Args |
|------|---------|---------------|
| `memory_add_backlog_item` | Add task to backlog | `session_id`, `title`, `description` |
| `memory_list_backlog` | List backlog items | `session_id` |
| `memory_update_backlog_item` | Update backlog item | `session_id`, `item_id` |
| `memory_complete_backlog_item` | Mark item done | `session_id`, `item_id` |

## Tool Details

### memory_start_session
```json
{
  "name": "memory_start_session",
  "arguments": {
    "project": "string (required) - project name e.g. 'myapp'",
    "claude_instance": "string (optional) - identifier e.g. 'cursor-main'",
    "task_description": "string (optional) - what you're working on"
  }
}
```
Returns: `session_id`, recent learnings, active work by others, handoff notes

### memory_query
```json
{
  "name": "memory_query",
  "arguments": {
    "session_id": "string (required)",
    "query": "string (required) - natural language search",
    "project": "string (optional) - limit to project",
    "memory_types": "array (optional) - filter types: architecture, learning, pattern, function_ref, etc.",
    "limit": "integer (optional, default 3) - max results"
  }
}
```
Returns: Matching documents with relevance scores

### memory_store
```json
{
  "name": "memory_store",
  "arguments": {
    "session_id": "string (required)",
    "title": "string (required)",
    "content": "string (required) - markdown supported",
    "memory_type": "string (required) - architecture, learning, pattern, api_spec, etc.",
    "project": "string (optional) - omit for shared/cross-project",
    "tags": "array (optional) - categorization tags"
  }
}
```

### memory_register_function
```json
{
  "name": "memory_register_function",
  "arguments": {
    "session_id": "string (required)",
    "name": "string (required) - function name",
    "file": "string (required) - file path with line number e.g. 'src/auth.py:45'",
    "purpose": "string (required) - one-line description",
    "project": "string (optional)",
    "gotchas": "string (optional) - warnings or non-obvious behaviors",
    "code": "string (optional) - full function code for deep analysis"
  }
}
```

### memory_find_function
```json
{
  "name": "memory_find_function",
  "arguments": {
    "session_id": "string (required)",
    "query": "string (required) - what you're looking for e.g. 'parse email'",
    "project": "string (optional)",
    "limit": "integer (optional, default 5)"
  }
}
```

### memory_lock_files
```json
{
  "name": "memory_lock_files",
  "arguments": {
    "session_id": "string (required)",
    "files": "array (required) - file paths to lock e.g. ['src/auth.py', 'src/users.py']",
    "reason": "string (required) - why you need exclusive access"
  }
}
```
Returns: `success`, `locked` (files you now hold), `conflicts` (files held by others)

### memory_add_backlog_item
```json
{
  "name": "memory_add_backlog_item",
  "arguments": {
    "session_id": "string (required)",
    "title": "string (required)",
    "description": "string (required)",
    "priority": "string (optional) - critical, high, medium (default), low",
    "project": "string (optional)",
    "assigned_to": "string (optional) - team or person",
    "tags": "array (optional)"
  }
}
```

### memory_list_backlog
```json
{
  "name": "memory_list_backlog",
  "arguments": {
    "session_id": "string (required)",
    "project": "string (optional) - filter by project",
    "status": "string (optional) - open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info",
    "priority": "string (optional) - critical, high, medium, low",
    "assigned_to": "string (optional)",
    "include_done": "boolean (optional, default false)"
  }
}
```

## Recommended Workflow

1. **Start session** - Get session_id, check what others are working on
2. **Query first** - Before implementing, check if patterns/functions exist
3. **Lock files** - Before editing shared code, lock to prevent conflicts
4. **Register functions** - When creating important functions, register them
5. **Record learnings** - When discovering gotchas, record for others
6. **End session** - Summarize work for next AI to continue

## Health Check

Simple GET request (no auth):
```bash
curl http://localhost:8080/health
```

Response:
```json
{
  "status": "healthy",
  "chroma": "healthy",
  "active_sessions": 3,
  "active_locks": 1,
  "active_signals": 0
}
```

## List All Tools

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Error Handling

Errors return in result text as JSON:
```json
{"error": "Session 'xxx' not found. Call memory_start_session first."}
```

Common errors:
- Missing session_id or session expired
- Invalid status/priority values
- Lock conflicts with other sessions

## Notes for AI Integration

1. **Session persistence**: Sessions expire after inactivity. Start a new session if you get session errors.

2. **Parsing responses**: The actual result is in `result.content[0].text` as a JSON string - parse it twice.

3. **Concurrent access**: Multiple AIs can connect simultaneously. Use file locking for coordination.

4. **Project isolation**: Use consistent project names across your team for shared context.

5. **Memory types**: Use appropriate types for better search:
   - `architecture` - system design docs
   - `learning` - gotchas and discoveries
   - `pattern` - reusable code patterns
   - `function_ref` - function documentation
   - `api_spec` - API documentation
