from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher(memory_cost=65536, time_cost=2, parallelism=1)

_PREFIX_TAG_LEN = len("omn_live_")  # length of "omn_live_" or "omn_test_"
_PREFIX_DISPLAY_LEN = 8             # chars of token shown in prefix (after tag)


def generate_api_key(*, test: bool = False) -> tuple[str, str]:
    """Return (raw_key, prefix) for a new API key.

    raw_key is the full credential, shown exactly once and never persisted.
    prefix is stored in the DB for indexed lookup.
    """
    tag = "omn_test_" if test else "omn_live_"
    token = secrets.token_urlsafe(32)
    raw_key = tag + token
    prefix = tag + token[:_PREFIX_DISPLAY_LEN]
    return raw_key, prefix


def hash_key(raw_key: str) -> str:
    return _ph.hash(raw_key)


def verify_key(stored_hash: str, raw_key: str) -> bool:
    try:
        return _ph.verify(stored_hash, raw_key)
    except VerifyMismatchError:
        return False


def extract_prefix(raw_key: str) -> str:
    """Return the prefix stored in DB for a given raw key."""
    tag = raw_key[: _PREFIX_TAG_LEN]
    token_prefix = raw_key[_PREFIX_TAG_LEN: _PREFIX_TAG_LEN + _PREFIX_DISPLAY_LEN]
    return tag + token_prefix


def cache_key_for(raw_key: str) -> str:
    """Redis key for caching auth result — uses sha256(raw_key), not the secret itself."""
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    return f"auth:apikey:{digest}"
