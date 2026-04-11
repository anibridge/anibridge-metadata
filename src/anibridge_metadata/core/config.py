"""Application configuration models."""

from datetime import timedelta
from importlib.metadata import version

from anibridge.utils.cache import cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the metadata service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        env_parse_none_str="null",
        env_prefix="ABM_",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/anibridge_metadata.db"
    sql_echo: bool = False
    cache_ttl_seconds: int = Field(default=21600, ge=1)
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    user_agent: str = Field(
        default_factory=lambda: f"anibridge-metadata/{version('anibridge-metadata')}"
    )

    @property
    def cache_ttl(self) -> timedelta:
        """Return the global cache TTL as a timedelta."""
        return timedelta(seconds=self.cache_ttl_seconds)


@cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
