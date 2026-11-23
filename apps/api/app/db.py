import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, text

from app.config import get_settings
from app.db_schema import APP_ROLE


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_s},
    )


@contextmanager
def tenant_session(tenant_id: uuid.UUID, engine: Engine | None = None) -> Iterator[Connection]:
    """Open a transaction that can only see and write rows of `tenant_id`.

    Switches to the non-owner application role so FORCE ROW LEVEL SECURITY applies, and binds the
    tenant for the lifetime of the transaction only (SET LOCAL semantics). Commits on success,
    rolls back on any exception.
    """
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a uuid.UUID")
    with (engine or get_engine()).begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        yield conn
