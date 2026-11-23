"""Collects the tenant-isolation matrix and writes it as markdown when INPROOM_ISOLATION_REPORT is set."""

import os
from pathlib import Path

MATRIX: list[tuple[str, str, str, str]] = []  # (route, attack, expected, observed)


def record(route: str, attack: str, expected: str, observed: str) -> None:
    MATRIX.append((route, attack, expected, observed))


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("INPROOM_ISOLATION_REPORT")
    if not target or not MATRIX:
        return
    lines = [
        "# Tenant isolation matrix",
        "",
        "Attacker: tenant B (tenant_admin) using tenant A's identifiers, plus forged/foreign tokens.",
        "",
        "| Route | Attack | Expected | Observed | Result |",
        "|---|---|---|---|---|",
    ]
    for route, attack, expected, observed in sorted(set(MATRIX)):
        verdict = "PASS" if expected == observed else "FAIL"
        lines.append(f"| `{route}` | {attack} | {expected} | {observed} | {verdict} |")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text("\n".join(lines) + "\n")
