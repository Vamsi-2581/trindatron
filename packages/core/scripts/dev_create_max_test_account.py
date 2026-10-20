"""Create a MAX test account in the LOCAL development database — never anywhere else.

    uv run --no-sync python scripts/dev_create_max_test_account.py

What it does, in order:
  1. Refuses to run unless ENVIRONMENT is development and DATABASE_URL is a local SQLite file.
  2. Copies that SQLite file to <file>.backup-<timestamp> before touching anything.
  3. Creates a new user with a fresh e-mail and a strong generated password (the password is printed once and
     stored only as a hash), then sets plan = "max" on that new user only.

It touches no existing account, no subscription, no payment, no provider. To undo: delete the printed e-mail's
row, or restore the backup file.
"""
from __future__ import annotations

import secrets
import shutil
import string
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.database import SessionLocal  # noqa: E402
from src.services.auth_service import create_user  # noqa: E402


def main() -> int:
    url = settings.database_url
    if settings.is_production or not url.startswith("sqlite:///"):
        print(f"REFUSED: this script only runs against a local SQLite development database (got {url.split(':')[0]}).")
        return 1
    db_file = Path(url.removeprefix("sqlite:///")).resolve()
    if not db_file.exists():
        print(f"REFUSED: database file not found: {db_file}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_file.with_name(f"{db_file.name}.backup-{stamp}")
    shutil.copy2(db_file, backup)

    # example.com: reserved for examples yet accepted by the login validator — ".local" is refused as special-use.
    email = f"max-test-{stamp}@example.com"
    alphabet = string.ascii_letters + string.digits + "-_.!@#"
    password = "".join(secrets.choice(alphabet) for _ in range(24))

    db = SessionLocal()
    try:
        user = create_user(db, email=email, password=password, full_name="MAX Test (local)")
        user.plan = "max"
        db.commit()
        db.refresh(user)
        print("AMBIENTE:  LOCAL (development)")
        print(f"BACKUP:    {backup}")
        print(f"LOGIN:     {email}")
        print(f"SENHA:     {password}")
        print(f"PLANO:     {user.plan.upper()}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
