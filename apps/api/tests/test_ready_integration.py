import pytest

pytestmark = pytest.mark.integration


def test_readyz_ok_against_real_postgres(client_for, test_database_url):
    r = client_for(test_database_url).get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "pgvector": "ok"}}
