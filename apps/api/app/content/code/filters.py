"""Which files of a repository tree are indexed, and why the others are not (INPROOM-173 / INPROOM-29)."""

from dataclasses import dataclass

from app.connectors.base import TreeEntry

# extension -> (language, media type the derivative producers recognise)
LANGUAGES = {
    ".py": ("python", "text/x-python"),
    ".js": ("javascript", "text/javascript"),
    ".jsx": ("javascript", "text/javascript"),
    ".mjs": ("javascript", "text/javascript"),
    ".cjs": ("javascript", "text/javascript"),
    ".ts": ("typescript", "text/x-typescript"),
    ".tsx": ("typescript", "text/x-typescript"),
    ".go": ("go", "text/x-go"),
    ".java": ("java", "text/x-java"),
    ".md": ("markdown", "text/markdown"),
    ".markdown": ("markdown", "text/markdown"),
    ".txt": ("text", "text/plain"),
}
EXCLUDED_DIRS = {
    "node_modules",
    "vendor",
    "third_party",
    "dist",
    "build",
    "out",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    "coverage",
}
GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.go",
    ".generated.ts",
    ".bundle.js",
)
LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "go.sum",
    "Cargo.lock",
    "composer.lock",
}
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")  # GitHub's search order
MAX_FILE_BYTES = 1_000_000
MAX_CODEOWNERS_BYTES = 3_000_000  # GitHub's own CODEOWNERS limit
MAX_FILES = 20_000  # per branch snapshot; more makes the snapshot partial
BLOBS_PER_RUN = 500  # downloads per job run; the job re-queues itself for the rest
LFS_POINTER = b"version https://git-lfs.github.com/spec/"


@dataclass(frozen=True)
class Decision:
    index: bool
    language: str | None = None
    media_type: str | None = None
    reason: str | None = None  # why a file is skipped (None: silently not indexed, e.g. images, configs)


def classify(entry: TreeEntry) -> Decision:
    if entry.type == "commit":
        return Decision(False, reason="submodule (not followed)")
    if entry.type != "blob":
        return Decision(False)  # directories
    if entry.mode == "120000":
        return Decision(False, reason="symbolic link (not followed)")
    parts = entry.path.split("/")
    name = parts[-1]
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return Decision(False, reason="vendored or generated directory")
    if name in LOCKFILES or name.endswith(GENERATED_SUFFIXES):
        return Decision(False, reason="generated file")
    ext = "." + name.rsplit(".", 1)[1].lower() if "." in name else ""
    if ext not in LANGUAGES:
        return Decision(False)
    if entry.size is not None and entry.size > MAX_FILE_BYTES:
        return Decision(False, reason=f"larger than {MAX_FILE_BYTES} bytes")
    language, media_type = LANGUAGES[ext]
    return Decision(True, language, media_type)


def content_problem(data: bytes) -> str | None:
    """Why fetched bytes are not indexable text, or None."""
    if data.startswith(LFS_POINTER):
        return "Git LFS pointer (the file is stored outside the repository)"
    if b"\x00" in data:
        return "binary content"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8 text"
    return None
