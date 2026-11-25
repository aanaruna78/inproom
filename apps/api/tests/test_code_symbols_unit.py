"""INPROOM-174 (story INPROOM-29 AC 2, 7): symbols and imports per language; syntax errors; CODEOWNERS."""

import json
from pathlib import Path

import pytest

from app.content import producers
from app.content.code import symbols as sym
from app.content.code.codeowners import owners_of, parse
from app.content.parsing import ParseError

FIXTURES = Path(__file__).parent / "code_fixtures"


def defs(name, media_type):
    doc = sym.symbols_document((FIXTURES / name).read_bytes(), media_type)
    return doc, [
        (d["kind"], d["container"], d["name"], d["line_start"], d["line_end"]) for d in doc["definitions"]
    ]


def test_python_definitions_are_exact():
    doc, found = defs("sample.py", "text/x-python")
    assert doc["method"] == "ast"
    assert found == [
        ("constant", None, "MAX_USERS", 9, 9),
        ("class", None, "Service", 14, 29),
        ("method", "Service", "__init__", 19, 20),
        ("method", "Service", "fetch", 22, 26),
        ("function", "Service.fetch", "helper", 23, 24),
        ("class", "Service", "Config", 28, 29),
        ("function", None, "handler", 32, 33),
    ]
    sigs = {d["name"]: d["signature"] for d in doc["definitions"]}
    assert (
        sigs["fetch"] == "async def fetch(self, user_id: int) -> User"
        and sigs["Service"] == "class Service(Base)"
    )
    assert [(i["module"], i["names"]) for i in doc["imports"]] == [
        ("os", []),
        ("typing", ["TYPE_CHECKING", "Any"]),
        (".models", ["User", "Order"]),
        ("app.db", ["Session"]),  # under "if TYPE_CHECKING:"
    ]


def test_typescript_definitions_and_imports():
    doc, found = defs("sample.ts", "text/x-typescript")
    assert doc["method"] == "scan"
    assert found == [
        ("constant", None, "MAX_RETRIES", 7, 7),
        ("interface", None, "Repo", 10, 12),
        ("type", None, "Id", 14, 14),
        ("enum", None, "Color", 16, 16),
        ("class", None, "UserService", 18, 35),
        ("method", "UserService", "constructor", 21, 23),
        ("method", "UserService", "get", 25, 30),
        ("method", "UserService", "create", 32, 34),
        ("function", None, "handler", 37, 39),
        ("function", None, "inner", 38, 38),
        ("function", None, "add", 41, 41),
        ("function", None, "mul", 42, 44),
    ]  # the commented-out function and the "{" inside a string are not code
    assert [(i["module"], i["names"]) for i in doc["imports"]] == [
        ("express", ["Router", "Request"]),
        ("node:path", ["path"]),
        ("./thing", ["defaultThing"]),
        ("fs", []),
    ]


def test_go_definitions_receivers_groups_and_imports():
    _, found = defs("sample.go", "text/x-go")
    assert found == [
        ("constant", None, "Version", 10, 10),
        ("constant", None, "MaxConns", 13, 13),
        ("constant", None, "minConns", 14, 14),
        ("type", None, "ID", 18, 18),
        ("struct", None, "Pair", 19, 19),
        ("struct", None, "Store", 23, 25),
        ("interface", None, "Reader", 27, 29),
        ("function", None, "New", 31, 35),  # the "{" in a raw string does not open a block
        ("method", "Store", "Get", 37, 42),  # container = receiver type
        ("function", None, "Map", 44, 44),  # generics
    ]


def test_java_types_members_nesting_and_imports():
    doc, found = defs("Sample.java", "text/x-java")
    assert found == [
        ("class", None, "OrderService", 9, 36),
        ("constant", "OrderService", "MAX_ITEMS", 10, 10),
        ("constructor", "OrderService", "OrderService", 13, 15),
        ("method", "OrderService", "find", 18, 23),
        ("method", "OrderService", "first", 25, 27),
        ("method", "OrderService", "hook", 29, 29),
        ("enum", "OrderService", "Status", 31, 31),
        ("class", "OrderService", "Line", 33, 35),
        ("method", "OrderService.Line", "qty", 34, 34),
        ("interface", None, "Orders", 38, 40),
        ("method", "Orders", "find", 39, 39),
        ("record", None, "Money", 42, 42),
    ]
    assert [(i["module"], i["names"]) for i in doc["imports"]][1] == (
        "java.util.Objects.requireNonNull",
        ["static"],
    )


