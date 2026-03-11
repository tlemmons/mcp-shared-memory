"""Database query tools - read-only access to external databases."""

import json
import re
import pymssql
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.state import active_sessions
from shared_memory.config import DB_REGISTRY, SQL_BLOCKED_KEYWORDS
from shared_memory.helpers import require_session


def _get_db_connection(db_name: str):
    """Get a connection to a registered database."""
    if db_name not in DB_REGISTRY:
        return None, f"Database '{db_name}' not registered. Available: {list(DB_REGISTRY.keys())}"

    config = DB_REGISTRY[db_name]
    if config["type"] == "mssql":
        try:
            conn = pymssql.connect(
                server=config["host"],
                port=config["port"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                login_timeout=10,
                timeout=config.get("query_timeout", 30),
                as_dict=True
            )
            return conn, None
        except Exception as e:
            return None, f"Connection failed: {str(e)}"
    else:
        return None, f"Unsupported database type: {config['type']}"


def _validate_sql_readonly(sql: str) -> str | None:
    """Validate SQL is read-only. Returns error message or None if safe."""
    stripped = sql.strip().rstrip(";").strip()

    # Must start with SELECT or WITH (for CTEs)
    if not re.match(r'^(SELECT|WITH)\b', stripped, re.IGNORECASE):
        return "Only SELECT queries are allowed (query must start with SELECT or WITH)"

    # Check for blocked keywords anywhere in the query
    match = SQL_BLOCKED_KEYWORDS.search(stripped)
    if match:
        return f"Blocked keyword detected: '{match.group()}'. Only read-only queries are allowed."

    # Block semicolons (prevent multi-statement injection)
    if ";" in stripped:
        return "Multiple statements (semicolons) are not allowed"

    # Block comments that could hide injection
    if "--" in stripped or "/*" in stripped:
        return "SQL comments are not allowed in queries"

    return None


@mcp.tool()
async def memory_db(
    session_id: str,
    action: str,
    database: str = None,
    query: str = None,
    table: str = None,
    limit: int = 100,
    ctx: Context = None
) -> str:
    """
    Query external databases (read-only). Use for data analysis, debugging, and verification.

    Actions:

    action="list" - List available databases
        No params required.

    action="schema" - Explore database schema
        Required: database
        Optional: table (show columns for specific table)
        Without table: lists all tables. With table: shows columns, types, nullability.

    action="query" - Run a SELECT query
        Required: database, query
        Optional: limit (default 100, max 500)
        Only SELECT/WITH statements allowed. No INSERT/UPDATE/DELETE/DROP.

    Args:
        session_id: Your session ID
        action: One of: list, schema, query
        database: Database name (e.g., "nimbus")
        query: SQL SELECT query to execute
        table: Table name for schema exploration
        limit: Max rows to return (default 100, max 500)
    """
    error = require_session(session_id)
    if error:
        return error

    # -- LIST --
    if action == "list":
        databases = []
        for name, config in DB_REGISTRY.items():
            databases.append({
                "name": name,
                "type": config["type"],
                "host": config["host"],
                "database": config["database"],
                "read_only": config.get("read_only", True),
                "max_rows": config.get("max_rows", 500),
            })
        return json.dumps({"count": len(databases), "databases": databases}, indent=2)

    # All other actions require database
    if not database:
        return json.dumps({"error": "database parameter required"})

    # -- SCHEMA --
    if action == "schema":
        conn, err = _get_db_connection(database)
        if err:
            return json.dumps({"error": err})

        try:
            cursor = conn.cursor()
            if table:
                # Show columns for specific table
                cursor.execute("""
                    SELECT
                        c.COLUMN_NAME as name,
                        c.DATA_TYPE as type,
                        c.CHARACTER_MAXIMUM_LENGTH as max_length,
                        c.IS_NULLABLE as nullable,
                        c.COLUMN_DEFAULT as default_value,
                        CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END as is_primary_key
                    FROM INFORMATION_SCHEMA.COLUMNS c
                    LEFT JOIN (
                        SELECT ku.COLUMN_NAME, ku.TABLE_NAME
                        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                            ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                    ) pk ON pk.TABLE_NAME = c.TABLE_NAME AND pk.COLUMN_NAME = c.COLUMN_NAME
                    WHERE c.TABLE_NAME = %s
                    ORDER BY c.ORDINAL_POSITION
                """, (table,))
                columns = cursor.fetchall()
                if not columns:
                    return json.dumps({"error": f"Table '{table}' not found"})
                return json.dumps({
                    "database": database,
                    "table": table,
                    "column_count": len(columns),
                    "columns": columns
                }, indent=2)
            else:
                # List all tables with row counts and schema
                cursor.execute("""
                    SELECT
                        t.TABLE_SCHEMA as [schema],
                        t.TABLE_NAME as name,
                        t.TABLE_SCHEMA + '.' + t.TABLE_NAME as qualified_name,
                        p.rows as row_count
                    FROM INFORMATION_SCHEMA.TABLES t
                    LEFT JOIN sys.partitions p
                        ON p.object_id = OBJECT_ID(t.TABLE_SCHEMA + '.' + t.TABLE_NAME)
                        AND p.index_id IN (0, 1)
                    WHERE t.TABLE_TYPE = 'BASE TABLE'
                    ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
                """)
                tables = cursor.fetchall()
                return json.dumps({
                    "database": database,
                    "table_count": len(tables),
                    "note": "Use qualified_name (e.g., picFrame.Frames) in queries",
                    "tables": tables
                }, indent=2)
        except Exception as e:
            return json.dumps({"error": f"Schema query failed: {str(e)}"})
        finally:
            conn.close()

    # -- QUERY --
    elif action == "query":
        if not query:
            return json.dumps({"error": "query parameter required"})

        config = DB_REGISTRY[database]

        # Enforce read-only
        if config.get("read_only", True):
            validation_error = _validate_sql_readonly(query)
            if validation_error:
                return json.dumps({"error": validation_error})

        # Cap limit
        max_rows = config.get("max_rows", 500)
        if limit > max_rows:
            limit = max_rows

        conn, err = _get_db_connection(database)
        if err:
            return json.dumps({"error": err})

        try:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchmany(limit + 1)  # Fetch one extra to detect truncation

            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]

            # Convert any non-serializable types
            clean_rows = []
            for row in rows:
                clean = {}
                for k, v in row.items():
                    if isinstance(v, datetime):
                        clean[k] = v.isoformat()
                    elif isinstance(v, bytes):
                        clean[k] = v.hex()
                    elif v is None:
                        clean[k] = None
                    else:
                        clean[k] = str(v) if not isinstance(v, (int, float, bool, str)) else v
                clean_rows.append(clean)

            result = {
                "database": database,
                "row_count": len(clean_rows),
                "truncated": truncated,
                "rows": clean_rows
            }
            if truncated:
                result["note"] = f"Results truncated to {limit} rows. Use LIMIT/TOP or narrow your WHERE clause."

            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": f"Query failed: {str(e)}"})
        finally:
            conn.close()

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Must be one of: list, schema, query"})
