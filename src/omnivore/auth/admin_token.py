import hmac

from omnivore.config import get_settings


def verify_admin_token(provided: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    expected = get_settings().ADMIN_BOOTSTRAP_TOKEN.get_secret_value()
    return hmac.compare_digest(provided.encode(), expected.encode())
