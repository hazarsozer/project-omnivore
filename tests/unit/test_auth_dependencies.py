"""Unit tests for auth/dependencies.py — require_auth, require_scope error paths."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnivore.auth.context import AuthContext
from omnivore.auth.dependencies import require_auth, require_scope
from omnivore.auth.errors import (
    InsufficientScopeError,
    InvalidCredentialsError,
)

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SCOPES = frozenset(["documents:read", "documents:write", "search:read"])


def _make_ctx(**kwargs) -> AuthContext:
    defaults = dict(
        tenant_id=_TENANT_ID,
        principal_id="test",
        principal_type="api_key",
        scopes=_SCOPES,
        raw_token_hash="",
    )
    defaults.update(kwargs)
    return AuthContext(**defaults)


def _make_request(api_key=None, bearer=None) -> MagicMock:
    req = MagicMock()
    return req


class TestRequireAuth:
    async def test_no_credentials_raises(self):
        with pytest.raises(InvalidCredentialsError):
            await require_auth(request=_make_request(), api_key_header=None, bearer=None)

    async def test_api_key_header_dispatches(self):
        ctx = _make_ctx()
        with patch("omnivore.auth.dependencies._resolve_api_key", AsyncMock(return_value=ctx)):
            result = await require_auth(
                request=_make_request(),
                api_key_header="omn_live_somekey",
                bearer=None,
            )
        assert result is ctx

    async def test_bearer_dispatches(self):
        ctx = _make_ctx(principal_type="jwt")
        bearer = MagicMock()
        bearer.credentials = "eyJ..."
        with patch("omnivore.auth.dependencies._resolve_jwt", AsyncMock(return_value=ctx)):
            result = await require_auth(
                request=_make_request(),
                api_key_header=None,
                bearer=bearer,
            )
        assert result is ctx

    async def test_api_key_takes_precedence_over_bearer(self):
        ctx_key = _make_ctx()
        ctx_jwt = _make_ctx(principal_type="jwt")
        bearer = MagicMock()
        bearer.credentials = "eyJ..."
        with (
            patch("omnivore.auth.dependencies._resolve_api_key", AsyncMock(return_value=ctx_key)),
            patch("omnivore.auth.dependencies._resolve_jwt", AsyncMock(return_value=ctx_jwt)),
        ):
            result = await require_auth(
                request=_make_request(),
                api_key_header="omn_live_key",
                bearer=bearer,
            )
        assert result.principal_type == "api_key"


class TestRequireScope:
    async def test_allowed_scope_passes(self):
        ctx = _make_ctx()
        dep = require_scope("documents:read")
        result = await dep(auth=ctx)
        assert result is ctx

    async def test_missing_scope_raises(self):
        ctx = _make_ctx(scopes=frozenset(["documents:read"]))
        dep = require_scope("admin:*")
        with pytest.raises(InsufficientScopeError) as exc:
            await dep(auth=ctx)
        assert exc.value.required_scope == "admin:*"

    async def test_empty_scopes_raises(self):
        ctx = _make_ctx(scopes=frozenset())
        dep = require_scope("documents:read")
        with pytest.raises(InsufficientScopeError):
            await dep(auth=ctx)
