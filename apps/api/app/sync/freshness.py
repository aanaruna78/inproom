"""How fresh is a source's data? (INPROOM-145 / story INPROOM-26). Computed, never stored.

excluded  a person excluded it from the sync scope (history kept)
paused    paused for access or because its connection was revoked / is not usable
failing   the latest sync attempts failed
never     no successful sync yet
stale     last success older than 3 x the sync interval
fresh     otherwise
`data_as_of` is the last successful sync: what readers should treat the data as current to.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings

USABLE = ("ACTIVE", "DEGRADED")


def freshness(
    source_status: str,
    pause_cause: str | None,
    connection_status: str,
    state: dict[str, Any] | None,
    now: datetime | None = None,
) -> str:
    state = state or {}
    if pause_cause == "excluded":
        return "excluded"
    if source_status != "ACTIVE" or connection_status not in USABLE:
        return "paused"
    if state.get("consecutive_failures"):
        return "failing"
    last = state.get("last_success_at")
    if last is None:
        return "never"
    limit = timedelta(seconds=3 * get_settings().sync_interval_seconds)
    return "stale" if (now or datetime.now(UTC)) - last > limit else "fresh"
