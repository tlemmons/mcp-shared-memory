#!/usr/bin/env python3
"""
Orchestrator UI - FastAPI backend for managing multi-Claude orchestration.

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.
"""

import os
import subprocess
import json
import sys
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from pymongo import MongoClient
import asyncio
import pty
import select
import struct
import fcntl
import termios

# Add orchestrator to path for brain import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "orchestrator"))
from brain import OrchestratorBrain, ProjectManager, run_brain_cycle

# MongoDB config
MONGO_HOST = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT = int(os.getenv("MONGO_PORT", "27018"))
MONGO_DB = os.getenv("MONGO_DB", "mcp_orchestrator")
MONGO_USER = os.getenv("MONGO_USER", "mcp_orch")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD", "")

# Paths
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ORCH_DIR = os.path.join(_BASE_DIR, "..", "orchestrator")
SPAWNER_PATH = os.path.join(_ORCH_DIR, "claude-spawn.py")
DISPATCHER_PATH = os.path.join(_ORCH_DIR, "dispatcher.py")
BRAIN_PATH = os.path.join(_ORCH_DIR, "brain.py")

app = FastAPI(title="Claude Orchestrator UI")
project_manager = ProjectManager()

# MongoDB connection
mongo_uri = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
client = MongoClient(mongo_uri)
db = client[MONGO_DB]


# Pydantic models
class SpawnRequest(BaseModel):
    project: str
    role: str
    initial_task: Optional[str] = None


class MessageRequest(BaseModel):
    from_instance: str
    to_instance: str
    message: str
    priority: str = "normal"


class ProjectRole(BaseModel):
    name: str
    specialty: str
    subdir: str = ""


class ProjectConfig(BaseModel):
    name: str
    working_dir: str
    roles: List[ProjectRole]


# API Routes

@app.get("/")
async def root():
    return FileResponse(os.path.join(_BASE_DIR, "static", "index.html"))


