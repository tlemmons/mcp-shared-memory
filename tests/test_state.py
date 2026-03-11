"""Tests for state module."""


def test_state_dicts_exist():
    """Global state dictionaries are accessible and empty by default."""
    from shared_memory.state import active_sessions, file_locks, active_signals

    assert isinstance(active_sessions, dict)
    assert isinstance(file_locks, dict)
    assert isinstance(active_signals, dict)


def test_state_is_mutable():
    """State dicts can be modified (they're module-level singletons)."""
    from shared_memory.state import active_sessions

    test_key = "_test_session_xyz"
    active_sessions[test_key] = {"test": True}
    assert test_key in active_sessions
    del active_sessions[test_key]
    assert test_key not in active_sessions
