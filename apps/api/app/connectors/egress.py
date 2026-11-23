"""Outbound-call allowlist for Git providers (SSRF protection, INPROOM-108).

A provider base URL is accepted only if it is HTTPS on an allowlisted hostname with the default port, or it
exactly matches an explicitly allowlisted origin (e.g. http://localhost:18090 for the local fake provider).
User-info, query strings and fragments are rejected; the adapter additionally never follows redirects.
"""

from urllib.parse import urlsplit

from app.config import get_settings


class EgressDenied(ValueError):
    pass


def _allowlist() -> tuple[set[str], set[str]]:
    hosts, origins = set(), set()
    for entry in filter(None, (e.strip().lower() for e in get_settings().git_allowed_hosts.split(","))):
        (origins if "://" in entry else hosts).add(entry.rstrip("/"))
    return hosts, origins


def validate_base_url(url: str) -> str:
    """Return the normalised base URL (no trailing slash) or raise EgressDenied."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise EgressDenied("base_url is not a valid URL") from exc
    if parts.username or parts.password or "@" in parts.netloc:
        raise EgressDenied("base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise EgressDenied("base_url must not contain a query or fragment")
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if not host:
        raise EgressDenied("base_url has no host")
    path = parts.path.rstrip("/")
    hosts, origins = _allowlist()
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    if origin in origins:
        return origin + path
    if scheme != "https":
        raise EgressDenied("base_url must use https")
    if port not in (None, 443):
        raise EgressDenied("base_url must use the default https port")
    if host not in hosts:
        raise EgressDenied(f"host {host!r} is not an allowed Git provider host")
    return f"https://{host}{path}"
