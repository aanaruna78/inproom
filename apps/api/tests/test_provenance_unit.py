"""INPROOM-184: the freshness decision and provider time parsing."""

from datetime import UTC, datetime

from app.content.provenance import _parse_time, decide

STALE = [{"code": "SOURCE_SYNC_FAILING", "detail": "x"}]


def test_freshness_decision_table():
    assert decide(1, 1, [])["status"] == "current"
    assert decide(1, 1, STALE) == {"status": "stale", "reasons": STALE, "current_seq": 1}
    superseded = decide(1, 3, STALE)
    assert superseded["status"] == "superseded" and [r["code"] for r in superseded["reasons"]] == [
        "NEWER_REVISION",
        "SOURCE_SYNC_FAILING",
    ]
    assert decide(2, None, [])["status"] == "current"  # no current revision recorded: nothing newer known


def test_provider_times_in_their_formats():
    assert _parse_time("2026-11-13T09:00:00.000Z") == datetime(2026, 11, 13, 9, tzinfo=UTC)
    assert _parse_time("2026-11-20T10:00:00.000+0000") == datetime(2026, 11, 20, 10, tzinfo=UTC)
    assert _parse_time("not a time") is None and _parse_time(None) is None
