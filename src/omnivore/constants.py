from __future__ import annotations

import uuid

# Used only in tests and seeds — routes/workers read tenant_id from AuthContext (Phase 4+).
TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Alias kept so existing tests that import DEFAULT_TENANT_ID continue to work.
DEFAULT_TENANT_ID = TEST_TENANT_ID

# Single source of truth for the GPU worker queue name.
# Used by: api/routes/documents.py (backpressure), worker/tasks.py (routing), worker/gpu_main.py (WorkerSettings).
# Centralised here to prevent the queue-name drift bug class (Phase 2c C1 incident).
GPU_QUEUE_NAME: str = "arq:gpu"
