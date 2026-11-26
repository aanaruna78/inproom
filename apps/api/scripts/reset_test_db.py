"""Recreate and migrate the test database before a test run (`make test`).

Tests create fresh tenants and never clean up. Run against a long-lived database, that data accumulates
(tens of thousands of tenants) and slows every scheduler and queue query. So each run starts from an empty,
migrated database named by INPROOM_TEST_DATABASE_URL.

Refuses to touch the development database: the test database name must differ from INPROOM_DATABASE_URL's
and must contain "test".
"""

import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

API_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    test_url = os.environ.get("INPROOM_TEST_DATABASE_URL")
    dev_url = os.environ.get("INPROOM_DATABASE_URL")
    if not test_url:
        print("reset_test_db: INPROOM_TEST_DATABASE_URL is not set", file=sys.stderr)
        return 2
    test = make_url(test_url)
    name = test.database or ""
    if "test" not in name or (dev_url and make_url(dev_url).database == name):
        print(
            f"reset_test_db: refusing to reset {name!r}. Point INPROOM_TEST_DATABASE_URL at a dedicated "
            "database whose name contains 'test' (for example .../inproom_test), never the dev database.",
            file=sys.stderr,
        )
        return 2
    started = time.monotonic()
    maintenance = test.set(database="postgres").render_as_string(hide_password=False)
    admin = create_engine(maintenance, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", test.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    print(f"reset_test_db: {name} recreated and migrated in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
