"""INPROOM-135 (story INPROOM-24 AC 1-7): Confluence connections, space selection and the catalog sync."""

import uuid

import pytest
from sqlalchemy import text

from tests.auth_helpers import bearer
from tests.fakes import confluence as fc

pytestmark = pytest.mark.integration
SITE = "https://acme.atlassian.net"
APPROVED = {"required_labels": ["approved"]}


@pytest.fixture
def env(db_client, tokens, owner_engine, fake_confluence):
    tenant = uuid.uuid4()

    class Env:
        client = db_client
        conf = fake_confluence

        def h(self, user="alice", roles=("tenant_admin",), **extra):
            return bearer(tokens.mint(tenant_id=tenant, user_id=user, roles=list(roles))) | extra

        def connect(self, token=fc.VALID_TOKEN, email=fc.EMAIL, name="Confluence", site=SITE):
            body = {"provider": "confluence", "display_name": name, "base_url": site, "token": token}
            if email:
                body["email"] = email
            return db_client.post("/v1/connections", json=body, headers=self.h())

        def select(self, key="ENG", filters=None):
            return db_client.put(
                f"/v1/projects/{self.pid}/sources/confluence/{self.cid}:{key}", json=filters, headers=self.h()
            )

        def sync(self):
            return db_client.post(
                f"/v1/projects/{self.pid}/sources/confluence/{self.sid}/sync", headers=self.h()
            )

        def pages(self):
            with owner_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT page_id, status, current_version, restrictions FROM confluence_pages "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                ).all()
            return {r.page_id: r for r in rows}

        def q(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant} | params).scalar()

        def events(self, kind):
            return self.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type=:k", k=kind)

    e = Env()
    e.tenant = tenant
    ws = db_client.post("/v1/workspaces", json={"slug": "conf", "name": "Conf"}, headers=e.h()).json()
    e.pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "CNF", "name": "Confluence"}, headers=e.h()
    ).json()["id"]
    return e


@pytest.fixture
def selected(env):
    r = env.connect()
    assert r.status_code == 201, r.text
    env.cid = r.json()["id"]
    r = env.select(filters=APPROVED)
    assert r.status_code == 200, r.text
    env.sid = r.json()["source"]["id"]
    return env


# ---------------------------------------------------------------- AC 1: connect


def test_connect_confluence_and_rejections_store_nothing(env):
    r = env.connect()
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["provider"], body["kind"], body["auth_type"], body["account_login"]) == (
        "confluence",
        "confluence",
        "basic_token",
        fc.EMAIL,
    )
    assert body["credential_fingerprint"] == "…AB34" and fc.VALID_TOKEN not in r.text
    for kwargs, code in (
        ({"token": fc.BAD_TOKEN, "name": "bad"}, "CREDENTIAL_INVALID"),
        ({"email": "x@acme.example", "name": "wrong email"}, "CREDENTIAL_INVALID"),
        ({"site": "https://atlassian.net.evil.com", "name": "ssrf"}, "BASE_URL_NOT_ALLOWED"),
    ):
        bad = env.connect(**kwargs)
        assert bad.status_code == 422 and bad.json()["code"] == code
    assert env.q("SELECT count(*) FROM source_connections WHERE tenant_id=:t") == 1
    assert env.q("SELECT count(*) FROM connection_secrets WHERE tenant_id=:t") == 1


# ---------------------------------------------------------------- AC 2, 4: selection, filters, restrictions


def test_approved_filter_catalogs_exactly_the_approved_current_pages(selected):
    env = selected
    pages = env.pages()
    assert {p: r.status for p, r in pages.items()} == {
        "100": "CURRENT",
        "101": "CURRENT",
        "102": "CURRENT",
        "103": "RESTRICTED",
        "104": "RESTRICTED",
    }  # never: 105 draft, 106 trashed, 107 unlabelled, 110 hidden, HR/SECRET pages
    assert pages["104"].restrictions["inherited"][0]["page_id"] == "103"
    assert pages["104"].restrictions["inherited"][0]["groups"] == ["bots"]
    assert pages["101"].current_version == 3
    source = env.client.get(f"/v1/projects/{env.pid}/sources/confluence", headers=env.h()).json()[0]
    assert source["page_counts"] == {"CURRENT": 3, "RESTRICTED": 2, "EXCLUDED": 0, "DELETED": 0}
    assert source["filters"]["required_labels"] == ["approved"]
    assert [w["code"] for w in source["warnings"]] == [
        "EXCESS_PRIVILEGE"
    ]  # token can edit, project read_only
    assert env.events("source.confluence.page.changed") == 3


