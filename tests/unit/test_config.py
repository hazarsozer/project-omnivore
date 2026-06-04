"""Unit tests for config.py — JWT PEM normalization (base64 / raw / \\n-escaped).

Regression coverage for the docker-compose launch bug: a raw multi-line PEM in
.env breaks docker compose's line-based parser, so keys must be accepted as a
single-line base64 (or \\n-escaped) value and normalized back to real PEM.
"""
from __future__ import annotations

import base64
import uuid
from unittest.mock import patch

from omnivore.auth.jwt import decode_token, issue_token
from omnivore.config import Settings, _decode_pem


def _rsa_pair() -> tuple[str, str]:
    """Generate a throwaway RSA keypair (real PEM text) for tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


_PRIV, _PUB = _rsa_pair()


class TestDecodePem:
    def test_base64_pem_is_decoded(self):
        b64 = base64.b64encode(_PRIV.encode()).decode()
        result = _decode_pem(b64)
        assert "-----BEGIN" in result
        assert result.strip() == _PRIV.strip()

    def test_raw_pem_passes_through(self):
        assert _decode_pem(_PRIV).strip() == _PRIV.strip()

    def test_newline_escaped_is_unescaped(self):
        escaped = _PRIV.replace("\n", "\\n")
        assert "\\n" in escaped and "\n" not in escaped.replace("\\n", "")
        assert _decode_pem(escaped).strip() == _PRIV.strip()

    def test_whitespace_around_base64_is_tolerated(self):
        b64 = base64.b64encode(_PRIV.encode()).decode()
        assert _decode_pem(f"  {b64}\n ").strip() == _PRIV.strip()

    def test_empty_returns_empty(self):
        assert _decode_pem("") == ""

    def test_garbage_returned_unchanged(self):
        # Not base64, no BEGIN marker -> left as-is for the startup validator to reject.
        assert _decode_pem("not-a-key") == "not-a-key"

    def test_base64_of_non_pem_returned_unchanged(self):
        b64 = base64.b64encode(b"hello, not a pem at all").decode()
        assert _decode_pem(b64) == b64


class TestSettingsJwtNormalization:
    def _settings(self, priv: str, pub: str) -> Settings:
        return Settings(
            _env_file=None,
            ENVIRONMENT="test",
            JWT_PRIVATE_KEY_PEM=priv,
            JWT_PUBLIC_KEY_PEM=pub,
        )

    def test_base64_keys_normalized_on_load(self):
        s = self._settings(
            base64.b64encode(_PRIV.encode()).decode(),
            base64.b64encode(_PUB.encode()).decode(),
        )
        assert s.JWT_PRIVATE_KEY_PEM.get_secret_value().strip() == _PRIV.strip()
        assert s.JWT_PUBLIC_KEY_PEM.strip() == _PUB.strip()

    def test_raw_pem_keys_accepted(self):
        s = self._settings(_PRIV, _PUB)
        assert s.JWT_PRIVATE_KEY_PEM.get_secret_value().strip() == _PRIV.strip()
        assert s.JWT_PUBLIC_KEY_PEM.strip() == _PUB.strip()

    def test_base64_loaded_key_signs_and_verifies(self):
        """End-to-end: a base64-configured keypair must sign and verify a JWT."""
        s = self._settings(
            base64.b64encode(_PRIV.encode()).decode(),
            base64.b64encode(_PUB.encode()).decode(),
        )
        with patch("omnivore.auth.jwt.get_settings", return_value=s):
            token = issue_token(
                tenant_id=uuid.uuid4(), principal_id="p", scopes=["documents:read"]
            )
            payload = decode_token(token)
        assert payload["sub"] == "p"
        assert payload["scopes"] == ["documents:read"]
