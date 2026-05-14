from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

from omnivore.auth.errors import InvalidCredentialsError
from omnivore.config import get_settings


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
        "iss": "omnivore",
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
            options={"require": ["exp", "iat", "sub", "tenant_id", "scopes"]},
        )
    except pyjwt.PyJWTError:
        raise InvalidCredentialsError()
