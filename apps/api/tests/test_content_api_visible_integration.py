"""INPROOM-228: `GET /content?visible=true` lists only items that may be served now - the Knowledge Explorer's
browse list - while the default listing keeps showing every item's metadata to project readers."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def ids(e, **params):
    r = e.client.get(f"/v1/projects/{e.pid}/content", params={"kind": "confluence"} | params, headers=e.h())
    assert r.status_code == 200, r.text
    return {x["external_id"] for x in r.json()["items"]}


def test_visible_lists_only_servable_items(selected):
    e = selected
    drain(e)
    assert {"101", "102"} <= ids(e, visible=True)
    e.conf.pages[102].groups = ["bots"]  # restricted at the source
    assert e.sync().status_code == 200
    assert "102" in ids(e) and "102" not in ids(e, visible=True)
    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # paused source
    assert ids(e, visible=True) == set() and "101" in ids(e)
