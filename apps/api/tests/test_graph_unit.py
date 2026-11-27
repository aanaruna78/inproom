"""INPROOM-215: node keys and the validated edges a Jira record states."""

from app.content.graph import jira_graph, mentions, node_key


def test_node_keys():
    assert node_key("jira", "20002", jira_key="ABC-2") == "jira:ABC-2"
    assert node_key("confluence", "101") == "confluence:101"
    assert node_key("confluence", "att:9") == "confluence:att:9"
    assert node_key("upload", "d0c") == "doc:d0c"
    assert node_key("git", "main:src/billing.py", repo="acme/api") == "file:acme/api:main:src/billing.py"


def _record(key, **fields):
    return {"id": "1", "key": key, "fields": {"summary": "S", "issuetype": {"name": "Story"}} | fields}


def test_jira_record_edges():
    record = _record(
        "ABC-2",
        parent={"id": "1", "key": "ABC-1"},
        subtasks=[{"id": "4", "key": "ABC-4"}],
        issuelinks=[
            {"id": "1", "type": "Blocks", "outwardIssue": {"id": "3", "key": "ABC-3"}},
            {"id": "2", "type": "Relates", "inwardIssue": {"id": "5", "key": "XYZ-5"}},
        ],
        components=[{"name": "Billing"}],
        fixVersions=[{"name": "4.2"}],
        versions=[{"name": "4.1"}],
        customfield_10014="ABC-9",
        status={"name": "In Progress"},
    )
    nodes, edges = jira_graph(record, epic_field="customfield_10014")
    me = nodes[0]
    assert (me["key"], me["type"], me["role"], me["label"]) == (
        "jira:ABC-2",
        "jira_issue",
        "item",
        "ABC-2: S",
    )
    assert me["attributes"]["issue_type"] == "story" and me["attributes"]["status"] == "In Progress"
    assert {(n["key"], n["type"], n["role"]) for n in nodes[1:]} == {
        ("component:ABC:Billing", "jira_component", "derived"),
        ("version:ABC:4.2", "jira_version", "derived"),
        ("version:ABC:4.1", "jira_version", "derived"),
    }
    assert {(e["from_key"], e["kind"], e["to_key"]) for e in edges} == {
        ("jira:ABC-2", "child_of", "jira:ABC-1"),
        ("jira:ABC-4", "child_of", "jira:ABC-2"),
        ("jira:ABC-2", "blocks", "jira:ABC-3"),  # outward: this -> other
        ("jira:XYZ-5", "relates", "jira:ABC-2"),  # inward: other -> this
        ("jira:ABC-2", "in_epic", "jira:ABC-9"),
        ("jira:ABC-2", "in_component", "component:ABC:Billing"),
        ("jira:ABC-2", "fix_version", "version:ABC:4.2"),
        ("jira:ABC-2", "affects_version", "version:ABC:4.1"),
    }
    assert all(e["origin"] == "validated" and e["confidence"] == 1.0 for e in edges)
    assert next(e for e in edges if e["kind"] == "blocks")["evidence"] == {
        "field": "issuelinks",
        "value": "ABC-3",
    }


def test_a_bare_record_states_only_its_node():
    nodes, edges = jira_graph({"id": "7", "key": "ABC-7", "fields": {}})
    assert [n["key"] for n in nodes] == ["jira:ABC-7"] and edges == []
    assert nodes[0]["attributes"]["issue_type"] == "other"


FILES = {"src/billing.py": ["file:acme/api:main:src/billing.py", "file:acme/api:dev:src/billing.py"]}


def found(text, projects=("ABC",)):
    return {k: (c, t) for k, c, t in mentions(text, jira_projects=set(projects), files=FILES)}


def test_jira_keys_known_unknown_and_not_issues():
    got = found("Fixes ABC-12 (see also PAY-7). Encoded as UTF-8, hashed with SHA-256, dated ISO-8601.")
    assert got == {"jira:ABC-12": (0.9, "ABC-12"), "jira:PAY-7": (0.5, "PAY-7")}
    assert found("abc-12 ABC-0 ABC-12x xABC-12 a/ABC-12") == {}  # case, zero, glued, part of a path


def test_confluence_links_and_file_paths():
    got = found(
        "Design: https://acme.atlassian.net/wiki/spaces/ENG/pages/101/Architecture and "
        "/pages/viewpage.action?pageId=102. Code in src/billing.py, tests in tests/test_x.py, "
        "image at assets/logo.png, docs at https://example.com/src/billing.py"
    )
    assert got["confluence:101"][0] == 0.9 and got["confluence:102"][0] == 0.9
    assert got["file:acme/api:main:src/billing.py"] == (0.8, "src/billing.py")
    assert "file:acme/api:dev:src/billing.py" in got  # every file at that path
    assert got["path:tests/test_x.py"] == (0.4, "tests/test_x.py")  # unknown code path: unresolved
    assert not any(k.startswith("path:assets") for k in got)  # not a code extension
    assert "path:example.com/src/billing.py" not in got  # inside a URL
