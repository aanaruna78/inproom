"""INPROOM-160 (story INPROOM-27): which revisions the retention policy releases."""

import uuid
from datetime import UTC, datetime, timedelta

from app.content.retention import purgeable
from app.modules.settings import Retention

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def revs(*ages_days):
    """Revisions seq 1..n fetched `age` days ago (oldest first)."""
    return [
        {"id": uuid.uuid4(), "seq": i + 1, "fetched_at": NOW - timedelta(days=age), "purged_at": None}
        for i, age in enumerate(ages_days)
    ]


def test_keeps_the_newest_and_purges_only_long_superseded_revisions():
    r = revs(400, 200, 100, 10, 1)
    policy = Retention(keep_revisions=2, superseded_days=90)
    out = purgeable(r, current_id=r[-1]["id"], deleted_at=None, policy=policy, now=NOW)
    # seq1 replaced 200d ago, seq2 replaced 100d ago: purged; seq3 replaced 10d ago: kept; seq4/5: newest two.
    assert out == [r[0]["id"], r[1]["id"]]


def test_never_purges_the_current_revision_or_purges_twice():
    r = revs(400, 300)
    r[0]["purged_at"] = NOW
    policy = Retention(keep_revisions=1, superseded_days=1)
    assert purgeable(r, current_id=r[0]["id"], deleted_at=None, policy=policy, now=NOW) == []
    assert (
        purgeable(r, current_id=r[1]["id"], deleted_at=None, policy=policy, now=NOW) == []
    )  # already purged


def test_deleted_items_lose_everything_only_after_the_deletion_window():
    r = revs(400, 300)
    policy = Retention(deleted_days=30)
    recent = NOW - timedelta(days=29)
    assert purgeable(r, current_id=r[1]["id"], deleted_at=recent, policy=policy, now=NOW) == []
    old = NOW - timedelta(days=31)
    assert purgeable(r, current_id=r[1]["id"], deleted_at=old, policy=policy, now=NOW) == [
        r[0]["id"],
        r[1]["id"],
    ]
