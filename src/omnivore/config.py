from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Phase 4 — Auth
    # Generate with: openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 | base64 -w0
    JWT_PRIVATE_KEY_PEM: SecretStr = SecretStr("")
    JWT_PUBLIC_KEY_PEM: str = ""
    JWT_ALGORITHM: str = "RS256"
    JWT_ACCESS_TOKEN_EXPIRE_SECONDS: int = 3600  # 1 hour
    ADMIN_BOOTSTRAP_TOKEN: SecretStr = SecretStr("change-me-admin-token")

    # Phase 4 — Rate limiting (token bucket, per-tenant defaults)
    RL_CAPACITY: int = 100          # max burst tokens
    RL_REFILL_RATE: float = 10.0    # tokens per second
    RL_UPLOAD_COST: int = 10        # tokens consumed per upload
    RL_DEFAULT_COST: int = 1        # tokens consumed per other request

    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"
    MAX_UPLOAD_SIZE_BYTES: int = 2_147_483_648
    IMAGE_OCR_LANGUAGES: list[str] = ["en"]  # default language list for EasyOCR
    MAX_QUEUE_DEPTH: int = 100     # reject new uploads (HTTP 429) when CPU queue exceeds this
    MAX_GPU_QUEUE_DEPTH: int = 20  # same guard for GPU queue (audio/video/image; max_jobs=2)

    # Phase 5 — Observability
    OTEL_ENABLED: bool = True
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
    OTEL_SERVICE_NAME: str = "omnivore"
    METRICS_ENABLED: bool = True
    METRICS_AUTH_TOKEN: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
