#!/usr/bin/env python3
"""
Claude Spawner - Launch Claude Code instances in tmux with proper context.

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.

Usage:
    ./claude-spawn.py <project> <role> [--working-dir PATH] [--initial-task TASK]

Examples:
    ./claude-spawn.py emailtriage frontend
    ./claude-spawn.py emailtriage backend --initial-task "Check backlog"
    ./claude-spawn.py webapp architect --working-dir /home/user/webapp
"""

import argparse
import subprocess
import sys
import os
import json
from pathlib import Path

# Default project configurations
# Override with --working-dir or create a projects.yaml
DEFAULT_PROJECTS = {
    "emailtriage": {
        "working_dir": os.path.expanduser("~/emailTriage"),
        "roles": {
            "frontend": {
                "specialty": "React, TypeScript, UI/UX, accessibility",
                "subdir": "web-reviewer"
            },
            "backend": {
                "specialty": "Python, API, database, authentication",
                "subdir": "TriageCore"
            },
            "tester": {
                "specialty": "Testing, QA, validation, edge cases",
                "subdir": ""
            },
            "architect": {
                "specialty": "System design, interfaces, documentation",
                "subdir": ""
            },
            "reviewer": {
                "specialty": "Code review, security, best practices",
                "subdir": ""
            }
        }
    },
    "ai-advisory-board": {
        "working_dir": os.path.expanduser("~/ai-advisory-board"),
        "roles": {
            "architect": {
                "specialty": "System design, project structure, technical planning",
                "subdir": ""
            },
            "frontend": {
                "specialty": "React, TypeScript, UI/UX, chat interfaces",
                "subdir": ""
            },
            "backend": {
                "specialty": "Python, FastAPI, WebSockets, API design",
                "subdir": ""
            }
        }
    }
}


def get_tmux_target(project: str, role: str) -> str:
    """Generate tmux target string."""
    return f"{project}:{role}"


def check_tmux_session_exists(session: str) -> bool:
    """Check if tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    return result.returncode == 0


def check_tmux_window_exists(target: str) -> bool:
    """Check if tmux window exists."""
    result = subprocess.run(
        ["tmux", "list-windows", "-t", target.split(":")[0], "-F", "#{window_name}"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return False
    windows = result.stdout.strip().split("\n")
    window_name = target.split(":")[1] if ":" in target else ""
    return window_name in windows


def create_tmux_session(session: str):
    """Create a new tmux session (detached)."""
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session],
        check=True
    )


def create_tmux_window(session: str, window: str, working_dir: str):
    """Create a new tmux window in existing session."""
    subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", window, "-c", working_dir],
        check=True
    )


def send_to_tmux(target: str, text: str):
    """Send text to tmux target."""
    subprocess.run(
        ["tmux", "send-keys", "-t", target, text, "Enter"],
        check=True
    )


def build_initial_prompt(project: str, role: str, specialty: str, initial_task: str = None) -> str:
    """Build the initial prompt for Claude."""
    task = initial_task or "Check backlog for items assigned to you"
    tmux_target = get_tmux_target(project, role)

    prompt = f"""You are the {role.upper()} specialist for the {project} project.

Your expertise: {specialty}

IMMEDIATE STARTUP SEQUENCE (do these in order):
1. Start session: memory_start_session(project="{project}", claude_instance="{role}")
2. Register with dispatcher: memory_heartbeat(session_id="<your_session_id>", status="idle", tmux_target="{tmux_target}")
3. Check backlog: memory_list_backlog(assigned_to="{role}")
4. Check messages: memory_get_messages()

WORKFLOW RULES:
- When done with a task, call memory_end_session with summary
- You will receive new tasks via this terminal (lines starting with "# MESSAGE:") - acknowledge them
- If you need another specialist, use memory_send_message to send them a message
- Lock files before editing shared code: memory_lock_files
- Periodically call memory_heartbeat to stay registered with dispatcher

