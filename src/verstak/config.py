from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VERSTAK_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql://verstak:verstak@127.0.0.1:55432/verstak"
    redis_url: str = "redis://127.0.0.1:56379/0"
    jwt_secret: str = Field(default="", repr=False)
    clients_json: str = Field(default="{}", repr=False)
    metrics_token: str = Field(default="", repr=False)
    token_seconds: int = Field(default=3600, ge=60, le=86400)
    rate_capacity: int = Field(default=30, ge=1)
    rate_per_minute: int = Field(default=30, ge=1)
    dispatch_seconds: int = Field(default=2, ge=1)
    dispatch_lease_seconds: int = Field(default=30, ge=5)
    worker_retries: int = Field(default=5, ge=0, le=10)


@lru_cache
def settings() -> Settings:
    return Settings()
