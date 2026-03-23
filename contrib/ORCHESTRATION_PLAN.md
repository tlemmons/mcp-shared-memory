# Multi-Claude Orchestration System - Architecture Plan

## Vision

A self-coordinating system of Claude agents that can:
- Run continuously in detached tmux sessions
- Receive work assignments automatically
- Coordinate with each other through a smart orchestrator
- Allow human attachment at any time for direct interaction
- Manage context/cost through intelligent batching and prioritization

## Architecture Overview

```
                                    ┌─────────────────────┐
                                    │      Human (You)    │
                                    │  - Add backlog      │
                                    │  - Attach to any    │
                                    │    Claude via tmux  │
                                    │  - Override/guide   │
                                    └──────────┬──────────┘
                                               │
                                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR (Smart - Claude-powered)                 │
│                                                                              │
│  - Monitors all Claude sessions                                              │
│  - Watches backlog for new/changed items                                     │
│  - Makes decisions: who works on what, when                                  │
│  - Manages inter-Claude communication                                        │
│  - Cost awareness: batches low-priority, limits concurrent work              │
│  - Conflict detection: prevents overlapping file edits                       │
│                                                                              │
│  Runs as: Lightweight daemon + periodic Claude "brain" calls                 │
└──────────────────────────────────────────────────────────────────────────────┘
           │                    │                    │
           │ tmux send-keys     │ tmux send-keys     │ tmux send-keys
           ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ tmux: frontend  │  │ tmux: backend   │  │ tmux: tester    │
│                 │  │                 │  │                 │
│ Claude Code     │  │ Claude Code     │  │ Claude Code     │
│ Specialty: UI   │  │ Specialty: API  │  │ Specialty: QA   │
│ Project: webapp │  │ Project: webapp │  │ Project: webapp │
│                 │  │                 │  │                 │
│ CLAUDE.md has   │  │ CLAUDE.md has   │  │ CLAUDE.md has   │
│ role context    │  │ role context    │  │ role context    │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   MCP Server    │
                    │   + Chroma      │
                    │                 │
                    │ - Sessions      │
                    │ - Backlog       │
                    │ - Messages      │
                    │ - File locks    │
                    │ - Learnings     │
                    └─────────────────┘
```

## Components

### 1. Claude Registry & Launcher

**Purpose:** Define and spawn Claude specialists

**Config file: `claudes.yaml`**
```yaml
claudes:
  frontend:
    specialty: "UI/UX, React, CSS, accessibility"
    project: webapp
    working_dir: /home/user/webapp/frontend
    auto_start: true
    max_concurrent_tasks: 1

  backend:
    specialty: "API, database, authentication, Python"
    project: webapp
    working_dir: /home/user/webapp/backend
    auto_start: true
    max_concurrent_tasks: 1

  tester:
    specialty: "Testing, QA, validation, edge cases"
    project: webapp
    working_dir: /home/user/webapp
    auto_start: false  # spawned on demand
    max_concurrent_tasks: 2

  reviewer:
    specialty: "Code review, architecture, security"
    project: webapp
    working_dir: /home/user/webapp
    auto_start: false
    max_concurrent_tasks: 3
```

**Launcher script:** `claude-spawn.py`
```bash
# Spawns a Claude in detached tmux with proper context
./claude-spawn.py frontend

# Creates:
#   tmux session: webapp:frontend
#   Injects initial prompt with specialty context
#   Registers with MCP as "frontend"
```

**What gets injected on spawn:**
```
You are the FRONTEND specialist for the webapp project.

Your expertise: UI/UX, React, CSS, accessibility

CRITICAL RULES:
1. Call memory_start_session first (project: webapp, claude_instance: frontend)
2. Check backlog for items assigned to you
3. When done with a task, call memory_end_session with summary
4. You will receive new tasks via this terminal - acknowledge them
5. If you need another specialist, add backlog item assigned to them
6. Lock files before editing: memory_lock_files

Current assignment: [injected by orchestrator or "Check backlog"]
```

### 2. Orchestrator Daemon

**Purpose:** The "brain" that coordinates everything

