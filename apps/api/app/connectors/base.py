"""Provider-neutral Git adapter contract (INPROOM-108). GitHub implements it first; GitLab/Bitbucket later."""

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Unexpected provider response. Messages never contain credentials."""

    code = "PROVIDER_ERROR"


class CredentialInvalid(ProviderError):
    code = "CREDENTIAL_INVALID"


class NotFoundOrNoAccess(ProviderError):
    code = "NOT_FOUND_OR_NO_ACCESS"


class RateLimited(ProviderError):
    code = "PROVIDER_RATE_LIMITED"

    def __init__(self, retry_after: int):
        super().__init__(f"provider rate limit reached; retry after {retry_after}s")
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"


@dataclass(frozen=True)
class Account:
    login: str
    id: str
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Repository:
    id: str
    full_name: str
    private: bool
    default_branch: str
    permissions: dict[str, bool] = field(default_factory=dict)

    @property
    def can_read(self) -> bool:
        return bool(
            self.permissions.get("pull") or self.permissions.get("push") or self.permissions.get("admin")
        )

    @property
    def excess_privileges(self) -> list[str]:
        """Write-level permissions INPROOM does not need for reading code."""
        return [p for p in ("admin", "maintain", "push") if self.permissions.get(p)]


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str  # git file mode: 100644 file, 100755 executable, 120000 symlink, 160000 submodule
    type: str  # blob | tree | commit
    sha: str  # git object id
    size: int | None = None


@dataclass(frozen=True)
class Tree:
    commit: str
    entries: list[TreeEntry]
    truncated: bool  # the provider cut the listing short (very large trees)


class GitProvider(Protocol):
    def whoami(self) -> Account: ...

    def list_repositories(self, q: str | None = None, page: int = 1) -> tuple[list[Repository], bool]: ...

    def get_repository(self, repo_id: str) -> Repository: ...

    def branch_exists(self, repo_id: str, branch: str) -> bool: ...

    def find_hook(self, repo_id: str, url: str) -> str | None: ...

    def create_hook(self, repo_id: str, url: str, secret: str, events: list[str]) -> str: ...

    def delete_hook(self, repo_id: str, hook_id: str) -> None: ...

    def tree(self, repo_id: str, commit: str) -> Tree: ...  # INPROOM-173

    def blob(self, repo_id: str, sha: str, max_bytes: int) -> bytes: ...
