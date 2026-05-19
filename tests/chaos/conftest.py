"""Shared fixtures for chaos/resilience tests.

All chaos tests are pure unit tests — they call route handlers and task
functions directly without a live HTTP server or real infrastructure.
The patterns here mirror tests/unit/test_routes_documents.py and
tests/unit/test_tasks.py.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnivore.auth.context import AuthContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_ALL_SCOPES = frozenset([
    "documents:read",
    "documents:write",
    "chunks:read",
    "entities:read",
    "search:read",
    "handlers:read",
    "tenant:manage",
])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_ctx() -> AuthContext:
    return AuthContext(
        tenant_id=_TEST_TENANT_ID,
        principal_id="chaos-test-key",
        principal_type="api_key",
        scopes=_ALL_SCOPES,
        raw_token_hash="",
    )


# ---------------------------------------------------------------------------
# Rate-limit mock (always allowed)
# ---------------------------------------------------------------------------

@pytest.fixture
def rl_allowed() -> MagicMock:
    rl = MagicMock()
    rl.allowed = True
    rl.remaining = 90.0
    rl.capacity = 100
    rl.reset_after_seconds = 0
    return rl


# ---------------------------------------------------------------------------
# Settings mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_settings() -> MagicMock:
    s = MagicMock()
    s.MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
    s.MAX_QUEUE_DEPTH = 100
    s.MAX_GPU_QUEUE_DEPTH = 20
    s.MINIO_ENDPOINT = "localhost:9000"
    s.MINIO_SECURE = False
    s.MINIO_ACCESS_KEY = "minioadmin"
    s.MINIO_SECRET_KEY = MagicMock()
    s.MINIO_SECRET_KEY.get_secret_value.return_value = "minioadmin"
    s.MINIO_BUCKET = "omnivore"
    s.RL_UPLOAD_COST = 10
    return s


# ---------------------------------------------------------------------------
# S3 session mock (success by default)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_s3_session():
    """Return (session_mock, s3_client_mock) — upload_fileobj succeeds by default."""
    s3 = AsyncMock()
    s3.head_bucket = AsyncMock()
    s3.upload_fileobj = AsyncMock()
    s3.create_bucket = AsyncMock()

    s3_cm = AsyncMock()
    s3_cm.__aenter__ = AsyncMock(return_value=s3)
    s3_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.client = MagicMock(return_value=s3_cm)
    return session, s3


# ---------------------------------------------------------------------------
# ARQ pool mock
# ---------------------------------------------------------------------------

def make_arq_pool(queue_depth: int = 0) -> AsyncMock:
    pool = AsyncMock()
    pool.default_queue_name = "arq:queue"
    pool.zcard = AsyncMock(return_value=queue_depth)
    return pool


# ---------------------------------------------------------------------------
# Request mock
# ---------------------------------------------------------------------------

def make_request(arq_pool=None) -> MagicMock:
    req = MagicMock()
    req.app = MagicMock()
    req.app.state = MagicMock()
    req.app.state.arq_pool = arq_pool
    return req


# ---------------------------------------------------------------------------
# Database mock
# ---------------------------------------------------------------------------

def make_db(scalar_return=None, get_return=None) -> AsyncMock:
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=scalar_return)
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    db.scalars = AsyncMock(return_value=scalars_mock)
    db.get = AsyncMock(return_value=get_return)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    execute_result = MagicMock()
    execute_result.fetchone = MagicMock(return_value=MagicMock())
    db.execute = AsyncMock(return_value=execute_result)
    return db


# ---------------------------------------------------------------------------
# Upload file mock
# ---------------------------------------------------------------------------

def make_upload(content: bytes = b"chaos test file content", filename: str = "chaos.txt") -> MagicMock:
    f = MagicMock()
    f.filename = filename
    f.read = AsyncMock(side_effect=[content, b""])
    f.seek = AsyncMock()
    f.file = io.BytesIO(content)
    return f


# ---------------------------------------------------------------------------
# Worker context mock (ARQ ctx dict)
# ---------------------------------------------------------------------------

def make_arq_ctx() -> dict:
    return {"redis": AsyncMock(), "job_id": "chaos-test-job"}


# ---------------------------------------------------------------------------
# Document model mock
# ---------------------------------------------------------------------------

def make_doc_mock(doc_id: uuid.UUID | None = None, status: str = "queued") -> MagicMock:
    doc = MagicMock()
    doc.id = doc_id or uuid.uuid4()
    doc.filename = "chaos_test.txt"
    doc.storage_uri = "s3://omnivore/raw/chaos_test.txt"
    doc.status = status
    doc.handler_name = None
    doc.handler_version = None
    doc.doc_metadata = {}
    doc.error = None
    doc.config = {}
    return doc


# ---------------------------------------------------------------------------
# Worker session mock (tenant_session context manager)
# ---------------------------------------------------------------------------

def make_worker_session(doc=None, claim_succeeds: bool = True):
    """Return (session_factory_mock, db_mock) for patching worker.tasks.tenant_session."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=doc)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    claim_row = MagicMock() if claim_succeeds else None
    execute_result = MagicMock()
    execute_result.fetchone = MagicMock(return_value=claim_row)
    db.execute = AsyncMock(return_value=execute_result)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=cm)
    return session_factory, db
