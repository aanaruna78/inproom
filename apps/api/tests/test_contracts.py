import json

from scripts.export_openapi import CONTRACT, render


def test_committed_contract_matches_app():
    assert CONTRACT.exists(), "packages/contracts/openapi.json missing - run `make contracts`"
    assert json.loads(CONTRACT.read_text()) == json.loads(render())


def test_contract_documents_health_endpoints():
    paths = json.loads(render())["paths"]
    assert {"/healthz", "/readyz"} <= paths.keys()
    assert "503" in paths["/readyz"]["get"]["responses"]
