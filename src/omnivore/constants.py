from __future__ import annotations

import uuid

# Hardcoded until Phase 4 (JWT multi-tenant auth).
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Single source of truth for the GPU worker queue name.
# Used by: api/routes/documents.py (backpressure), worker/tasks.py (routing), worker/gpu_main.py (WorkerSettings).
# Centralised here to prevent the queue-name drift bug class (Phase 2c C1 incident).
GPU_QUEUE_NAME: str = "arq:gpu"
