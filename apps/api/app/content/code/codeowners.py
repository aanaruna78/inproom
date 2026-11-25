"""CODEOWNERS: who owns a path, with GitHub's rules (INPROOM-174 / story INPROOM-29).

- One rule per line: a pattern, then owners (@user, @org/team or e-mail). A pattern with no owners is a
  valid rule meaning "nobody owns this" (it overrides earlier matches).
- The LAST matching rule wins.
- Patterns follow GitHub's gitignore subset: "/" at the start or in the middle anchors to the repository root;
  otherwise the pattern matches at any depth; a trailing "/" matches everything under that directory;
  "*" stays within a path segment, "**" crosses segments; "docs/*" matches docs' direct files only.
- Lines GitHub would skip are reported and ignored: "!" negation, "[ ]" ranges, "\\#" escapes, and lines
  with an invalid owner.
"""

import re
from dataclasses import dataclass

OWNER = re.compile(r"^(@[A-Za-z0-9][\w.-]*(/[\w.-]+)?|[^@\s]+@[^@\s]+\.[^@\s]+)$")


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]
    line: int
    regex: re.Pattern


def _compile(pattern: str) -> re.Pattern:
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in body
    direct_files_only = body.endswith("/*") and not body.endswith("/**/*")
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("(?:/.*)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    if directory:
        suffix = "/.*$"
    elif direct_files_only:
        suffix = "$"
    else:
        suffix = "(?:/.*)?$"  # a pattern naming a directory also covers what is inside it
    return re.compile(prefix + "".join(out) + suffix)


def parse(text: str) -> tuple[list[Rule], list[dict]]:
    """(rules in file order, errors [{line, reason}])."""
    rules, errors = [], []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        parts = line.replace("\\ ", "\0").split()
        pattern = parts[0].replace("\0", " ")
        owners = tuple(p.replace("\0", " ") for p in parts[1:])
        if pattern.startswith("!") or "[" in pattern or pattern.startswith("\\#"):
            errors.append({"line": number, "reason": f"unsupported pattern syntax: {pattern}"})
            continue
        bad = [o for o in owners if not OWNER.match(o)]
        if bad:  # GitHub skips a line with invalid syntax entirely
            errors.append({"line": number, "reason": f"invalid owner {bad[0]!r}"})
            continue
        rules.append(Rule(pattern, owners, number, _compile(pattern)))
    return rules, errors


def owners_of(rules: list[Rule], path: str) -> list[str]:
    """The owners of `path` (repository-relative, no leading "/"): the last matching rule's owners."""
    for rule in reversed(rules):
        if rule.regex.match(path):
            return list(rule.owners)
    return []
