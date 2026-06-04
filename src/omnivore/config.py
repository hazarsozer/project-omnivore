import base64
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEAK_DEFAULTS: dict[str, str] = {
    "SECRET_KEY": "change-me-in-production",
    "ADMIN_BOOTSTRAP_TOKEN": "change-me-before-first-run",
    "MINIO_SECRET_KEY": "omnivore123",
}


def _decode_pem(value: str) -> str:
    """Normalize a JWT key env value into real PEM text.

    Accepts three env-friendly encodings and always returns PEM with real
    newlines:

    * **base64-encoded PEM** — recommended; a single line with no quoting,
      newline, or escaping pitfalls, so it parses identically under docker
      compose interpolation, ``env_file``, python-dotenv, and shells.
    * **raw multi-line PEM** beginning with ``-----BEGIN`` — used as-is (and any
      literal ``\\n`` escapes are unescaped). Keeps existing setups working.
    * **single-line PEM with literal ``\\n`` escapes**.

    Malformed input is returned unchanged; the startup check in ``api/main.py``
    surfaces a clear error rather than failing cryptically inside PyJWT.
    """
    if not value:
        return value
    stripped = value.strip()
    if "-----BEGIN" in stripped:
        return stripped.replace("\\n", "\n")
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value
    return decoded if "-----BEGIN" in decoded else value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://omnivore:omnivore@localhost:5432/omnivore"
    REDIS_URL: str = "redis://localhost:6379/0"

    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "omnivore"
    MINIO_SECRET_KEY: SecretStr = SecretStr("omnivore123")
    MINIO_BUCKET: str = "omnivore-raw"
    MINIO_SECURE: bool = False

    SECRET_KEY: SecretStr = SecretStr("change-me-in-production")
    OPENAI_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None

    # LLM enrichment provider — controls summarization and vision captioning.
    # Options: "anthropic" | "openai" | "google" | "ollama"
    # Defaults per provider:
    #   anthropic  text=claude-haiku-4-5-20251001  vision=claude-haiku-4-5-20251001
    #   openai     text=gpt-4o-mini               vision=gpt-4o-mini
    #   google     text=gemini-2.0-flash           vision=gemini-2.0-flash
    #   ollama     text=qwen2.5:7b                 vision=qwen2.5-vl:7b
    LLM_PROVIDER: str = "anthropic"
    LLM_TEXT_MODEL: str = ""   # empty = use provider default
    LLM_VISION_MODEL: str = "" # empty = use provider default
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Phase 4 — Auth. JWT_*_KEY_PEM accept a base64-encoded PEM (recommended,
    # docker-safe) or a raw PEM; both are normalized to real PEM at load time.
    # See .env.example / README §2 for the generator commands.
    JWT_PRIVATE_KEY_PEM: SecretStr = SecretStr("")
    JWT_PUBLIC_KEY_PEM: str = ""
    JWT_ALGORITHM: str = "RS256"
    JWT_ACCESS_TOKEN_EXPIRE_SECONDS: int = 3600  # 1 hour
    ADMIN_BOOTSTRAP_TOKEN: SecretStr = SecretStr("change-me-before-first-run")

    # Phase 4 — Rate limiting (token bucket, per-tenant defaults)
    RL_CAPACITY: int = 100          # max burst tokens
    RL_REFILL_RATE: float = 10.0    # tokens per second
    RL_UPLOAD_COST: int = 10        # tokens consumed per upload
    RL_DEFAULT_COST: int = 1        # tokens consumed per other request

    # Pre-auth IP-based rate limit for POST /auth/token
    RL_AUTH_CAPACITY: int = 5       # max burst: 5 attempts
    RL_AUTH_REFILL_RATE: float = 0.1  # 1 token per 10 s → ~6/min sustained

    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"
    MAX_UPLOAD_SIZE_BYTES: int = 2_147_483_648
    IMAGE_OCR_LANGUAGES: list[str] = ["en"]  # default language list for EasyOCR
    MAX_QUEUE_DEPTH: int = 100     # reject new uploads (HTTP 429) when CPU queue exceeds this
    MAX_GPU_QUEUE_DEPTH: int = 20  # same guard for GPU queue (audio/video/image; max_jobs=2)

    # Vision enrichment (requires ANTHROPIC_API_KEY)
    VIDEO_FRAME_SAMPLE_INTERVAL: int = 30  # seconds between sampled frames
    VIDEO_MAX_VISION_FRAMES: int = 20      # hard cap on frames per video

    # Phase 6 — Hardening
    ALLOWED_ORIGINS: list[str] = ["*"]

    # Phase 5 — Observability
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
    OTEL_SERVICE_NAME: str = "omnivore"
    METRICS_ENABLED: bool = True
    METRICS_AUTH_TOKEN: SecretStr | None = None

    @field_validator("JWT_PRIVATE_KEY_PEM", "JWT_PUBLIC_KEY_PEM", mode="before")
    @classmethod
    def _normalize_jwt_pem(cls, v: object) -> object:
        """Accept base64 / raw / \\n-escaped PEM for either JWT key (see _decode_pem)."""
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        return _decode_pem(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _guard_weak_secrets(self) -> "Settings":
        """Abort startup if placeholder secrets are present outside development/test."""
        if self.ENVIRONMENT in ("development", "test"):
            return self
        bad: list[str] = []
        for field, placeholder in _WEAK_DEFAULTS.items():
            val = getattr(self, field)
            if isinstance(val, SecretStr):
                val = val.get_secret_value()
            if val == placeholder:
                bad.append(f"{field} (placeholder: {placeholder!r})")
        if bad:
            raise ValueError(
                "STARTUP ABORTED — the following secrets are set to their default placeholder "
                f"values and must be changed before running in '{self.ENVIRONMENT}' mode: "
                + ", ".join(bad)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
