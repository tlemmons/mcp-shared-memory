"""Tests that all tools register correctly."""


def test_all_38_tools_register():
    """All 38 tools are registered with the MCP server."""
    from shared_memory.app import create_app

    mcp = create_app()
    tools = mcp._tool_manager._tools

    assert len(tools) == 38, f"Expected 38 tools, got {len(tools)}: {sorted(tools.keys())}"


def test_expected_tools_present():
    """Key tools are present by name."""
    from shared_memory.app import create_app

    mcp = create_app()
    tools = set(mcp._tool_manager._tools.keys())

    expected = {
        "memory_start_session", "memory_end_session",
        "memory_query", "memory_store", "memory_record_learning",
        "memory_lock_files", "memory_unlock_files", "memory_get_locks",
        "memory_send_message", "memory_get_messages",
        "memory_add_backlog_item", "memory_list_backlog",
        "memory_register_function", "memory_find_function",
        "memory_project", "memory_checklist", "memory_db",
        "memory_define_spec", "memory_list_agents",
    }

    missing = expected - tools
    assert not missing, f"Missing tools: {missing}"
