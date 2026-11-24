"""Outbound-call allowlists for source providers (SSRF protection; INPROOM-108, Jira sites INPROOM-130).

A provider base URL is accepted only if it is HTTPS on an allowlisted hostname with the default port, or it
exactly matches an explicitly allowlisted origin (e.g. http://localhost:18090 for the local fake provider).
A hostname entry may start with one wildcard label: "*.atlassian.net" matches "acme.atlassian.net" but not
"atlassian.net", "a.b.atlassian.net" or "atlassian.net.evil.com". IP literals never match a wildcard.
User-info, query strings and fragments are rejected; the adapters additionally never follow redirects.
"""

import ipaddress
from urllib.parse import urlsplit

from app.config import get_settings


class EgressDenied(ValueError):
    pass


def _allowlist(raw: str) -> tuple[set[str], set[str]]:
    hosts, origins = set(), set()
    for entry in filter(None, (e.strip().lower() for e in raw.split(","))):
        (origins if "://" in entry else hosts).add(entry.rstrip("/"))
    return hosts, origins


def _host_allowed(host: str, hosts: set[str]) -> bool:
    if host in hosts:
        return True
    try:
        ipaddress.ip_address(host)
        return False  # wildcards never cover IP literals
    except ValueError:
        pass
    label, _, rest = host.partition(".")
    return bool(label and rest) and f"*.{rest}" in hosts


def validate_jira_url(url: str) -> str:
    """Jira sites are per customer, so they get their own allowlist (INPROOM_JIRA_ALLOWED_HOSTS)."""
    return validate_base_url(url, get_settings().jira_allowed_hosts, "Jira site")


def validate_base_url(url: str, allowed: str | None = None, what: str = "Git provider") -> str:
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
    hosts, origins = _allowlist(get_settings().git_allowed_hosts if allowed is None else allowed)
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    if origin in origins:
        return origin + path
    if scheme != "https":
        raise EgressDenied("base_url must use https")
    if port not in (None, 443):
        raise EgressDenied("base_url must use the default https port")
    if not _host_allowed(host, hosts):
        raise EgressDenied(f"host {host!r} is not an allowed {what} host")
    return f"https://{host}{path}"