**Two modes:**

**A) Lightweight polling (cheap, always-on)**
```python
while True:
    # Check MCP for:
    # - New backlog items
    # - Completed sessions
    # - Pending messages
    # - Stale locks

    # Simple rules:
    # - New high-priority item for "frontend"? tmux send to frontend
    # - Session ended? Check for next task
    # - Conflict detected? Alert human

    sleep(30)
```

**B) Smart decisions (Claude-powered, periodic)**
```python
# Every 5 minutes OR on significant event:
# - Summarize current state
# - Ask Claude (orchestrator brain) what to do
# - Execute decisions

orchestrator_prompt = """
Current state:
- frontend: working on "Add dark mode" (45 min)
- backend: idle for 10 min
- tester: not running

Backlog:
- [high] "API rate limiting" - unassigned
- [medium] "Dashboard tests" - assigned: tester
- [low] "Update README" - unassigned

Recent completions:
- frontend finished "User profile page" 20 min ago

What actions should I take?
Options: assign_task, spawn_claude, send_message, alert_human, wait
"""
```

**Cost control:**
- Orchestrator Claude calls are SHORT (just decisions)
- Use Haiku for orchestrator brain (cheap, fast)
- Batch low-priority decisions
- Set daily/hourly budget limits
- Log all orchestrator costs separately

### 3. Message Queue (in MCP)

**New tools:**

```python
memory_send_message(
    session_id: str,
    to_instance: str,      # "frontend", "backend", or "*" for all
    message: str,
    priority: str = "normal",  # normal, high, low
    wait_for_idle: bool = True  # deliver when task complete, or interrupt
)

memory_get_messages(
    session_id: str
) -> List[Message]

memory_acknowledge_message(
    session_id: str,
    message_id: str
)
```

**Message types:**
- `task_assignment` - New work from backlog
- `completion_notice` - Another Claude finished something relevant
- `question` - Claude needs input from another
- `human_override` - Direct human instruction
- `status_request` - Orchestrator checking in

### 4. Inter-Claude Communication

**Problem:** Claudes might need to discuss/coordinate

**Solution: Structured async messaging (NOT real-time chat)**

```
Frontend: memory_send_message(
    to="backend",
    message="Need API endpoint for user preferences. Fields: theme, language, notifications. Priority?"
)

Orchestrator: *delivers to backend*

Backend: memory_send_message(
    to="frontend",
    message="Will add /api/user/preferences. ETA: next task. Using existing auth middleware."
)

Orchestrator: *delivers to frontend*
```

**NOT a chatroom.** Async, queued, delivered between tasks. Prevents:
- Context explosion
- Runaway conversations burning tokens
- Circular dependencies

**Orchestrator can summarize/batch:**
- Multiple messages to same Claude? Combine them
- Low priority? Hold until task boundary
- Circular discussion detected? Alert human

### 5. Human Attachment Points

**You can always:**

```bash
# See all running Claudes
tmux ls

# Attach to any Claude
tmux attach -t webapp:frontend

# Detach without stopping
Ctrl+B, D

# Send quick message without attaching
tmux send-keys -t webapp:frontend "Pause current work, prioritize the login bug" Enter

# Kill a Claude
tmux kill-window -t webapp:frontend
```

**Dashboard integration:**
- See all Claude statuses
- View message queues
- Attach button (opens terminal)
- Cost tracking per Claude

### 6. Cost Management

**Tracking:**
```yaml
# In MCP, track per session:
session:
  claude_instance: frontend
  started: 2024-01-06T10:00:00
  tokens_in: 50000
  tokens_out: 5000
  estimated_cost: $0.45
  tasks_completed: 3
```

**Limits:**
```yaml
# orchestrator-config.yaml
cost_limits:
  per_claude_hourly: $2.00
  per_claude_daily: $20.00
  total_daily: $100.00
  orchestrator_hourly: $0.50  # keep orchestrator cheap

actions_on_limit:
  warn_at: 80%
  pause_at: 100%
  alert_human: true
```

**Orchestrator respects limits:**
- Don't spawn new Claude if over budget
- Pause non-critical work
- Alert human for override

