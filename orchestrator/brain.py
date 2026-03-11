#!/usr/bin/env python3
"""
Orchestrator Brain - Claude-powered decision making for multi-Claude coordination.

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.

The brain makes intelligent decisions about:
- When to spawn Claudes
- Task assignment
- Conflict resolution
- Progress monitoring
- Human escalation
"""

import os
import json
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from enum import Enum
import anthropic
from pymongo import MongoClient

# MongoDB config
MONGO_HOST = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT = int(os.getenv("MONGO_PORT", "27018"))
MONGO_DB = os.getenv("MONGO_DB", "mcp_orchestrator")
MONGO_USER = os.getenv("MONGO_USER", "mcp_orch")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD", "")

# Anthropic API key
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Paths
SPAWNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude-spawn.py")


class ProjectPhase(Enum):
    """Project lifecycle phases."""
    CREATED = "created"              # Project just created, needs architect
    ARCHITECT_CHAT = "architect_chat"  # Architect gathering requirements from human
    DESIGN = "design"                # Architect creating specs
    DESIGN_REVIEW = "design_review"  # Human reviewing specs
    DEVELOPMENT = "development"      # Devs building
    TESTING = "testing"              # QA/testing phase
    REVIEW = "review"                # Human review of deliverables
    COMPLETE = "complete"            # Project done
    PAUSED = "paused"                # Human paused the project


