"""INPROOM-215 (story INPROOM-34 AC 1): code files become nodes owned by their CODEOWNERS teams, at the commit
they were indexed from. A module of its own: the code fixtures' `env` differs from the Confluence one."""

import pytest

from tests import test_code_search_integration as code
from tests.test_graph_integration import nodes, triples

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_code_files_owners_and_commit(repo):
    e = repo
    keys = {n["key"]: n for n in nodes(e)}
    billing = "file:acme/api:main:src/billing.py"
    assert keys[billing]["type"] == "code_file" and keys[billing]["attributes"]["repository"] == "acme/api"
    assert (billing, "owned_by", "owner:@acme/payments") in triples(e)
    assert ("file:acme/api:main:cmd/main.go", "owned_by", "owner:@gopher") in triples(e)
    commits = [k for k in keys if k.startswith("commit:")]
    assert commits and (billing, "at_commit", commits[0]) in triples(e)
    assert keys["owner:@acme/payments"]["role"] == "derived"
