"""Secret removal for generated documents (INPROOM-224 / story INPROOM-35).

`scrub(text)` replaces credentials with `[REDACTED:<type>]` and reports how many of each type it removed.
It runs over every string a memory snapshot renders (titles, summaries, labels) BEFORE the snapshot is hashed
or stored, so a secret never reaches a snapshot, its sha256 or the database. Counts are kept; secrets never.

The rules aim at high-confidence credential shapes. An identifier that merely looks random (a UUID, a git sha,
a sha256) is not a secret and is left alone; so is prose such as "password policy" or "rotate the token".
Bump RULES_VERSION when a rule changes: it is part of the generator version, so snapshots stay reproducible.
"""

import re
from dataclasses import dataclass

RULES_VERSION = "1"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    group: int = 0  # the group that is secret (0: the whole match)


RULES: tuple[Rule, ...] = (
    Rule(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----)?", re.DOTALL
        ),
    ),
    Rule("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    Rule("SLACK_TOKEN", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    Rule("ATLASSIAN_TOKEN", re.compile(r"\bATATT[A-Za-z0-9_=-]{20,}")),
    Rule("API_KEY", re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{20,}")),
    Rule("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Rule("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/-]{16,}=*)"), 1),
    Rule("URL_PASSWORD", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:([^\s@/]+)@"), 1),
    Rule(
        "ASSIGNED_SECRET",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"\s*[:=]\s*[\"']?([^\s\"',;]{4,})"
        ),
        1,
    ),
)


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """(text with secrets replaced by [REDACTED:<type>], {type: count}). Pure (unit-tested)."""
    counts: dict[str, int] = {}
    for rule in RULES:

        def replace(m: re.Match, rule: Rule = rule) -> str:
            if m.group(rule.group).startswith("[REDACTED:"):
                return m.group(0)
            counts[rule.name] = counts.get(rule.name, 0) + 1
            if rule.group == 0:
                return f"[REDACTED:{rule.name}]"
            start, end = m.start(rule.group) - m.start(0), m.end(rule.group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + f"[REDACTED:{rule.name}]" + whole[end:]

        text = rule.pattern.sub(replace, text)
    return text, counts
