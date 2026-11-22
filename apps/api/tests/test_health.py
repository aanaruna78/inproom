from app.config import Settings
from tests.conftest import UNREACHABLE_DB


def test_settings_read_prefixed_env(monkeypatch):
    monkeypatch.setenv("INPROOM_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("INPROOM_ENV", "ci")
    s = Settings()
    assert s.database_url.endswith("@db:5432/x")
    assert s.env == "ci"


def test_healthz_is_dependency_free(client_for):
    client = client_for(UNREACHABLE_DB)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}


def test_trace_id_generated_and_echoed(client_for):
    client = client_for(UNREACHABLE_DB)
    generated = client.get("/healthz").headers["X-Trace-Id"]
    assert len(generated) == 32
    echoed = client.get("/healthz", headers={"X-Trace-Id": "abc12345-req"})
    assert echoed.headers["X-Trace-Id"] == "abc12345-req"
    # Unsafe values are replaced, not reflected.
    assert client.get("/healthz", headers={"X-Trace-Id": "<script>"}).headers["X-Trace-Id"] != "<script>"


def test_readyz_returns_problem_json_when_database_unreachable(client_for):
    client = client_for(UNREACHABLE_DB)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["code"] == "NOT_READY"
    assert body["status"] == 503
    assert body["checks"]["database"].startswith("unreachable")
    assert "database" in body["detail"]
    assert body["trace_id"] == r.headers["X-Trace-Id"]


def test_unknown_route_is_problem_json(client_for):
    r = client_for(UNREACHABLE_DB).get("/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["code"] == "HTTP_404"
