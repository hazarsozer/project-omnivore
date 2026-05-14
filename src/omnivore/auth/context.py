from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AuthContext:
    tenant_id: uuid.UUID
    principal_id: str
    principal_type: Literal["api_key", "jwt"]
    scopes: frozenset[str]
    raw_token_hash: str  # sha256(raw credential) — for audit logging only, never the secret
