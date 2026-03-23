"""Audit logging for security-sensitive operations.

Logs to MongoDB audit_log collection with TTL-based retention.
"""

from datetime import datetime
from typing import Any, Dict, Optional


def log_audit(
    event_type: str,
    actor: str,
    project: str = "",
    details: Optional[Dict[str, Any]] = None,
    session_id: str = "",
) -> None:
    """Log an audit event to MongoDB.

    Args:
        event_type: Category of event (e.g., "auth.login", "admin.key_created",
                    "spec.overwrite_blocked", "session.start")
        actor: Who performed the action (claude_instance or api_key name)
        project: Project context
        details: Additional event-specific data
        session_id: Session that triggered the event
    """
    # Lazy import to avoid circular dependency
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return  # MongoDB unavailable, skip audit (non-fatal)

    db.audit_log.insert_one({
        "event_type": event_type,
        "actor": actor,
        "project": project,
        "session_id": session_id,
        "details": details or {},
        "timestamp": datetime.now(),
    })
