from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

from omnivore.auth.errors import InvalidCredentialsError
from omnivore.config import get_settings

# Single source of truth for the JWT issuer claim. issue_token() sets it and
# decode_token() enforces it so tokens minted by another Omnivore instance are
# rejected — keep both sides reading this constant so they can't drift.
_ISSUER = "omnivore"


def issue_token(
    *,
    tenant_id: uuid.UUID,
    principal_id: str,
    scopes: list[str],
) -> str:
    settings = get_settings()
    private_key = settings.JWT_PRIVATE_KEY_PEM.get_secret_value()
    if not private_key:
        raise ValueError("JWT_PRIVATE_KEY_PEM not configured")

    now = datetime.now(UTC)
    payload = {
        "iss": _ISSUER,
        "sub": principal_id,
        "tenant_id": str(tenant_id),
        "scopes": scopes,
        "iat": now,
        "exp": now + timedelta(seconds=settings.JWT_ACCESS_TOKEN_EXPIRE_SECONDS),
        "jti": str(uuid.uuid4()),
        "token_type": "access",
    }
    return pyjwt.encode(payload, private_key, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    public_key = settings.JWT_PUBLIC_KEY_PEM
    if not public_key:
        raise InvalidCredentialsError()
    try:
        return pyjwt.decode(
            token,
            public_key,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "tenant_id", "scopes"]},
        )
    except pyjwt.PyJWTError:
        raise InvalidCredentialsError()
