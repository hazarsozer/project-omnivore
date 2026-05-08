from __future__ import annotations

import arq
from arq import cron
from arq.connections import RedisSettings

from omnivore.config import get_settings
from omnivore.worker.tasks import ingest_dispatch, on_shutdown, on_startup, outbox_relay


class WorkerSettings:
    functions = [ingest_dispatch, outbox_relay]
    cron_jobs = [cron(outbox_relay, second={0, 30})]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    # Must match the ArqRedis default (`arq:queue`) used by the API pool, which enqueues
    # without an explicit `_queue_name`. Mismatch was a silent bug from Phase 0 — fixed 2026-05-08.
    queue_name = "arq:queue"
    max_jobs = 10
    job_timeout = 3600
    keep_result = 86400
    retry_jobs = True
    max_tries = 5


def main() -> None:
    arq.run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
