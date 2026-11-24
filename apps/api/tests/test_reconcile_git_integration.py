"""INPROOM-142 (story INPROOM-25 AC 1-3): GitHub reconcile emits every real change exactly once, with no
webhook at all (missed deliveries are recovered by the scheduler), and never re-emits old changes."""

import pytest

from app.sync import worker
from tests import test_connections_integration as helpers
from tests.sync_helpers import due, events, payloads

pytestmark = pytest.mark.integration
env = helpers.env
connected = helpers.connected


def test_git_heads_change_once_without_webhooks(connected):
    e = connected
    assert e.select("1", ["main", "release/1.0"]).status_code == 200

    def run(**kw):
        return worker.run_once("w", transports={"github": e.gh.transport()}, tenant=e.tenant, **kw)

    first = run()
    assert first["status"] == "SUCCEEDED" and first["result"]["changed_branches"] == ["main", "release/1.0"]
    assert all(p["before_sha"] is None for p in payloads(e, "source.repository.changed"))  # baseline
    due(e.tenant)
    assert run()["result"]["changed_branches"] == []  # nothing new: nothing emitted
    e.gh.repos[1].branches["main"] = "e" * 40  # a push GitHub never told us about
    e.gh.failures = ["500"] * 3  # ...and the first attempt hits an outage
    due(e.tenant)
    assert run()["status"] == "QUEUED"
    assert events(e, "source.repository.changed") == 2  # the failed attempt emitted nothing
    due(e.tenant)
    assert run(schedule=False)["result"]["changed_branches"] == ["main"]
    last = payloads(e, "source.repository.changed")[-1]
    assert (last["before_sha"], last["after_sha"], last["reason"]) == ("a" * 40, "e" * 40, "schedule")
    assert events(e, "source.repository.changed") == 3
