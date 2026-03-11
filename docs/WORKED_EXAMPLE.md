# Worked Example: Two Agents Coordinate on an API Change

This walkthrough shows two Claude Code instances — "backend" and "frontend" — coordinating through the shared memory server while a single developer works across a Python API and a React app.

The backend agent is adding a `POST /api/v1/orders` endpoint. The frontend agent needs to build the form that calls it. Each agent is scoped to its own project working directory — backend can't read the React code and frontend can't read the Python code — but they share the same memory server running on the developer's machine.

**Prerequisites:** The shared memory server is running (`docker compose up -d`), and both projects have a `CLAUDE.md` based on the template that tells Claude Code to use the shared memory tools. See [Setting This Up in Your Projects](#setting-this-up-in-your-projects) at the end.

---

## 1. Both agents start sessions

Each agent registers itself on startup. The CLAUDE.md in each project tells Claude to do this automatically. The `role_description` is how other agents discover what you do.

**Backend** (in the Python API project) calls `memory_start_session` with:

```json
{
  "project": "acme_api",
  "claude_instance": "backend",
  "role_description": "Python FastAPI backend — REST endpoints, data models, auth"
}
```

**Frontend** (in the React project) calls `memory_start_session` with:

```json
{
  "project": "acme_web",
  "claude_instance": "frontend",
  "role_description": "React frontend — UI components, API integration, state management"
}
```

The server returns recent learnings, active agents, and any pending messages. Both agents now know each other exists.

## 2. Backend checks for assigned work

Before doing anything, backend calls `memory_list_backlog` to look for tasks assigned to it. This is how work gets handed off between sessions — a previous Claude (or the developer) may have queued something up.

```json
{
  "project": "acme_api",
  "assigned_to": "backend"
}
```

Maybe there's a backlog item: "Add POST /api/v1/orders endpoint per spec v2.1." Time to get to work.

## 3. Backend locks the route file

Before editing `routes/orders.py`, backend calls `memory_lock_files`. This prevents the unlikely-but-painful scenario where another agent touches the same file.

```json
{
  "files": ["src/routes/orders.py", "src/models/order.py"],
  "reason": "Implementing POST /api/v1/orders endpoint"
}
```

If another agent tries to lock these files, they'll see who holds the lock and why. Locks auto-release when the session ends, so nothing gets stuck if an agent crashes.

## 4. Backend registers the new function

After implementing the endpoint, backend calls `memory_register_function`. This is the single most useful habit for multi-agent work — it means any future Claude in any project can find this function instead of reimplementing it.

```json
{
  "name": "create_order",
  "file": "src/routes/orders.py:47",
  "purpose": "POST /api/v1/orders — creates a new order with line items, validates inventory",
  "gotchas": "Requires auth token. Returns 409 if any line item has insufficient stock."
}
```

The librarian daemon will pick this up in the background, read the source code, and enrich it with the full signature, parameters, and side effects.

## 5. Backend stores the API contract

Now backend calls `memory_store` to record the request/response shape so frontend can find it. This goes into the knowledge base as a searchable document.

```json
{
  "content": "POST /api/v1/orders\n\nRequest body:\n  customer_id: string (required)\n  items: [{sku: string, quantity: int}]\n  notes: string (optional)\n\nResponse 201:\n  order_id: string\n  status: \"pending\"\n  created_at: ISO-8601\n\nResponse 409: {error: \"Insufficient stock\", details: [...]}",
  "doc_type": "api_spec",
  "tags": ["orders", "api", "v1"],
  "project": "acme_api"
}
```

This is now findable by any agent searching for "orders API" or "create order endpoint."

## 6. Backend notifies frontend

Here's where coordination actually happens. Backend calls `memory_send_message` to send a direct message to frontend.

```json
{
  "to_agent": "frontend",
  "category": "task",
  "content": "POST /api/v1/orders is implemented and deployed to dev. Request body takes customer_id, items array [{sku, quantity}], and optional notes. Returns 201 with order_id. Auth required. Search for 'orders api_spec' in the knowledge base for the full contract.",
  "project": "acme_api"
}
```

Frontend will see this the next time it checks messages — even if that's in a completely different session hours later.

## 7. Backend updates its work status

Backend calls `memory_update_work` so anyone (human or agent) can see what it's currently doing.

```json
{
  "project": "acme_api",
  "status": "Orders endpoint complete. Moving to inventory webhook.",
  "files_touched": ["src/routes/orders.py", "src/models/order.py", "tests/test_orders.py"]
}
```

## 8. Frontend checks messages and queries the knowledge base

Meanwhile, frontend is working on something else. It calls `memory_get_messages` (either periodically or at session start — the standard CLAUDE.md pattern does this automatically).

The response includes the message from backend:

```
From: backend | Category: task
"POST /api/v1/orders is implemented and deployed to dev..."
```

Frontend acknowledges receipt with `memory_acknowledge_message` so backend knows the memo landed, then searches for the full API spec with `memory_query`:

```json
{
  "query": "POST orders endpoint request response format",
  "project": "acme_api"
}
```

The vector search returns the stored API contract with the full request/response shapes. Frontend now has everything it needs to build the order form — field names, types, error cases — without ever looking at the backend code.

## 9. Both agents end their sessions

When the developer wraps up, each agent calls `memory_end_session` to record what happened and leave notes for the next session.

**Backend:**

```json
{
  "summary": "Implemented POST /api/v1/orders with inventory validation. All tests passing.",
  "files_modified": ["src/routes/orders.py", "src/models/order.py", "tests/test_orders.py"],
  "handoff_notes": "Orders endpoint is live on dev. Still need to add rate limiting and pagination for GET /orders. Frontend has been notified."
}
```

**Frontend:**

```json
{
  "summary": "Built OrderForm component, wired up to POST /api/v1/orders.",
  "files_modified": ["src/components/OrderForm.tsx", "src/api/orders.ts"],
  "handoff_notes": "Order creation works end-to-end on dev. Need to add error handling for 409 (insufficient stock) — show which items failed."
}
```

These handoff notes are the first thing the next session sees. No context is lost.

---

## What Just Happened?

Two AI agents worked on different codebases, in different terminals, potentially at different times — and successfully coordinated an API change without the developer manually copy-pasting specs between them.

The key interactions were:

- **Locking** — Backend claimed the files it was editing, preventing conflicts.
- **Knowledge sharing** — The API contract was stored once and searched by anyone who needed it.
- **Direct messaging** — Backend told frontend exactly what changed and where to find the details.
- **Handoff** — Both agents left notes so the next session (same agent or different) can pick up where they left off.

None of this required the developer to act as a go-between. The shared memory server was the coordination layer.

---

## Setting This Up in Your Projects

The glue that makes agents use these tools automatically is the **CLAUDE.md file**. Each project should have one that tells Claude Code:

1. **Who am I?** — A name and role (e.g., "You are `backend`").
2. **What do I do at startup?** — Call `memory_start_session`, check backlog, check messages.
3. **What do I do before ending?** — Call `memory_end_session` with handoff notes.
4. **What do I do before writing functions?** — Search the function registry first.

There's a `CLAUDE.md.template` in the repo root that covers all of this. Copy it into your project, fill in the project name and agent name, and your Claude instances will coordinate out of the box.

The developer's global `~/.claude/CLAUDE.md` reinforces these habits across all projects — session start checklists, function registry discipline, and the rule that agents should never disappear mid-task without leaving context.