def test_include_restricted_and_whole_space_across_provider_pages(selected):
    env = selected
    r = env.select(filters=APPROVED | {"include_restricted": True}).json()
    assert r["counts"]["restored"] == 2 and env.pages()["104"].status == "CURRENT"
    assert env.pages()["104"].restrictions["restricted"] is True  # flagged for E03 ACL enforcement
    whole = env.select(filters={}).json()
    assert whole["source"]["last_sync"]["pages_listed"] == 71  # 2 provider pages
    assert whole["source"]["page_counts"]["CURRENT"] == 69 and not whole["truncated"]


# ---------------------------------------------------------------- AC 3: versions, idempotent sync


def test_edit_creates_one_version_and_unchanged_sync_writes_nothing(selected):
    env = selected
    env.conf.edit(101, "<h1>Architecture</h1><p>Three services.</p>")
    r = env.sync().json()
    assert r["changed"] and r["counts"]["updated"] == 1 and env.pages()["101"].current_version == 4
    assert env.q("SELECT count(*) FROM confluence_page_versions WHERE tenant_id=:t AND page_id='101'") == 2
    assert env.events("source.confluence.page.changed") == 4
    before = env.q("SELECT max(updated_at)::text FROM confluence_pages WHERE tenant_id=:t")
    again = env.sync().json()
    assert again["changed"] is False and again["counts"]["unchanged"] == 5
    assert env.q("SELECT max(updated_at)::text FROM confluence_pages WHERE tenant_id=:t") == before
    assert env.events("source.confluence.page.changed") == 4


# ---------------------------------------------------------------- AC 5, 6: deletion, restriction, access


def test_deleted_pages_are_tombstoned_and_restored(selected):
    env = selected
    env.conf.pages[102].status = "trashed"
    assert env.sync().json()["counts"]["deleted"] == 1
    assert env.pages()["102"].status == "DELETED" and env.events("source.confluence.page.deleted") == 1
    assert env.q("SELECT count(*) FROM confluence_page_versions WHERE tenant_id=:t AND page_id='102'") == 1
    env.conf.pages[102].status = "current"
    assert env.sync().json()["counts"]["restored"] == 1 and env.pages()["102"].status == "CURRENT"


def test_new_restriction_and_lost_space_access(selected):
    env = selected
    env.conf.pages[101].groups = ["bots"]
    assert env.sync().json()["counts"]["restricted"] == 1
    assert env.pages()["101"].status == "RESTRICTED"
    assert env.events("source.confluence.page.out_of_scope") == 1
    env.conf.revoke_space(fc.VALID_TOKEN, "ENG")
    check = env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()
    assert check["status"] == "DEGRADED"
    assert [(i["code"], i.get("space")) for i in check["health"]["issues"]] == [
        ("SPACE_NOT_ACCESSIBLE", "ENG")
    ]
    source = env.client.get(f"/v1/projects/{env.pid}/sources/confluence", headers=env.h()).json()[0]
    assert source["status"] == "PAUSED"


# ---------------------------------------------------------------- AC 7: failures, guards


def test_failure_mid_sync_leaves_the_catalog_unchanged(selected):
    env = selected
    env.conf.edit(101, "<p>changed</p>")
    before = {p: (r.status, r.current_version) for p, r in env.pages().items()}
    env.conf.fail_after, env.conf.failures = 1, ["ratelimit"]  # space lookup succeeds, the page listing fails
    r = env.sync()
    assert r.status_code == 503 and r.headers["retry-after"] == "42"
    env.conf.fail_after, env.conf.failures = 1, ["timeout"] * 3
    assert env.sync().status_code == 502
    assert {p: (r.status, r.current_version) for p, r in env.pages().items()} == before


def test_guards_kind_provider_revoke(selected):
    env = selected
    git = env.client.put(
        f"/v1/projects/{env.pid}/sources/repositories/{env.cid}/1", json={"branches": []}, headers=env.h()
    )
    assert git.status_code == 409 and git.json()["code"] == "CONNECTION_PROVIDER_MISMATCH"
    assert env.select("SECRET").json()["code"] == "SPACE_NOT_ACCESSIBLE"
    env.client.post(f"/v1/connections/{env.cid}/revoke", headers=env.h())
    source = env.client.get(f"/v1/projects/{env.pid}/sources/confluence", headers=env.h()).json()[0]
    assert (source["status"], source["status_reason"]) == ("PAUSED", "connection revoked")
    assert env.sync().status_code == 409
    gone = env.client.delete(f"/v1/projects/{env.pid}/sources/confluence/{env.cid}:ENG", headers=env.h())
    assert gone.status_code == 204 and env.q("SELECT count(*) FROM confluence_pages WHERE tenant_id=:t") == 0