### 7. Conflict Prevention

**Orchestrator rules:**
- Same file locked by another? Don't assign overlapping work
- Two Claudes in same directory? Heightened monitoring
- Completion in area X? Notify Claudes working nearby

**Smarter than current file locks:**
```python
# Orchestrator maintains "work zones"
work_zones:
  frontend:
    directories: [/frontend, /shared/components]
    files_touched_recently: [Button.tsx, Modal.tsx]
  backend:
    directories: [/backend, /shared/api]
    files_touched_recently: [auth.py, users.py]

# On new task assignment:
if task.likely_files intersects other_claude.files_touched_recently:
    either:
      - wait for other claude to finish
      - alert human
      - assign to same claude instead
```

## Implementation Phases

### Phase 1: Foundation (Do First)
- [ ] Add `tmux_target` to MCP session registration
- [ ] Add `memory_send_message` / `memory_get_messages` tools
- [ ] Create `claude-spawn.py` launcher script
- [ ] Basic dispatcher: deliver messages via tmux send-keys
- [ ] Test with 2 Claudes manually

### Phase 2: Basic Orchestrator
- [ ] Create orchestrator daemon (polling mode)
- [ ] Auto-dispatch backlog items to assigned Claudes
- [ ] Auto-notify on task completion
- [ ] Simple conflict detection (file lock based)
- [ ] Human alert on issues

### Phase 3: Smart Orchestrator
- [ ] Add Claude brain for decisions (Haiku)
- [ ] Cost tracking per session
- [ ] Budget limits and enforcement
- [ ] Work zone tracking
- [ ] Message batching/summarization

### Phase 4: Polish
- [ ] Dashboard integration
- [ ] Config-based Claude definitions (claudes.yaml)
- [ ] Auto-spawn on demand
- [ ] Idle detection and resource cleanup
- [ ] Cross-project orchestration

## Open Questions

1. **How chatty should inter-Claude communication be?**
   - Too little: coordination suffers
   - Too much: costs explode
   - Proposal: Orchestrator summarizes and batches

2. **Should orchestrator be always-on Claude or periodic?**
   - Always-on: responsive but expensive
   - Periodic: cheaper but delayed
   - Proposal: Hybrid - cheap polling + periodic smart decisions

3. **What if a Claude goes rogue (infinite loop, wrong direction)?**
   - Timeout per task?
   - Human checkpoint for long tasks?
   - Orchestrator can send "status check" and evaluate response

4. **How to handle multi-project?**
   - Separate orchestrator per project?
   - Single orchestrator, project-aware?
   - Proposal: Single orchestrator, project namespacing

5. **Recovery from crashes?**
   - tmux survives SSH disconnect ✓
   - MCP server restart: sessions lost but backlog persists
   - Claude crash: orchestrator detects idle, can respawn

## File Structure

```
mcp-shared-memory/
├── server.py                 # MCP server (updated with messaging)
├── librarian.py             # Code analysis (existing)
├── orchestrator/
│   ├── daemon.py            # Main orchestrator loop
│   ├── brain.py             # Claude-powered decisions
│   ├── dispatcher.py        # tmux send-keys delivery
│   ├── spawner.py           # Claude launcher
│   └── config.yaml          # Orchestrator settings
├── claudes.yaml             # Claude specialist definitions
└── deploy/
    └── ...                  # Deployment package
```

## Cost Estimate

**Per hour with 3 active Claudes:**
- 3 Claudes × ~$1-3/hr each = $3-9/hr active work
- Orchestrator (Haiku, periodic) = ~$0.10-0.30/hr
- Total: ~$3-10/hr when active

**Cost controls:**
- Idle Claudes cost nothing
- Orchestrator only calls Claude brain when needed
- Batch low-priority work
- Human sets budget caps

## Next Steps

1. Review this plan - adjust as needed
2. Start Phase 1: messaging + spawner
3. Test with you manually orchestrating 2 Claudes
4. Add basic orchestrator
5. Iterate based on real usage

---

*This is a living document. Update as we build and learn.*