@app.get("/api/status")
async def get_status():
    """Get overall system status."""
    # Check dispatcher
    result = subprocess.run(["pgrep", "-af", "dispatcher.py"], capture_output=True, text=True)
    dispatcher_running = "dispatcher.py" in result.stdout

    # Count agents and messages
    agent_count = db.agent_status.count_documents({})
    pending_messages = db.messages.count_documents({"status": "pending"})

    return {
        "dispatcher_running": dispatcher_running,
        "agent_count": agent_count,
        "pending_messages": pending_messages,
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/agents")
async def get_agents():
    """Get all registered agents."""
    agents = []
    for doc in db.agent_status.find():
        agents.append({
            "instance": doc.get("instance"),
            "session_id": doc.get("session_id"),
            "status": doc.get("status"),
            "tmux_target": doc.get("tmux_target"),
            "current_task": doc.get("current_task"),
            "last_heartbeat": doc.get("last_heartbeat").isoformat() if doc.get("last_heartbeat") else None
        })
    return {"agents": agents}


@app.get("/api/messages")
async def get_messages(status: Optional[str] = None, limit: int = 50):
    """Get messages from queue."""
    query = {}
    if status:
        query["status"] = status

    messages = []
    for doc in db.messages.find(query).sort("created_at", -1).limit(limit):
        messages.append({
            "id": doc.get("_id"),
            "from": doc.get("from_instance"),
            "to": doc.get("to_instance"),
            "message": doc.get("message"),
            "priority": doc.get("priority"),
            "status": doc.get("status"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "delivered_at": doc.get("delivered_at").isoformat() if doc.get("delivered_at") else None
        })
    return {"messages": messages}


@app.post("/api/messages")
async def send_message(req: MessageRequest):
    """Send a message to an agent."""
    import uuid
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    doc = {
        "_id": message_id,
        "from_instance": req.from_instance,
        "to_instance": req.to_instance,
        "message": req.message,
        "priority": req.priority,
        "status": "pending",
        "created_at": datetime.now()
    }
    db.messages.insert_one(doc)
    return {"status": "queued", "message_id": message_id}


@app.get("/api/tmux/sessions")
async def get_tmux_sessions():
    """Get all tmux sessions and windows."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"sessions": []}

    sessions = []
    for session in result.stdout.strip().split("\n"):
        if not session:
            continue
        # Get windows for this session
        win_result = subprocess.run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_index}:#{window_name}"],
            capture_output=True, text=True
        )
        windows = []
        if win_result.returncode == 0:
            for w in win_result.stdout.strip().split("\n"):
                if w:
                    idx, name = w.split(":", 1)
                    windows.append({"index": idx, "name": name, "target": f"{session}:{name}"})
        sessions.append({"name": session, "windows": windows})

    return {"sessions": sessions}


@app.post("/api/spawn")
async def spawn_agent(req: SpawnRequest):
    """Spawn a new Claude agent."""
    # Look up project working directory from database
    project_doc = db.projects.find_one({"name": req.project})
    working_dir = project_doc.get("working_dir") if project_doc else None

    # Look up role specialty
    specialty = None
    if project_doc and project_doc.get("roles"):
        for role in project_doc["roles"]:
            if role.get("name") == req.role:
                specialty = role.get("specialty")
                break

    cmd = ["python3", SPAWNER_PATH, req.project, req.role]
    if working_dir:
        cmd.extend(["--working-dir", working_dir])
    if specialty:
        cmd.extend(["--specialty", specialty])
    if req.initial_task:
        cmd.extend(["--initial-task", req.initial_task])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)

    return {
        "status": "spawned",
        "project": req.project,
        "role": req.role,
        "target": f"{req.project}:{req.role}",
        "output": result.stdout
    }


@app.post("/api/kill/{session}/{window}")
async def kill_agent(session: str, window: str):
    """Kill a tmux window (agent)."""
    target = f"{session}:{window}"
    result = subprocess.run(
        ["tmux", "kill-window", "-t", target],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Failed to kill {target}")

    # Also remove from agent_status
    db.agent_status.delete_many({"tmux_target": target})

    return {"status": "killed", "target": target}


@app.get("/api/dispatcher/status")
async def get_dispatcher_status():
    """Check if dispatcher is running."""
    result = subprocess.run(["pgrep", "-af", "dispatcher.py"], capture_output=True, text=True)

    if "dispatcher.py" in result.stdout:
        lines = [l for l in result.stdout.strip().split("\n") if "dispatcher.py" in l]
        return {"running": True, "processes": lines}
    return {"running": False, "processes": []}


@app.post("/api/dispatcher/start")
async def start_dispatcher():
    """Start the dispatcher daemon."""
    # Check if already running
    result = subprocess.run(["pgrep", "-f", "dispatcher.py"], capture_output=True, text=True)
    if result.stdout.strip():
        return {"status": "already_running"}

    subprocess.Popen(
        ["python3", DISPATCHER_PATH, "--mode", "poll"],
        stdout=open("/tmp/dispatcher.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    return {"status": "started"}


@app.post("/api/dispatcher/stop")
async def stop_dispatcher():
    """Stop the dispatcher daemon."""
    subprocess.run(["pkill", "-f", "dispatcher.py"], capture_output=True)
    return {"status": "stopped"}


@app.get("/api/projects")
async def get_projects():
    """Get configured projects from spawner."""
    # Read projects from MongoDB or return defaults
    projects = list(db.projects.find())
    if not projects:
        # Return hardcoded defaults from spawner
        return {"projects": [
            {
                "name": "emailtriage",
                "working_dir": os.path.expanduser("~/emailTriage"),
                "roles": [
                    {"name": "frontend", "specialty": "React, TypeScript, UI/UX, accessibility"},
                    {"name": "backend", "specialty": "Python, API, database, authentication"},
                    {"name": "tester", "specialty": "Testing, QA, validation, edge cases"},
                    {"name": "architect", "specialty": "System design, interfaces, documentation"},
                    {"name": "reviewer", "specialty": "Code review, security, best practices"}
                ]
            },
            {
                "name": "ai-advisory-board",
                "working_dir": os.path.expanduser("~/ai-advisory-board"),
                "roles": [
                    {"name": "architect", "specialty": "System design, project structure, technical planning"},
                    {"name": "frontend", "specialty": "React, TypeScript, UI/UX, chat interfaces"},
                    {"name": "backend", "specialty": "Python, FastAPI, WebSockets, API design"}
                ]
            }
        ]}

    return {"projects": [{
        "name": p.get("name"),
        "working_dir": p.get("working_dir"),
        "roles": p.get("roles", [])
    } for p in projects]}


@app.post("/api/projects")
async def save_project(project: ProjectConfig):
    """Save or update a project configuration."""
    db.projects.update_one(
        {"name": project.name},
        {"$set": {
            "name": project.name,
            "working_dir": project.working_dir,
            "roles": [r.dict() for r in project.roles]
        }},
        upsert=True
    )
    return {"status": "saved", "project": project.name}


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    """Delete a project configuration."""
    result = db.projects.delete_one({"name": name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted", "project": name}


# ============== PROJECT WORKFLOW ENDPOINTS ==============

class NewProjectRequest(BaseModel):
    name: str
    description: str
    working_dir: str


class HumanMessageRequest(BaseModel):
    message: str


class SpecsApprovalRequest(BaseModel):
    approved: bool
    feedback: Optional[str] = None


@app.post("/api/workflow/projects")
async def create_workflow_project(req: NewProjectRequest):
    """Create a new project and start the workflow."""
    # Check if project already exists
    existing = db.projects.find_one({"name": req.name})
    if existing:
        raise HTTPException(status_code=400, detail="Project already exists")

    result = project_manager.create_project(req.name, req.description, req.working_dir)
    return result


@app.get("/api/workflow/projects")
async def list_workflow_projects(phase: Optional[str] = None):
    """List all projects with their workflow status."""
    projects = project_manager.list_projects(phase)
    return {"projects": projects}


@app.get("/api/workflow/projects/{name}")
async def get_workflow_project(name: str):
    """Get detailed project info including conversation history."""
    project = project_manager.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {
        "name": project.get("name"),
        "description": project.get("description"),
        "working_dir": project.get("working_dir"),
        "phase": project.get("phase"),
        "specs": project.get("specs"),
        "roles": project.get("roles", []),
        "human_input_needed": project.get("human_input_needed", False),
        "pending_questions": project.get("pending_questions", []),
        "conversation_history": project.get("conversation_history", []),
        "created_at": project.get("created_at").isoformat() if project.get("created_at") else None
    }


@app.post("/api/workflow/projects/{name}/message")
async def send_human_message(name: str, req: HumanMessageRequest):
    """Send a message from human to the project (architect will receive it)."""
    project = project_manager.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Add to conversation history
    project_manager.add_conversation(name, "human", req.message)

    # If in architect_chat phase, send message to architect agent
    if project.get("phase") == "architect_chat":
        # Queue message for architect
        import uuid
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        db.messages.insert_one({
            "_id": message_id,
            "from_instance": "human",
            "to_instance": "architect",
            "message": f"[HUMAN INPUT for {name}] {req.message}",
            "priority": "high",
            "status": "pending",
            "created_at": datetime.now(),
            "project": name
        })

    # Clear human_input_needed flag
    db.projects.update_one(
        {"name": name},
        {"$set": {"human_input_needed": False, "pending_questions": []}}
    )

    return {"status": "sent", "project": name}


@app.post("/api/workflow/projects/{name}/specs/approve")
async def approve_project_specs(name: str, req: SpecsApprovalRequest):
    """Approve or request changes to project specs."""
    project = project_manager.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.get("phase") != "design_review":
        raise HTTPException(status_code=400, detail="Project not in design_review phase")

    if req.approved:
        result = project_manager.approve_specs(name, req.feedback)
        # Add to conversation
        project_manager.add_conversation(name, "human", f"[APPROVED SPECS] {req.feedback or 'Looks good, proceed.'}")
    else:
        if not req.feedback:
            raise HTTPException(status_code=400, detail="Feedback required when requesting changes")
        result = project_manager.request_changes(name, req.feedback)
        # Add to conversation
        project_manager.add_conversation(name, "human", f"[REQUESTED CHANGES] {req.feedback}")

    return result


@app.post("/api/workflow/projects/{name}/phase")
async def set_project_phase(name: str, phase: str):
    """Manually set project phase (admin override)."""
    valid_phases = ["created", "architect_chat", "design", "design_review", "development", "testing", "review", "complete", "paused"]
    if phase not in valid_phases:
        raise HTTPException(status_code=400, detail=f"Invalid phase. Must be one of: {valid_phases}")

    db.projects.update_one(
        {"name": name},
        {"$set": {"phase": phase, "phase_updated_at": datetime.now()}}
    )
    return {"status": "ok", "project": name, "phase": phase}


# ============== BRAIN ENDPOINTS ==============

@app.get("/api/brain/status")
async def get_brain_status():
    """Check if brain daemon is running."""
    result = subprocess.run(["pgrep", "-af", "brain.py"], capture_output=True, text=True)

    if "brain.py" in result.stdout:
        lines = [l for l in result.stdout.strip().split("\n") if "brain.py" in l]
        return {"running": True, "processes": lines}
    return {"running": False, "processes": []}


@app.post("/api/brain/start")
async def start_brain():
    """Start the brain daemon."""
    result = subprocess.run(["pgrep", "-f", "brain.py"], capture_output=True, text=True)
    if result.stdout.strip():
        return {"status": "already_running"}

    # Get API key from environment
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    subprocess.Popen(
        ["python3", BRAIN_PATH, "--interval", "30"],
        stdout=open("/tmp/brain.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "ANTHROPIC_API_KEY": api_key}
    )
    return {"status": "started"}


@app.post("/api/brain/stop")
async def stop_brain():
    """Stop the brain daemon."""
    subprocess.run(["pkill", "-f", "brain.py"], capture_output=True)
    return {"status": "stopped"}


@app.post("/api/brain/cycle")
async def run_brain_once():
    """Run one brain cycle manually."""
    try:
        results = run_brain_cycle()
        return {"status": "ok", "actions": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/brain/state")
async def get_brain_state():
    """Get the current system state as the brain sees it."""
    brain = OrchestratorBrain(use_haiku=True)
    state = brain.get_system_state()
    return state


@app.get("/api/alerts")
async def get_alerts(acknowledged: Optional[bool] = None):
    """Get alerts from the brain."""
    query = {}
    if acknowledged is not None:
        query["acknowledged"] = acknowledged

    alerts = []
    for doc in db.alerts.find(query).sort("created_at", -1).limit(50):
        alerts.append({
            "id": str(doc.get("_id")),
            "message": doc.get("message"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "acknowledged": doc.get("acknowledged", False)
        })
    return {"alerts": alerts}


@app.post("/api/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str):
    """Acknowledge an alert."""
    from bson import ObjectId
    db.alerts.update_one(
        {"_id": ObjectId(alert_id)},
        {"$set": {"acknowledged": True, "acknowledged_at": datetime.now()}}
    )
    return {"status": "ok"}


# ============== TMUX STREAMING ==============

import asyncio

async def tmux_stream_generator(target: str):
    """Generate SSE events from tmux pane output."""
    last_content = ""
    env = os.environ.copy()
    env["TERM"] = "xterm"

    while True:
        try:
            # Capture tmux pane content
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", "-100"],
                capture_output=True, text=True, timeout=5, env=env
            )

            if result.returncode == 0:
                content = result.stdout

                # Only send if content changed
                if content != last_content:
                    # Send the full content (client will handle display)
                    data = json.dumps({"content": content, "target": target})
                    yield f"data: {data}\n\n"
                    last_content = content
            else:
                # Session might not exist
                yield f"data: {json.dumps({'error': 'tmux target not found', 'target': target})}\n\n"
                break

        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            break

        await asyncio.sleep(1)  # Poll every second


@app.get("/api/tmux/stream/{session}/{window}")
async def stream_tmux(session: str, window: str):
    """Stream tmux pane content via SSE."""
    target = f"{session}:{window}"

    # Check if target exists - pass TERM env to avoid tmux issues
    env = os.environ.copy()
    env["TERM"] = "xterm"
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
        env=env
    )
    if result.returncode != 0:
        # Log the error for debugging
        print(f"[STREAM] tmux has-session failed for {session}: rc={result.returncode}, stderr={result.stderr}")
        raise HTTPException(status_code=404, detail=f"tmux session {session} not found: {result.stderr.decode()}")

    return StreamingResponse(
        tmux_stream_generator(target),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/api/tmux/capture/{session}/{window}")
async def capture_tmux(session: str, window: str, lines: int = 100):
    """Capture current tmux pane content (one-shot)."""
    target = f"{session}:{window}"

    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        raise HTTPException(status_code=404, detail=f"tmux target {target} not found")

    return {"target": target, "content": result.stdout}


@app.post("/api/tmux/send/{session}/{window}")
async def send_to_tmux(session: str, window: str, text: str, enter: bool = True):
    """Send text to tmux pane."""
    target = f"{session}:{window}"
    env = os.environ.copy()
    env["TERM"] = "xterm"

    # Send text first (literal, no Enter)
    result = subprocess.run(
        ["tmux", "send-keys", "-t", target, "-l", text],
        capture_output=True, text=True, env=env
    )

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Failed to send text to {target}: {result.stderr}")

    # Then send Enter if requested
    if enter:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to send Enter to {target}: {result.stderr}")

    return {"status": "sent", "target": target}


# ============== TTYD TERMINAL ==============

# Track running ttyd processes per target
ttyd_processes = {}
ttyd_port_base = 7680


@app.get("/api/terminal/start/{session}/{window}")
async def start_ttyd_terminal(session: str, window: str):
    """Start a ttyd process for accessing a tmux pane."""
    target = f"{session}:{window}"

    # Check if already running
    if target in ttyd_processes:
        proc_info = ttyd_processes[target]
        # Check if still running
        if proc_info["process"].poll() is None:
            return {"status": "running", "port": proc_info["port"], "url": f"http://{os.uname().nodename}:{proc_info['port']}"}
        else:
            # Process died, clean up
            del ttyd_processes[target]

    # Find available port
    port = ttyd_port_base
    used_ports = {p["port"] for p in ttyd_processes.values()}
    while port in used_ports:
        port += 1

    # Start ttyd with our proxy script (avoids tmux keybinding issues)
    proxy_script = os.path.join(_BASE_DIR, "tmux-proxy.py")
    cmd = [
        "ttyd",
        "-p", str(port),
        "-W",  # Writable (allow input)
        "-t", "fontSize=14",
        "-t", "fontFamily=Menlo, Monaco, Courier New, monospace",
        "python3", proxy_script, target
    ]

    print(f"[TTYD] Starting: {' '.join(cmd)}")
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ttyd_processes[target] = {"process": process, "port": port}

    # Give it a moment to start
    await asyncio.sleep(0.5)

    return {
        "status": "started",
        "port": port,
        "url": f"http://{os.uname().nodename}:{port}"
    }


@app.get("/api/terminal/stop/{session}/{window}")
async def stop_ttyd_terminal(session: str, window: str):
    """Stop a ttyd process."""
    target = f"{session}:{window}"
    if target in ttyd_processes:
        proc_info = ttyd_processes[target]
        proc_info["process"].terminate()
        del ttyd_processes[target]
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.get("/terminal")
async def terminal_page():
    """Serve the terminal popup page."""
    return FileResponse(os.path.join(_BASE_DIR, "static", "terminal.html"))


@app.websocket("/ws/terminal/{session}/{window}")
async def websocket_terminal(websocket: WebSocket, session: str, window: str):
    """WebSocket endpoint for interactive terminal access to tmux pane."""
    print(f"[WS] New connection request: session={session}, window={window}")
    await websocket.accept()
    print(f"[WS] Connection accepted for {session}:{window}")

    target = f"{session}:{window}"
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    # Check if target exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True, env=env
    )
    if result.returncode != 0:
        await websocket.send_text(f"\r\n\x1b[31mError: tmux session '{session}' not found\x1b[0m\r\n")
        await websocket.close()
        return

    # Send initial screen content with ANSI escape sequences preserved
    print(f"[WS] Capturing initial pane content for {target}")
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-e"],  # -e preserves escape sequences
        capture_output=True, text=True, env=env
    )
    if result.returncode == 0 and result.stdout:
        print(f"[WS] Sending initial content: {len(result.stdout)} chars")
        await websocket.send_text(result.stdout)
    else:
        print(f"[WS] capture-pane failed or empty: rc={result.returncode}, stderr={result.stderr}")

    last_content = result.stdout if result.returncode == 0 else ""

    async def read_output():
        """Poll tmux pane and send changes to websocket."""
        nonlocal last_content
        while True:
            await asyncio.sleep(0.1)  # Poll every 100ms
            try:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", target, "-p", "-e"],  # -e preserves escape sequences
                    capture_output=True, text=True, env=env, timeout=2
                )
                if result.returncode == 0:
                    content = result.stdout
                    if content != last_content:
                        # Clear screen and redraw with preserved formatting
                        await websocket.send_text("\x1b[2J\x1b[H" + content)
                        last_content = content
            except subprocess.TimeoutExpired:
                pass
            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"[WS read] Error: {e}")
                break

    # Start reading task
    read_task = asyncio.create_task(read_output())

    # Track current terminal size
    current_cols = 80
    current_rows = 24

    try:
        while True:
            # Receive input from websocket
            data = await websocket.receive_text()

            # Handle special key commands
            if data.startswith('\x00RESIZE:'):
                # Parse resize command: \x00RESIZE:cols:rows\x00
                try:
                    parts = data.strip('\x00').split(':')
                    if len(parts) == 3:
                        cols = int(parts[1])
                        rows = int(parts[2])
                        print(f"[WS] Resizing tmux pane to {cols}x{rows}")
                        # Resize the tmux pane
                        subprocess.run(
                            ["tmux", "resize-window", "-t", target, "-x", str(cols), "-y", str(rows)],
                            env=env, timeout=2, capture_output=True
                        )
                        current_cols = cols
                        current_rows = rows
                except Exception as e:
                    print(f"[WS] Resize error: {e}")
            elif data == '\x00ENTER\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], env=env, timeout=2)
            elif data == '\x00BACKSPACE\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "BSpace"], env=env, timeout=2)
            elif data == '\x00UP\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "Up"], env=env, timeout=2)
            elif data == '\x00DOWN\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "Down"], env=env, timeout=2)
            elif data == '\x00LEFT\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "Left"], env=env, timeout=2)
            elif data == '\x00RIGHT\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "Right"], env=env, timeout=2)
            elif data == '\x00CTRLC\x00':
                subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], env=env, timeout=2)
            else:
                # Send literal text
                subprocess.run(["tmux", "send-keys", "-t", target, "-l", data], env=env, timeout=2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        read_task.cancel()


# Mount static files
app.mount("/static", StaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
