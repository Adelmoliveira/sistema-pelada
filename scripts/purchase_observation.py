#!/usr/bin/env python3
"""Explicit administrative activation/recovery; never applies migrations."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import DbWrapper, connect_sqlite
from src.services.purchase_observation import activate_observation, process_observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "process", "status", "deactivate"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sqlite", help="Existing isolated SQLite file")
    target.add_argument("--postgres", action="store_true", help="Read DATABASE_URL from environment")
    parser.add_argument("--confirm-environment", choices=("homologation", "production"))
    args = parser.parse_args()
    if args.postgres:
        if not args.confirm_environment or os.environ.get("APP_ENV") != args.confirm_environment:
            parser.error("PostgreSQL requires --confirm-environment matching APP_ENV")
        import psycopg2
        from psycopg2.extras import DictCursor
        db = DbWrapper(psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require", connect_timeout=10,
                                       options="-c timezone=UTC", cursor_factory=DictCursor), True)
    else:
        if not Path(args.sqlite).is_file():
            parser.error("SQLite database must already exist; initialization is a separate operation")
        db = connect_sqlite(args.sqlite)
    try:
        if args.action == "activate":
            result = activate_observation(db)
        elif args.action == "process":
            result = process_observation(db)
        elif args.action == "deactivate":
            # Same mutex as activation/processing; SQL writes remain untouched.
            from src.services.purchase_observation import _transaction
            with _transaction(db, snapshot=False) as locked:
                if locked:
                    db.execute("UPDATE purchase_observation_control SET enabled=0 WHERE id=1")
                result = {"deactivated": bool(locked)}
        else:
            control = db.execute("SELECT * FROM purchase_observation_control WHERE id=1").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL").fetchone()[0]
            result = {"control": dict(control), "pending": pending}
        print(json.dumps(result))
    finally:
        db.close()


if __name__ == "__main__":
    main()