Current assignment: {task}"""

    return prompt


def create_expect_script(working_dir: str) -> str:
    """Create an expect script for auto-accepting Claude's permission dialog.

    Returns path to the created script.
    """
    script_path = "/tmp/claude-autoaccept.exp"
    script_content = f'''#!/usr/bin/expect -f
set timeout 60
cd "{working_dir}"
spawn claude --dangerously-skip-permissions
expect {{
    "No, exit" {{
        # Arrow down to "Yes, proceed" option
        send "\\033\\[B"
        sleep 0.3
        send "\\r"
        interact
    }}
    ">" {{
        # Claude started without dialog (cached acceptance)
        interact
    }}
    timeout {{
        puts "Timeout waiting for Claude to start"
        exit 1
    }}
}}
'''
    with open(script_path, 'w') as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)
    return script_path


def spawn_claude(
    project: str,
    role: str,
    working_dir: str,
    specialty: str,
    initial_task: str = None,
    skip_permissions: bool = True
):
    """Spawn a Claude Code instance in tmux."""

    session = project
    window = role
    target = get_tmux_target(project, role)

    # Ensure session exists
    if not check_tmux_session_exists(session):
        print(f"Creating tmux session: {session}")
        create_tmux_session(session)
        # First window is auto-created, rename it
        subprocess.run(
            ["tmux", "rename-window", "-t", f"{session}:0", window],
            check=True
        )
    elif check_tmux_window_exists(target):
        print(f"Window {target} already exists. Attaching message instead.")
        # Just send a wake-up message
        send_to_tmux(target, f"# Wake up! New task: {initial_task or 'Check backlog'}")
        return target
    else:
        print(f"Creating tmux window: {window}")
        create_tmux_window(session, window, working_dir)

    # Start Claude
    print(f"Starting Claude in {target}...")
    send_to_tmux(target, f"cd {working_dir}")

    import time
    time.sleep(0.5)

    # Launch Claude directly (permissions should be cached)
    # Use --dangerously-skip-permissions to avoid interactive prompts
    send_to_tmux(target, "claude --dangerously-skip-permissions")

    # Wait for Claude to fully initialize (watch for the prompt)
    print("Waiting for Claude to start...")
    time.sleep(8)  # Give Claude time to initialize

    # Check if Claude started by looking for its prompt
    for attempt in range(5):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True, text=True
        )
        if ">" in result.stdout or "❯" in result.stdout:
            print("Claude prompt detected!")
            break
        time.sleep(2)

    # Send initial prompt and submit it
    prompt = build_initial_prompt(project, role, specialty, initial_task)
    print(f"Sending initial prompt to {role}...")
    # Use tmux send-keys without Enter first (to paste), then send Enter separately
    subprocess.run(
        ["tmux", "send-keys", "-t", target, prompt],
        check=True
    )
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        check=True
    )

    print(f"\nClaude '{role}' spawned in tmux target: {target}")
    print(f"To attach: tmux attach -t {session}")
    print(f"To view this window: Ctrl+B, then type :{window}")

    return target


def main():
    parser = argparse.ArgumentParser(
        description="Spawn Claude Code instances in tmux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s emailtriage frontend
  %(prog)s emailtriage backend --initial-task "Implement auth API"
  %(prog)s webapp architect --working-dir /home/user/webapp

To see running Claudes:
  tmux ls

To attach to a session:
  tmux attach -t <project>
        """
    )

    parser.add_argument("project", help="Project name (e.g., emailtriage)")
    parser.add_argument("role", help="Role name (e.g., frontend, backend, architect)")
    parser.add_argument("--working-dir", "-d", help="Working directory (overrides default)")
    parser.add_argument("--initial-task", "-t", help="Initial task description")
    parser.add_argument("--specialty", "-s", help="Role specialty (overrides default)")
    parser.add_argument("--allow-permissions", action="store_true",
                       help="Don't skip permission prompts (default: skip)")
    parser.add_argument("--list-roles", action="store_true",
                       help="List available roles for a project")

    args = parser.parse_args()

    # Get project config
    project_config = DEFAULT_PROJECTS.get(args.project, {})

    if args.list_roles:
        if project_config:
            print(f"Available roles for {args.project}:")
            for role, info in project_config.get("roles", {}).items():
                print(f"  {role}: {info.get('specialty', 'General')}")
        else:
            print(f"No default config for {args.project}. Use --specialty to define role.")
        return

    # Determine working directory
    if args.working_dir:
        working_dir = args.working_dir
    elif project_config:
        base_dir = project_config.get("working_dir", os.getcwd())
        role_config = project_config.get("roles", {}).get(args.role, {})
        subdir = role_config.get("subdir", "")
        working_dir = os.path.join(base_dir, subdir) if subdir else base_dir
    else:
        working_dir = os.getcwd()

    # Determine specialty
    if args.specialty:
        specialty = args.specialty
    elif project_config:
        role_config = project_config.get("roles", {}).get(args.role, {})
        specialty = role_config.get("specialty", f"{args.role} specialist")
    else:
        specialty = f"{args.role} specialist"

    # Verify working directory exists
    if not os.path.isdir(working_dir):
        print(f"Error: Working directory does not exist: {working_dir}")
        sys.exit(1)

    # Spawn Claude
    target = spawn_claude(
        project=args.project,
        role=args.role,
        working_dir=working_dir,
        specialty=specialty,
        initial_task=args.initial_task,
        skip_permissions=not args.allow_permissions
    )

    print(f"\nTmux target for MCP registration: {target}")


if __name__ == "__main__":
    main()
