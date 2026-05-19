# Scripts

Standalone maintenance scripts for the Omnivore pipeline.

---

## `create_monthly_partition.py`

Creates a new monthly RANGE partition on `core.jobs` in PostgreSQL.

The `core.jobs` table is partitioned by `created_at` (RANGE). Each calendar month needs its own child partition. Without it, rows fall into the `jobs_default` catch-all partition, which defeats the purpose of partitioning (pruning, parallel scan, easier data archiving).

### Usage

```bash
# Create next calendar month's partition (most common usage)
uv run python scripts/create_monthly_partition.py

# Create a specific month's partition
uv run python scripts/create_monthly_partition.py 2026-08

# Dry-run: print the SQL without executing
uv run python scripts/create_monthly_partition.py --dry-run
uv run python scripts/create_monthly_partition.py 2026-09 --dry-run
```

### When to run

Run on the **first day of each month** (or a few days before month-end), before the new month's data arrives. The script is idempotent — if the partition already exists, it prints a message and exits 0 without touching anything.

### Environment / configuration

The script reads `DATABASE_URL` from:

1. The `DATABASE_URL` environment variable (takes priority)
2. A `.env` file in the project root
3. Falls back to the development default: `postgresql+asyncpg://omnivore:omnivore@localhost:5432/omnivore`

### Setting up a cron job

Add this to the crontab on the host that has access to the database:

```cron
# Create the next month's jobs partition on the 1st of each month at 00:00
0 0 1 * * cd /path/to/omnivore && uv run python scripts/create_monthly_partition.py >> /var/log/omnivore-partitions.log 2>&1
```

Or, if you prefer to run it a few days early to guarantee the partition exists before the month turns:

```cron
# Run on the 28th of each month at 01:00 (always before month end)
0 1 28 * * cd /path/to/omnivore && uv run python scripts/create_monthly_partition.py >> /var/log/omnivore-partitions.log 2>&1
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (partition created or already existed) |
| 1 | Error (bad date format, connection failure, SQL error) |
