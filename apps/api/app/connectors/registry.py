"""Provider construction and error translation for API routes (INPROOM-109)."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import httpx
from fastapi import Depends

from app.api_errors import ApiError
from app.connectors.base import (
    CredentialInvalid,
    GitProvider,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from app.connectors.confluence import ConfluenceProvider
from app.connectors.egress import EgressDenied, validate_base_url, validate_confluence_url, validate_jira_url
from app.connectors.github import GitHubProvider
from app.connectors.jira import JiraProvider
from app.connectors.vault import Secret

Transport = httpx.BaseTransport | None


def get_git_transport() -> Transport:
    """Real network by default. Tests override this dependency with the fake GitHub transport."""
    return None


def get_jira_transport() -> Transport:
    """Real network by default. Tests override this dependency with the fake Jira transport."""
    return None


def get_confluence_transport() -> Transport:
    """Real network by default. Tests override this dependency with the fake Confluence transport."""
    return None


GitTransport = Annotated[Transport, Depends(get_git_transport)]
JiraTransport = Annotated[Transport, Depends(get_jira_transport)]
ConfluenceTransport = Annotated[Transport, Depends(get_confluence_transport)]


def get_transports(
    git: GitTransport, jira: JiraTransport, confluence: ConfluenceTransport
) -> dict[str, Transport]:
    return {"github": git, "jira": jira, "confluence": confluence}


Transports = Annotated[dict[str, Transport], Depends(get_transports)]


def _not_allowed(exc: EgressDenied) -> ApiError:
    return ApiError(422, "BASE_URL_NOT_ALLOWED", "Unprocessable request", str(exc))


def validate_site(provider: str, base_url: str) -> str:
    """Provider-specific SSRF allowlist (Git hosts, Jira/Confluence sites); 422 BASE_URL_NOT_ALLOWED."""
    try:
        if provider == "jira":
            return validate_jira_url(base_url)
        if provider == "confluence":
            return validate_confluence_url(base_url)
        return validate_base_url(base_url)
    except EgressDenied as exc:
        raise _not_allowed(exc) from exc


def provider_for(
    provider: str, base_url: str, token: Secret, transport: Transport | dict[str, Transport]
) -> GitProvider:
    """The Git adapter for a connection. `transport` may be the per-provider map from `Transports`."""
    if provider != "github":
        raise ApiError(
            409,
            "CONNECTION_PROVIDER_MISMATCH",
            "Conflict",
            f"This is a {provider} connection; it cannot be used as a Git source",
        )
    if isinstance(transport, dict):
        transport = transport["github"]
    try:
        return GitHubProvider(base_url, token, transport=transport)
    except EgressDenied as exc:
        raise _not_allowed(exc) from exc


def jira_for(row: dict, token: Secret, transports: dict[str, Transport]) -> JiraProvider:
    """The Jira adapter for a connection row. Cloud connections authenticate as account_login (the email)."""
    if row["provider"] != "jira":
        raise ApiError(
            409,
            "CONNECTION_PROVIDER_MISMATCH",
            "Conflict",
            f"This is a {row['provider']} connection; it cannot be used as a Jira source",
        )
    email = row["account_login"] if row["auth_type"] == "basic_token" else None
    try:
        return JiraProvider(row["base_url"], token, email=email, transport=transports["jira"])
    except EgressDenied as exc:
        raise _not_allowed(exc) from exc


def confluence_for(row: dict, token: Secret, transports: dict[str, Transport]) -> ConfluenceProvider:
    """The Confluence adapter for a connection row. Cloud authenticates as account_login (the email)."""
    if row["provider"] != "confluence":
        raise ApiError(
            409,
            "CONNECTION_PROVIDER_MISMATCH",
            "Conflict",
            f"This is a {row['provider']} connection; it cannot be used as a Confluence source",
        )
    email = row["account_login"] if row["auth_type"] == "basic_token" else None
    try:
        return ConfluenceProvider(row["base_url"], token, email=email, transport=transports["confluence"])
    except EgressDenied as exc:
        raise _not_allowed(exc) from exc


@contextmanager
def provider_errors(not_found: tuple[str, str] | None = None) -> Iterator[None]:
    """Translate adapter errors into actionable problem+json responses. Nothing has been written when these
    are raised: routes call the provider before (or outside) their database transaction."""
    try:
        yield
    except CredentialInvalid as exc:
        raise ApiError(
            422,
            "CREDENTIAL_INVALID",
            "Unprocessable request",
            "The provider rejected the credential (invalid, expired or revoked). Use a new token.",
        ) from exc
    except NotFoundOrNoAccess as exc:
        code, detail = not_found or ("NOT_FOUND_OR_NO_ACCESS", "Not found, or the credential has no access")
        raise ApiError(422, code, "Unprocessable request", detail) from exc
    except RateLimited as exc:
        raise ApiError(
            503,
            "PROVIDER_RATE_LIMITED",
            "Provider rate limit",
            f"The provider rate limit was reached. Retry after {exc.retry_after} seconds.",
            headers={"Retry-After": str(exc.retry_after)},
            retry_after=exc.retry_after,
        ) from exc
    except ProviderUnavailable as exc:
        raise ApiError(
            502, "PROVIDER_UNAVAILABLE", "Provider unavailable", f"{exc} Try again shortly."
        ) from exc
    except ProviderError as exc:
        raise ApiError(502, "PROVIDER_ERROR", "Provider error", str(exc)) from exc
