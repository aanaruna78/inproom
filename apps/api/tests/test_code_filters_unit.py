"""INPROOM-173: which repository files are indexed, and the reason for every one that is not."""

import pytest

from app.connectors.base import TreeEntry
from app.content.code.filters import MAX_FILE_BYTES, classify, content_problem


def entry(path, mode="100644", type_="blob", size=10):
    return TreeEntry(path=path, mode=mode, type=type_, sha="a" * 40, size=size)


@pytest.mark.parametrize(
    ("path", "language", "media_type"),
    [
        ("src/app.py", "python", "text/x-python"),
        ("web/App.tsx", "typescript", "text/x-typescript"),
        ("web/index.mjs", "javascript", "text/javascript"),
        ("cmd/main.go", "go", "text/x-go"),
        ("src/main/java/Acme.java", "java", "text/x-java"),
        ("docs/Guide.MD", "markdown", "text/markdown"),
        ("NOTES.txt", "text", "text/plain"),
    ],
)
def test_supported_files_are_indexed_with_their_language(path, language, media_type):
    d = classify(entry(path))
    assert (d.index, d.language, d.media_type) == (True, language, media_type)


@pytest.mark.parametrize(
    ("e", "reason"),
    [
        (entry("node_modules/a/index.js"), "vendored or generated directory"),
        (entry("pkg/vendor/x.go"), "vendored or generated directory"),
        (entry("dist/app.js"), "vendored or generated directory"),
        (entry("static/app.min.js"), "generated file"),
        (entry("api/v1/service_pb2.py"), "generated file"),
        (entry("package-lock.json"), "generated file"),
        (entry("go.sum"), "generated file"),
        (entry("link.py", mode="120000"), "symbolic link (not followed)"),
        (entry("libs/sub", mode="160000", type_="commit"), "submodule (not followed)"),
        (entry("huge.py", size=MAX_FILE_BYTES + 1), f"larger than {MAX_FILE_BYTES} bytes"),
    ],
)
def test_skipped_files_say_why(e, reason):
    d = classify(e)
    assert (d.index, d.reason) == (False, reason)


def test_other_files_are_silently_not_indexed():
    for path in ("logo.png", "Makefile", "config.yaml", "src"):
        d = classify(entry(path, type_="tree" if path == "src" else "blob"))
        assert (d.index, d.reason) == (False, None)


def test_content_problems():
    assert content_problem(b"version https://git-lfs.github.com/spec/v1\noid sha256:x").startswith("Git LFS")
    assert content_problem(b"a\x00b") == "binary content"
    assert content_problem("café".encode("latin-1")) == "not UTF-8 text"
    assert content_problem("ok ✓".encode()) is None
