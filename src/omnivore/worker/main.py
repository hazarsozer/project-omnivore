from __future__ import annotations

import arq
from arq.connections import RedisSettings

from omnivore.config import get_settings
from omnivore.worker.tasks import ingest_dispatch, on_shutdown, on_startup


class WorkerSettings:
    functions = [ingest_dispatch]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    queue_name = "arq:default"
    max_jobs = 10
    job_timeout = 3600
    keep_result = 86400
    retry_jobs = True
    max_tries = 5


def main() -> None:
    arq.run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
