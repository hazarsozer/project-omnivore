class AuthError(Exception):
    """Base for all authentication / authorisation errors."""

    code: str = "UNAUTHENTICATED"
    http_status: int = 401

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message)
        self.message = message


class InvalidCredentialsError(AuthError):
    code = "UNAUTHENTICATED"
    http_status = 401

    def __init__(self) -> None:
        super().__init__("Invalid or expired credentials")


class InsufficientScopeError(AuthError):
    code = "INSUFFICIENT_SCOPE"
    http_status = 403

    def __init__(self, required_scope: str) -> None:
        super().__init__(f"Scope required: {required_scope}")
        self.required_scope = required_scope


class TenantSuspendedError(AuthError):
    code = "TENANT_SUSPENDED"
    http_status = 403

    def __init__(self) -> None:
        super().__init__("Tenant account is suspended")
