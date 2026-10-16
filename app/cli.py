"""Operational commands.

    python -m app.cli create-admin --email admin@example.com --full-name "Admin"
    python -m app.cli purge-expired

``create-admin`` reads the password from ``ADMIN_PASSWORD`` (for automation)
or prompts for it; it is never accepted as a command-line argument, where it
would end up in shell history and process listings.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import PasswordHasher
from app.db.errors import UNIQUE_VIOLATION, sqlstate
from app.db.session import create_engine, create_sessionmaker
from app.models.enums import Role
from app.models.support import AuditEvent
from app.models.user import User
from app.repositories.support import MaintenanceRepository
from app.schemas.users import UserCreate

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3


def _read_password() -> str:
    from_env = os.environ.get("ADMIN_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise SystemExit("ADMIN_PASSWORD is not set and no terminal is available to prompt.")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


async def create_admin(email: str, full_name: str) -> int:
    try:
        data = UserCreate(
            email=email, full_name=full_name, role=Role.ADMIN, password=_read_password()
        )
    except ValidationError as exc:
        for error in exc.errors(include_input=False):
            print(f"invalid {'.'.join(map(str, error['loc']))}: {error['msg']}", file=sys.stderr)  # noqa: T201
        return EXIT_USAGE

    settings = get_settings()
    engine = create_engine(settings)
    try:
        hasher = PasswordHasher(settings)
        async with create_sessionmaker(engine)() as session:
            user = User(
                email=data.email,
                full_name=data.full_name,
                role=Role.ADMIN.value,
                password_hash=await hasher.hash(data.password),
                is_active=True,
                token_version=0,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError as exc:
                if sqlstate(exc) == UNIQUE_VIOLATION:
                    print(f"A user with email {data.email} already exists.", file=sys.stderr)  # noqa: T201
                    return EXIT_CONFLICT
                raise
            session.add(
                AuditEvent(
                    action="user.created",
                    entity_type="user",
                    entity_id=str(user.id),
                    details={"role": user.role, "source": "cli"},
                )
            )
            await session.commit()
            print(f"Created admin {user.email} ({user.id}).")  # noqa: T201
            return EXIT_OK
    finally:
        await engine.dispose()


async def purge_expired() -> int:
    settings = get_settings()
    engine = create_engine(settings)
    now = datetime.now(UTC)
    try:
        async with create_sessionmaker(engine)() as session:
            counts = await MaintenanceRepository(session).purge(
                # Keep expired tokens for a day so reuse detection still works
                # for clients that retry slightly after expiry.
                tokens_expired_before=now - timedelta(days=1),
                keys_created_before=now - timedelta(seconds=settings.idempotency_key_ttl_seconds),
                windows_before=now - timedelta(days=1),
            )
            await session.commit()
        print(json.dumps({"purged": counts}))  # noqa: T201
        return EXIT_OK
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="Create an admin user (bootstrap)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--full-name", required=True)
    commands.add_parser("purge-expired", help="Delete expired tokens, idempotency keys, counters")
    args = parser.parse_args(argv)

    configure_logging(get_settings())
    if args.command == "create-admin":
        return asyncio.run(create_admin(args.email, args.full_name))
    return asyncio.run(purge_expired())


if __name__ == "__main__":
    raise SystemExit(main())
