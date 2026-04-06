"""Tests for helper functions."""

from datetime import datetime, timedelta, timezone


def test_generate_doc_id():
    """generate_doc_id produces string IDs of expected length."""
    from shared_memory.helpers import generate_doc_id

    id1 = generate_doc_id("test content", "learning")

    assert isinstance(id1, str)
    assert len(id1) == 16  # Hash prefix length

    # Different content = different ID (includes timestamp, so same content also differs)
    id2 = generate_doc_id("different content", "learning")
    assert isinstance(id2, str)
    assert len(id2) == 16


def test_generate_content_hash():
    """Content hash normalizes whitespace."""
    from shared_memory.helpers import generate_content_hash

    h1 = generate_content_hash("hello   world")
    h2 = generate_content_hash("hello world")
    h3 = generate_content_hash("hello world  ")

    # All should produce same hash after normalization
    assert h1 == h2 == h3


def test_calculate_expiry():
    """Expiry dates calculated correctly per memory type."""
    from shared_memory.helpers import calculate_expiry

    # Learning type has expiry (returns ISO string)
    expiry = calculate_expiry("learning")
    assert expiry is not None
    assert isinstance(expiry, str)
    # Should be a valid ISO date in the future
    expiry_dt = datetime.fromisoformat(expiry)
    assert expiry_dt > datetime.now(timezone.utc)

    # Architecture type never expires
    expiry = calculate_expiry("architecture")
    assert expiry is None

    # Custom days
    expiry = calculate_expiry("anything", custom_days=7)
    assert expiry is not None
    expiry_dt = datetime.fromisoformat(expiry)
    assert expiry_dt > datetime.now(timezone.utc)

    # Explicit no-expiry
    expiry = calculate_expiry("anything", custom_days=0)
    assert expiry is None


def test_is_expired():
    """Expired documents detected correctly."""
    from shared_memory.helpers import is_expired

    past = (datetime.now() - timedelta(days=1)).isoformat()
    future = (datetime.now() + timedelta(days=1)).isoformat()

    assert is_expired({"expires_at": past}) is True
    assert is_expired({"expires_at": future}) is False
    assert is_expired({}) is False  # No expiry = never expires


def test_normalize_path():
    """Path normalization strips slashes and normalizes separators."""
    from shared_memory.helpers import normalize_path

    # Strips leading/trailing slashes
    assert normalize_path("/home/user/file.py") == "home/user/file.py"
    assert normalize_path("home/user/file.py/") == "home/user/file.py"
    # Normalizes backslashes
    assert normalize_path("home\\user\\file.py") == "home/user/file.py"


def test_is_lock_stale():
    """Stale lock detection: stale if session doesn't exist."""
    from shared_memory.helpers import is_lock_stale
    from shared_memory.state import active_sessions

    # Lock for non-existent session is stale
    stale_lock = {"locked_at": datetime.now().isoformat(), "session_id": "nonexistent"}
    assert is_lock_stale(stale_lock) is True

    # Lock for active session with recent activity is not stale
    active_sessions["test_lock_session"] = {
        "project": "test",
        "last_activity": datetime.now().isoformat()
    }
    fresh_lock = {"locked_at": datetime.now().isoformat(), "session_id": "test_lock_session"}
    assert is_lock_stale(fresh_lock) is False

    # Cleanup
    del active_sessions["test_lock_session"]


def test_check_session():
    """Session validation works."""
    from shared_memory.helpers import check_session
    from shared_memory.state import active_sessions

    # Non-existent session
    assert check_session("nonexistent") is False

    # Add a session and check
    active_sessions["test_session"] = {"project": "test", "last_activity": datetime.now().isoformat()}
    assert check_session("test_session") is True

    # Cleanup
    del active_sessions["test_session"]


def test_require_session():
    """require_session returns error string for missing sessions."""
    from shared_memory.helpers import require_session

    result = require_session("nonexistent_session")
    assert result is not None
    assert "not found" in result.lower() or "error" in result.lower()


def test_path_matches_pattern():
    """Path pattern matching works."""
    from shared_memory.helpers import path_matches_pattern

    assert path_matches_pattern("/home/user/src/main.py", "/home/user/src/*")
    assert not path_matches_pattern("/home/user/other/main.py", "/home/user/src/*")
