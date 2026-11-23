"""Write docs/security/rbac-matrix.md from the policy code, or verify it is current (--check)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.authz.matrix import render  # noqa: E402

DOC = Path(__file__).resolve().parents[3] / "docs" / "security" / "rbac-matrix.md"


def main() -> int:
    current = render()
    if "--check" in sys.argv:
        if not DOC.exists() or DOC.read_text() != current:
            print(
                "RBAC MATRIX DRIFT: docs/security/rbac-matrix.md is out of date. Run `make rbac-matrix`.",
                file=sys.stderr,
            )
            return 1
        print("rbac: rbac-matrix.md is up to date")
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(current)
    print(f"rbac: wrote {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
