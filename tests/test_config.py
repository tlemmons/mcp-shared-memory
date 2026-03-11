"""Tests for configuration module."""

import os


def test_config_imports():
    """Config module loads without error."""
    from shared_memory.config import (
        CHROMA_HOST, CHROMA_PORT,
        MONGO_HOST, MONGO_PORT,
        MEMORY_TYPES, DOC_STATUSES,
        BACKLOG_STATUSES, BACKLOG_PRIORITIES,
        MESSAGE_PRIORITIES, MESSAGE_CATEGORIES,
        SQL_BLOCKED_KEYWORDS,
    )
    assert isinstance(CHROMA_HOST, str)
    assert isinstance(CHROMA_PORT, int)
    assert len(MEMORY_TYPES) > 10
    assert "active" in DOC_STATUSES
    assert "critical" in BACKLOG_PRIORITIES


def test_db_registry_from_env(monkeypatch):
    """DB_REGISTRY is built from DB_<NAME>_* environment variables."""
    monkeypatch.setenv("DB_TESTDB_TYPE", "mssql")
    monkeypatch.setenv("DB_TESTDB_HOST", "test.example.com")
    monkeypatch.setenv("DB_TESTDB_PORT", "1433")
    monkeypatch.setenv("DB_TESTDB_NAME", "mydb")
    monkeypatch.setenv("DB_TESTDB_USER", "user")
    monkeypatch.setenv("DB_TESTDB_PASS", "pass")

    from shared_memory.config import DB_REGISTRY, _build_db_registry
    _build_db_registry()

    assert "testdb" in DB_REGISTRY
    assert DB_REGISTRY["testdb"]["host"] == "test.example.com"
    assert DB_REGISTRY["testdb"]["type"] == "mssql"
    assert DB_REGISTRY["testdb"]["read_only"] is True


def test_sql_blocked_keywords():
    """SQL_BLOCKED_KEYWORDS catches dangerous SQL."""
    from shared_memory.config import SQL_BLOCKED_KEYWORDS

    assert SQL_BLOCKED_KEYWORDS.search("DROP TABLE users")
    assert SQL_BLOCKED_KEYWORDS.search("INSERT INTO foo VALUES (1)")
    assert SQL_BLOCKED_KEYWORDS.search("DELETE FROM bar")
    assert SQL_BLOCKED_KEYWORDS.search("EXEC sp_something")
    assert not SQL_BLOCKED_KEYWORDS.search("SELECT * FROM users")
    assert not SQL_BLOCKED_KEYWORDS.search("SELECT COUNT(*) FROM orders WHERE status = 'active'")
