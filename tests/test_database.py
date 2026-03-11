"""Tests for database query SQL validation."""


def test_validate_sql_readonly_allows_select():
    """Valid SELECT queries pass validation."""
    from shared_memory.tools.database import _validate_sql_readonly

    assert _validate_sql_readonly("SELECT * FROM users") is None
    assert _validate_sql_readonly("SELECT COUNT(*) FROM orders WHERE status = 'active'") is None
    assert _validate_sql_readonly("SELECT TOP 10 name, email FROM users") is None


def test_validate_sql_readonly_allows_with():
    """WITH (CTE) queries pass validation."""
    from shared_memory.tools.database import _validate_sql_readonly

    query = "WITH cte AS (SELECT id FROM users) SELECT * FROM cte"
    assert _validate_sql_readonly(query) is None


def test_validate_sql_readonly_blocks_mutations():
    """Dangerous SQL statements are blocked."""
    from shared_memory.tools.database import _validate_sql_readonly

    assert _validate_sql_readonly("INSERT INTO users VALUES (1, 'test')") is not None
    assert _validate_sql_readonly("UPDATE users SET name = 'x'") is not None
    assert _validate_sql_readonly("DELETE FROM users") is not None
    assert _validate_sql_readonly("DROP TABLE users") is not None
    assert _validate_sql_readonly("ALTER TABLE users ADD col INT") is not None
    assert _validate_sql_readonly("TRUNCATE TABLE users") is not None
    assert _validate_sql_readonly("EXEC sp_something") is not None


def test_validate_sql_readonly_blocks_semicolons():
    """Semicolons (multi-statement) are blocked."""
    from shared_memory.tools.database import _validate_sql_readonly

    assert _validate_sql_readonly("SELECT 1; DROP TABLE users") is not None


def test_validate_sql_readonly_blocks_comments():
    """SQL comments are blocked (injection vector)."""
    from shared_memory.tools.database import _validate_sql_readonly

    assert _validate_sql_readonly("SELECT * FROM users -- drop table") is not None
    assert _validate_sql_readonly("SELECT * FROM users /* comment */") is not None


def test_validate_sql_readonly_blocks_non_select():
    """Queries not starting with SELECT or WITH are blocked."""
    from shared_memory.tools.database import _validate_sql_readonly

    assert _validate_sql_readonly("SHOW TABLES") is not None
    assert _validate_sql_readonly("DESCRIBE users") is not None
