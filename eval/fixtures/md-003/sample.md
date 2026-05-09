# PostgreSQL Internals and Performance Guide

A practical reference covering query planning, indexing strategies, concurrency control,
and operational patterns for production PostgreSQL deployments.

## Query Planning and the Planner

The PostgreSQL query planner transforms a parsed SQL statement into an execution plan by
estimating the cost of alternative access paths. Cost is measured in abstract units
representing disk I/O and CPU time. The planner collects statistics — row counts,
histogram buckets, correlation — from `pg_statistic` and updates them via `ANALYZE`.

Run `EXPLAIN (ANALYZE, BUFFERS)` to see the actual plan, row counts, and buffer hits.
A large discrepancy between estimated and actual rows is the primary symptom of stale
statistics. Set `autovacuum_analyze_scale_factor` lower on large, frequently-written
tables to keep statistics fresh.

## Index Types and When to Use Them

B-tree is the default index type and handles equality and range predicates on sortable
types. Hash indexes are smaller and faster for equality-only lookups but are not
WAL-logged before PostgreSQL 10.

GIN (Generalized Inverted Index) indexes array elements, JSONB keys, and full-text
tsvector values. A GIN index on a JSONB column lets you query nested keys efficiently
with `@>` and `?` operators. GIN builds are slow and index updates are expensive;
enable `fastupdate` to batch pending insertions.

BRIN (Block Range Index) is tiny and useful for naturally-ordered columns like
timestamps in append-only tables. A BRIN index covers thousands of rows per range entry
and is O(1) in size relative to the table.

## Partial and Expression Indexes

A partial index adds a `WHERE` clause and covers only rows that match. This shrinks the
index, speeds up writes on unindexed rows, and can be used by the planner only when the
query predicate implies the index predicate.

An expression index indexes a function of one or more columns. Common uses:
`lower(email)` for case-insensitive lookups, `date_trunc('day', created_at)` for daily
aggregations, and computed JSONB paths. The expression must match exactly — including
function name and argument order — for the planner to use the index.

## MVCC and Transaction Isolation

PostgreSQL implements MVCC (Multi-Version Concurrency Control): each transaction sees a
snapshot of the database as it existed at a consistent point in time. Writers never
block readers and readers never block writers. Row versions are stored in the heap;
obsolete versions are reclaimed by VACUUM.

The default isolation level is Read Committed: each statement sees the latest committed
data. Repeatable Read prevents non-repeatable reads within a transaction. Serializable
Snapshot Isolation (SSI) detects and aborts transactions that would produce anomalies;
it is the strongest guarantee and suitable for financial applications.

## Locking and Deadlock Prevention

Row-level locks (`SELECT FOR UPDATE`, `FOR SHARE`) prevent concurrent modifications.
Table-level locks from DDL statements (`ALTER TABLE`, `CREATE INDEX`) are the most
disruptive: they block all DML until the lock is released.

To create an index without blocking: use `CREATE INDEX CONCURRENTLY`. This takes a
weaker lock and performs two scans of the table, which is slower but safe on a live
system. Deadlocks occur when two transactions wait for each other; PostgreSQL detects
and breaks them automatically by aborting one. Design application code to acquire locks
in a consistent order to minimize deadlock frequency.

## VACUUM and Table Bloat

VACUUM reclaims space from dead row versions and updates the visibility map. Without it,
tables bloat as dead tuples accumulate. `autovacuum` runs automatically based on
`autovacuum_vacuum_scale_factor` and `autovacuum_vacuum_threshold`.

`VACUUM FULL` rewrites the table, reclaims all free space, and acquires an exclusive
lock — use only when bloat is severe and off-hours maintenance is acceptable. Monitor
bloat with `pgstattuple` or `pg_bloat_info` from `pg_freespacemap`. On heavily-written
tables, tune `autovacuum_vacuum_cost_delay` to zero to let autovacuum run faster.

## Connection Pooling with PgBouncer

Each PostgreSQL backend is an OS process (~5 MB RSS). A web application that opens one
connection per request will exhaust connection slots within seconds under load.
PgBouncer multiplexes many client connections onto a smaller pool of server connections.

In transaction pooling mode, a server connection is returned to the pool after each
transaction commit or rollback. This allows thousands of application threads to share
tens of server connections. Avoid session-level state (`SET`, prepared statements) in
transaction pooling mode — the session may be reused by a different client.

## Write-Ahead Log and Durability

