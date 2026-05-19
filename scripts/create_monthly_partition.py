#!/usr/bin/env python3
"""
Create a new monthly partition for core.jobs.

Usage:
    uv run python scripts/create_monthly_partition.py           # creates next month's partition
    uv run python scripts/create_monthly_partition.py 2026-08   # creates a specific month
    uv run python scripts/create_monthly_partition.py --dry-run # print SQL without executing

Run monthly (e.g. first day of the month via cron):
    0 0 1 * * cd /path/to/omnivore && uv run python scripts/create_monthly_partition.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path


def next_month(today: date) -> tuple[int, int]:
    """Return (year, month) for the month after today."""
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


def parse_year_month(value: str) -> tuple[int, int]:
    """Parse a YYYY-MM string into (year, month). Raises ValueError on bad input."""
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected YYYY-MM format, got: {value!r}")
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        raise ValueError(f"Expected YYYY-MM format, got: {value!r}")
    if not (1 <= month <= 12):
        raise ValueError(f"Month must be 1–12, got: {month}")
    if year < 2000 or year > 2100:
        raise ValueError(f"Year out of reasonable range: {year}")
    return year, month


def partition_name(year: int, month: int) -> str:
    return f"jobs_{year:04d}_{month:02d}"


def partition_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (from_date, to_date) strings for the partition range."""
    from_date = date(year, month, 1)
    # First day of next month
    last_day = monthrange(year, month)[1]
    end = date(year, month, last_day) + timedelta(days=1)
    return str(from_date), str(end)


def build_sql(year: int, month: int) -> str:
    name = partition_name(year, month)
    from_date, to_date = partition_bounds(year, month)
    return (
        f"CREATE TABLE IF NOT EXISTS core.{name}\n"
        f"    PARTITION OF core.jobs\n"
        f"    FOR VALUES FROM ('{from_date}') TO ('{to_date}');"
    )


def get_database_url() -> str:
    """Read DATABASE_URL from environment or .env file, preferring environment."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    # Try loading from .env in the project root (one level up from scripts/)
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line[len("DATABASE_URL="):].strip().strip('"').strip("'")
                if url:
                    return url

    # Fall back to the default used in config.py
    return "postgresql+asyncpg://omnivore:omnivore@localhost:5432/omnivore"


def asyncpg_url(database_url: str) -> str:
    """Ensure the URL uses the postgresql+asyncpg:// scheme."""
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


async def _run_sql(database_url: str, sql: str) -> None:
    """Execute SQL using the asyncpg driver (no psycopg2 required)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = asyncpg_url(database_url)
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
            await conn.commit()
    finally:
        await engine.dispose()


async def _check_exists(database_url: str, year: int, month: int) -> bool:
    """Return True if the partition table already exists in core schema."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    name = partition_name(year, month)
    url = asyncpg_url(database_url)
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'core' AND c.relname = :tname"
                ),
                {"tname": name},
            )
            return result.fetchone() is not None
    finally:
        await engine.dispose()


def execute_sql(database_url: str, sql: str) -> None:
    asyncio.run(_run_sql(database_url, sql))


def partition_exists(database_url: str, year: int, month: int) -> bool:
    return asyncio.run(_check_exists(database_url, year, month))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a monthly partition for core.jobs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "month",
        nargs="?",
        metavar="YYYY-MM",
        help="Month to create partition for (default: next calendar month)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the SQL that would be executed without running it",
    )
    args = parser.parse_args()

    # Determine target year/month
    if args.month:
        try:
            year, month = parse_year_month(args.month)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        year, month = next_month(date.today())

    sql = build_sql(year, month)

    if args.dry_run:
        print("-- DRY RUN: the following SQL would be executed")
        print(sql)
        return 0

    database_url = get_database_url()

    try:
        # Check existence before attempting CREATE to give a friendlier message.
        # (CREATE TABLE IF NOT EXISTS is idempotent but we still want the message.)
        exists = partition_exists(database_url, year, month)
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        return 1

    if exists:
        name = partition_name(year, month)
        print(f"Partition core.{name} already exists, nothing to do.")
        return 0

    try:
        execute_sql(database_url, sql)
    except Exception as exc:
        print(f"ERROR: Failed to create partition: {exc}", file=sys.stderr)
        return 1

    name = partition_name(year, month)
    from_date, to_date = partition_bounds(year, month)
    print(
        f"Created partition core.{name} "
        f"(FROM '{from_date}' TO '{to_date}')."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
