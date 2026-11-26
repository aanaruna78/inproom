"""INPROOM-214: the golden set's code queries (tests/data/golden_retrieval.json, "code"): identifiers in
source files are retrieved by keyword and hybrid retrieval, with supported evidence. A module of its own: the
code fixtures' `env` differs from the Confluence one."""

import pytest

from tests import test_code_search_integration as code
from tests.test_retrieval_golden_integration import GOLDEN, label, retrieve

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_golden_code_queries(repo):
    e = repo
    for case in GOLDEN["code"]:
        for mode in ("keyword", "hybrid"):
            out = retrieve(e, case["q"], mode)
            top = [label(h, {}) for h in out["items"][:5]]
            assert set(top) & set(case["expect"]), (case, mode, top)
            assert out["evidence"]["status"] == "sufficient"