The WAL (Write-Ahead Log) is an append-only record of every change to the database.
Changes are written to WAL before they are applied to the heap, guaranteeing durability:
if the server crashes mid-write, recovery replays WAL from the last checkpoint.
WAL files live under `pg_wal/` and are named with a 24-character hexadecimal segment
identifier. Use `pg_walfile_name(pg_current_wal_lsn())` to find the current WAL file,
and `pg_waldump` to inspect its contents for debugging.

`fsync = on` is the default and must not be disabled in production. Disabling it makes
writes faster but sacrifices durability — a crash can corrupt the database cluster
entirely. Use `synchronous_commit = off` for individual transactions that can tolerate
a small replication lag while keeping `fsync` enabled globally.

Checkpoints flush all dirty buffers to disk and write a checkpoint record to WAL.
PostgreSQL performs automatic checkpoints whenever `max_wal_size` is reached or
`checkpoint_timeout` seconds elapse (default 5 minutes). Aggressive writes can trigger
frequent checkpoints — monitor `pg_stat_bgwriter.checkpoints_timed` versus
`checkpoints_req` to detect checkpoint pressure. Spreading checkpoints with
`checkpoint_completion_target = 0.9` reduces I/O spikes.

`wal_level` controls how much information is written to WAL. The default is `replica`,
which supports streaming replication and point-in-time recovery (PITR). Set
`wal_level = logical` to enable logical decoding and replication slots for CDC
(change data capture) tools. The `minimal` level writes less WAL but does not support
replication — suitable only for standalone instances where PITR is not required.

`wal_buffers` (default 1/32 of `shared_buffers`, capped at 16 MB) controls the
amount of WAL data held in shared memory before being flushed. For write-heavy
workloads, increasing `wal_buffers` to 64 MB can reduce flush frequency and improve
throughput. WAL compression (`wal_compression = on`) reduces WAL volume at the cost
of CPU, which is usually worthwhile on spinning disks or expensive network replication.

## Partitioning Large Tables

Declarative partitioning divides a table into child tables (partitions) by range, list,
or hash of a partition key. The planner prunes partitions that cannot satisfy the query
predicate, scanning only relevant children. This dramatically speeds up time-range
queries on time-series data.

Partition pruning requires that the query predicate includes the partition key with a
constant or parameter the planner can evaluate at plan time. Dynamic expressions or
subqueries prevent pruning. Indexes on the parent table are not inherited; create them
on each child partition or on the parent with `CREATE INDEX ON ONLY`.

## Full-Text Search

`tsvector` stores a pre-processed list of lexemes (normalized word stems) and their
positions within a document. `tsquery` represents a boolean expression of lexemes.
The `@@` operator matches a vector against a query.

Index with GIN for fast lookups. Use `ts_rank` or `ts_rank_cd` to score results by
lexeme frequency and proximity. For multilingual content, specify a text search
configuration matching the document language: `to_tsvector('french', body)`.
Full-text search is less precise than embedding-based semantic search but has no model
inference cost and handles exact term matching better.

## Logical Replication

Logical replication decodes WAL changes into row-level events (INSERT, UPDATE, DELETE)
and streams them to subscribers. Unlike physical replication, logical replication can
target different PostgreSQL major versions and replicate selected tables.

Use it for zero-downtime major-version upgrades, real-time data sync to analytics
warehouses, or building Change Data Capture (CDC) pipelines. Each publication defines
which tables to replicate; each subscription connects to one publication. Row filters
(PostgreSQL 15+) allow replicating a subset of rows to a subscriber.

## Row-Level Security

RLS (Row-Level Security) attaches security policies directly to tables, enforcing access
control inside the database engine rather than the application layer. Policies are
expressions evaluated per row for each SQL command type (SELECT, INSERT, UPDATE, DELETE).

Enable RLS with `ALTER TABLE … ENABLE ROW LEVEL SECURITY`. Define policies with
`CREATE POLICY`. Superusers and table owners bypass RLS by default; set
`FORCE ROW LEVEL SECURITY` to apply policies even to owners. In multi-tenant SaaS
applications, set `app.tenant_id` as a session variable and reference it in every
policy to isolate tenant data at the database level.

## pg_stat_statements for Query Analysis

The `pg_stat_statements` extension tracks execution statistics for every distinct query
text seen by the server. Metrics include total and mean execution time, I/O time, rows
returned, and shared buffer hits and misses.

Query: `SELECT query, mean_exec_time, calls FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;`
This surfaces the slowest queries by cumulative cost, which are the most valuable
candidates for optimization. Reset statistics with `pg_stat_statements_reset()` after
a tuning session to measure the impact of changes.
