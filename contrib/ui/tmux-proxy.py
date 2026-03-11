#!/usr/bin/env python3
"""
Terminal proxy for ttyd to interact with a tmux pane.
This allows direct typing without tmux keybindings.

Usage: tmux-proxy.py session:window
"""

import sys
import os
import subprocess
import select
import time
import tty
import termios

def main():
    if len(sys.argv) < 2:
        print("Usage: tmux-proxy.py session:window")
        sys.exit(1)

    target = sys.argv[1]
    session = target.split(':')[0]

    # Check if session exists
    result = subprocess.run(['tmux', 'has-session', '-t', session],
                          capture_output=True)
    if result.returncode != 0:
        print(f"Error: tmux session not found: {session}")
        sys.exit(1)

    # Set up terminal
    os.environ['TERM'] = 'xterm-256color'

    # Get initial content
    last_content = ""

    # Save terminal settings
    old_settings = termios.tcgetattr(sys.stdin)

    try:
        # Set terminal to raw mode for character-by-character input
        tty.setraw(sys.stdin.fileno())

        while True:
            # Check for input with short timeout
            rlist, _, _ = select.select([sys.stdin], [], [], 0.1)

            if rlist:
                # Read available input
                char = sys.stdin.read(1)

                if char == '\x03':  # Ctrl+C
                    subprocess.run(['tmux', 'send-keys', '-t', target, 'C-c'],
                                 capture_output=True)
                elif char == '\x04':  # Ctrl+D - exit proxy
                    break
                elif char == '\r' or char == '\n':  # Enter
                    subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'],
                                 capture_output=True)
                elif char == '\x7f':  # Backspace
                    subprocess.run(['tmux', 'send-keys', '-t', target, 'BSpace'],
                                 capture_output=True)
                elif char == '\x1b':  # Escape sequence
                    # Read more characters for arrow keys etc
                    if select.select([sys.stdin], [], [], 0.01)[0]:
                        seq = sys.stdin.read(2)
                        if seq == '[A':
                            subprocess.run(['tmux', 'send-keys', '-t', target, 'Up'],
                                         capture_output=True)
                        elif seq == '[B':
                            subprocess.run(['tmux', 'send-keys', '-t', target, 'Down'],
                                         capture_output=True)
                        elif seq == '[C':
                            subprocess.run(['tmux', 'send-keys', '-t', target, 'Right'],
                                         capture_output=True)
                        elif seq == '[D':
                            subprocess.run(['tmux', 'send-keys', '-t', target, 'Left'],
                                         capture_output=True)
                        else:
                            # Unknown escape sequence, send as-is
                            subprocess.run(['tmux', 'send-keys', '-t', target, '-l', '\x1b' + seq],
                                         capture_output=True)
                    else:
                        # Just Escape key
                        subprocess.run(['tmux', 'send-keys', '-t', target, 'Escape'],
                                     capture_output=True)
                else:
                    # Regular character
                    subprocess.run(['tmux', 'send-keys', '-t', target, '-l', char],
                                 capture_output=True)

            # Capture and display pane content
            result = subprocess.run(
                ['tmux', 'capture-pane', '-t', target, '-p', '-e'],
                capture_output=True, text=True
            )

            if result.returncode == 0:
                content = result.stdout
                if content != last_content:
                    # Clear screen and redraw
                    sys.stdout.write('\x1b[2J\x1b[H')
                    # Convert LF to CRLF for proper terminal display
                    sys.stdout.write(content.replace('\n', '\r\n'))
                    sys.stdout.flush()
                    last_content = content

    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print("\r\nProxy disconnected.")

if __name__ == '__main__':
    main()
