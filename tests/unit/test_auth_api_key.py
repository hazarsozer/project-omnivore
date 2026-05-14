"""Unit tests for auth/api_key.py — Argon2id hash/verify, prefix extraction."""
from __future__ import annotations

from omnivore.auth.api_key import (
    cache_key_for,
    extract_prefix,
    generate_api_key,
    hash_key,
    verify_key,
)


class TestGenerateApiKey:
    def test_returns_two_strings(self):
        raw, prefix = generate_api_key()
        assert isinstance(raw, str)
        assert isinstance(prefix, str)

    def test_live_key_prefix(self):
        raw, prefix = generate_api_key()
        assert raw.startswith("omn_live_")
        assert prefix.startswith("omn_live_")

    def test_test_key_prefix(self):
        raw, prefix = generate_api_key(test=True)
        assert raw.startswith("omn_test_")
        assert prefix.startswith("omn_test_")

    def test_raw_longer_than_prefix(self):
        raw, prefix = generate_api_key()
        assert len(raw) > len(prefix)

    def test_prefix_is_prefix_of_raw(self):
        raw, prefix = generate_api_key()
        assert raw.startswith(prefix)

    def test_unique_keys(self):
        key1, _ = generate_api_key()
        key2, _ = generate_api_key()
        assert key1 != key2


class TestHashVerify:
    def test_roundtrip(self):
        raw, _ = generate_api_key()
        stored = hash_key(raw)
        assert verify_key(stored, raw) is True

    def test_wrong_key_fails(self):
        raw, _ = generate_api_key()
        other, _ = generate_api_key()
        stored = hash_key(raw)
        assert verify_key(stored, other) is False

    def test_hash_is_not_plaintext(self):
        raw, _ = generate_api_key()
        stored = hash_key(raw)
        assert raw not in stored


class TestExtractPrefix:
    def test_extract_matches_generate(self):
        raw, prefix = generate_api_key()
        assert extract_prefix(raw) == prefix

    def test_test_key_prefix(self):
        raw, prefix = generate_api_key(test=True)
        assert extract_prefix(raw) == prefix


class TestCacheKey:
    def test_cache_key_is_deterministic(self):
        raw, _ = generate_api_key()
        assert cache_key_for(raw) == cache_key_for(raw)

    def test_cache_key_does_not_contain_raw(self):
        raw, _ = generate_api_key()
        ck = cache_key_for(raw)
        assert raw not in ck

    def test_different_keys_different_cache_keys(self):
        raw1, _ = generate_api_key()
        raw2, _ = generate_api_key()
        assert cache_key_for(raw1) != cache_key_for(raw2)
