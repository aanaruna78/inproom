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
from app.connectors.egress import EgressDenied
from app.connectors.github import GitHubProvider
from app.connectors.vault import Secret


def get_git_transport() -> httpx.BaseTransport | None:
    """Real network by default. Tests override this dependency with the fake GitHub transport."""
    return None


GitTransport = Annotated[httpx.BaseTransport | None, Depends(get_git_transport)]


def provider_for(
    provider: str, base_url: str, token: Secret, transport: httpx.BaseTransport | None
) -> GitProvider:
    if provider != "github":
        raise ApiError(
            422, "PROVIDER_NOT_SUPPORTED", "Unprocessable request", f"Unsupported provider {provider!r}"
        )
    try:
        return GitHubProvider(base_url, token, transport=transport)
    except EgressDenied as exc:
        raise ApiError(422, "BASE_URL_NOT_ALLOWED", "Unprocessable request", str(exc)) from exc


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
