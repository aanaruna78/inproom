"""Write the API's OpenAPI document to packages/contracts/openapi.json, or verify it is current (--check)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

CONTRACT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    current = render()
    if "--check" in sys.argv:
        committed = CONTRACT.read_text() if CONTRACT.exists() else ""
        if committed != current:
            print(
                f"CONTRACT DRIFT: {CONTRACT.relative_to(CONTRACT.parents[2])} is out of date.\n"
                "Run `make contracts` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("contracts: openapi.json is up to date")
        return 0
    CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT.write_text(current)
    print(f"contracts: wrote {CONTRACT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
