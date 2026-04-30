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

    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"
    MAX_UPLOAD_SIZE_BYTES: int = 2_147_483_648


@lru_cache
def get_settings() -> Settings:
    return Settings()
