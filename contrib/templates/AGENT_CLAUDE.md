# {PROJECT} - {AGENT_NAME}

## Identity

- **name:** {AGENT_NAME}
- **project:** {PROJECT}
- **role:** {ROLE_DESCRIPTION}

## Tech Stack

{TECH_STACK_LIST}

## Scope

This agent works ONLY on files in this repository.
Do NOT modify files outside your scope. If work is needed elsewhere,
create a backlog item assigned to the appropriate team via memory_add_backlog_item
or send a message via memory_send_message.

---

## SESSION LIFECYCLE — MANDATORY, EVERY SESSION

### On Start (do these FIRST, before reading ANY code)

1. Call `memory_start_session(project="{PROJECT}", claude_instance="{AGENT_NAME}")`
2. Call `memory_list_backlog(assigned_to="{AGENT_NAME}", project="{PROJECT}")`
3. Call `memory_get_messages()`
4. Read the guidelines returned by memory_start_session. They are authoritative.

### Before Any Implementation

5. Call `memory_query(query="description of what you're about to work on")` — check
   for existing learnings, known gotchas, prior decisions, related work.
6. Call `memory_find_function(query="what you're about to implement")` — check if
   relevant functions already exist. If you skip this and rediscover something
   already in the knowledge base, that is a failure.

### During Work — Record Immediately, Not Later

7. Call `memory_record_learning` IMMEDIATELY when you encounter ANY of:
   - A bug whose root cause was non-obvious
   - A data model quirk or relationship gotcha
   - A deployment or config issue
   - A workaround you had to use
   - Something that contradicted your initial assumption
   - An undocumented API behavior or race condition
   - Anything you debugged for more than 10 minutes
   Do NOT wait until parking. Record it NOW.

8. Call `memory_register_function` for every function you create or significantly
   modify. Include: name, file path with line number, purpose, and gotchas.

### On Park (before memory_end_session)

9.  Record all remaining non-obvious learnings via `memory_record_learning`
10. Register all new/changed functions via `memory_register_function`
11. Store topic-scoped context via `memory_store` with specific titles
    (e.g., "auth-migration-status", NOT a giant blob)
12. Update `state:{AGENT_NAME}` with ONLY: current task, next steps, blockers.
    Keep it under 30 lines. Use topic-scoped memory_store for everything else.
13. Create backlog items for any incomplete work
14. Call `memory_end_session` with meaningful handoff_notes

---

## ABSOLUTE RULES

### No Local Memory Files
NEVER write learnings, state, or persistent context to local files (MEMORY.md,
notes.md, .context, etc). These are invisible to other agents and lost on repo
switches. ALL persistent knowledge goes to the MCP shared memory server:
- `memory_record_learning` for discoveries
- `memory_store` for substantial context
- `memory_define_spec` for contracts and state
- `memory_register_function` for code

### Session Length Discipline
Park after completing focused work units (1-3 related tasks). Do NOT run
marathon sessions. Long sessions cause context window degradation — you start
forgetting instructions and making mistakes as the window fills. When in doubt,
park early and leave good handoff notes.

### Topic-Scoped Parking
Do NOT dump everything into one monolith state:{AGENT_NAME} spec. The goal is
many small focused memories, not one giant blob. state:{AGENT_NAME} should be
a brief pointer (< 30 lines), not a session transcript.

### Respect File Locks
Check memory_start_session output for relevant_locks. If another agent has a
file locked, do not modify it.

### Specs Before Cross-System Changes
Before modifying interfaces, protocols, or contracts:
```
memory_list_specs(project="{PROJECT}", spec_type="interface")
memory_find_function(query="what you're about to change")
```
Do not break contracts other agents depend on.
