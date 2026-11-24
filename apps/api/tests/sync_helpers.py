"""Shared helpers for sync worker tests (INPROOM-25)."""

from sqlalchemy import text

from app.db import get_engine


def due(tenant):
    """Test clock control: the source is due now and any queued job is runnable now."""
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE source_sync_state SET next_due_at = now() WHERE tenant_id=:t"), {"t": tenant}
        )
        conn.execute(
            text("UPDATE sync_jobs SET run_after = now() WHERE tenant_id=:t AND status='QUEUED'"),
            {"t": tenant},
        )


def events(e, kind):
    return e.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type=:k", k=kind)


def payloads(e, kind):
    with get_engine().connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT payload FROM outbox_events WHERE tenant_id=:t AND type=:k "
                    "ORDER BY created_at, id"
                ),
                {"t": e.tenant, "k": kind},
            )
        ]