def test_javascript_uses_the_same_scanner():
    src = (
        b"const { a } = require('x');\nclass A {\n  #secret() {}\n  get value() { return 1 }\n}\n"
        b"export default function main() {}\n"
    )
    doc = sym.symbols_document(src, "text/javascript")
    assert [(d["kind"], d["name"]) for d in doc["definitions"]] == [
        ("class", "A"),
        ("method", "#secret"),
        ("method", "value"),
        ("function", "main"),
    ]
    assert doc["imports"] == [{"module": "x", "names": [], "line": 1}]


def test_syntax_errors_and_limits_fail_with_codes(monkeypatch):
    with pytest.raises(ParseError) as exc:
        sym.symbols_document(b"def broken(:\n    pass\n", "text/x-python")
    assert exc.value.code == "CODE_SYNTAX_ERROR" and "line 1" in str(exc.value)
    monkeypatch.setattr(sym, "MAX_DEFINITIONS", 2)
    with pytest.raises(ParseError) as exc:
        sym.symbols_document(b"def a(): pass\ndef b(): pass\ndef c(): pass\n", "text/x-python")
    assert exc.value.code == "PARSE_LIMIT_EXCEEDED"
    with pytest.raises(ParseError) as exc:
        sym.symbols_document(b"\xff\xfe", "text/x-go")
    assert exc.value.code == "PARSE_MALFORMED"


def test_minified_lines_are_not_scanned_and_say_so(monkeypatch):
    monkeypatch.setattr(sym, "MAX_LINE", 50)
    doc = sym.symbols_document(b"function ok() {}\n" + b"var a=" + b"1+" * 40 + b"1;\n", "text/javascript")
    assert [d["name"] for d in doc["definitions"]] == ["ok"]
    assert doc["warnings"] == [{"code": "LONG_LINES_NOT_SCANNED", "lines": [2], "count": 1}]


def test_output_is_deterministic_and_registered_as_symbols_v1():
    data = (FIXTURES / "sample.go").read_bytes()
    assert sym.build(data, "text/x-go") == sym.build(data, "text/x-go")
    assert json.loads(sym.build(data, "text/x-go"))["language"] == "go"
    assert producers.PRODUCERS["symbols"].kind == "code"
    assert producers.for_media_type("text/x-python") == [producers.SYMBOLS]


CODEOWNERS = """# GitHub's documented example
*       @global-owner1 @global-owner2
*.js    @js-owner #This is an inline comment.
*.go docs@example.com
/build/logs/ @doctocat
docs/*  docs@example.com
apps/ @octocat
/docs/ @doctocat
/scripts/ @doctocat @octocat
**/logs @octocat
/apps/ @octocat
/apps/github
!negated @x
src/ not-an-owner
"""


@pytest.mark.parametrize(
    ("path", "owners"),
    [
        ("README.md", ["@global-owner1", "@global-owner2"]),
        ("src/app.js", ["@js-owner"]),
        ("cmd/main.go", ["docs@example.com"]),
        ("build/logs/x.txt", ["@octocat"]),  # **/logs comes later: last match wins
        ("docs/getting-started.md", ["@doctocat"]),
        ("apps/a.py", ["@octocat"]),
        ("web/apps/b.py", ["@octocat"]),  # "apps/" matches at any depth
        ("deep/nested/logs/z", ["@octocat"]),
        ("apps/github/q.py", []),  # a rule without owners: nobody
        ("scripts/s.sh", ["@doctocat", "@octocat"]),
    ],
)
def test_codeowners_follow_githubs_rules(path, owners):
    rules, _ = parse(CODEOWNERS)
    assert owners_of(rules, path) == owners


def test_codeowners_patterns_and_errors():
    rules, errors = parse("docs/* @a\n/src/**/*.py @b\nfile\\ name.md @c\n")
    assert owners_of(rules, "docs/x.md") == ["@a"] and owners_of(rules, "docs/x/y.md") == []
    assert owners_of(rules, "src/a/b/c.py") == ["@b"] and owners_of(rules, "lib/src/c.py") == []
    assert owners_of(rules, "file name.md") == ["@c"]
    _, errors = parse(CODEOWNERS)
    assert [e["line"] for e in errors] == [13, 14]  # "!negated" and the invalid owner: both lines skipped
