# v3.0 Architecture Spec: MCP + Skills + Hooks

**Status:** Draft for review
**Date:** 2026-04-10
**Authors:** shared-memory (Claude), admin-review (Claude), Tom Lemmons

---

## 1. What's Working Today

Before proposing changes, here's what the current system does well:

- **585+ sessions** across 6 specialized agents on a commercial codebase
- **200+ learnings** that survive across sessions and agents
- **Server-managed guidelines** pushed to every agent at session start
- **Function registry** with librarian enrichment — agents don't re-read code
- **Cross-agent messaging**, file locking, backlog tracking
- **Works across machines** — one central server, agents connect from anywhere
- **Auth/RBAC/tenant isolation** ready for hosted use (disabled by default)
- **40 tools across 14 categories** — comprehensive, not a toy

**The system works.** Agents coordinate, knowledge persists, sessions hand off.
The problems below are real but they are friction, not failure.

---

## 2. What Doesn't Work Well

### 2a. Guideline compliance degrades over long sessions

Guidelines are delivered at session start as a tool result (lowest priority tier
in Claude's hierarchy). As context fills, agents start skipping steps — especially
the parking checklist (function registration, learning recording). The
`--append-system-prompt-file` launcher mitigates this by elevating guidelines to
system-prompt authority (highest tier), but it's a workaround that requires
per-machine setup.

**Evidence:** The parking checklist guideline had to be rewritten three times with
increasingly explicit language. The anti-sycophancy guideline was added because
agents were agreeing with wrong premises. The "verify code-claim learnings"
learning exists because coordinator trusted stale memory entries twice in two days.

### 2b. No enforcement at lifecycle boundaries

Nothing prevents an agent from:
- Editing a file without checking memory first
- Ending a session without recording learnings or registering functions
- Ignoring file locks
- Skipping the parking checklist entirely

Guidelines say "you must" but there's no mechanism to enforce "you did."

### 2c. Context loss after compaction

When Claude Code compacts the context window, guidelines and state specs are
lost. The agent continues working but without the rules it started with.
There's no way to re-inject critical context after compaction today.

### 2d. Per-machine setup burden

Each machine needs:
- `~/.claude/CLAUDE.md` (global bootstrap)
- Project `CLAUDE.md` per project directory
- MCP server connection config (`~/.claude.json` or `.mcp.json`)
- Optionally the launcher script + agent configs

Updating behavioral rules requires either server-side guidelines (good) or
editing files on every machine (bad). The launcher script helps but is
PowerShell-only and Windows-specific.

---

## 3. The Three-Layer Architecture

### Layer 1 — MCP Server (exists today)

The foundation. Tools + shared state + cross-machine coordination.

- 40+ tools, MongoDB + ChromaDB, auth/RBAC, audit logging
- Persistent across sessions, machines, and agents
- Single source of truth for knowledge, state, and coordination
- **No changes needed to this layer for v3.** It stays as-is.

### Layer 2 — Skills (new)

Workflow knowledge packaged as SKILL.md files with YAML frontmatter.

Skills are an **open standard** ([agentskills.io](https://agentskills.io))
supported by 25+ tools including Claude Code, Cursor, Windsurf, Cline,
GitHub Copilot, Gemini CLI, and others.

Skills wrap MCP tools with workflow instructions — the "when and how" that
guidelines currently try to communicate via text rules.

### Layer 3 — Hooks (new)

Deterministic lifecycle enforcement. Shell commands or HTTP calls that fire
at specific points (session start, before file edit, after tool use, on stop).

**Hooks are Claude Code-only.** Not part of the Agent Skills standard.
Other MCP clients (Cursor, Windsurf, etc.) do not support hooks.

---

## 4. Proposed Skills

### 4a. `memory-first-coding`

**Problem it solves:** Agents edit code without checking what's already known.

```yaml
---
name: memory-first-coding
description: >
  Before writing or editing code, search shared memory for existing learnings,
  known gotchas, and registered functions related to the files being modified.
paths:
  - "**/*.py"
  - "**/*.ts"
  - "**/*.cs"
  - "**/*.java"
---
```

**Instructions:** Call `memory_query` with the file path and purpose before
editing. Call `memory_find_function` to check if relevant functions exist.
Apply what you find. If nothing exists, proceed — but record what you learn.

**Trade-offs:**
- (+) Structural behavior instead of a text rule agents can forget
- (+) Path-based activation — only fires when editing code files
- (+) Works on Cursor, Windsurf, and other SKILL.md-compatible tools
- (-) Adds latency — memory query before every code edit
- (-) Progressive mode (fetching instructions from server) adds network dependency
- **Recommendation:** Ship with inline instructions first. Add progressive
  fetch later if instructions change frequently enough to justify it.

### 4b. `session-handoff`

**Problem it solves:** Agents skip the parking checklist.

```yaml
---
name: session-handoff
description: >
  When ending a session or parking work, follow the complete handoff checklist:
  register functions, record learnings, update state spec, create backlog items.
  Invoke with /session-handoff when done working.
disable-model-invocation: true
---
```

**Instructions:** The full parking checklist (functions, learnings, messages,
context, state spec, end session) as structured steps.

**Trade-offs:**
- (+) Explicit invocation (`/session-handoff`) makes it a conscious action
- (+) Portable across MCP clients
- (-) `disable-model-invocation: true` means it won't auto-activate — agent
  must remember to call it (or a hook triggers it)
- (-) Without hooks, there's no enforcement that this was actually called
- **Recommendation:** Ship this. Pair with a Stop hook (Claude Code only)
  that checks whether `memory_end_session` was called.

### 4c. `anti-sycophancy-review`

**Problem it solves:** Agents agree with wrong premises, give unearned validation.

```yaml
---
name: anti-sycophancy-review
description: >
  When proposing a solution, design decision, or validating user input,
  first identify the strongest reasons it might fail and state uncertainty
  explicitly. Agreement should be earned through analysis.
---
```

**Trade-offs:**
- (+) Behavioral guardrail that works across all MCP clients
- (-) Hard to scope activation correctly — when IS this relevant?
- (-) Risk of over-triggering and annoying the user with constant caveats
- **Recommendation:** Start with `disable-model-invocation: true` (manual only).
  Let users invoke `/anti-sycophancy-review` when they want a critical eye.
  Revisit auto-activation after testing.

### 4d. `memory-query` (quick search)

**Problem it solves:** Agents need a fast way to search memory contextually.

```yaml
---
name: memory-query
description: >
  Search the shared memory knowledge base for learnings, functions, specs,
  and context related to your current task. Use before implementing anything.
user-invocable: true
argument-hint: "[search query]"
---
```

**Trade-offs:**
- (+) User can invoke `/memory-query database connection pooling` as a shortcut
- (+) Wraps memory_query + memory_find_function in one action
- (-) Thin wrapper — may not justify being a skill vs. just calling the tool
- **Recommendation:** Include it. The value is discoverability for new users.

---

## 5. Proposed Hooks (Claude Code only)

### 5a. `PostCompact` — Re-inject state after compaction

**Problem it solves:** Context loss after compaction.

```json
{
  "hooks": {
    "PostCompact": [{
      "type": "http",
      "url": "http://localhost:8080/hook/post-compact",
      "timeout": 10
    }]
  }
}
```

Server endpoint returns: current state spec + active guidelines + critical
context, injected into Claude's context via hook stdout (max 10,000 chars).

**Trade-offs:**
- (+) Solves the #1 operational pain point (guideline loss after compaction)
- (+) HTTP hook — no shell script to maintain per machine
- (+) Server decides what to inject, so changes propagate without client updates
- (-) Claude Code only — Cursor/Windsurf users lose context on compaction
- (-) 10,000 char limit means we must be selective about what to re-inject
- (-) If server is unreachable, hook silently fails (HTTP hooks are non-blocking)
- **Recommendation:** High priority. This is the highest-value hook.
- **Mitigation for non-Claude clients:** Skills can include a note: "If you
  notice your guidelines are missing, call `memory_start_session` again to
  reload them." Not as good as automatic re-injection, but better than nothing.

### 5b. `PreToolUse` on Edit/Write — Memory check before file modification

**Problem it solves:** Agents edit files without checking existing knowledge.

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write",
      "type": "http",
      "url": "http://localhost:8080/hook/pre-edit",
      "timeout": 5
    }]
  }
}
```

Server endpoint receives the file path, returns relevant learnings/functions.
Injected into Claude's context before the edit proceeds.

**Trade-offs:**
- (+) Deterministic — cannot be skipped
- (+) Contextual — only fires on actual file edits, not reads
- (-) Latency on every single file edit (5s timeout)
- (-) Claude Code only
- (-) Could be noisy if memory has many results for common files
- **Recommendation:** Medium priority. The `memory-first-coding` skill handles
  this for most clients. The hook adds enforcement for Claude Code users.
- **Risk:** If timeout is too aggressive and server is slow, edit operations
  feel sluggish. Need to test latency empirically.

### 5c. `Stop` — Verify parking checklist was completed

**Problem it solves:** Agents end sessions without recording learnings/functions.

```json
{
  "hooks": {
    "Stop": [{
      "type": "http",
      "url": "http://localhost:8080/hook/stop-check",
      "timeout": 5
    }]
  }
}
```

Server checks: was `memory_end_session` called this session? If not, returns
a reminder that gets injected into context. Exit code 2 would prevent stopping
entirely, but that's too aggressive — use exit 0 with a warning instead.

**Trade-offs:**
- (+) Catches the most common compliance failure (skipping the parking checklist)
- (-) Only warns, doesn't enforce (exit 0 not exit 2)
- (-) Claude Code only
- (-) Fires on every response, not just session end — need to filter
- **Recommendation:** Low priority initially. The `session-handoff` skill is
  the primary mechanism. This hook is a safety net.

### 5d. `SessionStart` — Auto-initialize session

**Problem it solves:** Agents sometimes forget to call `memory_start_session`.

```json
{
  "hooks": {
    "SessionStart": [{
      "type": "http",
      "url": "http://localhost:8080/hook/session-start",
      "timeout": 10
    }]
  }
}
```

**Trade-offs:**
- (+) Guarantees session initialization happens
- (-) Hook fires before Claude has agent identity — how does the server know
  which agent this is? Would need to derive from working directory or config.
- (-) Duplicates what CLAUDE.md already instructs
- **Recommendation:** Defer. CLAUDE.md + guidelines handle this adequately.
  Revisit if we see agents consistently failing to start sessions.

---

## 6. HTTP API Endpoints Required

Skills call MCP tools directly (through Claude's tool system). Hooks need HTTP
endpoints because they run outside Claude's tool context.

### Minimal endpoint set

| Endpoint | Used by | Purpose |
|----------|---------|---------|
| `POST /hook/post-compact` | PostCompact hook | Return state spec + guidelines for re-injection |
| `POST /hook/pre-edit` | PreToolUse hook | Return relevant learnings for a file path |
| `POST /hook/stop-check` | Stop hook | Check if session was properly ended |
| `GET /health` | (exists) | Health check |

### Design principles

1. **Same auth as MCP.** API keys work on both transports. No separate auth system.
2. **Thin wrappers.** Each endpoint calls existing MCP tool logic internally.
   No new business logic in the HTTP layer.
3. **Fail open.** If the server is unreachable, hooks return nothing and Claude
   continues. Never block the developer's workflow because the memory server is down.
4. **Rate limiting.** PreToolUse can fire very frequently. Server should handle
   burst traffic gracefully (in-memory caching of recent results).

### Implementation note

The server already runs on FastMCP with Starlette underneath. Adding custom
routes is straightforward — we already have `/health` as a custom route in
`__main__.py`. The HTTP endpoints would be additional custom routes, not a
separate service.

---

## 7. Multi-Client Compatibility Matrix

| Feature | Claude Code | Cursor | Windsurf | Cline | Other MCP clients |
|---------|-------------|--------|----------|-------|-------------------|
| MCP tools (Layer 1) | Yes | Yes | Yes | Yes | Yes |
| Skills / SKILL.md (Layer 2) | Yes | Yes | Yes | Yes | If SKILL.md compatible |
| Hooks (Layer 3) | Yes | **No** | **No** | **No** | **No** |
| `--append-system-prompt-file` | Yes | No | No | No | No |
| PostCompact re-injection | Yes (hook) | **No** | **No** | **No** | **No** |
| PreToolUse enforcement | Yes (hook) | **No** | **No** | **No** | **No** |

### What non-Claude clients get

- **Full MCP tool access** — all 40 tools work on any MCP-compatible client
- **Skills** — workflow knowledge via SKILL.md files (open standard)
- **Guidelines** — pushed at session start via `memory_start_session`

### What non-Claude clients miss

- **No lifecycle enforcement** — hooks don't exist, so agents CAN skip steps
- **No post-compaction recovery** — if the client compacts context, guidelines
  are gone with no way to re-inject them
- **No pre-edit memory check** — the skill suggests it, but can't enforce it

### Mitigations for non-Claude clients

| Gap | Mitigation | Effectiveness |
|-----|------------|---------------|
| No hooks | Skills include behavioral instructions inline (not just hook triggers) | Moderate — relies on model compliance |
| No post-compact | Skill instruction: "If guidelines seem missing, call `memory_start_session` again" | Weak — agent must notice the problem |
| No pre-edit enforcement | `memory-first-coding` skill activates on code file paths, instructs query before edit | Moderate — structural but not enforced |
| No stop check | `session-handoff` skill with explicit checklist | Moderate — user must invoke it |
| No system-prompt elevation | CLAUDE.md instructs to follow guidelines; skill descriptions reinforce this | Moderate — varies by client's priority handling |

### Honest assessment

**Claude Code users get the full stack.** MCP + Skills + Hooks = tools +
workflow + enforcement. This is the complete experience.

**Other MCP client users get Layer 1 + Layer 2.** Tools + workflow knowledge.
No enforcement. This is still significantly better than raw MCP tools alone —
skills package the "how to use these tools well" knowledge that currently only
exists in CLAUDE.md files and guidelines. But agents can still skip steps.

**The gap is real but acceptable.** Most MCP memory servers have zero workflow
guidance. Shipping skills that work on 25+ tools is a competitive advantage
even without hooks. Hooks are a Claude Code bonus, not a requirement.

---

## 8. Distribution Model

### What lives where

| Artifact | Location | Update mechanism |
|----------|----------|------------------|
| MCP server | Docker container (one machine) | `docker compose build && up -d` |
| Guidelines | MongoDB (server-side) | `memory_guidelines(action="set", ...)` — instant propagation |
| Learnings, state, specs | MongoDB + ChromaDB (server-side) | MCP tools — instant propagation |
| Skills (SKILL.md files) | GitHub repo `skills/` directory | `git pull` on each machine |
| Hooks (settings.json snippets) | GitHub repo `hooks/` directory | `git pull` + merge into local settings |
| CLAUDE.md templates | GitHub repo `contrib/templates/` | `git pull` + copy to project |
| Agent configs | Local per-machine | Manual setup (one-time) |

### The git-pull problem

Skills and hooks live in the GitHub repo. Updating them requires `git pull` on
every machine. This is the same distribution model as every other Claude Code
tool (claude-brain, etc.) and is acceptable for the following reasons:

1. Skills change rarely — they encode workflow patterns, not runtime data
2. Hooks change even more rarely — they're just HTTP endpoint URLs
3. The things that change frequently (guidelines, learnings, state) already
   propagate instantly via the server

### Version check (nice to have, not required)

A `SessionStart` hook or skill could call the server's `/health` endpoint
(or a new `/version` endpoint) and compare against the local skill/hook
versions. If out of date, inject a warning: "Your skills are out of date.
Run `git pull` in the mcp-shared-memory repo."

---

## 9. Risks and Open Questions

### 9a. HTTP endpoints increase attack surface

**Risk:** Adding HTTP routes alongside MCP means two entry points to secure.

**Mitigation:** Same API key auth on both transports. Hooks send the API key
in the Authorization header. Rate limiting on hook endpoints. Audit logging
already covers all operations.

**Residual risk:** Low. The endpoints are thin wrappers around existing
tool logic, not new business logic.

### 9b. Server unreachability degrades hooks silently

**Risk:** If the MCP server is down or unreachable, all HTTP hooks return
nothing. Agents continue without memory context, file locking, or guidelines.

**Mitigation:** HTTP hooks fail open (by design — never block the developer).
Skills still contain inline instructions that work without the server. The
`/health` endpoint already exists for monitoring.

**Residual risk:** Medium. A prolonged outage means agents work without
shared memory. This is the same as today (if the server is down, MCP tools
fail too). Hooks don't make it worse.

### 9c. PreToolUse hook latency

**Risk:** A 5-second HTTP call before every file edit could feel sluggish.

**Mitigation:** Server-side caching of recent results per file path. Short
timeout (2-3s instead of 5s). Only fire on Edit/Write, not Read/Grep/Bash.

**Residual risk:** Needs empirical testing. If latency is unacceptable,
drop PreToolUse and rely on the `memory-first-coding` skill instead.

### 9d. Buggy hook breaks all sessions

**Risk:** A hook that crashes or returns exit code 2 incorrectly blocks every
session on every machine.

**Mitigation:** HTTP hooks fail open (non-2xx responses don't block). Never
use exit code 2 on Stop hooks. Document a kill switch: rename `settings.json`
to disable hooks. Ship hooks as opt-in, not default.

**Residual risk:** Low if we follow the "fail open" principle consistently.

### 9e. Skills instruction quality

**Risk:** Skills with vague instructions ("check memory before editing") don't
change behavior. Skills with over-specific instructions become stale as the
server evolves.

**Mitigation:** Keep skill instructions short and delegate specifics to the
server. Example: "Call `memory_query` with the current file path" is better
than a 200-line playbook. The server's response is always current.

**Residual risk:** Low. This is a writing discipline problem, not a technical one.

### 9f. Fragmentation across MCP clients

**Risk:** Claude Code users get a significantly better experience than
Cursor/Windsurf users. This could confuse users or create support burden.

**Mitigation:** Document the compatibility matrix clearly (Section 7 above).
Position skills as the baseline and hooks as a Claude Code enhancement.
Never require hooks for basic functionality.

**Residual risk:** Acceptable. The MCP tools work everywhere. Skills work on
25+ tools. Hooks are a bonus. This is the same model as every IDE extension
that has extra features in one editor.

---

## 10. Sequencing and Dependencies

```
                    ┌─────────────────────┐
                    │  HTTP API Endpoints  │  ← prerequisite for hooks
                    │  (backlog_e84fe178)  │
                    └─────────┬───────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
    ┌─────────▼──────┐  ┌────▼────────┐  ┌───▼──────────────┐
    │  Skills (4)    │  │  Hooks (3)  │  │  PostCompact     │
    │  SKILL.md files│  │  HTTP hooks │  │  re-injection    │
    │  (backlog_f615)│  │  (backlog_  │  │  (highest value) │
    └─────────┬──────┘  │  32ce0)     │  └───┬──────────────┘
              │         └──────┬──────┘      │
              └────────────────┼──────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  v3.0 Release       │
                    │  (backlog_60b4c4d0) │
                    └─────────────────────┘
