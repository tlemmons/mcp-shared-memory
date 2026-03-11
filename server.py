#!/usr/bin/env python3
"""
Shared Memory MCP Server - Compatibility shim.

This file imports from the modular package in src/shared_memory/.
For the monolithic version, see server_monolith.py.

Usage:
    python server.py [--host HOST] [--port PORT]
    python -m shared_memory [--host HOST] [--port PORT]
"""

import sys
import os

# Add src/ to path so the package can be found
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from shared_memory.__main__ import main

if __name__ == "__main__":
    main()
