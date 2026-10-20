from sqlalchemy.orm import Session as DBSession

from src.models.usage import UsageRecord


def record_usage(
    db: DBSession,
    user_id: str,
    session_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    duration_ms: int,
    tcu: float = 0.0,
    message_id: str | None = None,
    status: str = "completed",
) -> UsageRecord:
    """Create a UsageRecord directly (status='completed' by default).

    NOTE: In the main chat/agent flow, entitlement_service.finalize_usage()
    is used instead (it updates the pending reservation). This function is
    kept for backward compatibility with direct callers and tests.
    """
    record = UsageRecord(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
        tcu=tcu,
        duration_ms=duration_ms,
        status=status,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_user_usage(db: DBSession, user_id: str) -> dict:
    """Return all-time totals for a user (completed records only).

    DEPRECATED for API responses — use entitlement_service.get_user_period_usage()
    which returns daily/monthly breakdowns with live limits.
    Kept for backward compatibility with existing tests.
    """
    records = (
        db.query(UsageRecord)
        .filter(UsageRecord.user_id == user_id, UsageRecord.status == "completed")
        .all()
    )
    return {
        "user_id": user_id,
        "total_requests": len(records),
        "total_input_tokens": sum(r.input_tokens for r in records),
        "total_output_tokens": sum(r.output_tokens for r in records),
        "total_cached_tokens": sum(r.cached_tokens for r in records),
        "total_cost_usd": round(sum(r.cost_usd for r in records), 6),
        "total_tcu": round(sum(r.tcu for r in records), 4),
        "total_duration_ms": sum(r.duration_ms for r in records),
    }


def get_session_usage(db: DBSession, session_id: str, user_id: str) -> dict:
    """Return completed usage totals for a session."""
    records = (
        db.query(UsageRecord)
        .filter(
            UsageRecord.session_id == session_id,
            UsageRecord.user_id == user_id,
            UsageRecord.status == "completed",
        )
        .all()
    )
    return {
        "session_id": session_id,
        "total_calls": len(records),
        "total_input_tokens": sum(r.input_tokens for r in records),
        "total_output_tokens": sum(r.output_tokens for r in records),
        "total_cached_tokens": sum(r.cached_tokens for r in records),
        "total_cost_usd": round(sum(r.cost_usd for r in records), 6),
        "total_tcu": round(sum(r.tcu for r in records), 4),
        "total_duration_ms": sum(r.duration_ms for r in records),
    }