```

### Recommended order

1. **HTTP API endpoints** (3-5 custom routes in `__main__.py`)
   - `/hook/post-compact` — returns state spec + guidelines
   - `/hook/pre-edit` — returns relevant learnings for file
   - `/hook/stop-check` — checks session completion
   - Uses existing MCP tool logic, same auth
   - **This unblocks everything else**

2. **Skills** (4 SKILL.md files)
   - `memory-first-coding` — path-activated, code file editing
   - `session-handoff` — manual invocation for parking
   - `anti-sycophancy-review` — manual invocation
   - `memory-query` — user-invokable search shortcut
   - **Can be built and shipped independently of hooks**
   - **Works on 25+ tools immediately**

3. **PostCompact hook** (single highest-value hook)
   - Depends on `/hook/post-compact` endpoint
   - Claude Code only, but solves the biggest pain point
   - Ship this first, test before adding more hooks

4. **Remaining hooks** (PreToolUse, Stop)
   - Lower priority, build after PostCompact is proven
   - Test latency impact empirically before shipping PreToolUse

5. **v3.0 release packaging**
   - README updates, setup guide, compatibility docs
   - Version check mechanism (nice to have)

### What NOT to build

- **Don't build a skill distribution system.** Git pull is sufficient.
- **Don't build progressive skill content fetching.** Inline instructions
  work. Add server-fetch later only if instructions change so frequently
  that git pull becomes a bottleneck.
- **Don't replace the launcher script.** It works. Hooks complement it for
  Claude Code users; it remains the only option for non-Claude clients that
  support `--append-system-prompt-file` or similar.
- **Don't add a SessionStart hook.** CLAUDE.md handles this. The hook would
  duplicate existing behavior and create confusion about which one is
  authoritative.

---

## 11. Success Criteria

### Measurable

- PostCompact hook re-injects guidelines: verify by checking if agent
  behavior stays consistent after compaction (currently it degrades)
- Function registration rate improves: compare registered functions per
  session before and after v3 (currently agents frequently skip this)
- Pre-edit memory queries increase: log `memory_query` calls that include
  file paths (currently rare)

### Qualitative

- New users can set up and get value from skills without reading CLAUDE.md
- Claude Code users notice fewer "agent forgot the rules" incidents
- Non-Claude-Code users still get a significantly better experience than
  raw MCP tools alone

---

## 12. Summary

| What | Change | Effort | Impact | Risk |
|------|--------|--------|--------|------|
| HTTP API endpoints | New routes in __main__.py | Small | Enables hooks | Low |
| Skills (4 SKILL.md files) | New files in skills/ dir | Small | Workflow knowledge for 25+ tools | Low |
| PostCompact hook | New endpoint + hook config | Small | Fixes #1 pain point (context loss) | Low |
| PreToolUse hook | New endpoint + hook config | Small | Enforcement for file edits | Medium (latency) |
| Stop hook | New endpoint + hook config | Small | Safety net for parking | Low |
| v3.0 release packaging | Docs + README + setup | Medium | Positioning + adoption | Low |

**Total effort:** Moderate. Most items are small. The HTTP endpoints are the
only new server code; everything else is configuration files and markdown.

**The honest pitch:** v3.0 turns "here are 40 tools, good luck" into "here's
a complete workflow system." Skills teach agents how to use the tools well.
Hooks enforce it for Claude Code users. The MCP server remains the foundation
that works everywhere.
