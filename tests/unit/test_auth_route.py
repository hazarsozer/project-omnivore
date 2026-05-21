"""Unit tests for POST /v1/auth/token — all response branches and rate limit."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, Response

from omnivore.api.routes.auth_route import TokenRequest, _ip_rate_limit, exchange_token
from omnivore.auth.rate_limit import RateLimitResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rl(allowed: bool = True) -> RateLimitResult:
    return RateLimitResult(allowed=allowed, remaining=4.0, capacity=5, cost=1, refill_rate=0.1)


def _api_key_row(*, tenant_id: uuid.UUID) -> MagicMock:
    row = MagicMock()
    row.tenant_id = tenant_id
    row.id = uuid.uuid4()
    row.key_hash = "stored_hash"
    row.scopes = ["documents:read"]
    row.revoked_at = None
    return row


def _tenant(*, status: str = "active") -> MagicMock:
    t = MagicMock()
    t.status = status
    return t


def _settings(*, private_key: str = "real-pem-key") -> MagicMock:
    s = MagicMock()
    pem = MagicMock()
    pem.get_secret_value.return_value = private_key
    s.JWT_PRIVATE_KEY_PEM = pem
    s.JWT_ACCESS_TOKEN_EXPIRE_SECONDS = 3600
    return s


def _make_admin_session(rows, tenant):
    """Return a mock admin_session that serves both calls in exchange_token."""
    @asynccontextmanager
    async def _session():
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = rows
        db.scalars = AsyncMock(return_value=result)
        db.get = AsyncMock(return_value=tenant)
        yield db
    return _session


# ---------------------------------------------------------------------------
# _ip_rate_limit dependency
# ---------------------------------------------------------------------------

async def test_ip_rate_limit_passes_when_allowed():
    request = MagicMock(spec=Request)
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "1.2.3.4"
    response = MagicMock(spec=Response)
    response.headers = {}

    with patch(
        "omnivore.api.routes.auth_route.check_auth_rate_limit",
        AsyncMock(return_value=_rl(allowed=True)),
    ):
        await _ip_rate_limit(request, response)  # should not raise


async def test_ip_rate_limit_raises_429_when_blocked():
    request = MagicMock(spec=Request)
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "1.2.3.4"
    response = MagicMock(spec=Response)
    response.headers = {}

    with patch(
        "omnivore.api.routes.auth_route.check_auth_rate_limit",
        AsyncMock(return_value=_rl(allowed=False)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _ip_rate_limit(request, response)

    assert exc_info.value.status_code == 429


async def test_ip_rate_limit_uses_x_forwarded_for():
    request = MagicMock(spec=Request)
    request.headers = {"X-Forwarded-For": "10.0.0.1, 192.168.1.1"}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    response = MagicMock(spec=Response)
    response.headers = {}

    captured_ip: list[str] = []

    async def _capture(ip: str) -> RateLimitResult:
        captured_ip.append(ip)
        return _rl(allowed=True)

    with patch("omnivore.api.routes.auth_route.check_auth_rate_limit", _capture):
        await _ip_rate_limit(request, response)

    assert captured_ip[0] == "10.0.0.1"


# ---------------------------------------------------------------------------
# exchange_token — JWT not configured
# ---------------------------------------------------------------------------

async def test_exchange_token_501_when_jwt_not_configured():
    body = TokenRequest(api_key="omni_test_abc123")

    with patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings(private_key="")):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(body)

    assert exc_info.value.status_code == 501


# ---------------------------------------------------------------------------
# exchange_token — 401 invalid credentials
# ---------------------------------------------------------------------------

async def test_exchange_token_401_when_no_rows_found():
    body = TokenRequest(api_key="omni_test_deadbeef")

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.admin_session", _make_admin_session([], _tenant())),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(body)

    assert exc_info.value.status_code == 401


async def test_exchange_token_401_when_hash_mismatch():
    body = TokenRequest(api_key="omni_test_deadbeef")
    row = _api_key_row(tenant_id=uuid.uuid4())

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.verify_key", return_value=False),
        patch("omnivore.api.routes.auth_route.admin_session", _make_admin_session([row], _tenant())),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(body)

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# exchange_token — 403 tenant issues
# ---------------------------------------------------------------------------

async def test_exchange_token_403_when_tenant_not_found():
    body = TokenRequest(api_key="omni_test_deadbeef")
    row = _api_key_row(tenant_id=uuid.uuid4())

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.verify_key", return_value=True),
        patch("omnivore.api.routes.auth_route.admin_session", _make_admin_session([row], None)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(body)

    assert exc_info.value.status_code == 403


async def test_exchange_token_403_when_tenant_suspended():
    body = TokenRequest(api_key="omni_test_deadbeef")
    row = _api_key_row(tenant_id=uuid.uuid4())

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.verify_key", return_value=True),
        patch(
            "omnivore.api.routes.auth_route.admin_session",
            _make_admin_session([row], _tenant(status="suspended")),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(body)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# exchange_token — 200 happy path
# ---------------------------------------------------------------------------

async def test_exchange_token_returns_access_token():
    body = TokenRequest(api_key="omni_test_deadbeef")
    row = _api_key_row(tenant_id=uuid.uuid4())

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.verify_key", return_value=True),
        patch("omnivore.api.routes.auth_route.issue_token", return_value="signed.jwt.token"),
        patch(
            "omnivore.api.routes.auth_route.admin_session",
            _make_admin_session([row], _tenant()),
        ),
    ):
        response = await exchange_token(body)

    assert response.success is True
    assert response.data is not None
    assert response.data["access_token"] == "signed.jwt.token"
    assert response.data["token_type"] == "Bearer"
    assert response.data["expires_in"] == 3600


async def test_exchange_token_uses_first_matching_key():
    """When multiple rows share a prefix, only the one that verifies is used."""
    body = TokenRequest(api_key="omni_test_deadbeef")
    row_bad = _api_key_row(tenant_id=uuid.uuid4())
    row_good = _api_key_row(tenant_id=uuid.uuid4())

    verify_results = [False, True]

    with (
        patch("omnivore.api.routes.auth_route.get_settings", return_value=_settings()),
        patch("omnivore.api.routes.auth_route.extract_prefix", return_value="pfx"),
        patch("omnivore.api.routes.auth_route.verify_key", side_effect=verify_results),
        patch("omnivore.api.routes.auth_route.issue_token", return_value="tok"),
        patch(
            "omnivore.api.routes.auth_route.admin_session",
            _make_admin_session([row_bad, row_good], _tenant()),
        ),
    ):
        response = await exchange_token(body)

    assert response.success is True
