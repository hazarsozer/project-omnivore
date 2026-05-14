"""Unit tests for auth/jwt.py — RS256 sign/verify."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from omnivore.auth.errors import InvalidCredentialsError
from omnivore.auth.jwt import decode_token, issue_token


def _rsa_pair():
    """Generate a throwaway RSA key pair for tests."""
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
_TENANT = uuid.uuid4()
_PRINCIPAL = "test-principal"
_SCOPES = ["documents:read", "search:read"]


def _mock_settings(private=_PRIV, public=_PUB):
    from unittest.mock import MagicMock
    s = MagicMock()
    s.JWT_PRIVATE_KEY_PEM.get_secret_value.return_value = private
    s.JWT_PUBLIC_KEY_PEM = public
    s.JWT_ALGORITHM = "RS256"
    s.JWT_ACCESS_TOKEN_EXPIRE_SECONDS = 3600
    return s


class TestIssueToken:
    def test_returns_string(self):
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings()):
            token = issue_token(tenant_id=_TENANT, principal_id=_PRINCIPAL, scopes=_SCOPES)
        assert isinstance(token, str)
        assert token.startswith("ey")

    def test_raises_when_no_private_key(self):
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings(private="")):
            with pytest.raises(ValueError, match="JWT_PRIVATE_KEY_PEM"):
                issue_token(tenant_id=_TENANT, principal_id=_PRINCIPAL, scopes=_SCOPES)


class TestDecodeToken:
    def _issue(self):
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings()):
            return issue_token(tenant_id=_TENANT, principal_id=_PRINCIPAL, scopes=_SCOPES)

    def test_roundtrip(self):
        token = self._issue()
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings()):
            payload = decode_token(token)
        assert payload["tenant_id"] == str(_TENANT)
        assert payload["sub"] == _PRINCIPAL
        assert set(payload["scopes"]) == set(_SCOPES)

    def test_bad_token_raises(self):
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings()):
            with pytest.raises(InvalidCredentialsError):
                decode_token("not.a.valid.token")

    def test_wrong_signature_raises(self):
        token = self._issue()
        # Tamper with the signature
        parts = token.split(".")
        tampered = parts[0] + "." + parts[1] + "." + "invalidsig"
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings()):
            with pytest.raises(InvalidCredentialsError):
                decode_token(tampered)

    def test_no_public_key_raises(self):
        token = self._issue()
        with patch("omnivore.auth.jwt.get_settings", return_value=_mock_settings(public="")):
            with pytest.raises(InvalidCredentialsError):
                decode_token(token)