class OrchestratorBrain:
    """Claude-powered orchestrator brain."""

    def __init__(self, use_haiku: bool = True):
        """Initialize the brain.

        Args:
            use_haiku: Use Haiku for cheap/fast decisions (recommended)
        """
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = "claude-3-5-haiku-20241022" if use_haiku else "claude-sonnet-4-20250514"

        # MongoDB connection
        mongo_uri = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
        self.mongo = MongoClient(mongo_uri)
        self.db = self.mongo[MONGO_DB]

    def get_system_state(self) -> Dict[str, Any]:
        """Gather current system state for decision making."""
        state = {
            "timestamp": datetime.now().isoformat(),
            "projects": [],
            "agents": [],
            "pending_messages": [],
            "tmux_sessions": []
        }

        # Get all projects
        for proj in self.db.projects.find():
            state["projects"].append({
                "name": proj.get("name"),
                "phase": proj.get("phase", "created"),
                "description": proj.get("description"),
                "specs": proj.get("specs"),
                "working_dir": proj.get("working_dir"),
                "roles": proj.get("roles", []),
                "created_at": proj.get("created_at"),
                "human_input_needed": proj.get("human_input_needed", False),
                "pending_questions": proj.get("pending_questions", [])
            })

        # Get active agents
        for agent in self.db.agent_status.find():
            last_hb = agent.get("last_heartbeat")
            if last_hb:
                idle_minutes = (datetime.now() - last_hb).total_seconds() / 60
            else:
                idle_minutes = None

            state["agents"].append({
                "instance": agent.get("instance"),
                "project": agent.get("project"),
                "status": agent.get("status"),
                "current_task": agent.get("current_task"),
                "tmux_target": agent.get("tmux_target"),
                "idle_minutes": round(idle_minutes, 1) if idle_minutes else None
            })

        # Get pending messages
        for msg in self.db.messages.find({"status": "pending"}).limit(20):
            state["pending_messages"].append({
                "id": msg.get("_id"),
                "from": msg.get("from_instance"),
                "to": msg.get("to_instance"),
                "priority": msg.get("priority"),
                "message": msg.get("message", "")[:100]
            })

        # Get tmux sessions
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                for session in result.stdout.strip().split("\n"):
                    if session:
                        win_result = subprocess.run(
                            ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
                            capture_output=True, text=True
                        )
                        windows = win_result.stdout.strip().split("\n") if win_result.returncode == 0 else []
                        state["tmux_sessions"].append({
                            "session": session,
                            "windows": [w for w in windows if w]
                        })
        except Exception:
            pass

        return state

    def decide(self, state: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Make decisions about what actions to take.

        Returns list of actions to execute.
        """
        if state is None:
            state = self.get_system_state()

        prompt = self._build_decision_prompt(state)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        # Parse response into actions
        response_text = response.content[0].text
        return self._parse_actions(response_text)

    def _build_decision_prompt(self, state: Dict[str, Any]) -> str:
        """Build the prompt for decision making."""
        return f"""You are the orchestrator brain for a multi-Claude development system.

CURRENT STATE:
{json.dumps(state, indent=2, default=str)}

PROJECT PHASES:
- created: New project, needs architect spawned
- architect_chat: Architect gathering requirements from human
- design: Architect creating technical specs
- design_review: Human reviewing specs (wait for approval)
- development: Devs building (spawn devs, assign tasks)
- testing: QA phase
- review: Human reviewing deliverables
- complete: Done
- paused: Human paused

YOUR JOB:
Analyze the state and decide what actions to take. Be conservative - only take actions that are clearly needed.

AVAILABLE ACTIONS:
1. spawn_agent(project, role, initial_task) - Start a new Claude
2. send_message(from, to, message, priority) - Queue a message
3. update_project_phase(project, new_phase) - Change project phase
4. set_human_input_needed(project, questions) - Flag for human input
5. assign_task(project, agent, task_description) - Assign work to an agent
6. alert_human(message) - Escalate to human
7. no_action(reason) - Do nothing (explain why)

RULES:
- If a project is in "created" phase and has a description, spawn an architect
- If architect_chat phase and no architect agent running, spawn one
- If design_review phase, wait for human (don't auto-advance)
- If development phase and no devs running, spawn appropriate devs
- Don't spawn duplicate agents for same role in same project
- If an agent has been idle >30 min, check on it
- If pending messages exist for non-existent agents, alert human

IMPORTANT: For role names, ONLY use these EXACT values:
- "architect" (NOT "Systems Architect", NOT "Architect", just lowercase "architect")
- "frontend" (NOT "Frontend Developer", just "frontend")
- "backend" (NOT "Backend Developer", just "backend")
- "tester" (for QA/testing)
- "reviewer" (for code review)

RESPOND WITH JSON array of actions:
[
  {{"action": "spawn_agent", "project": "...", "role": "...", "initial_task": "..."}},
  {{"action": "no_action", "reason": "..."}}
]

Only output the JSON array, no other text."""

    def _parse_actions(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse the brain's response into actions."""
        try:
            # Find JSON array in response
            start = response_text.find('[')
            end = response_text.rfind(']') + 1
            if start >= 0 and end > start:
                return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass
        return [{"action": "no_action", "reason": "Failed to parse brain response"}]

    def execute_actions(self, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute the decided actions.

        Returns results of each action.
        """
        results = []

        for action in actions:
            action_type = action.get("action")
            result = {"action": action_type, "status": "unknown"}

            try:
                if action_type == "spawn_agent":
                    result = self._execute_spawn(action)
                elif action_type == "send_message":
                    result = self._execute_send_message(action)
                elif action_type == "update_project_phase":
                    result = self._execute_update_phase(action)
                elif action_type == "set_human_input_needed":
                    result = self._execute_set_human_input(action)
                elif action_type == "assign_task":
                    result = self._execute_assign_task(action)
                elif action_type == "alert_human":
                    result = self._execute_alert(action)
                elif action_type == "no_action":
                    result = {"action": "no_action", "status": "ok", "reason": action.get("reason")}
                else:
                    result = {"action": action_type, "status": "error", "error": "Unknown action"}
            except Exception as e:
                result = {"action": action_type, "status": "error", "error": str(e)}

            results.append(result)

        return results

    def _execute_spawn(self, action: Dict) -> Dict:
        """Spawn a new Claude agent."""
        project = action.get("project")
        role = action.get("role")
        task = action.get("initial_task", "Check backlog and await instructions")

        # Check if already running - match by role keyword (case-insensitive)
        # This handles cases where agent registered as "Systems Architect" but role is "architect"
        role_lower = role.lower()
        for agent in self.db.agent_status.find({"project": project}):
            instance = (agent.get("instance") or "").lower()
            if role_lower in instance or instance in role_lower:
                return {"action": "spawn_agent", "status": "skipped",
                        "reason": f"Agent matching '{role}' already running: {agent.get('instance')}"}

        # Also check tmux windows for the project session
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-t", project, "-F", "#{window_name}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                windows = result.stdout.strip().lower().split("\n")
                for win in windows:
                    if role_lower in win or win in role_lower:
                        return {"action": "spawn_agent", "status": "skipped",
                                "reason": f"tmux window matching '{role}' already exists: {win}"}
        except Exception:
            pass

        # Check recent spawns to avoid rapid re-spawning
        recent = self.db.spawn_history.find_one({
            "project": project,
            "role": role,
            "spawned_at": {"$gte": datetime.now() - timedelta(minutes=5)}
        })
        if recent:
            return {"action": "spawn_agent", "status": "skipped",
                    "reason": f"{role} was spawned recently (within 5 min)"}

        cmd = ["python3", SPAWNER_PATH, project, role, "--initial-task", task]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            # Record the spawn to prevent rapid re-spawning
            self.db.spawn_history.insert_one({
                "project": project,
                "role": role,
                "spawned_at": datetime.now(),
                "initial_task": task
            })
            return {"action": "spawn_agent", "status": "ok", "project": project, "role": role}
        else:
            return {"action": "spawn_agent", "status": "error", "error": result.stderr}

    def _execute_send_message(self, action: Dict) -> Dict:
        """Send a message to an agent."""
        import uuid
        message_id = f"msg_{uuid.uuid4().hex[:12]}"

        doc = {
            "_id": message_id,
            "from_instance": action.get("from", "orchestrator"),
            "to_instance": action.get("to"),
            "message": action.get("message"),
            "priority": action.get("priority", "normal"),
            "status": "pending",
            "created_at": datetime.now()
        }
        self.db.messages.insert_one(doc)
        return {"action": "send_message", "status": "ok", "message_id": message_id}

    def _execute_update_phase(self, action: Dict) -> Dict:
        """Update a project's phase."""
        project = action.get("project")
        new_phase = action.get("new_phase")

        result = self.db.projects.update_one(
            {"name": project},
            {"$set": {
                "phase": new_phase,
                "phase_updated_at": datetime.now()
            }}
        )

        if result.modified_count > 0:
            return {"action": "update_project_phase", "status": "ok", "project": project, "phase": new_phase}
        else:
            return {"action": "update_project_phase", "status": "error", "error": "Project not found"}

    def _execute_set_human_input(self, action: Dict) -> Dict:
        """Flag a project as needing human input."""
        project = action.get("project")
        questions = action.get("questions", [])

        self.db.projects.update_one(
            {"name": project},
            {"$set": {
                "human_input_needed": True,
                "pending_questions": questions
            }}
        )
        return {"action": "set_human_input_needed", "status": "ok", "project": project}

    def _execute_assign_task(self, action: Dict) -> Dict:
        """Assign a task to an agent via message."""
        return self._execute_send_message({
            "from": "orchestrator",
            "to": action.get("agent"),
            "message": f"[TASK ASSIGNMENT] {action.get('task_description')}",
            "priority": "high"
        })

    def _execute_alert(self, action: Dict) -> Dict:
        """Alert the human (log for now, could be notification)."""
        message = action.get("message")

        # Store alert in MongoDB
        self.db.alerts.insert_one({
            "message": message,
            "created_at": datetime.now(),
            "acknowledged": False
        })

        # Also print to console
        print(f"[ALERT] {message}")

        return {"action": "alert_human", "status": "ok", "message": message}


class ProjectManager:
    """Manage project lifecycle."""

    def __init__(self):
        mongo_uri = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
        self.mongo = MongoClient(mongo_uri)
        self.db = self.mongo[MONGO_DB]

    def create_project(self, name: str, description: str, working_dir: str) -> Dict:
        """Create a new project."""
        project = {
            "name": name,
            "description": description,
            "working_dir": working_dir,
            "phase": ProjectPhase.CREATED.value,
            "created_at": datetime.now(),
            "roles": [],
            "specs": None,
            "human_input_needed": False,
            "pending_questions": [],
            "conversation_history": []
        }

        self.db.projects.insert_one(project)
        return {"status": "created", "project": name, "phase": "created"}

    def get_project(self, name: str) -> Optional[Dict]:
        """Get a project by name."""
        return self.db.projects.find_one({"name": name})

    def update_specs(self, name: str, specs: str) -> Dict:
        """Update project specs (from architect)."""
        self.db.projects.update_one(
            {"name": name},
            {"$set": {"specs": specs, "specs_updated_at": datetime.now()}}
        )
        return {"status": "ok", "project": name}

    def add_conversation(self, name: str, role: str, message: str) -> Dict:
        """Add to project conversation history."""
        entry = {
            "role": role,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        self.db.projects.update_one(
            {"name": name},
            {"$push": {"conversation_history": entry}}
        )
        return {"status": "ok"}

    def approve_specs(self, name: str, feedback: str = None) -> Dict:
        """Human approves specs, move to development."""
        update = {
            "phase": ProjectPhase.DEVELOPMENT.value,
            "human_input_needed": False,
            "specs_approved_at": datetime.now()
        }
        if feedback:
            update["specs_feedback"] = feedback

        self.db.projects.update_one({"name": name}, {"$set": update})
        return {"status": "ok", "project": name, "phase": "development"}

    def request_changes(self, name: str, feedback: str) -> Dict:
        """Human requests changes to specs."""
        self.db.projects.update_one(
            {"name": name},
            {"$set": {
                "phase": ProjectPhase.DESIGN.value,
                "human_input_needed": False,
                "specs_feedback": feedback
            }}
        )
        return {"status": "ok", "project": name, "phase": "design"}

    def set_roles(self, name: str, roles: List[Dict]) -> Dict:
        """Set the roles for a project (from architect recommendation)."""
        self.db.projects.update_one(
            {"name": name},
            {"$set": {"roles": roles}}
        )
        return {"status": "ok", "project": name}

    def list_projects(self, phase: str = None) -> List[Dict]:
        """List all projects, optionally filtered by phase."""
        query = {}
        if phase:
            query["phase"] = phase

        projects = []
        for proj in self.db.projects.find(query):
            projects.append({
                "name": proj.get("name"),
                "description": proj.get("description"),
                "phase": proj.get("phase"),
                "working_dir": proj.get("working_dir"),
                "created_at": proj.get("created_at"),
                "human_input_needed": proj.get("human_input_needed", False)
            })
        return projects


def run_brain_cycle():
    """Run one cycle of the orchestrator brain."""
    brain = OrchestratorBrain(use_haiku=True)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running brain cycle...")

    state = brain.get_system_state()
    print(f"  Projects: {len(state['projects'])}, Agents: {len(state['agents'])}, Pending msgs: {len(state['pending_messages'])}")

    actions = brain.decide(state)
    print(f"  Decisions: {len(actions)}")

    results = brain.execute_actions(actions)
    for r in results:
        print(f"    {r['action']}: {r['status']}")

    return results


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Orchestrator Brain")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles")
    args = parser.parse_args()

    if args.once:
        run_brain_cycle()
    else:
        print(f"Starting orchestrator brain (interval: {args.interval}s)")
        while True:
            try:
                run_brain_cycle()
            except Exception as e:
                print(f"[ERROR] Brain cycle failed: {e}")
            time.sleep(args.interval)
